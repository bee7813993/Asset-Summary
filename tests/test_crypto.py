"""暗号資産クラスと CoinGecko プロバイダ。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from asset_summary.core.models import ASSET_CLASS_META, AssetClass, PriceSourceType
from asset_summary.core.providers import base, coingecko
from asset_summary.web.app import create_app

D = Decimal


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(base, "_sleep", lambda _s: None)
    base.reset_throttle()
    yield
    base.set_client(None)


def _client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ms(y, m, d):
    return int(datetime(y, m, d, 12, tzinfo=timezone.utc).timestamp() * 1000)


# ----------------------------------------------------------------------
# モデル
# ----------------------------------------------------------------------

def test_crypto_asset_class_registered():
    assert AssetClass.CRYPTO.value == "crypto"
    assert ASSET_CLASS_META["crypto"]["ja"] == "暗号資産"
    assert PriceSourceType.COINGECKO.value == "coingecko"


# ----------------------------------------------------------------------
# プロバイダ
# ----------------------------------------------------------------------

def test_fetch_spot_parses_jpy():
    def handler(request):
        assert request.url.path.endswith("/simple/price")
        assert request.url.params["ids"] == "bitcoin"
        return httpx.Response(200, json={"bitcoin": {"jpy": 15000000}})

    got = coingecko.fetch_spot("bitcoin", client=_client_with(handler))
    assert got == (D("15000000"), "JPY")


def test_fetch_spot_unknown_coin_warns():
    warns = []
    got = coingecko.fetch_spot(
        "nope", warn=warns.append,
        client=_client_with(lambda r: httpx.Response(200, json={})),
    )
    assert got is None
    assert warns


def test_fetch_history_buckets_to_daily_close():
    """intraday の系列を UTC日次の終値（その日の最後の値）へ畳むこと。"""
    payload = {"prices": [
        [_ms(2026, 8, 1), 100], [_ms(2026, 8, 1) + 3600_000, 110],   # 8/1 → 110
        [_ms(2026, 8, 2), 120],
    ]}
    res = coingecko.fetch_history(
        "bitcoin", date(2026, 8, 1), date(2026, 8, 2),
        client=_client_with(lambda r: httpx.Response(200, json=payload)),
    )
    assert res.prices == {date(2026, 8, 1): D("110"), date(2026, 8, 2): D("120")}
    assert res.currency == "JPY"
    assert res.covered == [(date(2026, 8, 1), date(2026, 8, 2))]


def test_fetch_history_trims_outside_range():
    payload = {"prices": [[_ms(2026, 7, 30), 90], [_ms(2026, 8, 1), 100]]}
    res = coingecko.fetch_history(
        "bitcoin", date(2026, 8, 1), date(2026, 8, 1),
        client=_client_with(lambda r: httpx.Response(200, json=payload)),
    )
    assert list(res.prices) == [date(2026, 8, 1)]


def test_fetch_history_empty_is_not_covered():
    """価格が無いときは被覆を記録させない（次回再試行できるように）。"""
    res = coingecko.fetch_history(
        "bitcoin", date(2026, 8, 1), date(2026, 8, 2), warn=lambda _m: None,
        client=_client_with(lambda r: httpx.Response(200, json={"prices": []})),
    )
    assert res.prices == {}
    assert res.covered == []


def test_search_maps_fields():
    payload = {"coins": [
        {"id": "bitcoin", "name": "Bitcoin", "symbol": "btc", "market_cap_rank": 1},
        {"id": "", "name": "broken"},
    ]}
    out = coingecko.search(
        "bit", client=_client_with(lambda r: httpx.Response(200, json=payload))
    )
    assert out == [{"name": "Bitcoin", "symbol": "BTC", "ref": "bitcoin", "rank": 1}]


def test_provider_errors_do_not_raise():
    for resp in (httpx.Response(500), httpx.Response(200, content=b"not json")):
        warns = []
        assert coingecko.fetch_spot(
            "bitcoin", warn=warns.append, client=_client_with(lambda r, _r=resp: _r)
        ) is None
        assert warns


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "crypto.db")))


def test_crypto_security_and_holding_roundtrip(client: TestClient):
    r = client.post("/api/securities", json={
        "name": "Bitcoin (BTC)", "asset_class": "crypto",
        "price_source_type": "coingecko", "price_source_ref": "bitcoin",
    })
    assert r.status_code == 200
    sec_id = r.json()["id"]
    sec = client.get("/api/securities").json()["securities"][0]
    assert sec["asset_class"] == "crypto"
    assert sec["price_source_type"] == "coingecko"
    assert sec["price_source_status"] == "linked"

    assert client.post("/api/holdings", json={
        "security_id": sec_id, "account_name": "架空取引所", "quantity": "0.5",
        "avg_cost": "12000000",
    }).status_code == 200


def test_crypto_class_can_be_excluded_from_total(client: TestClient):
    meta = client.get("/api/meta").json()
    assert "crypto" in meta["settings"]["include_classes"]
    assert client.put(
        "/api/settings", json={"include_classes": {"crypto": False}}
    ).status_code == 200
    assert client.get("/api/meta").json()["settings"]["include_classes"]["crypto"] is False


def test_coin_search_endpoint(client: TestClient, monkeypatch):
    import asset_summary.web.app as web_app

    monkeypatch.setattr(
        web_app, "search_coins",
        lambda q, warn=None: [{"name": "Bitcoin", "symbol": "BTC", "ref": "bitcoin"}],
    )
    d = client.get("/api/coin-search?q=btc").json()
    assert d["results"][0]["ref"] == "bitcoin"
    # 空クエリはネットワークを叩かない
    assert client.get("/api/coin-search?q=").json()["results"] == []


def test_coin_search_errors_are_warnings(client: TestClient, monkeypatch):
    import asset_summary.web.app as web_app

    def boom(q, warn=None):
        raise RuntimeError("down")

    monkeypatch.setattr(web_app, "search_coins", boom)
    d = client.get("/api/coin-search?q=btc").json()
    assert d["results"] == []
    assert d["warnings"]
