"""レビューで発見した欠陥の回帰テスト（価格層・Web層）。"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from asset_summary.core import price_store
from asset_summary.core.models import (
    AssetClass,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.core.prices import fetch_spot
from asset_summary.core.store import Store, StoreError
from asset_summary.web.app import create_app

D = Decimal


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "web.db")))


# ----------------------------------------------------------------------
# 手動評価（不動産）がサマリーに反映される
# ----------------------------------------------------------------------

def test_manual_valuation_appears_in_spot(store: Store):
    sec = Security(
        name="自宅マンション",
        name_key="じたくまんしょん",
        asset_class=AssetClass.REAL_ESTATE,
        unit=Unit.UNIT,
        price_source_type=PriceSourceType.MANUAL,
        price_source_status=PriceSourceStatus.MANUAL,
    )
    sec_id = store.create_security(sec)
    stored = store.get_security(sec_id)
    store.upsert_daily_price("manual", str(sec_id), "2026-08-01", D("52000000"))
    spot = fetch_spot(store, [stored], warn=lambda _m: None)
    assert spot[sec_id] == D("52000000")


def test_real_estate_manual_price_in_summary(client: TestClient):
    r = client.post(
        "/api/securities",
        json={"name": "自宅マンション", "asset_class": "real_estate"},
    )
    sec_id = r.json()["id"]
    client.post(
        "/api/holdings",
        json={"security_id": sec_id, "account_name": "不動産", "quantity": "1",
              "avg_cost": "40000000"},
    )
    client.post(
        f"/api/securities/{sec_id}/manual-price",
        json={"date": "2026-08-01", "value": "52000000"},
    )
    s = client.get("/api/summary?currency=JPY").json()
    assert s["total_value"] == "52000000"
    assert s["total_pl"] == "12000000"      # 旧: 評価額0・損益 -40,000,000
    assert s["priced_count"] == 1


# ----------------------------------------------------------------------
# mf_reported フォールバック時の通貨
# ----------------------------------------------------------------------

def test_mf_reported_fallback_is_jpy(store: Store):
    """外貨建て銘柄でも取込記載値は円建てなので JPY を返すこと。"""
    sec_id = store.create_security(
        Security(name="US株", name_key="usstock", asset_class=AssetClass.STOCK_FOREIGN,
                 currency="USD")
    )
    sec = store.get_security(sec_id)
    store.upsert_daily_price("mf_reported", str(sec_id), "2026-08-03", D("30000"))
    series, currency = store.price_series_for_security(sec)
    assert series == {"2026-08-03": D("30000")}
    assert currency == "JPY"   # 旧: 'USD' → 為替換算が二重に掛かり約150倍


# ----------------------------------------------------------------------
# 被覆記録の並行更新
# ----------------------------------------------------------------------

def test_record_range_is_atomic_under_concurrency(store: Store):
    """同一銘柄の並行記録で被覆が失われないこと。"""
    days = [date(2026, 1, 1) + timedelta(days=i * 2) for i in range(12)]
    barrier = threading.Barrier(len(days))

    def worker(d: date):
        barrier.wait()
        price_store.record_range(store, "yahoo", "TEST.T", d, d)

    threads = [threading.Thread(target=worker, args=(d,)) for d in days]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    covered = price_store.get_ranges(store, "yahoo", "TEST.T")
    total = sum((e - s).days + 1 for s, e in covered)
    assert total == len(days)   # 旧: ロストアップデートで 2〜6 日程度に減る


# ----------------------------------------------------------------------
# 失敗時のネガティブキャッシュ
# ----------------------------------------------------------------------

def test_attempt_recording(store: Store):
    assert price_store.attempted_today(store, "yahoo", "X.T") is False
    price_store.record_attempt(store, "yahoo", "X.T", ok=False)
    assert price_store.attempted_today(store, "yahoo", "X.T") is True
    assert price_store.attempted_today(store, "yahoo", "X.T", only_failed=True) is True
    price_store.record_attempt(store, "yahoo", "X.T", ok=True)
    assert price_store.attempted_today(store, "yahoo", "X.T", only_failed=True) is False
    price_store.clear_attempts(store)
    assert price_store.attempted_today(store, "yahoo", "X.T") is False


# ----------------------------------------------------------------------
# 入力検証（500 ではなく 400/404 を返す）
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        {"security_id": "abc", "account_name": "A", "quantity": "1"},
        {"security_id": 10**30, "account_name": "A", "quantity": "1"},
        {"security_id": 1, "account_id": "xyz", "quantity": "1"},
    ],
)
def test_holdings_invalid_ids_are_400(client: TestClient, payload):
    assert client.post("/api/holdings", json=payload).status_code in (400, 404)


@pytest.mark.parametrize("bad_qty", ["NaN", "Infinity", "1e400"])
def test_holdings_non_finite_quantity_is_400(client: TestClient, bad_qty):
    sec_id = client.post(
        "/api/securities", json={"name": "テスト", "asset_class": "stock_jp"}
    ).json()["id"]
    r = client.post(
        "/api/holdings",
        json={"security_id": sec_id, "account_name": "A", "quantity": bad_qty},
    )
    assert r.status_code == 400


def test_account_sort_order_must_be_int(client: TestClient):
    client.post(
        "/api/securities", json={"name": "テスト", "asset_class": "stock_jp"}
    )
    client.post(
        "/api/holdings",
        json={"security_id": 1, "account_name": "テスト証券", "quantity": "1"},
    )
    acct_id = client.get("/api/accounts").json()["accounts"][0]["id"]
    assert client.put(f"/api/accounts/{acct_id}", json={"sort_order": "abc"}).status_code == 400
    # アプリが壊れていないこと（旧: 以後あらゆるAPIが500になった）
    assert client.get("/api/accounts").status_code == 200
    assert client.get("/api/summary").status_code == 200


@pytest.mark.parametrize("divisor", [0, -1])
def test_price_unit_divisor_must_be_positive(client: TestClient, divisor):
    sec_id = client.post(
        "/api/securities", json={"name": "テスト", "asset_class": "stock_jp"}
    ).json()["id"]
    r = client.put(f"/api/securities/{sec_id}", json={"price_unit_divisor": divisor})
    assert r.status_code == 400
    assert client.get("/api/summary").status_code == 200


@pytest.mark.parametrize(
    "ref", ["9202\n.T", "../../v7/finance/quote", "9202 .T", {"a": 1}]
)
def test_price_source_ref_rejects_unsafe_values(client: TestClient, ref):
    """URLに埋め込まれる値なので制御文字・空白・パス区切り・非文字列を拒否する。"""
    sec_id = client.post(
        "/api/securities", json={"name": "テスト", "asset_class": "stock_jp"}
    ).json()["id"]
    r = client.put(f"/api/securities/{sec_id}", json={"price_source_ref": ref})
    assert r.status_code == 400


def test_price_source_ref_trims_surrounding_whitespace(client: TestClient):
    """貼り付け時の前後の改行・空白は正規化して受理する。"""
    sec_id = client.post(
        "/api/securities", json={"name": "テスト", "asset_class": "stock_jp"}
    ).json()["id"]
    r = client.put(
        f"/api/securities/{sec_id}",
        json={"price_source_type": "yahoo", "price_source_ref": "\n 9202.T \n"},
    )
    assert r.status_code == 200
    secs = client.get("/api/securities").json()["securities"]
    assert secs[0]["price_source_ref"] == "9202.T"


def test_import_non_pdf_is_400(client: TestClient):
    import base64

    r = client.post(
        "/api/import/pdf",
        json={"filename": "x.pdf", "content_b64": base64.b64encode(b"hello").decode()},
    )
    assert r.status_code == 400


def test_commit_refuses_blank_account_name(store: Store):
    """口座名を読めなかった行を確定させない（名前のない口座を作らない）。

    2026-08-07 のPDFで 保有金融機関 が縦組みになりパースが崩れたときの被害を
    確定直前で止めるための安全弁。
    """
    from asset_summary.core.models import ImportBatch
    from asset_summary.importers.matching import build_matches
    from asset_summary.importers.service import commit_batch
    from tests.fixtures.factories import make_result, stock

    rows, _, _ = build_matches(
        store,
        make_result(stock("架空工業", "1234", "100", "500", "600", "60000", inst="")),
    )
    store.create_batch(
        ImportBatch(id="b-blank", filename="x.pdf", parse_report={"rows": rows})
    )
    with pytest.raises(StoreError, match="口座名が空"):
        commit_batch(store, "b-blank", date(2026, 8, 7))
    # トランザクションごと巻き戻り、口座もスナップショットも作られない
    assert store.list_accounts() == []
    assert store.current_holdings() == []
    assert store.get_batch("b-blank").status == "previewed"


def test_commit_unknown_batch_is_404(client: TestClient):
    r = client.post("/api/import/does-not-exist/commit", json={"as_of": "2026-08-01"})
    assert r.status_code == 404


def test_spot_failure_of_one_security_does_not_break_others(store: Store, monkeypatch):
    """1銘柄の例外が他銘柄の現在値を巻き添えにしないこと。"""
    from asset_summary.core import prices as prices_mod

    good = Security(
        id=1, name="良い銘柄", name_key="good", asset_class=AssetClass.STOCK_JP,
        price_source_type=PriceSourceType.YAHOO, price_source_ref="9202.T",
        price_source_status=PriceSourceStatus.LINKED,
    )
    bad = Security(
        id=2, name="壊れた銘柄", name_key="bad", asset_class=AssetClass.STOCK_JP,
        price_source_type=PriceSourceType.YAHOO, price_source_ref="BROKEN",
        price_source_status=PriceSourceStatus.LINKED,
    )

    def fake_spot_for(store_, stype, ref, w):
        if ref == "BROKEN":
            raise RuntimeError("boom")
        return (D("3100"), "JPY")

    monkeypatch.setattr(prices_mod, "_spot_for", fake_spot_for)
    warns: list[str] = []
    out = fetch_spot(store, [good, bad], warns.append)
    assert out == {1: D("3100")}      # 旧: 例外が伝播し全銘柄が消えた
    assert warns
