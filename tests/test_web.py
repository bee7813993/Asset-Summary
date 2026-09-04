"""Web API (web/app.py) のテスト。

価格・取込関数はモジュール名前空間経由で参照されるため、
monkeypatch.setattr(web_app, "fetch_spot", fake) で差し替える。
"""

from __future__ import annotations

import base64
import os
import tempfile

# モジュールレベルの app = create_app(...) がリポジトリの data/assets.db を
# 作らないよう、import 前に一時パスへ向ける。
os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import asset_summary.web.app as web_app
from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.importers.service import DuplicateImportError

D = Decimal


@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = web_app.create_app(str(tmp_path / "t.db"))
    # 既定の偽実装（各テストで必要に応じて上書き）
    monkeypatch.setattr(web_app, "fetch_spot", lambda store, secs, warn=None: {})
    monkeypatch.setattr(
        web_app,
        "fetch_fx_rates",
        lambda store, ccys, warn=None: {c: D("150") for c in ccys},
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
def real_spot(monkeypatch):
    """app フィクスチャが差し替えた fetch_spot を本物に戻す。

    手動評価は _LOCAL_SOURCES に入っていて daily_prices を引くだけなので、
    ネットワーク無しで現在値の解決（_manual_spot）をそのまま検証できる。
    """
    from asset_summary.core import prices as core_prices

    monkeypatch.setattr(web_app, "fetch_spot", core_prices.fetch_spot)


def _seed_stock(store, account="テスト証券", code="9999"):
    """株100株 @取得1000円 を保有する状態を作る。"""
    acct = store.get_or_create_account(account, kind="broker")
    sec_id = store.create_security(
        Security(
            name="テスト工業",
            name_key="てすとこうぎょう",
            code=code,
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
    return acct, sec_id


# ----------------------------------------------------------------------
# meta / root
# ----------------------------------------------------------------------


def test_meta(client):
    data = client.get("/api/meta").json()
    assert data["app"] == "asset-summary"
    assert data["version"]
    assert "JPY" in data["currencies"]
    stock = next(c for c in data["asset_classes"] if c["id"] == "stock_jp")
    assert stock["label_ja"] == "国内株式"
    assert stock["color"].startswith("#")
    assert data["settings"]["include_pension"] is True
    assert data["settings"]["default_currency"] == "JPY"


def test_root_serves_no_store(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


# ----------------------------------------------------------------------
# summary
# ----------------------------------------------------------------------


def test_summary_pl(client, store, monkeypatch):
    _, sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    data = client.get("/api/summary").json()
    assert data["currency"] == "JPY"
    assert data["total_value"] == "120000"
    assert data["total_cost"] == "100000"
    assert data["total_pl"] == "20000"
    assert data["total_pl_pct"] == "20.00"
    assert data["holding_count"] == 1
    assert data["priced_count"] == 1
    assert data["unpriced"] == []
    assert data["warnings"] == []
    assert data["generated_at"]
    h = data["holdings"][0]
    assert h["name"] == "テスト工業"
    assert h["account"] == "テスト証券"
    assert h["value"] == "120000"
    assert h["pl"] == "20000"
    assert h["has_price"] is True
    assert h["in_total"] is True
    c = data["classes"][0]
    assert c["class"] == "stock_jp"
    assert c["label"] == "国内株式"
    assert c["weight"] == "100.00"


def test_summary_currency_fallback(client, store, monkeypatch):
    _, sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    # 未対応通貨 → JPY へフォールバック
    data = client.get("/api/summary", params={"currency": "xxx"}).json()
    assert data["currency"] == "JPY"
    assert data["total_value"] == "120000"
    # 小文字でも upper() され、fx=150 で表示通貨換算される
    data2 = client.get("/api/summary", params={"currency": "usd"}).json()
    assert data2["currency"] == "USD"
    assert D(data2["total_value"]) == D("800")  # 120000 / 150


def test_summary_survives_price_layer_failure(client, store, monkeypatch):
    """価格層が NotImplementedError でも 200 + warnings で応答する。"""
    _seed_stock(store)

    def boom(*a, **k):
        raise NotImplementedError("未実装")

    monkeypatch.setattr(web_app, "fetch_spot", boom)
    r = client.get("/api/summary")
    assert r.status_code == 200
    data = r.json()
    assert any("価格取得エラー" in w for w in data["warnings"])


def test_include_points_toggle(client, store, monkeypatch):
    _, sec_id = _seed_stock(store)
    point_id = store.create_security(
        Security(
            name="楽天ポイント",
            name_key="らくてんぽいんと",
            asset_class=AssetClass.POINT,
            unit=Unit.POINT,
            price_source_status=PriceSourceStatus.NOT_REQUIRED,
        )
    )
    acct = store.get_or_create_account("楽天", kind="point")
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=acct.id,
            security_id=point_id,
            as_of_date=date(2026, 8, 1),
            quantity=D("2256"),
            avg_cost=None,
            reported_value_jpy=D("2256"),
        )
    )
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    assert client.get("/api/summary").json()["total_value"] == "122256"
    # トグル OFF → 合計から除外（行は in_total=False で残る）
    assert client.put("/api/settings", json={"include_points": False}).json()["ok"] is True
    data = client.get("/api/summary").json()
    assert data["total_value"] == "120000"
    point_row = next(h for h in data["holdings"] if h["asset_class"] == "point")
    assert point_row["in_total"] is False
    assert client.get("/api/meta").json()["settings"]["include_points"] is False
    # 戻すと復帰（可逆）
    client.put("/api/settings", json={"include_points": True})
    assert client.get("/api/summary").json()["total_value"] == "122256"


# ----------------------------------------------------------------------
# accounts / classes
# ----------------------------------------------------------------------


def test_accounts_aggregate_and_rename(client, store, monkeypatch):
    acct, sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    rows = client.get("/api/accounts").json()["accounts"]
    row = next(r for r in rows if r["id"] == acct.id)
    assert row["value"] == "120000"
    assert row["pl"] == "20000"
    assert row["pl_pct"] == "20.00"
    assert row["holding_count"] == 1
    # 表示名変更
    assert (
        client.put(f"/api/accounts/{acct.id}", json={"display_name": "メイン証券"}).json()["ok"]
        is True
    )
    rows2 = client.get("/api/accounts").json()["accounts"]
    assert next(r for r in rows2 if r["id"] == acct.id)["display_name"] == "メイン証券"
    # account-holdings は表示名で引く
    ah = client.get("/api/account-holdings", params={"account": "メイン証券"}).json()
    assert ah["account"] == "メイン証券"
    assert len(ah["holdings"]) == 1
    assert ah["total_value"] == "120000"


def test_class_holdings(client, store, monkeypatch):
    _, sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    data = client.get("/api/class-holdings", params={"class": "stock_jp"}).json()
    assert data["class"] == "stock_jp"
    assert data["label"] == "国内株式"
    assert data["total_value"] == "120000"
    assert len(data["holdings"]) == 1
    assert client.get("/api/class-holdings", params={"class": "bogus"}).status_code == 400


# ----------------------------------------------------------------------
# securities / holdings CRUD
# ----------------------------------------------------------------------


def test_securities_create_defaults_and_holdings_crud(client):
    # fund_jp は unit=kuchi / divisor=10000 が既定補完される
    r = client.post(
        "/api/securities", json={"name": "テストファンド（1234）", "asset_class": "fund_jp"}
    ).json()
    assert r["ok"] is True
    sec_id = r["id"]
    listed = client.get("/api/securities", params={"class": "fund_jp"}).json()["securities"]
    sec = next(s for s in listed if s["id"] == sec_id)
    assert sec["unit"] == "kuchi"
    assert sec["price_unit_divisor"] == 10000
    assert sec["price_source_status"] == "unlinked"
    # 保有登録（口座は名前指定で自動作成、as_of 既定=今日）
    r2 = client.post(
        "/api/holdings",
        json={
            "security_id": sec_id,
            "account_name": "手動口座",
            "quantity": "50000",
            "avg_cost": "40000",
        },
    ).json()
    assert r2["ok"] is True
    rows = client.get("/api/holdings").json()["holdings"]
    assert len(rows) == 1
    row = rows[0]
    assert row["account"] == "手動口座"
    assert row["quantity"] == "50000"
    assert row["avg_cost"] == "40000"
    assert row["origin"] == "manual"
    assert row["as_of"] == date.today().isoformat()
    # フィルタ
    assert (
        client.get("/api/holdings", params={"security_id": sec_id}).json()["holdings"]
    )
    assert (
        client.get("/api/holdings", params={"security_id": sec_id + 999}).json()["holdings"]
        == []
    )
    # 削除
    assert client.delete(f"/api/holdings/{row['id']}").json()["ok"] is True
    assert client.get("/api/holdings").json()["holdings"] == []
    assert client.delete(f"/api/holdings/{row['id']}").status_code == 404


def test_security_update_sets_linked_status(client):
    sec_id = client.post(
        "/api/securities", json={"name": "リンク投信", "asset_class": "fund_jp"}
    ).json()["id"]
    r = client.put(
        f"/api/securities/{sec_id}",
        json={"price_source_type": "toushin", "price_source_ref": "JP90C000XXXX:0331"},
    )
    assert r.json()["ok"] is True
    sec = next(
        s
        for s in client.get("/api/securities").json()["securities"]
        if s["id"] == sec_id
    )
    assert sec["price_source_status"] == "linked"
    assert sec["price_source_ref"] == "JP90C000XXXX:0331"


def test_security_delete_conflict_409(client, store):
    _, sec_id = _seed_stock(store)
    assert client.delete(f"/api/securities/{sec_id}").status_code == 409
    # スナップショットが無い銘柄は削除できる
    free_id = client.post(
        "/api/securities", json={"name": "未使用銘柄", "asset_class": "other"}
    ).json()["id"]
    assert client.delete(f"/api/securities/{free_id}").json()["ok"] is True
    assert client.delete(f"/api/securities/{free_id}").status_code == 404


# ----------------------------------------------------------------------
# manual prices
# ----------------------------------------------------------------------


def test_manual_price_roundtrip(client):
    sec_id = client.post(
        "/api/securities", json={"name": "自宅マンション", "asset_class": "real_estate"}
    ).json()["id"]
    r = client.post(
        f"/api/securities/{sec_id}/manual-price",
        json={"date": "2026-08-01", "value": "30000000"},
    )
    assert r.json()["ok"] is True
    prices = client.get(f"/api/securities/{sec_id}/manual-prices").json()["prices"]
    assert prices == [{"date": "2026-08-01", "value": "30000000"}]
    assert (
        client.delete(f"/api/securities/{sec_id}/manual-prices/2026-08-01").json()["ok"]
        is True
    )
    assert client.get(f"/api/securities/{sec_id}/manual-prices").json()["prices"] == []
    # 不正な日付は 400
    assert (
        client.post(
            f"/api/securities/{sec_id}/manual-price",
            json={"date": "08/01", "value": "1"},
        ).status_code
        == 400
    )


# ----------------------------------------------------------------------
# portfolio history
# ----------------------------------------------------------------------


def test_portfolio_history_scopes(client, store):
    # 証券A: 株100株（manual価格1000円）、証券B: 金10g（manual価格500円/g）
    acct_a, stock_id = _seed_stock(store, account="証券A")
    acct_b = store.get_or_create_account("証券B", kind="manual")
    metal_id = store.create_security(
        Security(
            name="金地金",
            name_key="きんじがね",
            asset_class=AssetClass.METAL,
            unit=Unit.GRAM,
        )
    )
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=acct_b.id,
            security_id=metal_id,
            as_of_date=date(2026, 8, 1),
            quantity=D("10"),
            avg_cost=D("400"),
        )
    )
    client.post(
        f"/api/securities/{stock_id}/manual-price",
        json={"date": "2026-08-01", "value": "1000"},
    )
    client.post(
        f"/api/securities/{metal_id}/manual-price",
        json={"date": "2026-08-01", "value": "500"},
    )

    data = client.get("/api/portfolio-history", params={"range": "7d"}).json()
    assert data["currency"] == "JPY"
    assert data["range"] == "7d"
    assert data["scope"] == "total"
    assert len(data["points"]) == 8  # start..end 両端含む
    assert data["points"][-1]["value"] == "105000"  # 100*1000 + 10*500
    assert data["is_partial"] is False
    assert data["unpriced"] == []

    by_scope = {}
    for scope in ("class:metal", f"security:{stock_id}", "account:証券B"):
        d = client.get(
            "/api/portfolio-history", params={"range": "7d", "scope": scope}
        ).json()
        assert d["scope"] == scope
        by_scope[scope] = d["points"][-1]["value"]
    assert by_scope["class:metal"] == "5000"
    assert by_scope[f"security:{stock_id}"] == "100000"
    assert by_scope["account:証券B"] == "5000"

    assert client.get(
        "/api/portfolio-history", params={"scope": "bogus"}
    ).status_code == 400


def test_portfolio_history_unpriced_partial(client, store):
    _seed_stock(store)  # 価格系列なし
    data = client.get("/api/portfolio-history", params={"range": "7d"}).json()
    assert data["is_partial"] is True
    assert data["unpriced"] == ["テスト工業"]


# ----------------------------------------------------------------------
# 不動産の推移
# ----------------------------------------------------------------------


def _seed_estate(store, ref=None, cost="45000000"):
    """不動産1件（取得価額のみ、査定額はまだ無い）を作る。"""
    acct = store.get_or_create_account("不動産", kind="manual")
    sec_id = store.create_security(
        Security(
            name="自宅マンション",
            name_key="じたくまんしょん",
            asset_class=AssetClass.REAL_ESTATE,
            unit=Unit.UNIT,
            price_source_type=PriceSourceType.MANUAL,
            price_source_status=PriceSourceStatus.MANUAL,
            price_source_ref=ref,
        )
    )
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=acct.id,
            security_id=sec_id,
            as_of_date=date(2026, 8, 1),
            quantity=D("1"),
            avg_cost=D(cost),
        )
    )
    return acct, sec_id


def test_estate_without_valuation_is_not_reported_as_loading(client, store):
    """査定額待ちは「⏳ 取得中」ではない。待っても出ないので入力を促す側に出す。"""
    _seed_estate(store)
    data = client.get("/api/portfolio-history", params={"range": "7d"}).json()
    assert data["unpriced"] == []
    assert data["needs_valuation"] == ["自宅マンション"]
    assert data["is_partial"] is False


def test_estate_without_valuation_is_excluded_not_zero(client, store):
    """4,500万円の架空の損失を出さない（value は 0 ではなく不明）。"""
    _seed_estate(store)
    summary = client.get("/api/summary").json()
    row = [h for h in summary["holdings"] if h["name"] == "自宅マンション"][0]
    assert row["value"] is None


def test_estate_index_without_valuation_still_has_no_value(client, store):
    """指数は水準であって価格ではない。査定額が無ければ評価額も出ない。"""
    _, sec_id = _seed_estate(store, ref="re_index:nanto:condo")
    store.upsert_daily_price("re_index", "nanto:condo", "2026-01-01", D("100"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-07-01", D("110"))

    data = client.get(
        f"/api/security/{sec_id}", params={"range": "1y"}
    ).json()
    assert data["tiles"]["value"] is None  # 0 ではなく「不明」
    assert data["price_history"] == []

    hist = client.get("/api/portfolio-history", params={"range": "7d"}).json()
    assert hist["needs_valuation"] == ["自宅マンション"]


def test_linking_an_index_keeps_the_security_manual(client, store):
    """指数refを付けても status が linked へ飛ばないこと。

    api_security_update の status 追従は ref より先に type を見る2行の順序依存。
    飛ぶと ensure_price_history が manual 銘柄に走ってしまう。
    """
    _, sec_id = _seed_estate(store)
    r = client.put(
        f"/api/securities/{sec_id}",
        json={"price_source_ref": "re_index:nanto:condo"},
    )
    assert r.status_code == 200
    sec = store.get_security(sec_id)
    assert sec.price_source_ref == "re_index:nanto:condo"
    assert sec.price_source_status == PriceSourceStatus.MANUAL
    assert sec.price_source_type == PriceSourceType.MANUAL


def test_unlinking_an_index_clears_the_ref(client, store):
    """「連携しない」に戻せること。

    update_security は None を「変更しない」と解釈するため、明示的な null は
    clear 経由でないと落ちる。これが効かないと一度連携したら二度と外せない。
    """
    _, sec_id = _seed_estate(store, ref="re_index:nanto:condo")
    r = client.put(f"/api/securities/{sec_id}", json={"price_source_ref": None})
    assert r.status_code == 200
    sec = store.get_security(sec_id)
    assert sec.price_source_ref is None
    assert sec.price_source_status == PriceSourceStatus.MANUAL

    # 連携が外れたら指数で延長しない（最新査定額のまま）
    store.upsert_daily_price("manual", str(sec_id), "2026-01-01", D("50000000"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-01-01", D("100"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-06-01", D("110"))
    series, _ = store.price_series_for_security(sec, end="2026-06-01")
    assert set(series.values()) == {D("50000000")}


def test_estate_current_value_is_extended_and_flagged(client, store, real_spot):
    """最終査定日より後は指数で延長され、「目安」の印が付く。"""
    _, sec_id = _seed_estate(store, ref="re_index:nanto:condo")
    store.upsert_daily_price("manual", str(sec_id), "2026-01-01", D("50000000"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-01-01", D("100"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-06-01", D("110"))

    data = client.get(f"/api/security/{sec_id}", params={"range": "1y"}).json()
    # 指数の最終月(2026-06)以降は横ばいなので +10% で止まる
    assert data["tiles"]["value"] == "55000000"
    assert data["tiles"]["estimated"] is True
    assert data["tiles"]["day_change"] is None  # 目安に前日比は付けない


def test_estate_without_an_index_is_not_flagged_as_estimated(client, store, real_spot):
    _, sec_id = _seed_estate(store)
    store.upsert_daily_price("manual", str(sec_id), "2026-01-01", D("50000000"))
    data = client.get(f"/api/security/{sec_id}", params={"range": "1y"}).json()
    assert data["tiles"]["value"] == "50000000"
    assert data["tiles"]["estimated"] is False


def test_estate_valuations_become_a_daily_series(client, store):
    """疎な査定額が日次になり、階段ではなく線になる。"""
    _, sec_id = _seed_estate(store)
    for day, value in (("2026-07-02", "50000000"), ("2026-08-01", "53000000")):
        assert client.post(
            f"/api/securities/{sec_id}/manual-price",
            json={"date": day, "value": value},
        ).status_code == 200

    data = client.get(f"/api/security/{sec_id}", params={"range": "1y"}).json()
    hist = {p["t"]: p["price"] for p in data["price_history"]}
    assert hist["2026-07-02"] == "50000000"
    assert hist["2026-08-01"] == "53000000"
    # 2点の間が埋まっている（登録した日だけではない）
    assert "2026-07-17" in hist
    assert D(hist["2026-07-02"]) < D(hist["2026-07-17"]) < D(hist["2026-08-01"])


# ----------------------------------------------------------------------
# security detail
# ----------------------------------------------------------------------


def test_security_detail(client, store, monkeypatch):
    _, sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    client.post(
        f"/api/securities/{sec_id}/manual-price",
        json={"date": "2026-08-01", "value": "1150"},
    )
    data = client.get(f"/api/security/{sec_id}", params={"range": "all"}).json()
    assert data["security"]["name"] == "テスト工業"
    tiles = data["tiles"]
    assert tiles["quantity"] == "100"
    assert tiles["avg_cost"] == "1000"
    assert tiles["price"] == "1200"
    assert tiles["value"] == "120000"
    assert tiles["pl"] == "20000"
    assert tiles["pl_pct"] == "20.00"
    assert data["accounts"][0]["account"] == "テスト証券"
    assert data["lots"][0]["lot_seq"] == 0
    assert any(p["t"] == "2026-08-01" and p["price"] == "1150" for p in data["price_history"])
    assert client.get("/api/security/99999").status_code == 404


# ----------------------------------------------------------------------
# import flow
# ----------------------------------------------------------------------


def test_import_pdf_preview_and_commit(client, monkeypatch):
    fake_preview = {
        "batch_id": "b-1",
        "filename": "test.pdf",
        "suggested_as_of": "2026-08-01",
        "sections": [],
        "diff": [],
        "report": {},
        "warnings": [],
    }
    captured = {}

    def fake_build_preview(store, pdf_bytes, filename):
        captured["bytes"] = pdf_bytes
        captured["filename"] = filename
        return fake_preview

    monkeypatch.setattr(web_app, "build_preview", fake_build_preview)
    b64 = base64.b64encode(b"%PDF-1.4 fake").decode()
    r = client.post("/api/import/pdf", json={"filename": "test.pdf", "content_b64": b64})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["batch_id"] == "b-1"
    assert captured["bytes"] == b"%PDF-1.4 fake"
    assert captured["filename"] == "test.pdf"

    def fake_commit(store, batch_id, as_of_date, **kw):
        assert batch_id == "b-1"
        assert as_of_date == date(2026, 8, 1)
        assert kw["include_crypto"] is False
        return {"created": 3, "updated": 1, "zeroed": 2}

    monkeypatch.setattr(web_app, "commit_batch", fake_commit)
    r2 = client.post("/api/import/b-1/commit", json={"as_of": "2026-08-01"})
    assert r2.status_code == 200
    assert r2.json() == {
        "ok": True,
        "created": 3,
        "updated": 1,
        "zeroed": 2,
        "snapshot_date": "2026-08-01",
        # 新規銘柄が無いので自動連携は何もしない（投信協会へは照会しない）
        "autolink": {"attempted": 0, "linked": [], "unresolved": []},
        "warnings": [],
    }
    # as_of 不正は 400
    assert (
        client.post("/api/import/b-1/commit", json={"as_of": "not-a-date"}).status_code
        == 400
    )


def test_import_duplicate_pdf_409(client, monkeypatch):
    def dup(store, pdf_bytes, filename):
        raise DuplicateImportError("old-batch", as_of_date="2026-07-01")

    monkeypatch.setattr(web_app, "build_preview", dup)
    r = client.post(
        "/api/import/pdf",
        json={"filename": "t.pdf", "content_b64": base64.b64encode(b"x").decode()},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["existing_batch_id"] == "old-batch"
    assert "detail" in body


def test_import_history_and_batch_delete(client, store):
    from asset_summary.core.models import ImportBatch

    store.create_batch(ImportBatch(id="b-9", file_sha256="sha", status="committed"))
    imports = client.get("/api/import/history").json()["imports"]
    assert imports[0]["id"] == "b-9"
    r = client.delete("/api/import/batches/b-9").json()
    assert r["ok"] is True
    assert client.get("/api/import/history").json()["imports"] == []
    assert client.delete("/api/import/batches/b-9").status_code == 404


# ----------------------------------------------------------------------
# fund search / refresh / settings
# ----------------------------------------------------------------------


def test_fund_search(client, monkeypatch):
    results = [
        {
            "name": "テスト・インデックス",
            "isin": "JP90C000XXXX",
            "assoc_cd": "0331",
            "ref": "JP90C000XXXX:0331",
            "category": "国際株式",
        }
    ]
    monkeypatch.setattr(web_app, "search_funds", lambda q, warn=None: results)
    data = client.get("/api/fund-search", params={"q": "テスト"}).json()
    assert data["results"] == results
    assert data["warnings"] == []
    # 空クエリは検索せず空結果
    assert client.get("/api/fund-search").json()["results"] == []


def test_refresh_prices_clears_spot_cache(client, store):
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO spot_cache (source, source_id, price, currency, fetched_at)"
            " VALUES ('yahoo','9433.T','100','JPY',0)"
        )
    assert client.post("/api/refresh-prices").json()["ok"] is True
    with store.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM spot_cache").fetchone()["c"]
    assert n == 0


def test_settings_put(client):
    assert (
        client.put("/api/settings", json={"default_currency": "usd"}).json()["ok"] is True
    )
    meta = client.get("/api/meta").json()
    assert meta["settings"]["default_currency"] == "USD"
    # 既定通貨が summary の currency 解決にも効く
    assert client.get("/api/summary").json()["currency"] == "USD"
    # 未対応通貨は 400
    assert client.put("/api/settings", json={"default_currency": "XXX"}).status_code == 400
    # include_pension トグル
    client.put("/api/settings", json={"include_pension": False})
    assert client.get("/api/meta").json()["settings"]["include_pension"] is False


# ----------------------------------------------------------------------
# 前日比（day_change）
# ----------------------------------------------------------------------


def _seed_prev_close(store, ref="9999.T", days_ago=1, price="1000"):
    """前日終値を daily_prices に置く（前日比の基準）。"""
    from datetime import timedelta

    store.upsert_daily_price(
        "yahoo", ref, (date.today() - timedelta(days=days_ago)).isoformat(),
        D(price), currency="JPY",
    )


@pytest.fixture()
def priced_stock(store, monkeypatch):
    """前日 1000円 → 現在 1200円 の株を100株。前日比は +20,000円 (+20%)。"""
    acct, sec_id = _seed_stock(store)
    store.update_security(
        sec_id, price_source_type="yahoo", price_source_ref="9999.T",
        price_source_status=PriceSourceStatus.LINKED.value,
    )
    _seed_prev_close(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    return acct, sec_id


def test_summary_exposes_day_change(client, priced_stock):
    data = client.get("/api/summary").json()
    assert data["total_day_change"] == "20000"
    assert data["total_day_change_pct"] == "20.00"
    assert data["day_change_partial"] is False
    h = data["holdings"][0]
    assert h["day_change"] == "20000"
    assert h["day_change_pct"] == "20.00"
    assert h["day_change_as_of"]
    assert data["holdings_by_security"][0]["day_change"] == "20000"


def test_summary_day_change_follows_the_cash_balance(client, store):
    """現金は前日の残高との差が前日比になる（数量がそのまま金額のため）。"""
    from datetime import timedelta

    acct = store.get_or_create_account("テスト銀行", kind="bank")
    sec_id = store.create_security(
        Security(
            name="現金・預金",
            name_key="げんきん",
            asset_class=AssetClass.CASH,
            currency="JPY",
        )
    )
    for day, qty in (
        (date.today() - timedelta(days=1), "60000"),
        (date.today(), "50000"),
    ):
        store.upsert_snapshot(
            HoldingSnapshot(
                account_id=acct.id, security_id=sec_id, as_of_date=day,
                quantity=D(qty), avg_cost=None,
            )
        )

    data = client.get("/api/summary").json()
    assert data["total_value"] == "50000"
    assert data["total_day_change"] == "-10000"
    assert data["holdings"][0]["prev_value"] == "60000"


def _seed_cash_history(store, pairs):
    """(as_of, 残高) の並びで現金スナップショットを作る。"""
    acct = store.get_or_create_account("テスト銀行", kind="bank")
    sec_id = store.create_security(
        Security(
            name="現金・預金", name_key="げんきん",
            asset_class=AssetClass.CASH, currency="JPY",
        )
    )
    for day, qty in pairs:
        store.upsert_snapshot(
            HoldingSnapshot(
                account_id=acct.id, security_id=sec_id, as_of_date=day,
                quantity=D(qty), avg_cost=None,
            )
        )
    return acct, sec_id


def test_summary_day_change_survives_the_date_rollover(client, store):
    """今日まだ取り込んでいなくても、前回の取込で判った増減は出続ける。

    基準を「今日の前日」に固定すると、取込がもたらした変化は取込時刻から
    深夜0時までしか出ない（夜に取り込めば数時間で消える）。
    """
    from datetime import timedelta

    _seed_cash_history(store, [
        (date.today() - timedelta(days=2), "60000"),
        (date.today() - timedelta(days=1), "50000"),   # 昨日の取込が最新
    ])

    data = client.get("/api/summary").json()
    assert data["total_value"] == "50000"
    assert data["total_day_change"] == "-10000"   # 0 に戻らない
    assert data["holdings"][0]["prev_value"] == "60000"


def test_summary_day_change_goes_quiet_when_imports_stop(client, store):
    """取込が2日以上空いたら、古い変化を前日比として出し続けない。"""
    from datetime import timedelta

    _seed_cash_history(store, [
        (date.today() - timedelta(days=5), "60000"),
        (date.today() - timedelta(days=4), "50000"),
    ])

    data = client.get("/api/summary").json()
    assert data["total_value"] == "50000"
    assert data["total_day_change"] == "0"


def test_summary_without_history_reports_no_day_change(client, store, monkeypatch):
    """履歴が無くても 500 にならず、前日比だけ空になる。"""
    _, sec_id = _seed_stock(store)
    monkeypatch.setattr(
        web_app, "fetch_spot", lambda s, secs, warn=None: {sec_id: D("1200")}
    )
    data = client.get("/api/summary").json()
    assert data["total_value"] == "120000"
    assert data["total_day_change"] is None
    assert data["day_change_partial"] is False
    assert data["holdings"][0]["day_change"] is None


def test_classes_and_class_holdings_carry_day_change(client, priced_stock):
    cls = client.get("/api/classes").json()["classes"][0]
    assert cls["day_change"] == "20000"
    assert cls["day_change_pct"] == "20.00"
    assert cls["day_change_partial"] is False

    detail = client.get("/api/class-holdings?class=stock_jp").json()
    # 一覧のクラス行と詳細ヘッダの数字が一致すること
    assert detail["total_day_change"] == cls["day_change"]
    assert detail["total_value"] == "120000"
    assert detail["total_pl"] == "20000"
    assert detail["holdings"][0]["day_change"] == "20000"


def test_accounts_and_account_holdings_carry_day_change(client, priced_stock):
    acct = client.get("/api/accounts").json()["accounts"][0]
    assert acct["day_change"] == "20000"
    assert acct["pl"] == "20000"
    assert acct["weight"] == "100.00"

    detail = client.get("/api/account-holdings?account=テスト証券").json()
    assert detail["total_day_change"] == acct["day_change"]
    assert detail["total_pl"] == acct["pl"]


def test_security_detail_tiles_carry_day_change(client, priced_stock):
    _acct, sec_id = priced_stock
    tiles = client.get(f"/api/security/{sec_id}").json()["tiles"]
    assert tiles["day_change"] == "20000"
    assert tiles["day_change_pct"] == "20.00"
    assert tiles["day_change_as_of"]


def test_tag_summary_apportions_day_change(client, store, priced_stock):
    _acct, sec_id = priced_stock
    tag_id = client.post("/api/tags", json={"name": "国内株"}).json()["id"]
    client.put(
        f"/api/securities/{sec_id}/tags",
        json={"allocations": [{"tag_id": tag_id, "weight": "50"}]},
    )
    data = client.get("/api/tag-summary").json()
    tagged = next(r for r in data["by_tag"] if r["tag_id"] == tag_id)
    # 配分率50% → 評価額も前日比も半分
    assert tagged["value"] == "60000"
    assert tagged["day_change"] == "10000"
    assert tagged["day_change_pct"] == "20.00"


def test_tag_holdings_rows_have_the_same_fields_as_the_holdings_list(
    client, store, priced_stock
):
    """タグ詳細の構成銘柄を保有一覧と同じ表で描けること（列が揃っていること）。"""
    _acct, sec_id = priced_stock
    tag_id = client.post("/api/tags", json={"name": "国内株"}).json()["id"]
    client.put(
        f"/api/securities/{sec_id}/tags",
        json={"allocations": [{"tag_id": tag_id, "weight": "100"}]},
    )
    row = client.get(f"/api/tags/{tag_id}/holdings").json()["holdings"][0]
    for field in (
        "id", "name", "account", "quantity", "avg_cost", "price", "value",
        "day_change", "day_change_pct", "pl", "pl_pct",
        "portfolio_ratio", "portfolio_value",
    ):
        assert field in row, field
    assert row["day_change"] == "20000"
    # 行クリックで銘柄詳細へ飛べるように id が銘柄idであること
    assert row["id"] == sec_id


# ----------------------------------------------------------------------
# 推移グラフ: タグ / Myポートフォリオのスコープ
# ----------------------------------------------------------------------


def _history_value(client, scope):
    data = client.get(f"/api/portfolio-history?scope={scope}&range=7d").json()
    return data["points"][-1]["value"], data


def test_portfolio_history_tag_scope_applies_allocation_ratio(
    client, store, priced_stock
):
    _acct, sec_id = priced_stock
    tag_id = client.post("/api/tags", json={"name": "国内株"}).json()["id"]
    client.put(
        f"/api/securities/{sec_id}/tags",
        json={"allocations": [{"tag_id": tag_id, "weight": "40"}]},
    )
    total, _ = _history_value(client, "total")
    tagged, data = _history_value(client, f"tag:{tag_id}")
    assert data["scope"] == f"tag:{tag_id}"
    assert D(tagged) == D(total) * D("0.4")


def test_portfolio_history_portfolio_scope(client, store, priced_stock):
    _acct, sec_id = priced_stock
    tag_id = client.post("/api/tags", json={"name": "国内株"}).json()["id"]
    client.put(
        f"/api/securities/{sec_id}/tags",
        json={"allocations": [{"tag_id": tag_id, "weight": "100"}]},
    )
    pf_id = client.post(
        "/api/portfolios", json={"name": "株のみ", "tag_ids": [tag_id]}
    ).json()["id"]
    total, _ = _history_value(client, "total")
    scoped, _ = _history_value(client, f"portfolio:{pf_id}")
    assert D(scoped) == D(total)


def test_portfolio_history_unknown_scope_target_is_404(client):
    assert client.get("/api/portfolio-history?scope=tag:9999").status_code == 404
    assert client.get("/api/portfolio-history?scope=portfolio:9999").status_code == 404
    assert client.get("/api/portfolio-history?scope=bogus:1").status_code == 400


def test_portfolio_history_tag_scope_excludes_untagged_securities(
    client, store, priced_stock
):
    tag_id = client.post("/api/tags", json={"name": "空タグ"}).json()["id"]
    value, _ = _history_value(client, f"tag:{tag_id}")
    assert D(value) == D("0")
