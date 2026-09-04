"""Crypto-Summary 由来の資産をタグ／Myポートフォリオで分類できること。

CS のコインは AS の DB に無い仮想保有なので、タグ配分は securities ではなく
external_asset_tags（キー "cs:BTC"）に保存する。tagging 層は id を辞書キーと
してしか見ないため、両者を混ぜたマップで按分集計がそのまま動く。
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import asset_summary.web.app as web_app
from asset_summary.core import crypto_summary_client
from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    Security,
)

D = Decimal


@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = web_app.create_app(str(tmp_path / "t.db"))
    monkeypatch.setattr(web_app, "fetch_spot", lambda store, secs, warn=None: {})
    monkeypatch.setattr(
        web_app, "fetch_fx_rates", lambda store, ccys, warn=None: {}
    )
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "ensure_fx_history", lambda *a, **k: None)
    monkeypatch.setenv("CS_BASE_URL", "http://cs.test")
    monkeypatch.delenv("CS_USER_SUB", raising=False)
    crypto_summary_client.clear_cache()
    monkeypatch.setattr(
        web_app,
        "fetch_cs_summary",
        lambda currency, user_sub, warn=None: {
            "currency": currency,
            "total_value": "5100000",
            "asset_count": 2,
            "unpriced": [],
            "generated_at": "2026-08-07T00:00:00+00:00",
            "assets": [
                {"asset": "BTC", "balance": "0.3", "price": "15000000",
                 "value": "4500000", "has_price": True},
                {"asset": "ETH", "balance": "2", "price": "300000",
                 "value": "600000", "has_price": True},
            ],
        },
    )
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def store(app):
    return app.state.store


def _tag(client: TestClient, name: str) -> int:
    return client.post("/api/tags", json={"name": name}).json()["id"]


def _seed_manual(store) -> int:
    """AS ネイティブの銘柄（評価額 0 でよい。存在確認だけに使う）。"""
    acct = store.get_or_create_account("テスト証券", kind="broker")
    sec_id = store.create_security(
        Security(name="テスト株", name_key="てすとかぶ", code="9999",
                 asset_class=AssetClass.STOCK_JP)
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=acct.id, security_id=sec_id,
                        as_of_date=date(2026, 8, 1), quantity=D("10"))
    )
    return sec_id


# ---- タグ別サマリーに出ること ----


def test_cs_assets_appear_untagged(client):
    d = client.get("/api/tag-summary").json()
    untagged = next(t for t in d["by_tag"] if t["tag_id"] == 0)
    assert D(untagged["value"]) == D("5100000")
    assert {u["security_id"] for u in d["unallocated"]} == {"cs:BTC", "cs:ETH"}


def test_assign_tag_to_cs_asset(client):
    tid = _tag(client, "ビットコイン")
    r = client.put(
        "/api/asset-tags/cs:BTC", json={"allocations": [{"tag_id": tid, "weight": 100}]}
    )
    assert r.status_code == 200

    d = client.get("/api/tag-summary").json()
    row = next(t for t in d["by_tag"] if t["tag_id"] == tid)
    assert D(row["value"]) == D("4500000")
    # ETH は未分類のまま
    untagged = next(t for t in d["by_tag"] if t["tag_id"] == 0)
    assert D(untagged["value"]) == D("600000")
    assert {u["security_id"] for u in d["unallocated"]} == {"cs:ETH"}


def test_partial_allocation_splits(client):
    a = _tag(client, "コアA")
    b = _tag(client, "サテライトB")
    client.put("/api/asset-tags/cs:BTC", json={"allocations": [
        {"tag_id": a, "weight": 70}, {"tag_id": b, "weight": 30},
    ]})
    d = client.get("/api/tag-summary").json()
    by = {t["tag_id"]: D(t["value"]) for t in d["by_tag"]}
    assert by[a] == D("4500000") * D("70") / D("100")
    assert by[b] == D("4500000") * D("30") / D("100")


def test_symbol_is_normalized_to_upper(client):
    tid = _tag(client, "t")
    client.put("/api/asset-tags/cs:btc", json={"allocations": [{"tag_id": tid, "weight": 100}]})
    allocs = client.get("/api/security-tags").json()["allocations"]
    assert "cs:BTC" in allocs and "cs:btc" not in allocs


# ---- Myポートフォリオ ----


def test_portfolio_includes_cs_assets_by_tag(client):
    tid = _tag(client, "暗号資産")
    client.put("/api/asset-tags/cs:BTC", json={"allocations": [{"tag_id": tid, "weight": 100}]})
    pid = client.post("/api/portfolios", json={"name": "暗号", "tag_ids": [tid]}).json()["id"]

    detail = client.get(f"/api/portfolios/{pid}").json()
    assert D(detail["total_value"]) == D("4500000")
    assert [h["id"] for h in detail["holdings"]] == ["cs:BTC"]

    listing = client.get("/api/portfolios").json()["portfolios"]
    assert D(next(p for p in listing if p["id"] == pid)["value"]) == D("4500000")


def test_tag_drilldown_matches_summary(client):
    tid = _tag(client, "暗号資産")
    client.put("/api/asset-tags/cs:ETH", json={"allocations": [{"tag_id": tid, "weight": 100}]})
    summary_row = next(
        t for t in client.get("/api/tag-summary").json()["by_tag"] if t["tag_id"] == tid
    )
    drill = client.get(f"/api/tags/{tid}/holdings").json()
    assert D(drill["total_value"]) == D(summary_row["value"]) == D("600000")


# ---- AS の銘柄と共存すること ----


def test_native_and_external_tags_coexist(client, store):
    sec_id = _seed_manual(store)
    tid = _tag(client, "共通")
    assert client.put(
        f"/api/securities/{sec_id}/tags",
        json={"allocations": [{"tag_id": tid, "weight": 100}]},
    ).status_code == 200
    assert client.put(
        "/api/asset-tags/cs:BTC", json={"allocations": [{"tag_id": tid, "weight": 100}]}
    ).status_code == 200

    allocs = client.get("/api/security-tags").json()["allocations"]
    assert str(sec_id) in allocs and "cs:BTC" in allocs
    # タグ使用件数は両方を数える
    tag = next(t for t in client.get("/api/tags").json()["tags"] if t["id"] == tid)
    assert tag["security_count"] == 2


def test_deleting_tag_removes_external_allocation(client):
    tid = _tag(client, "消す")
    client.put("/api/asset-tags/cs:BTC", json={"allocations": [{"tag_id": tid, "weight": 100}]})
    assert client.delete(f"/api/tags/{tid}").status_code == 200
    assert client.get("/api/security-tags").json()["allocations"] == {}


def test_reassign_replaces_previous(client):
    a, b = _tag(client, "A"), _tag(client, "B")
    client.put("/api/asset-tags/cs:BTC", json={"allocations": [{"tag_id": a, "weight": 100}]})
    client.put("/api/asset-tags/cs:BTC", json={"allocations": [{"tag_id": b, "weight": 100}]})
    allocs = client.get("/api/security-tags").json()["allocations"]["cs:BTC"]
    assert [x["tag_id"] for x in allocs] == [b]


# ---- 入力検証 ----


@pytest.mark.parametrize("key", ["evil:BTC", "BTC", "cs:", "cs:B TC", "cs:../x", "cs:" + "A" * 33])
def test_invalid_asset_keys_rejected(client, key):
    r = client.put(f"/api/asset-tags/{key}", json={"allocations": []})
    assert r.status_code in (400, 404)


def test_weight_over_100_rejected(client):
    a, b = _tag(client, "A"), _tag(client, "B")
    r = client.put("/api/asset-tags/cs:BTC", json={"allocations": [
        {"tag_id": a, "weight": 60}, {"tag_id": b, "weight": 60},
    ]})
    assert r.status_code == 400


# ---- CS 停止時 ----


def test_cs_down_leaves_tag_summary_working(client, store, monkeypatch):
    _seed_manual(store)
    monkeypatch.setattr(
        web_app, "fetch_cs_summary", lambda currency, user_sub, warn=None: None
    )
    d = client.get("/api/tag-summary").json()
    assert not any(str(u["security_id"]).startswith("cs:") for u in d["unallocated"])
    # 保存済みの分類は消えない
    tid = _tag(client, "残る")
    client.put("/api/asset-tags/cs:BTC", json={"allocations": [{"tag_id": tid, "weight": 100}]})
    assert "cs:BTC" in client.get("/api/security-tags").json()["allocations"]


# ---- ルールベースの自動配分（暗号資産 / ステーブルコイン の2分類） ----


@pytest.fixture()
def app_with_coins(app, monkeypatch):
    """BTC・USDT・USDC・DAI を持つ CS を模す。"""
    monkeypatch.setattr(
        web_app,
        "fetch_cs_summary",
        lambda currency, user_sub, warn=None: {
            "currency": currency, "total_value": "0", "asset_count": 4,
            "unpriced": [], "generated_at": "2026-08-07T00:00:00+00:00",
            "assets": [
                {"asset": s, "balance": "1", "price": "100", "value": "100",
                 "has_price": True}
                for s in ("BTC", "USDT", "USDC", "DAI")
            ],
        },
    )
    return app


def _suggestions(client):
    d = client.post("/api/tag-rules/suggest").json()
    return {s["security_id"]: s for s in d["suggestions"]}


def test_rules_split_coins_into_two_buckets(app_with_coins):
    client = TestClient(app_with_coins)
    for name in ("暗号資産", "ステーブルコイン"):
        client.post("/api/tags", json={"name": name})

    s = _suggestions(client)
    assert s["cs:BTC"]["suggested"][0]["name"] == "暗号資産"
    for stable in ("cs:USDT", "cs:USDC", "cs:DAI"):
        assert s[stable]["suggested"][0]["name"] == "ステーブルコイン", stable
    # シンボル完全一致で判定していることを記録に残す
    assert s["cs:USDT"]["matched_by"] == "symbol"
    assert all(s[k]["status"] == "new" for k in s if k.startswith("cs:"))


def test_apply_writes_external_allocations(app_with_coins):
    client = TestClient(app_with_coins)
    for name in ("暗号資産", "ステーブルコイン"):
        client.post("/api/tags", json={"name": name})

    r = client.post("/api/tag-rules/apply",
                    json={"security_ids": ["cs:BTC", "cs:USDT"]})
    assert r.status_code == 200 and r.json()["applied"] == 2

    allocs = client.get("/api/security-tags").json()["allocations"]
    tags = {t["name"]: t["id"] for t in client.get("/api/tags").json()["tags"]}
    assert allocs["cs:BTC"][0]["tag_id"] == tags["暗号資産"]
    assert allocs["cs:USDT"][0]["tag_id"] == tags["ステーブルコイン"]
    # 2回目は unchanged になる
    assert _suggestions(client)["cs:BTC"]["status"] == "unchanged"


def test_apply_reports_missing_tag(app_with_coins):
    client = TestClient(app_with_coins)
    d = client.post("/api/tag-rules/apply", json={"security_ids": ["cs:BTC"]}).json()
    assert d["applied"] == 0
    assert d["skipped"][0]["reason"] == "missing-tag"
    assert d["warnings"]


def test_apply_rejects_unknown_external_key(app_with_coins):
    client = TestClient(app_with_coins)
    assert client.post(
        "/api/tag-rules/apply", json={"security_ids": ["evil:BTC"]}
    ).status_code == 400


def test_suggest_without_cs_still_covers_securities(app, store, monkeypatch):
    monkeypatch.setattr(
        web_app, "fetch_cs_summary", lambda currency, user_sub, warn=None: None
    )
    _seed_manual(store)
    client = TestClient(app)
    d = client.post("/api/tag-rules/suggest").json()
    assert all(not str(s["security_id"]).startswith("cs:") for s in d["suggestions"])


# ---- 価格の付かないコインは扱わない ----


def test_unpriced_coins_are_ignored(app, monkeypatch):
    monkeypatch.setattr(
        web_app,
        "fetch_cs_summary",
        lambda currency, user_sub, warn=None: {
            "currency": currency, "total_value": "4500000", "asset_count": 2,
            "unpriced": ["SPAM"], "generated_at": "2026-08-07T00:00:00+00:00",
            "assets": [
                {"asset": "BTC", "balance": "0.3", "price": "15000000",
                 "value": "4500000", "has_price": True},
                {"asset": "SPAM", "balance": "9999", "price": None,
                 "value": None, "has_price": False},
            ],
        },
    )
    client = TestClient(app)
    d = client.get("/api/summary").json()
    ids = [h["id"] for h in d["holdings"] if h.get("origin") == "crypto_summary"]
    assert ids == ["cs:BTC"]           # 行として出さない
    assert d["unpriced"] == []          # 警告にも出さない
    # 割当対象にもならない
    assert not any(
        u["security_id"] == "cs:SPAM"
        for u in client.get("/api/tag-summary").json()["unallocated"]
    )
