"""取引履歴取込 API の検証（TestClient）。

既存 test_web.py と同じ作法: モジュール名前空間の関数を monkeypatch する。
"""

from __future__ import annotations

import base64
import os
import tempfile
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault("AS_DB_PATH", tempfile.mkdtemp(prefix="as-txweb-") + "/t.db")

from fastapi.testclient import TestClient  # noqa: E402

from asset_summary.core.models import (  # noqa: E402
    AssetClass,
    HoldingSnapshot,
    Security,
)
from asset_summary.core.store import Store  # noqa: E402
from asset_summary.web import app as web_app  # noqa: E402

BROKER = "架空証券"
CSV = (
    "約定日,受渡日,銘柄コード,銘柄名,取引区分,数量,単価,約定代金,手数料,受渡金額,口座区分\n"
    "2026/01/05,2026/01/07,1234,架空商事,買付,100,2000,200000,0,200000,特定\n"
    "2026/02/10,2026/02/12,1234,架空商事,売却,50,2400,120000,0,120000,特定\n"
    "2026/03/03,2026/03/05,1234,架空商事,買付,25,2200,55000,0,55000,特定\n"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    store = Store(db)
    account = store.get_or_create_account(BROKER, kind="broker")
    security_id = store.create_security(
        Security(code="1234", name="架空商事", name_key="架空商事",
                 asset_class=AssetClass.STOCK_JP)
    )
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=account.id, security_id=security_id,
            as_of_date=date(2026, 8, 1), quantity=Decimal("300"),
            avg_cost=Decimal("1500"), origin="mf",
            raw={"meta": {"acquired_on": "2019/03/14"}},
        )
    )
    monkeypatch.setattr(web_app, "fetch_spot", lambda *a, **k: {})
    monkeypatch.setattr(web_app, "fetch_fx_rates", lambda *a, **k: {})
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "ensure_fx_history", lambda *a, **k: None)
    app = web_app.create_app(str(db))
    return TestClient(app), store, security_id


def _b64(text: str, encoding: str = "cp932") -> str:
    return base64.b64encode(text.encode(encoding)).decode()


# ----------------------------------------------------------------------
# 取込
# ----------------------------------------------------------------------


def test_preview_and_commit_round_trip(client):
    api, store, security_id = client
    r = api.post("/api/import/table", json={
        "filename": "trades.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    })
    assert r.status_code == 200
    preview = r.json()
    assert preview["ok"] is True
    assert len(preview["rows"]) == 3
    assert preview["unmatched_securities"] == []
    assert "generated_at" in preview
    mapping = {c["field"]: c["index"] for c in preview["detection"]["columns"]}
    assert mapping["trade_date"] == 0 and mapping["quantity"] == 5

    r2 = api.post(f"/api/import/table/{preview['batch_id']}/commit",
                  json={"account_name": BROKER})
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is True and body["inserted"] == 3
    assert store.count_transactions() == 3


def test_pasted_text_is_accepted(client):
    api, store, _sid = client
    pasted = (
        "約定日\t銘柄名\t取引区分\t数量\t単価\t約定代金\n"
        "2026/01/05\t架空商事\t買付\t100\t2000\t200000\n"
        "2026/02/10\t架空商事\t売却\t50\t2400\t120000\n"
        "2026/03/03\t架空商事\t買付\t25\t2200\t55000\n"
    )
    r = api.post("/api/import/table", json={
        "filename": "paste", "text": pasted, "account_name": BROKER,
    })
    assert r.status_code == 200
    assert len(r.json()["rows"]) == 3


def test_missing_payload_is_400(client):
    api, _store, _sid = client
    assert api.post("/api/import/table", json={"filename": "x.csv"}).status_code == 400
    assert api.post("/api/import/table", json={
        "filename": "x.csv", "content_b64": "!!!not-base64!!!"
    }).status_code == 400


def test_empty_file_is_400(client):
    api, _store, _sid = client
    r = api.post("/api/import/table", json={
        "filename": "x.csv", "content_b64": base64.b64encode(b"").decode()
    })
    assert r.status_code == 400


def test_duplicate_file_is_409(client):
    api, _store, _sid = client
    body = {"filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER}
    first = api.post("/api/import/table", json=body).json()
    api.post(f"/api/import/table/{first['batch_id']}/commit", json={"account_name": BROKER})

    again = api.post("/api/import/table", json=body)
    assert again.status_code == 409
    assert again.json()["existing_batch_id"] == first["batch_id"]


def test_commit_on_a_missing_batch_is_404(client):
    api, _store, _sid = client
    r = api.post("/api/import/table/does-not-exist/commit", json={"account_name": BROKER})
    assert r.status_code == 404


def test_commit_without_an_account_is_409(client):
    api, _store, _sid = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV),
    }).json()
    r = api.post(f"/api/import/table/{preview['batch_id']}/commit", json={})
    assert r.status_code == 409
    assert "口座" in r.json()["detail"]


def test_remap_changes_the_column_mapping(client):
    api, _store, _sid = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    }).json()
    r = api.post(f"/api/import/table/{preview['batch_id']}/remap",
                 json={"column_overrides": {"6": "gross_amount"}})
    assert r.status_code == 200
    mapping = {c["field"]: c["index"] for c in r.json()["detection"]["columns"]}
    assert mapping["gross_amount"] == 6


# ----------------------------------------------------------------------
# 取引履歴と取得原価
# ----------------------------------------------------------------------


def test_transactions_endpoint(client):
    api, _store, security_id = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    }).json()
    api.post(f"/api/import/table/{preview['batch_id']}/commit", json={"account_name": BROKER})

    r = api.get(f"/api/securities/{security_id}/transactions")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    first = body["transactions"][0]
    assert first["tx_type"] == "buy"
    assert first["quantity"] == "100"
    assert first["account"] == BROKER
    assert "generated_at" in body


def test_transactions_for_an_unknown_security_is_404(client):
    api, _store, _sid = client
    assert api.get("/api/securities/999/transactions").status_code == 404


def test_cost_basis_endpoint_reports_the_subtraction(client):
    api, _store, security_id = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    }).json()
    api.post(f"/api/import/table/{preview['batch_id']}/commit", json={"account_name": BROKER})

    r = api.get("/api/cost-basis", params={"security_id": security_id})
    assert r.status_code == 200
    group = r.json()["groups"][0]
    assert group["coverage"] == "partial"
    assert group["applies_to_pl"] is False
    assert Decimal(group["residual_quantity"]).quantize(Decimal("0.01")) == Decimal("190.38")


def test_recompute_endpoint(client):
    api, _store, _sid = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    }).json()
    api.post(f"/api/import/table/{preview['batch_id']}/commit", json={"account_name": BROKER})

    r = api.post("/api/cost-basis/recompute")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["reconciled"] == 1


def test_security_detail_carries_cost_basis_and_acquired_on(client):
    api, _store, security_id = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    }).json()
    api.post(f"/api/import/table/{preview['batch_id']}/commit", json={"account_name": BROKER})

    body = api.get(f"/api/security/{security_id}").json()
    assert body["transaction_count"] == 3
    assert body["cost_basis"][0]["coverage"] == "partial"
    assert body["lots"][0]["acquired_on"] == "2019-03-14"
    assert body["lot_events"][0]["kind"] == "opening"


def test_security_detail_shows_mf_acquired_on_without_any_transactions(client):
    """取引履歴が無くても、MF PDF が持っていた取得日は出せる。"""
    api, _store, security_id = client
    body = api.get(f"/api/security/{security_id}").json()
    assert body["transaction_count"] == 0
    assert body["lots"][0]["acquired_on"] == "2019-03-14"


def test_rollback_removes_transactions(client):
    api, store, _sid = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    }).json()
    api.post(f"/api/import/table/{preview['batch_id']}/commit", json={"account_name": BROKER})
    assert store.count_transactions() == 3

    r = api.delete(f"/api/import/batches/{preview['batch_id']}")
    assert r.status_code == 200
    assert store.count_transactions() == 0
    assert store.list_cost_basis() == []


def test_import_history_labels_the_source_kind(client):
    api, _store, _sid = client
    preview = api.post("/api/import/table", json={
        "filename": "t.csv", "content_b64": _b64(CSV), "account_name": BROKER,
    }).json()
    api.post(f"/api/import/table/{preview['batch_id']}/commit", json={"account_name": BROKER})

    imports = api.get("/api/import/history").json()["imports"]
    csv_batch = next(b for b in imports if b["source_kind"] == "broker_csv")
    assert csv_batch["row_count"] == 3
