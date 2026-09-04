"""Crypto-Summary 連携の Web 層テスト（合算・プロキシ・状態ブロック）。

CS フェッチャーはモジュール名前空間経由で参照されるため、
monkeypatch.setattr(web_app, "fetch_cs_summary", fake) で差し替える。
"""

from __future__ import annotations

import os
import tempfile

# モジュールレベルの app = create_app(...) がリポジトリの data/assets.db を
# 作らないよう、import 前に一時パスへ向ける。
os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import asset_summary.web.app as web_app
from asset_summary.core import crypto_summary_client
from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)

D = Decimal
CS_URL = "http://cs.test"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = web_app.create_app(str(tmp_path / "t.db"))
    monkeypatch.setattr(web_app, "fetch_spot", lambda store, secs, warn=None: {})
    monkeypatch.setattr(
        web_app,
        "fetch_fx_rates",
        lambda store, ccys, warn=None: {c: D("150") for c in ccys},
    )
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "ensure_fx_history", lambda *a, **k: None)
    monkeypatch.setenv("CS_BASE_URL", CS_URL)
    monkeypatch.delenv("CS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("CS_USER_SUB", raising=False)
    crypto_summary_client.clear_cache()
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def store(app):
    return app.state.store


def _cs_payload(**over):
    d = {
        "currency": "JPY",
        "total_value": "4500000",
        "asset_count": 1,
        "priced_count": 1,
        "unpriced": [],
        "assets": [
            {"asset": "BTC", "balance": "0.3", "price": "15000000",
             "value": "4500000", "has_price": True},
        ],
        "warnings": [],
        "generated_at": "2026-08-06T00:00:00+00:00",
    }
    d.update(over)
    return d


def _patch_cs_summary(monkeypatch, payload=None, calls=None):
    def fake(currency, user_sub, warn=None):
        if calls is not None:
            calls.append((currency, user_sub))
        return payload

    monkeypatch.setattr(web_app, "fetch_cs_summary", fake)


def _seed_stock(store):
    """株100株 @取得1000円（スポット1200円で評価額12万円）。"""
    acct = store.get_or_create_account("テスト証券", kind="broker")
    sec_id = store.create_security(
        Security(
            name="テスト工業",
            name_key="てすとこうぎょう",
            code="9999",
            asset_class=AssetClass.STOCK_JP,
        )
    )
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=acct.id,
            security_id=sec_id,
            as_of_date=date(2026, 8, 1),
            quantity=D("100"),
            avg_cost=D("1000"),
        )
    )
    return sec_id


def _seed_manual_crypto(store):
    """手動登録の 0.1 BTC（スポット1500万円で評価額150万円）。"""
    acct = store.get_or_create_account("架空取引所", kind="manual")
    sec_id = store.create_security(
        Security(
            name="Bitcoin (BTC)",
            name_key="bitcoin btc",
            asset_class=AssetClass.CRYPTO,
            unit=Unit.UNIT,
            price_source_type=PriceSourceType.COINGECKO,
            price_source_ref="bitcoin",
            price_source_status=PriceSourceStatus.LINKED,
        )
    )
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=acct.id,
            security_id=sec_id,
            as_of_date=date(2026, 8, 1),
            quantity=D("0.1"),
        )
    )
    return sec_id


# ----------------------------------------------------------------------
# /api/summary
# ----------------------------------------------------------------------


def test_summary_merges_cs(client, store, monkeypatch):
    sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    calls: list = []
    _patch_cs_summary(monkeypatch, _cs_payload(), calls)

    data = client.get("/api/summary").json()
    assert data["total_value"] == "4620000"  # 120000 + 4500000
    cs_row = next(h for h in data["holdings"] if h["id"] == "cs:BTC")
    assert cs_row["origin"] == "crypto_summary"
    assert cs_row["account"] == "Crypto-Summary"
    assert cs_row["quantity"] == "0.3"  # Decimal は文字列で JSON 化
    assert cs_row["value"] == "4500000"
    assert cs_row["pl"] is None
    crypto_cls = next(c for c in data["classes"] if c["class"] == "crypto")
    assert crypto_cls["value"] == "4500000"
    assert crypto_cls["in_total"] is True
    assert crypto_cls["weight"] is not None
    assert data["crypto_summary"] == {
        "configured": True,
        "connected": True,
        "generated_at": "2026-08-06T00:00:00+00:00",
    }
    assert calls == [("JPY", None)]


def test_summary_disabled_without_env(client, monkeypatch):
    monkeypatch.setenv("CS_BASE_URL", "")

    def boom(currency, user_sub, warn=None):  # 呼ばれてはいけない
        raise AssertionError("CS へのアクセスが発生した")

    monkeypatch.setattr(web_app, "fetch_cs_summary", boom)
    data = client.get("/api/summary").json()
    assert data["crypto_summary"] == {
        "configured": False, "connected": None, "generated_at": None,
    }
    assert all(h.get("origin") != "crypto_summary" for h in data["holdings"])


def test_summary_cs_down_degrades_with_warning(client, store, monkeypatch):
    sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )

    def fake(currency, user_sub, warn=None):
        if warn:
            warn("Crypto-Summary に接続できません")
        return None

    monkeypatch.setattr(web_app, "fetch_cs_summary", fake)
    data = client.get("/api/summary").json()
    assert data["total_value"] == "120000"  # 手動分のみ
    assert data["crypto_summary"]["configured"] is True
    assert data["crypto_summary"]["connected"] is False
    assert any("接続できません" in w for w in data["warnings"])


def test_summary_respects_include_crypto_off(client, store, monkeypatch):
    sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    _patch_cs_summary(monkeypatch, _cs_payload())
    assert client.put(
        "/api/settings", json={"include_classes": {"crypto": False}}
    ).status_code == 200

    data = client.get("/api/summary").json()
    assert data["total_value"] == "120000"  # CS 分は総額に入らない
    crypto_cls = next(c for c in data["classes"] if c["class"] == "crypto")
    assert crypto_cls["in_total"] is False
    assert crypto_cls["value"] == "4500000"  # クラス行としては見える


def test_summary_passes_user_sub_from_env(client, monkeypatch):
    monkeypatch.setenv("CS_USER_SUB", "777")
    calls: list = []
    _patch_cs_summary(monkeypatch, _cs_payload(), calls)
    client.get("/api/summary")
    assert calls == [("JPY", "777")]


def test_summary_fx_fallback_fetches_cs_in_jpy(client, store, monkeypatch):
    """FXレート無しで円ベースにフォールバックしたら、CS も JPY で取得する
    （表示通貨建ての CS 値を円ベースの合計に混ぜない）。"""
    _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_fx_rates", lambda store, ccys, warn=None: {}
    )
    calls: list = []
    _patch_cs_summary(monkeypatch, _cs_payload(), calls)
    data = client.get("/api/summary?currency=USD").json()
    assert calls == [("JPY", None)]
    assert any("円ベース" in w for w in data["warnings"])
    # レートがあるときは表示通貨のまま
    monkeypatch.setattr(
        web_app, "fetch_fx_rates",
        lambda store, ccys, warn=None: {c: D("150") for c in ccys},
    )
    calls.clear()
    client.get("/api/summary?currency=USD")
    assert calls == [("USD", None)]


def test_history_fx_fallback_fetches_cs_in_jpy(client, store, monkeypatch):
    _seed_stock(store)
    calls: list = []
    _patch_cs_history(monkeypatch, [], calls)
    # store に FX 履歴が無い状態で USD を要求 → 円ベースフォールバック
    client.get("/api/portfolio-history?range=7d&scope=total&currency=USD")
    assert calls and calls[0][0] == "JPY"


# ----------------------------------------------------------------------
# /api/classes, /api/class-holdings, /api/account-holdings
# ----------------------------------------------------------------------


def test_classes_include_cs(client, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload())
    data = client.get("/api/classes").json()
    crypto_cls = next(c for c in data["classes"] if c["class"] == "crypto")
    assert crypto_cls["value"] == "4500000"
    assert crypto_cls["label"] == "暗号資産"
    assert data["crypto_summary"]["connected"] is True


def test_class_holdings_merges_manual_and_cs(client, store, monkeypatch):
    sec_id = _seed_manual_crypto(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("15000000")}
    )
    _patch_cs_summary(monkeypatch, _cs_payload())

    data = client.get("/api/class-holdings?class=crypto").json()
    ids = [h["id"] for h in data["holdings"]]
    assert sec_id in ids and "cs:BTC" in ids  # 手動分と CS 分が併記
    assert D(data["total_value"]) == D("6000000")  # 1500000 + 4500000
    assert data["crypto_summary"]["connected"] is True


def test_class_holdings_non_crypto_skips_cs(client, store, monkeypatch):
    _seed_stock(store)

    def boom(currency, user_sub, warn=None):
        raise AssertionError("crypto 以外のクラスで CS が呼ばれた")

    monkeypatch.setattr(web_app, "fetch_cs_summary", boom)
    data = client.get("/api/class-holdings?class=stock_jp").json()
    assert "crypto_summary" not in data


def test_account_holdings_cs_pseudo_account(client, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload())
    data = client.get("/api/account-holdings?account=Crypto-Summary").json()
    assert [h["id"] for h in data["holdings"]] == ["cs:BTC"]
    assert data["total_value"] == "4500000"
    # 他口座では CS 行は現れない
    data2 = client.get("/api/account-holdings?account=どこか").json()
    assert data2["holdings"] == []


# ----------------------------------------------------------------------
# /api/portfolio-history
# ----------------------------------------------------------------------


def _patch_cs_history(monkeypatch, points, calls=None):
    def fake(currency, range_key, scope, user_sub, warn=None):
        if calls is not None:
            calls.append((currency, range_key, scope, user_sub))
        return {"points": points, "is_partial": False, "unpriced": []}

    monkeypatch.setattr(web_app, "fetch_cs_history", fake)


def test_history_total_merges_cs(client, store, monkeypatch):
    _seed_stock(store)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    calls: list = []
    _patch_cs_history(monkeypatch, [{"t": yesterday, "value": "500"}], calls)

    data = client.get("/api/portfolio-history?range=7d&scope=total").json()
    assert calls and calls[0][2] == "total"
    # 前日以降は CS 分が乗る（前方フィルで当日も 500）
    assert data["points"][-1]["value"] == "500"
    # CS 開始前の日は 0 のまま
    assert data["points"][0]["value"] == "0"


def test_history_class_crypto_maps_to_cs_total(client, monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    calls: list = []
    _patch_cs_history(monkeypatch, [{"t": yesterday, "value": "500"}], calls)
    data = client.get("/api/portfolio-history?range=7d&scope=class:crypto").json()
    assert calls and calls[0][2] == "total"  # CS 側は常に total スコープ
    assert data["points"][-1]["value"] == "500"


def test_history_cs_account_scope(client, monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _patch_cs_history(monkeypatch, [{"t": yesterday, "value": "500"}])
    data = client.get(
        "/api/portfolio-history?range=7d&scope=account:Crypto-Summary"
    ).json()
    assert data["points"][-1]["value"] == "500"


def test_history_other_scopes_skip_cs(client, store, monkeypatch):
    _seed_stock(store)

    def boom(currency, range_key, scope, user_sub, warn=None):
        raise AssertionError("対象外スコープで CS が呼ばれた")

    monkeypatch.setattr(web_app, "fetch_cs_history", boom)
    client.get("/api/portfolio-history?range=7d&scope=account:テスト証券")
    client.get("/api/portfolio-history?range=7d&scope=class:stock_jp")


def test_history_include_crypto_off_skips_cs_for_total(client, store, monkeypatch):
    _seed_stock(store)
    assert client.put(
        "/api/settings", json={"include_classes": {"crypto": False}}
    ).status_code == 200

    def boom(currency, range_key, scope, user_sub, warn=None):
        raise AssertionError("除外設定なのに total で CS が呼ばれた")

    monkeypatch.setattr(web_app, "fetch_cs_history", boom)
    client.get("/api/portfolio-history?range=7d&scope=total")


# ----------------------------------------------------------------------
# プロキシ / meta / health
# ----------------------------------------------------------------------


def test_cs_status_enabled(client, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload())
    data = client.get("/api/crypto-summary/status").json()
    assert data["configured"] is True
    assert data["connected"] is True
    assert data["url"] == CS_URL
    assert data["asset_count"] == 1
    assert data["total_value"] == "4500000"
    assert data["cs_generated_at"] == "2026-08-06T00:00:00+00:00"


def test_cs_status_disabled(client, monkeypatch):
    monkeypatch.setenv("CS_BASE_URL", "")
    data = client.get("/api/crypto-summary/status").json()
    assert data == {
        "configured": False,
        "connected": None,
        "url": None,
        "currency": "JPY",
        "asset_count": None,
        "total_value": None,
        "cs_generated_at": None,
        "warnings": [],
        "generated_at": data["generated_at"],
    }


def test_cs_asset_detail(client, monkeypatch):
    def fake_accounts(asset, currency, user_sub, warn=None):
        assert asset == "BTC"
        return {
            "asset": "BTC",
            "price": "15000000",
            "total_balance": "0.3",
            "total_value": "4500000",
            "accounts": [{"account": "bitFlyer", "balance": "0.3", "value": "4500000"}],
        }

    def fake_history(currency, range_key, scope, user_sub, warn=None):
        assert scope == "asset:BTC"
        return {"points": [{"t": "2026-08-01", "value": "4400000"}], "is_partial": False}

    monkeypatch.setattr(web_app, "fetch_cs_asset_accounts", fake_accounts)
    monkeypatch.setattr(web_app, "fetch_cs_history", fake_history)

    data = client.get("/api/crypto-summary/asset/btc?range=30d").json()
    assert data["asset"] == "BTC"  # 大文字に正規化
    assert data["balance"] == "0.3"
    assert data["value"] == "4500000"
    assert data["accounts"][0]["account"] == "bitFlyer"
    assert data["history"]["points"][0]["value"] == "4400000"
    assert data["connected"] is True
    assert data["range"] == "30d"


def test_cs_asset_detail_disabled_404(client, monkeypatch):
    monkeypatch.setenv("CS_BASE_URL", "")
    assert client.get("/api/crypto-summary/asset/BTC").status_code == 404


def test_cs_coin_icons_passthrough(client, monkeypatch):
    monkeypatch.setattr(
        web_app, "fetch_cs_coin_icons", lambda warn=None: {"BTC": "http://x/btc.png"}
    )
    assert client.get("/api/crypto-summary/coin-icons").json() == {
        "BTC": "http://x/btc.png"
    }
    monkeypatch.setattr(web_app, "fetch_cs_coin_icons", lambda warn=None: None)
    assert client.get("/api/crypto-summary/coin-icons").json() == {}


def test_meta_has_cs_block(client, monkeypatch):
    data = client.get("/api/meta").json()
    assert data["crypto_summary"] == {"enabled": True, "url": CS_URL}
    monkeypatch.setenv("CS_PUBLIC_URL", "https://cs.example.com")
    data = client.get("/api/meta").json()
    assert data["crypto_summary"]["url"] == "https://cs.example.com"
    monkeypatch.setenv("CS_BASE_URL", "")
    monkeypatch.delenv("CS_PUBLIC_URL", raising=False)
    data = client.get("/api/meta").json()
    assert data["crypto_summary"] == {"enabled": False, "url": None}


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


# ----------------------------------------------------------------------
# CS 分の前日比
# ----------------------------------------------------------------------


def _patch_cs_history_by_scope(monkeypatch, by_scope, calls=None):
    """scope → points のマップで応答を差し替える。載っていない scope は None。"""

    def fake(currency, range_key, scope, user_sub, warn=None):
        if calls is not None:
            calls.append(scope)
        points = by_scope.get(scope)
        if points is None:
            return None
        return {"points": points, "is_partial": False, "unpriced": []}

    monkeypatch.setattr(web_app, "fetch_cs_history", fake)


def _yesterday_point(value):
    return [{"t": (date.today() - timedelta(days=1)).isoformat(), "value": value}]


def test_summary_total_day_change_comes_from_cs_total_history(
    client, store, monkeypatch
):
    """CS の合計は履歴1本（scope=total）で賄う — コイン別には取りに行かない。"""
    _patch_cs_summary(monkeypatch, _cs_payload())
    calls: list = []
    _patch_cs_history_by_scope(monkeypatch, {"total": _yesterday_point("4000000")}, calls)

    data = client.get("/api/summary").json()
    assert data["total_day_change"] == "500000"      # 450万 − 400万
    assert data["total_day_change_pct"] == "12.50"
    assert calls == ["total"]                        # asset:BTC は呼ばない
    # ダッシュボードの明細では CS コイン行は「—」のまま
    cs_row = next(h for h in data["holdings"] if h["origin"] == "crypto_summary")
    assert cs_row["day_change"] is None

    crypto = next(c for c in data["classes"] if c["class"] == "crypto")
    assert crypto["day_change"] == "500000"


def test_summary_marks_partial_when_cs_day_change_is_unavailable(
    client, store, monkeypatch
):
    _patch_cs_summary(monkeypatch, _cs_payload())
    _patch_cs_history_by_scope(monkeypatch, {})      # 履歴が取れない

    data = client.get("/api/summary").json()
    assert data["total_day_change"] is None
    assert data["day_change_partial"] is True


def test_crypto_class_detail_fills_per_coin_day_change(client, store, monkeypatch):
    """コイン一覧を出す画面だけ、コインごとに CS 履歴を取る。"""
    _patch_cs_summary(monkeypatch, _cs_payload())
    calls: list = []
    _patch_cs_history_by_scope(
        monkeypatch,
        {"total": _yesterday_point("4000000"), "asset:BTC": _yesterday_point("4200000")},
        calls,
    )

    data = client.get("/api/class-holdings?class=crypto").json()
    row = next(h for h in data["holdings"] if h["origin"] == "crypto_summary")
    assert row["day_change"] == "300000"             # 450万 − 420万
    assert "asset:BTC" in calls


def test_cs_account_detail_fills_per_coin_day_change(client, store, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload())
    _patch_cs_history_by_scope(
        monkeypatch,
        {"total": _yesterday_point("4000000"), "asset:BTC": _yesterday_point("4200000")},
    )
    account = crypto_summary_client.CS_ACCOUNT_NAME
    data = client.get(f"/api/account-holdings?account={account}").json()
    row = next(h for h in data["holdings"] if h["origin"] == "crypto_summary")
    assert row["day_change"] == "300000"
    assert data["total_day_change"] == "300000"


def test_cs_asset_detail_day_change_reuses_the_history_it_already_fetches(
    client, monkeypatch
):
    calls: list = []
    _patch_cs_history_by_scope(monkeypatch, {"asset:BTC": _yesterday_point("4200000")}, calls)
    monkeypatch.setattr(
        web_app, "fetch_cs_asset_accounts",
        lambda asset, currency, user_sub, warn=None: {
            "price": "15000000", "total_balance": "0.3", "total_value": "4500000",
            "accounts": [],
        },
    )
    data = client.get("/api/crypto-summary/asset/BTC").json()
    assert data["day_change"] == "300000"
    assert data["day_change_pct"] == "7.14"
    assert calls == ["asset:BTC"]                    # 追加リクエストなし


def test_cs_down_leaves_day_change_empty_without_error(client, store, monkeypatch):
    _seed_stock(store)
    _patch_cs_summary(monkeypatch, None)

    def boom(*a, **k):
        raise RuntimeError("CS down")

    monkeypatch.setattr(web_app, "fetch_cs_history", boom)
    for url in ("/api/summary", "/api/class-holdings?class=crypto"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.json()["day_change_partial"] is False


def test_stale_cs_history_is_not_treated_as_yesterday(client, store, monkeypatch):
    """何週間も前の点を「前日」と呼ばない。"""
    _patch_cs_summary(monkeypatch, _cs_payload())
    old = (date.today() - timedelta(days=30)).isoformat()
    _patch_cs_history_by_scope(monkeypatch, {"total": [{"t": old, "value": "4000000"}]})
    data = client.get("/api/summary").json()
    assert data["total_day_change"] is None
    assert data["day_change_partial"] is True


# ----------------------------------------------------------------------
# CS が /api/summary で前日値を返す場合（履歴を引かずに全画面を埋める）
# ----------------------------------------------------------------------


def _cs_payload_with_prev(**over):
    """prev_price / prev_value / prev_date を持つ新しい CS の応答。

    BTC: 420万 → 450万（+30万 / +7.14%）
    """
    payload = _cs_payload(
        total_prev_value="4200000",
        prev_missing=[],
        assets=[
            {"asset": "BTC", "balance": "0.3", "price": "15000000",
             "value": "4500000", "has_price": True,
             "prev_price": "14000000", "prev_value": "4200000",
             "prev_date": "2026-08-12"},
        ],
    )
    payload.update(over)
    return payload


def _forbid_history(monkeypatch):
    """履歴が1本でも呼ばれたら落ちるようにする。"""

    def boom(*a, **k):
        raise AssertionError("履歴を引いてはいけない場面で fetch_cs_history が呼ばれた")

    monkeypatch.setattr(web_app, "fetch_cs_history", boom)


def test_summary_uses_prev_value_without_touching_history(client, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload_with_prev())
    _forbid_history(monkeypatch)

    data = client.get("/api/summary").json()
    assert data["total_day_change"] == "300000"
    assert data["total_day_change_pct"] == "7.14"
    assert data["day_change_partial"] is False

    # ダッシュボード・保有一覧の明細行にも前日比が出る（以前は「—」だった）
    row = next(h for h in data["holdings"] if h["origin"] == "crypto_summary")
    assert row["day_change"] == "300000"
    assert row["day_change_pct"] == "7.14"
    assert row["day_change_as_of"] == "2026-08-12"
    assert next(h for h in data["holdings_by_security"]
                if h["origin"] == "crypto_summary")["day_change"] == "300000"

    crypto = next(c for c in data["classes"] if c["class"] == "crypto")
    assert crypto["day_change"] == "300000"


def test_prev_balance_makes_crypto_day_change_an_actual_difference(client, monkeypatch):
    """CS が前日残高で評価した前日値を、AS はそのまま使う。

    今日 0.3 → 0.5 BTC に買い増した状態。ここで AS が「いまの残高 × 前日終値」に
    計算し直すと買い増しぶんが消え、AS 内の保有と定義がずれてしまう。
    """
    _patch_cs_summary(monkeypatch, _cs_payload_with_prev(
        total_prev_value="4200000",
        assets=[
            {"asset": "BTC", "balance": "0.5", "price": "15000000",
             "value": "7500000", "has_price": True,
             "prev_price": "14000000", "prev_balance": "0.3",
             "prev_value": "4200000", "prev_date": "2026-08-12"},
        ],
    ))
    _forbid_history(monkeypatch)

    data = client.get("/api/summary").json()
    row = next(h for h in data["holdings"] if h["origin"] == "crypto_summary")
    assert row["prev_value"] == "4200000"        # 0.3 × 1400万
    assert row["day_change"] == "3300000"        # 買い増しぶんも含む
    assert data["total_day_change"] == "3300000"


def test_crypto_class_detail_needs_no_per_coin_history_anymore(client, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload_with_prev())
    _forbid_history(monkeypatch)

    data = client.get("/api/class-holdings?class=crypto").json()
    row = next(h for h in data["holdings"] if h["origin"] == "crypto_summary")
    assert row["day_change"] == "300000"
    assert data["total_day_change"] == "300000"


def test_cs_account_detail_needs_no_per_coin_history_anymore(client, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload_with_prev())
    _forbid_history(monkeypatch)

    account = crypto_summary_client.CS_ACCOUNT_NAME
    data = client.get(f"/api/account-holdings?account={account}").json()
    assert data["total_day_change"] == "300000"


def test_portfolio_members_show_cs_day_change(client, store, monkeypatch):
    """タグの構成銘柄でも CS コインの前日比が出る（以前は「—」だった）。"""
    _patch_cs_summary(monkeypatch, _cs_payload_with_prev())
    _forbid_history(monkeypatch)

    tag_id = client.post("/api/tags", json={"name": "暗号資産"}).json()["id"]
    client.put(
        f"/api/asset-tags/{crypto_summary_client.EXTERNAL_KEY_PREFIX}BTC",
        json={"allocations": [{"tag_id": tag_id, "weight": "100"}]},
    )
    data = client.get(f"/api/tags/{tag_id}/holdings").json()
    row = data["holdings"][0]
    assert row["day_change"] == "300000"
    assert data["total_day_change"] == "300000"


def test_partially_missing_prev_values_are_marked_partial(client, monkeypatch):
    """前日値が取れないコインが混ざったら、取れた分だけ足して partial を立てる。"""
    _patch_cs_summary(monkeypatch, _cs_payload_with_prev(
        total_value="5500000",
        prev_missing=["XRP"],
        assets=[
            {"asset": "BTC", "balance": "0.3", "price": "15000000",
             "value": "4500000", "has_price": True,
             "prev_price": "14000000", "prev_value": "4200000",
             "prev_date": "2026-08-12"},
            {"asset": "XRP", "balance": "10000", "price": "100",
             "value": "1000000", "has_price": True,
             "prev_price": None, "prev_value": None, "prev_date": None},
        ],
    ))
    _forbid_history(monkeypatch)

    data = client.get("/api/summary").json()
    assert data["total_day_change"] == "300000"     # BTC の分だけ
    assert data["day_change_partial"] is True
    xrp = next(h for h in data["holdings"] if h.get("code") == "XRP")
    assert xrp["day_change"] is None


def test_old_cs_without_prev_value_still_falls_back_to_history(client, monkeypatch):
    """前日値を返さない CS が相手でも、合計は履歴1本から出せる。"""
    _patch_cs_summary(monkeypatch, _cs_payload())   # prev_* なし
    calls: list = []
    _patch_cs_history_by_scope(monkeypatch, {"total": _yesterday_point("4000000")}, calls)

    data = client.get("/api/summary").json()
    assert data["total_day_change"] == "500000"
    assert calls == ["total"]


def test_cs_asset_detail_matches_the_row_in_the_holdings_table(client, monkeypatch):
    """コイン詳細のタイルと保有テーブルの行が同じ数字になる。

    CS の履歴は「その日の残高」で積むため、前日に入出金があると
    末尾2点の差は「価格が動いたぶん」からずれる。/api/summary の前日値
    （いまの残高 × 前日終値）を正とする。
    """
    _patch_cs_summary(monkeypatch, _cs_payload_with_prev())
    # 履歴の末尾は前日に入金があったことにして、意図的に食い違わせる
    _patch_cs_history_by_scope(monkeypatch, {"asset:BTC": _yesterday_point("1000000")})
    monkeypatch.setattr(
        web_app, "fetch_cs_asset_accounts",
        lambda asset, currency, user_sub, warn=None: {
            "price": "15000000", "total_balance": "0.3", "total_value": "4500000",
            "accounts": [],
        },
    )
    detail = client.get("/api/crypto-summary/asset/BTC").json()
    summary = client.get("/api/summary").json()
    row = next(h for h in summary["holdings"] if h["origin"] == "crypto_summary")
    assert detail["day_change"] == row["day_change"] == "300000"
    assert detail["day_change_pct"] == "7.14"


def test_cs_asset_detail_falls_back_to_history_for_old_cs(client, monkeypatch):
    _patch_cs_summary(monkeypatch, _cs_payload())   # prev_* なし
    _patch_cs_history_by_scope(monkeypatch, {"asset:BTC": _yesterday_point("4200000")})
    monkeypatch.setattr(
        web_app, "fetch_cs_asset_accounts",
        lambda asset, currency, user_sub, warn=None: {
            "price": "15000000", "total_balance": "0.3", "total_value": "4500000",
            "accounts": [],
        },
    )
    assert client.get("/api/crypto-summary/asset/BTC").json()["day_change"] == "300000"
