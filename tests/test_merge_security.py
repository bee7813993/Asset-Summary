"""銘柄の統合（名寄せ）: store.merge_security と POST /api/securities/{id}/merge。

MF PDF は同じファンドを証券会社ごとの表記で書くため、
「eMAXIS Slim全世界株式(オール・カントリー)」と「eMAXIS Slim全世界株オール(8782)」
のような二重登録が起きる。統合で1銘柄になり、保有一覧では「A証券 他1件」の
1行に合算されることを固定する。
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from fastapi.testclient import TestClient

import asset_summary.web.app as web_app
from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    ImportBatch,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Transaction,
    Unit,
)
from asset_summary.core.store import ConflictError, Store, StoreError

D = Decimal

ALLCOUNTRY = "eMAXIS Slim全世界株式(オール・カントリー)"
ALLCOUNTRY_ALT = "eMAXIS Slim全世界株オール(8782)"


def _fund(name: str, **kw) -> Security:
    defaults = dict(
        name=name,
        name_key=name.lower(),
        asset_class=AssetClass.FUND_JP,
        code=None,
        unit=Unit.KUCHI,
        price_unit_divisor=10000,
    )
    defaults.update(kw)
    return Security(**defaults)


def _snap(sec_id: int, acct_id: int, **kw) -> HoldingSnapshot:
    defaults = dict(
        account_id=acct_id,
        security_id=sec_id,
        as_of_date=date(2026, 8, 1),
        quantity=D("100000"),
        avg_cost=D("20000"),
        origin="mf",
    )
    defaults.update(kw)
    return HoldingSnapshot(**defaults)


def _two_funds(store: Store) -> tuple[int, int, int, int]:
    """(target_id, source_id, 口座1, 口座2)。別口座で同じファンドを別名保有。"""
    a1 = store.get_or_create_account("架空証券", kind="broker")
    a2 = store.get_or_create_account("架空ネット銀行", kind="broker")
    target = store.create_security(_fund(ALLCOUNTRY))
    source = store.create_security(_fund(ALLCOUNTRY_ALT))
    store.upsert_snapshot(_snap(target, a1.id, quantity=D("300000"),
                                reported_value_jpy=D("900000")))
    store.upsert_snapshot(_snap(source, a2.id, quantity=D("100000"),
                                reported_value_jpy=D("300000")))
    return target, source, a1.id, a2.id


# ----------------------------------------------------------------------
# store.merge_security
# ----------------------------------------------------------------------


def test_merge_moves_holdings_and_learns_alias(store: Store):
    target, source, a1, a2 = _two_funds(store)
    result = store.merge_security(source, target)

    assert result["snapshots"] == 1
    assert store.get_security(source) is None
    lots = store.current_holdings()
    assert {(l.security_id, l.account_id) for l in lots} == {(target, a1), (target, a2)}
    # 旧名は alias として記憶され、次回の取込から自動で当たる
    assert store.resolve_security(name_key=ALLCOUNTRY_ALT.lower()) == target
    assert store.resolve_security(name_key=ALLCOUNTRY.lower()) == target


def test_merge_remaps_lot_seq_when_same_account_overlaps(store: Store):
    """同一口座に同じ lot_seq・同じ基準日の行があっても UNIQUE 制約で落ちない。"""
    acct = store.get_or_create_account("架空証券")
    target = store.create_security(_fund(ALLCOUNTRY))
    source = store.create_security(_fund(ALLCOUNTRY_ALT))
    day = date(2026, 8, 1)
    store.upsert_snapshot(_snap(target, acct.id, lot_seq=0, as_of_date=day,
                                quantity=D("100")))
    store.upsert_snapshot(_snap(source, acct.id, lot_seq=0, as_of_date=day,
                                quantity=D("200")))
    # 系列としての同一性を確かめるため、source 側は複数日を持たせる
    store.upsert_snapshot(_snap(source, acct.id, lot_seq=0,
                                as_of_date=date(2026, 7, 1), quantity=D("150")))

    store.merge_security(source, target)

    lots = store.latest_lots(acct.id, target)
    assert len(lots) == 2                       # 両ロットが保たれる
    assert sum(l.quantity for l in lots) == D("300")
    # 振り直された source 系列は日をまたいで同じ lot_seq のまま
    moved = [s for s in store.all_snapshots() if s.lot_seq != 0]
    assert {s.as_of_date for s in moved} == {date(2026, 7, 1), date(2026, 8, 1)}
    assert len({s.lot_seq for s in moved}) == 1


def test_merge_keeps_lot_seq_when_accounts_differ(store: Store):
    target, source, _a1, a2 = _two_funds(store)
    store.merge_security(source, target)
    lots = store.latest_lots(a2, target)
    assert [l.lot_seq for l in lots] == [0]     # 口座が違えば付番はそのまま


def test_merge_requires_compatible_attributes(store: Store):
    target = store.create_security(_fund(ALLCOUNTRY))
    other_ccy = store.create_security(_fund("同名の外貨建て", currency="USD"))
    with pytest.raises(ConflictError):
        store.merge_security(other_ccy, target)
    stock = store.create_security(
        Security(name="株式のほう", name_key="株式のほう",
                 asset_class=AssetClass.STOCK_JP, code="9999")
    )
    with pytest.raises(ConflictError):
        store.merge_security(stock, target)
    with pytest.raises(StoreError):
        store.merge_security(target, target)


def _insert_buy_tx(store: Store, account_id: int, security_id: int) -> None:
    store.create_batch(ImportBatch(id="tx-batch-1", source_kind="broker_csv"))
    store.insert_transactions(
        [Transaction(dedup_key="tx-1", account_id=account_id,
                     security_id=security_id, trade_date=date(2026, 7, 15),
                     tx_type="buy", quantity=D("100000"),
                     unit_price=D("29000"), gross_amount=D("290000"))],
        batch_id="tx-batch-1",
    )


def test_merge_moves_transactions_tags_and_manual_prices(store: Store):
    target, source, _a1, a2 = _two_funds(store)
    _insert_buy_tx(store, a2, source)
    t1 = store.create_tag("全世界株")
    t2 = store.create_tag("米国株")
    store.set_security_tags(target, {t1: D("60")})
    store.set_security_tags(source, {t1: D("100"), t2: D("40")})
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO daily_prices (source, source_id, date, price, currency) "
            "VALUES ('manual', ?, '2026-08-01', '30000', 'JPY')",
            (str(source),),
        )

    store.merge_security(source, target)

    assert store.count_transactions(security_id=target) == 1
    assert store.count_transactions(security_id=source) == 0
    # タグは統合先の既存配分を優先しつつ、無いタグだけ引き継ぐ
    assert store.get_security_tags(target) == {t1: D("60"), t2: D("40")}
    prices, _ccy = store.get_price_rows("manual", str(target))
    assert prices == {"2026-08-01": D("30000")}


def test_merge_adopts_code_and_price_source(store: Store):
    target = store.create_security(_fund(ALLCOUNTRY))
    source = store.create_security(
        _fund(
            ALLCOUNTRY_ALT,
            code="878A",
            price_source_type=PriceSourceType.TOUSHIN,
            price_source_ref="JP90C000H1T1:0331418A",
            price_source_status=PriceSourceStatus.LINKED,
        )
    )
    acct = store.get_or_create_account("架空ネット銀行")
    store.upsert_snapshot(_snap(source, acct.id))

    result = store.merge_security(source, target)

    merged = store.get_security(target)
    assert merged.code == "878A"
    assert merged.price_source_type == PriceSourceType.TOUSHIN
    assert merged.price_source_ref == "JP90C000H1T1:0331418A"
    assert merged.price_source_status == PriceSourceStatus.LINKED
    assert result["adopted_price_source"] is True


# ----------------------------------------------------------------------
# POST /api/securities/{id}/merge
# ----------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = web_app.create_app(str(tmp_path / "t.db"))
    monkeypatch.setattr(web_app, "fetch_spot", lambda store, secs, warn=None: {})
    monkeypatch.setattr(web_app, "fetch_fx_rates", lambda store, ccys, warn=None: {})
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "ensure_fx_history", lambda *a, **k: None)
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


def test_api_merge_then_summary_shows_single_row(app, client):
    store: Store = app.state.store
    target, source, _a1, _a2 = _two_funds(store)

    res = client.post(f"/api/securities/{target}/merge", json={"source_id": source})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["snapshots"] == 1
    assert body["source_name"] == ALLCOUNTRY_ALT

    summary = client.get("/api/summary").json()
    rows = [
        h for h in summary["holdings_by_security"] if h["name"] == ALLCOUNTRY
    ]
    assert len(rows) == 1
    assert rows[0]["account"] == "架空証券 他1件"
    assert rows[0]["quantity"] == "400000"
    assert not any(
        h["name"] == ALLCOUNTRY_ALT for h in summary["holdings_by_security"]
    )


def test_api_merge_validations(app, client):
    store: Store = app.state.store
    target, source, _a1, _a2 = _two_funds(store)
    other = store.create_security(_fund("外貨建てファンド", currency="USD"))

    assert client.post(
        f"/api/securities/{target}/merge", json={"source_id": target}
    ).status_code == 400
    assert client.post(
        f"/api/securities/{target}/merge", json={"source_id": 99999}
    ).status_code == 404
    assert client.post(
        "/api/securities/99999/merge", json={"source_id": source}
    ).status_code == 404
    assert client.post(
        f"/api/securities/{target}/merge", json={"source_id": other}
    ).status_code == 409
    # 失敗しても何も変わっていない
    assert store.get_security(source) is not None


def test_api_merge_recomputes_cost_basis_when_transactions_move(app, client):
    """取引が移った統合では原価が作り直される（source 向け派生行が残らない）。"""
    store: Store = app.state.store
    target, source, _a1, a2 = _two_funds(store)
    _insert_buy_tx(store, a2, source)
    res = client.post(f"/api/securities/{target}/merge", json={"source_id": source})
    assert res.status_code == 200
    groups = store.list_cost_basis()
    assert all(g["security_id"] == target for g in groups)
