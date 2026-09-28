"""手動の取引（取引履歴をダウンロードできない証券会社のぶん）の Web API。

取込と同じ台帳に入り、銘柄詳細の推移グラフ（最初の取込より前のさかのぼり）と
取得原価の計算に使われる。単価の無い買付を 0 円で入れないことが要点。
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from datetime import date, timedelta
from decimal import Decimal

import pytest
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

D = Decimal
NAV_REF = "JP90C000H1T1:0331418A"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = web_app.create_app(str(tmp_path / "t.db"))
    monkeypatch.setattr(web_app, "fetch_spot", lambda store, secs, warn=None: {})
    monkeypatch.setattr(
        web_app, "fetch_fx_rates", lambda store, ccys, warn=None: {c: D("150") for c in ccys}
    )
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "ensure_fx_history", lambda *a, **k: None)
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def store(app):
    return app.state.store


@pytest.fixture()
def fund(store):
    """SMBC日興で 350,000口（8/4 の取込）。基準価額の履歴あり。"""
    acct = store.get_or_create_account("SMBC日興証券", kind="broker")
    sec_id = store.create_security(
        Security(
            name="全世界インデックス", name_key="ぜんせかいいんでっくす",
            asset_class=AssetClass.FUND_JP, unit=Unit.KUCHI, price_unit_divisor=10000,
            price_source_type=PriceSourceType.TOUSHIN, price_source_ref=NAV_REF,
            price_source_status=PriceSourceStatus.LINKED,
        )
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=acct.id, security_id=sec_id, as_of_date=date(2026, 8, 4),
                        quantity=D("350000"), avg_cost=D("18000"), origin="mf")
    )
    for day, nav in (("2022-05-10", "15000"), ("2023-03-15", "17000"),
                     ("2024-09-30", "25000"), ("2026-08-03", "30000")):
        store.upsert_daily_price("toushin", NAV_REF, day, D(nav), "JPY")
    return acct.id, sec_id


def _post(client, acct_id, sec_id, **kw):
    body = {"account_id": acct_id, "security_id": sec_id, "tx_type": "buy", **kw}
    return client.post("/api/transactions", json=body)


def test_manual_buy_goes_into_the_ledger_with_its_amount(client, store, fund):
    acct_id, sec_id = fund
    res = _post(client, acct_id, sec_id, trade_date="2022-05-10",
                quantity="100000", unit_price="15000", fee="0")
    assert res.status_code == 200
    body = res.json()
    tx = body["transaction"]
    assert tx["origin"] == "manual"
    assert tx["quantity"] == "100000"
    assert D(tx["gross_amount"]) == D("150000")        # 10万口 × 15,000円 ÷ 1万口
    assert D(tx["net_amount"]) == D("-150000")
    assert body["price_filled_from"] is None
    stored = store.list_transactions(security_id=sec_id)
    assert [t.batch_id for t in stored] == [None]      # どのバッチにも属さない


def test_missing_price_is_filled_from_the_price_history(client, fund):
    """単価が空なら約定日（休日なら直前）の基準価額で埋める。0 円の買付にしない。"""
    acct_id, sec_id = fund
    res = _post(client, acct_id, sec_id, trade_date="2024-10-01", quantity="100000")
    body = res.json()
    assert body["price_filled_from"] == "2024-09-30"
    assert body["transaction"]["unit_price"] == "25000"
    assert D(body["transaction"]["gross_amount"]) == D("250000")
    assert "2024-09-30" in body["transaction"]["note"]


def test_missing_price_without_history_is_refused(client, fund):
    acct_id, sec_id = fund
    res = _post(client, acct_id, sec_id, trade_date="2019-01-10", quantity="1000")
    assert res.status_code == 400
    assert "単価" in res.json()["detail"]


def test_manual_trades_walk_the_chart_back_and_price_the_cost_basis(client, store, fund):
    acct_id, sec_id = fund
    _post(client, acct_id, sec_id, trade_date="2022-05-10", quantity="100000")
    _post(client, acct_id, sec_id, trade_date="2023-03-15", quantity="150000")
    last = _post(client, acct_id, sec_id, trade_date="2024-10-01", quantity="100000").json()
    assert last["chart_status"] == "traced"

    data = client.get(
        "/api/portfolio-history", params={"range": "all", "scope": f"security:{sec_id}"}
    ).json()
    pts = {p["t"]: p for p in data["points"]}
    assert pts["2022-05-09"]["quantity"] == "0"
    assert pts["2022-05-10"]["quantity"] == "100000"
    assert pts["2023-03-15"]["quantity"] == "250000"
    assert pts["2024-10-01"]["quantity"] == "350000"
    assert not any(p["backfilled"] for p in data["points"])

    # 取得原価は入れた金額から（0 円の買付にはなっていない）
    basis = store.list_cost_basis(security_id=sec_id)[0]
    assert basis["coverage"] == "full"
    assert D(basis["avg_cost"]).quantize(D("1")) == D("18714")   # 655,000円 ÷ 35万口 × 1万口


def test_chart_status_explains_why_a_trade_is_not_used(client, store, fund):
    acct_id, sec_id = fund
    # 最初の取込より後の取引は、取込の数量が正なのでさかのぼりには使わない
    body = _post(client, acct_id, sec_id, trade_date="2026-08-10", quantity="10").json()
    assert body["chart_status"] == "no_earlier_trades"
    assert body["first_snapshot"] == "2026-08-04"
    # 今の口数より多く買ったことになる（売却の入れ漏れなど）
    body = _post(client, acct_id, sec_id, trade_date="2022-05-10", quantity="400000").json()
    assert body["chart_status"] == "inconsistent"
    # この銘柄の記録が無い口座
    other = store.get_or_create_account("別証券", kind="broker")
    body = _post(client, other.id, sec_id, trade_date="2022-05-10", quantity="10").json()
    assert body["chart_status"] == "no_snapshot"
    assert body["first_snapshot"] is None


@pytest.mark.parametrize(
    "override, status",
    [
        ({"tx_type": "split"}, 400),
        ({"trade_date": (date.today() + timedelta(days=1)).isoformat()}, 400),
        ({"trade_date": "yesterday"}, 400),
        ({"quantity": "0"}, 400),
        ({"quantity": "-5"}, 400),
        ({"unit_price": "0"}, 400),
        ({"fee": "-1"}, 400),
        ({"account_id": 999999}, 404),
        ({"security_id": 999999}, 404),
    ],
)
def test_manual_trade_validation(client, fund, override, status):
    acct_id, sec_id = fund
    body = {"trade_date": "2022-05-10", "quantity": "100", "unit_price": "15000", **override}
    assert _post(client, acct_id, sec_id, **body).status_code == status


def test_sell_is_stored_as_a_negative_quantity(client, fund):
    acct_id, sec_id = fund
    body = _post(client, acct_id, sec_id, tx_type="sell", trade_date="2023-03-15",
                 quantity="50000", unit_price="17000", fee="100").json()
    tx = body["transaction"]
    assert tx["tx_type"] == "sell"
    assert tx["quantity"] == "-50000"
    assert D(tx["net_amount"]) == D("85000") - D("100")


def test_manual_list_shows_only_manual_trades(client, store, fund):
    acct_id, sec_id = fund
    _post(client, acct_id, sec_id, trade_date="2022-05-10", quantity="100000")
    _insert_imported(store, acct_id, sec_id)
    rows = client.get("/api/transactions/manual").json()["transactions"]
    assert len(rows) == 1
    assert rows[0]["security"] == "全世界インデックス"
    assert rows[0]["account"] == "SMBC日興証券"


def test_manual_trade_can_be_deleted_one_by_one(client, store, fund):
    acct_id, sec_id = fund
    tx_id = _post(client, acct_id, sec_id, trade_date="2022-05-10",
                  quantity="350000").json()["transaction"]["id"]
    assert store.list_cost_basis(security_id=sec_id)
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 200
    assert store.get_transaction(tx_id) is None
    assert store.list_cost_basis(security_id=sec_id) == []      # 再計算済み
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 404


def test_imported_trades_are_not_deleted_from_here(client, store, fund):
    acct_id, sec_id = fund
    tx_id = _insert_imported(store, acct_id, sec_id)
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 409
    assert store.get_transaction(tx_id) is not None


def test_rolling_back_an_import_keeps_manual_trades(client, store, fund):
    acct_id, sec_id = fund
    _post(client, acct_id, sec_id, trade_date="2022-05-10", quantity="100000")
    _insert_imported(store, acct_id, sec_id)
    store.delete_batch("csv-1")
    remaining = store.list_transactions(security_id=sec_id)
    assert [t.origin for t in remaining] == ["manual"]


def _insert_imported(store, acct_id, sec_id) -> int:
    store.create_batch(ImportBatch(id="csv-1", source_kind="broker_csv"))
    store.insert_transactions(
        [Transaction(dedup_key="csv-row-1", account_id=acct_id, security_id=sec_id,
                     trade_date=date(2023, 3, 15), tx_type="buy", quantity=D("150000"),
                     unit_price=D("17000"), gross_amount=D("255000"))],
        batch_id="csv-1",
    )
    return next(t.id for t in store.list_transactions(security_id=sec_id)
                if t.origin != "manual")


# ----------------------------------------------------------------------
# 銘柄詳細の破線（遡って描いている数量）から、登録に要る値を取引タブへ渡す
# ----------------------------------------------------------------------


def _carried_back(client, sec_id):
    return client.get(
        "/api/portfolio-history", params={"range": "all", "scope": f"security:{sec_id}"}
    ).json()["carried_back"]


def test_history_names_what_it_carries_back_so_it_can_be_registered(client, store, fund):
    """最初の記録より前の取引が無い口座は、最初の記録の数量をそのまま遡って描く（破線）。
    その数量・平均取得単価・取込の取得日を、取引の登録に入れておけるよう返す。"""
    acct_id, sec_id = fund
    store.upsert_snapshot(
        HoldingSnapshot(account_id=acct_id, security_id=sec_id, as_of_date=date(2026, 8, 4),
                        quantity=D("350000"), avg_cost=D("18000"), origin="mf",
                        raw={"meta": {"acquired_on": "2022/05/10"}})
    )
    assert _carried_back(client, sec_id) == [{
        "account_id": acct_id, "account": "SMBC日興証券", "quantity": "350000",
        "until": "2026-08-04", "unit_price": "18000", "acquired_on": "2022-05-10",
    }]
    # 入れておいた値のまま登録すると、遡りが消える
    res = _post(client, acct_id, sec_id, trade_date="2022-05-10", quantity="350000",
                unit_price="18000")
    assert res.json()["chart_status"] == "traced"
    assert _carried_back(client, sec_id) == []


def test_a_partly_covered_holding_offers_the_part_before_its_history(client, fund):
    """取引履歴が一部だけなら、その前から持っていた分（期首）を、取引履歴の最初の日より
    前の買付として。単価は取得原価の「取得日不明」分の単価（平均取得単価から逆算）。"""
    acct_id, sec_id = fund
    _post(client, acct_id, sec_id, trade_date="2023-03-15", quantity="100000", unit_price="17000")
    (row,) = _carried_back(client, sec_id)
    # (35万口 × 18,000 − 10万口 × 17,000) ÷ 25万口 = 18,400（1万口あたり）
    assert (row["quantity"], row["until"], row["unit_price"], row["acquired_on"]) == (
        "250000", "2023-03-15", "18400", None)


def test_lots_that_start_on_different_days_are_not_offered(client, store, fund):
    """ロットごとに記録の始まる日が違う口座は、取引を足しても遡りが消えないので案内しない。"""
    acct_id, sec_id = fund
    store.upsert_snapshot(
        HoldingSnapshot(account_id=acct_id, security_id=sec_id, lot_seq=1,
                        as_of_date=date(2026, 9, 1), quantity=D("50000"),
                        avg_cost=D("30000"), origin="mf")
    )
    assert _carried_back(client, sec_id) == []
