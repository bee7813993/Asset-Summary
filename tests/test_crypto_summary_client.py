"""Crypto-Summary 連携クライアント（HTTP 層 + 純粋マージ）の単体テスト。"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from asset_summary.core import crypto_summary_client as csc
from asset_summary.core.providers import base

D = Decimal
BASE = "http://cs.test"
TOKEN = "svc-token"
SUB = "12345"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(base, "_sleep", lambda _s: None)
    base.reset_throttle()
    csc.clear_cache()
    monkeypatch.setenv("CS_BASE_URL", BASE)
    monkeypatch.setenv("CS_SERVICE_TOKEN", TOKEN)
    monkeypatch.delenv("CS_PUBLIC_URL", raising=False)
    yield
    base.set_client(None)
    csc.clear_cache()


def _install(handler):
    base.set_client(httpx.Client(transport=httpx.MockTransport(handler)))


def _summary_payload(**over):
    d = {
        "currency": "JPY",
        "total_value": "4500000",
        "asset_count": 2,
        "priced_count": 1,
        "unpriced": ["MYSTERY"],
        "assets": [
            {"asset": "BTC", "balance": "0.3", "price": "15000000",
             "value": "4500000", "has_price": True},
            {"asset": "MYSTERY", "balance": "999", "price": None,
             "value": None, "has_price": False},
        ],
        "warnings": [],
        "generated_at": "2026-08-06T00:00:00+00:00",
    }
    d.update(over)
    return d


# ----------------------------------------------------------------------
# 設定・HTTP 層
# ----------------------------------------------------------------------


def test_disabled_without_base_url(monkeypatch):
    monkeypatch.setenv("CS_BASE_URL", "")
    calls = []
    _install(lambda r: calls.append(r) or httpx.Response(200, json={}))
    assert csc.is_enabled() is False
    assert csc.fetch_cs_summary("JPY", SUB) is None
    assert calls == []


def test_public_url_fallback(monkeypatch):
    assert csc.public_url() == BASE
    monkeypatch.setenv("CS_PUBLIC_URL", "https://cs.example.com/")
    assert csc.public_url() == "https://cs.example.com"


def test_fetch_summary_sends_auth_headers_and_params():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["sub"] = request.headers.get("X-CS-User")
        return httpx.Response(200, json=_summary_payload())

    _install(handler)
    got = csc.fetch_cs_summary("JPY", SUB)
    assert got["total_value"] == "4500000"
    assert seen["url"] == f"{BASE}/api/summary?currency=JPY"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["sub"] == SUB


def test_no_token_no_auth_header(monkeypatch):
    monkeypatch.setenv("CS_SERVICE_TOKEN", "")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["sub"] = request.headers.get("X-CS-User")
        return httpx.Response(200, json=_summary_payload())

    _install(handler)
    assert csc.fetch_cs_summary("JPY", None) is not None
    assert seen["auth"] is None
    assert seen["sub"] is None


def test_history_params():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"points": []})

    _install(handler)
    csc.fetch_cs_history("USD", "90d", "total", SUB)
    assert seen["params"] == {"currency": "USD", "range": "90d", "scope": "total"}


def test_ttl_cache_avoids_second_request():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=_summary_payload())

    _install(handler)
    assert csc.fetch_cs_summary("JPY", SUB) is not None
    assert csc.fetch_cs_summary("JPY", SUB) is not None
    assert len(calls) == 1
    # 通貨・sub が違えば別キー
    csc.fetch_cs_summary("USD", SUB)
    assert len(calls) == 2
    csc.clear_cache()
    csc.fetch_cs_summary("JPY", SUB)
    assert len(calls) == 3


def test_failure_negative_cache_and_expiry(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("refused")

    _install(handler)
    t = [1000.0]
    monkeypatch.setattr(csc, "_now", lambda: t[0])

    warns: list[str] = []
    assert csc.fetch_cs_summary("JPY", SUB, warn=warns.append) is None
    assert len(calls) == 1
    assert warns  # 接続失敗が warning に

    # ネガティブキャッシュ中は再リクエストしない（warning は再放出される）
    warns2: list[str] = []
    assert csc.fetch_cs_summary("JPY", SUB, warn=warns2.append) is None
    assert len(calls) == 1
    assert warns2

    # FAILURE_TTL 経過後は再試行する
    t[0] += csc.FAILURE_TTL + 1
    assert csc.fetch_cs_summary("JPY", SUB) is None
    assert len(calls) == 2


def test_http_401_and_404_warnings():
    _install(lambda r: httpx.Response(401, json={"detail": "Not authenticated"}))
    warns: list[str] = []
    assert csc.fetch_cs_summary("JPY", SUB, warn=warns.append) is None
    assert any("CS_SERVICE_TOKEN" in w for w in warns)

    csc.clear_cache()
    _install(lambda r: httpx.Response(404, json={"detail": "no ledger"}))
    warns = []
    assert csc.fetch_cs_summary("JPY", SUB, warn=warns.append) is None
    assert any("台帳" in w for w in warns)


def test_non_json_response_warns():
    _install(lambda r: httpx.Response(200, content=b"<html>oops</html>"))
    warns: list[str] = []
    assert csc.fetch_cs_summary("JPY", SUB, warn=warns.append) is None
    assert warns


# ----------------------------------------------------------------------
# cs_holding_rows
# ----------------------------------------------------------------------


def test_cs_holding_rows_mapping():
    rows = csc.cs_holding_rows(_summary_payload(), in_total=True, currency="JPY")
    # 価格の付かない MYSTERY は AS では扱わない（評価額に寄与せず雑音になるため）
    assert [r["code"] for r in rows] == ["BTC"]
    btc = rows[0]
    assert btc["id"] == "cs:BTC"
    assert btc["origin"] == "crypto_summary"
    assert btc["account"] == "Crypto-Summary"
    assert btc["account_id"] is None
    assert btc["asset_class"] == "crypto"
    assert btc["quantity"] == D("0.3")
    assert btc["price"] == D("15000000")
    assert btc["value"] == D("4500000")
    assert btc["avg_cost"] is None and btc["pl"] is None
    assert btc["has_price"] is True
    assert btc["in_total"] is True
    assert btc["as_of"] == "2026-08-06"


def test_cs_holding_rows_drop_unpriced():
    """価格の付かないコインは行にしない（警告にも出さない）。"""
    payload = _summary_payload(assets=[
        {"asset": "SPAM", "balance": "999", "price": None, "value": None,
         "has_price": False},
        {"asset": "HALF", "balance": "1", "price": "10", "value": None,
         "has_price": True},   # 価格ありを名乗るが評価額が無い壊れた行
    ])
    warns: list[str] = []
    assert csc.cs_holding_rows(payload, True, "JPY", warn=warns.append) == []
    assert warns == []


def test_cs_holding_rows_skips_garbage():
    warns: list[str] = []
    payload = _summary_payload(assets=[
        {"asset": "BTC", "balance": "not-a-number"},
        {"asset": "", "balance": "1"},
        "junk",
        {"asset": "ETH", "balance": "2", "price": "500000", "value": "1000000",
         "has_price": True},
    ])
    rows = csc.cs_holding_rows(payload, True, "JPY", warn=warns.append)
    assert [r["code"] for r in rows] == ["ETH"]
    assert len(warns) == 2  # 不正2件（"junk" は dict でないため黙ってスキップ）


# ----------------------------------------------------------------------
# merge_cs_into_summary
# ----------------------------------------------------------------------


def _as_result(total="1000", with_crypto_class=False):
    classes = [
        {"class": "stock_jp", "value": D("1000"), "cost": None, "pl": None,
         "pl_pct": None, "holding_count": 1, "in_total": True, "weight": D("100")},
    ]
    if with_crypto_class:
        classes.append(
            {"class": "crypto", "value": D("200"), "cost": None, "pl": None,
             "pl_pct": None, "holding_count": 1, "in_total": True, "weight": None}
        )
    return {
        "total_value": D(total),
        "total_cost": None,
        "total_pl": None,
        "total_pl_pct": None,
        "pl_excluded_count": 0,
        "holdings": [],
        "classes": classes,
        "unpriced": [],
    }


def test_merge_adds_total_class_and_weights():
    res = _as_result()
    csc.merge_cs_into_summary(res, _summary_payload(), {}, "JPY")
    assert res["total_value"] == D("1000") + D("4500000")
    crypto = next(c for c in res["classes"] if c["class"] == "crypto")
    assert crypto["value"] == D("4500000")
    assert crypto["holding_count"] == 1   # 価格の付かない MYSTERY は数えない
    assert crypto["in_total"] is True
    # weight 再計算（クラス合計 / 新しい総額）
    total = res["total_value"]
    assert crypto["weight"] == D("4500000") / total * 100
    stock = next(c for c in res["classes"] if c["class"] == "stock_jp")
    assert stock["weight"] == D("1000") / total * 100
    # 評価額降順
    assert res["classes"][0]["class"] == "crypto"
    assert [h["id"] for h in res["holdings"]][0] == "cs:BTC"
    # CS 側の unpriced は AS の警告に持ち込まない
    assert res["unpriced"] == []


def test_merge_adds_into_existing_crypto_class():
    res = _as_result(with_crypto_class=True)
    csc.merge_cs_into_summary(res, _summary_payload(), {}, "JPY")
    crypto = next(c for c in res["classes"] if c["class"] == "crypto")
    assert crypto["value"] == D("200") + D("4500000")
    assert crypto["holding_count"] == 2


def test_merge_respects_include_crypto_off():
    res = _as_result()
    csc.merge_cs_into_summary(
        res, _summary_payload(), {"include_crypto": "0"}, "JPY"
    )
    assert res["total_value"] == D("1000")  # 総額に加算されない
    crypto = next(c for c in res["classes"] if c["class"] == "crypto")
    assert crypto["in_total"] is False
    assert crypto["weight"] is None
    assert crypto["value"] == D("4500000")  # クラス行そのものは出る


def test_merge_none_or_empty_is_noop():
    res = _as_result()
    csc.merge_cs_into_summary(res, None, {}, "JPY")
    assert res["total_value"] == D("1000")
    csc.merge_cs_into_summary(res, _summary_payload(assets=[]), {}, "JPY")
    assert res["total_value"] == D("1000")
    assert all(c["class"] != "crypto" for c in res["classes"])


# ----------------------------------------------------------------------
# merge_cs_history
# ----------------------------------------------------------------------


def test_merge_history_forward_fill_and_no_backfill():
    points = [
        {"t": "2026-08-01", "value": D("100"), "cost": D("50")},
        {"t": "2026-08-02", "value": D("100"), "cost": D("50")},
        {"t": "2026-08-03", "value": D("100"), "cost": D("50")},
        {"t": "2026-08-04", "value": D("100"), "cost": D("50")},
    ]
    cs_points = [
        {"t": "2026-08-02", "value": "10"},
        {"t": "2026-08-04", "value": "20"},
    ]
    assert csc.merge_cs_history(points, cs_points) is True
    # 初日（CS 開始前）は加算しない
    assert points[0]["value"] == D("100")
    assert points[1]["value"] == D("110")
    # 欠測日 8/3 は前方フィル
    assert points[2]["value"] == D("110")
    assert points[3]["value"] == D("120")
    # cost は不変
    assert all(p["cost"] == D("50") for p in points)


def test_merge_history_empty_or_garbage():
    points = [{"t": "2026-08-01", "value": D("100"), "cost": None}]
    assert csc.merge_cs_history(points, None) is False
    assert csc.merge_cs_history(points, []) is False
    assert csc.merge_cs_history(points, [{"t": "", "value": "x"}]) is False
    assert points[0]["value"] == D("100")
