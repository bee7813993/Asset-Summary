"""投信自動連携（fund_autolink）のテスト。

銘柄名はすべて架空、または一般に流通する公募投信の商品名パターンを模した合成。
ネットワークは一切叩かない（検索・NAV取得はフェイク関数を注入）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from asset_summary.core import fund_autolink as fal
from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.core.providers import toushin as ts
from asset_summary.core.providers.base import HistoryResult
from asset_summary.core.store import Store

D = Decimal


# ----------------------------------------------------------------------
# 名前の正規化・クエリ生成
# ----------------------------------------------------------------------

def test_clean_name_strips_broker_code_and_alias_parens():
    assert fal.clean_name("テスト全世界株オール(8782)") == "テスト全世界株オール"
    # 別名の重複括弧（本体と先頭を共有する末尾括弧）を除去
    assert (
        fal.clean_name("架空・プラチナ・ファンド(為替ヘッジなし)(架空・プラチナ(為替ヘッジなし))")
        == "架空・プラチナ・ファンド(為替ヘッジなし)"
    )


def test_clean_name_strips_angle_segments():
    assert (
        fal.clean_name("架空NASDAQ100インデックスファンド<購入・換金手数料なし>")
        == "架空NASDAQ100インデックスファンド"
    )


def test_build_queries_splits_ascii_cjk_boundary():
    qs = fal.build_queries("eMAXIS Slim米国株式(S&P500)")
    # 半角スペース区切りのANDクエリ（括弧内は初回クエリから除外）
    assert qs[0] == "eMAXIS Slim 米国株式"
    assert any(q == "eMAXIS Slim" for q in qs)


def test_build_queries_nakaguro_tokens():
    qs = fal.build_queries("野村インデックスファンド・TOPIX")
    assert qs[0] == "野村インデックスファンド TOPIX"


# ----------------------------------------------------------------------
# 類似度
# ----------------------------------------------------------------------

def test_name_score_fullwidth_and_spacing_insensitive():
    s = fal.name_score("eMAXIS Slim米国株式(S&P500)", "eMAXIS Slim 米国株式(S&P500)")
    assert s > 0.95


def test_name_score_angle_prefix_vs_suffix():
    # 正式名は接頭・MF名は接尾に <手数料なし> が付くパターン
    s = fal.name_score(
        "架空NASDAQ100インデックスファンド<購入・換金手数料なし>",
        "<購入・換金手数料なし>架空NASDAQ100インデックスファンド",
    )
    assert s > 0.95


def test_name_score_penalizes_hedge_mismatch():
    s = fal.name_score("架空ゴールド(為替ヘッジなし)", "架空ゴールド(為替ヘッジあり)")
    assert s < 0.4


# ----------------------------------------------------------------------
# 基準価額照合
# ----------------------------------------------------------------------

def test_verify_nav_matches_within_window():
    prices = {date(2026, 8, 1): D("42902"), date(2026, 7, 31): D("42500")}
    ok, matched, latest = fal.verify_nav(prices, D("42902"), date(2026, 8, 4))
    assert ok is True
    assert matched == date(2026, 8, 1)
    assert latest == D("42902")


def test_verify_nav_rejects_outside_window_or_mismatch():
    prices = {date(2026, 7, 20): D("42902")}  # 窓の外
    ok, _, _ = fal.verify_nav(prices, D("42902"), date(2026, 8, 4))
    assert ok is False
    ok2, _, _ = fal.verify_nav({date(2026, 8, 3): D("99999")}, D("42902"), date(2026, 8, 4))
    assert ok2 is False


# ----------------------------------------------------------------------
# suggest_links 一巡
# ----------------------------------------------------------------------

def _seed_fund(store: Store, name: str, reported: str | None = "42902") -> int:
    from asset_summary.importers.base import make_name_key

    sec_id = store.create_security(
        Security(
            name=name,
            name_key=make_name_key(name),
            asset_class=AssetClass.FUND_JP,
            unit=Unit.KUCHI,
            price_unit_divisor=10000,
            price_source_status=PriceSourceStatus.UNLINKED,
        )
    )
    if reported is not None:
        acct = store.get_or_create_account("テスト証券")
        store.upsert_snapshot(
            HoldingSnapshot(
                account_id=acct.id,
                security_id=sec_id,
                as_of_date=date(2026, 8, 4),
                quantity=D("10000"),
                reported_price=D(reported),
                origin="mf",
            )
        )
    return sec_id


def _fake_search(results):
    def search(query, warn=None, **kw):
        return results
    return search


def _fake_history(nav_by_ref):
    def fetch(ref, warn=None, **kw):
        return HistoryResult(nav_by_ref.get(ref, {}), "JPY", [])
    return fetch


def test_suggest_auto_when_nav_disambiguates_hedge_variants(store: Store):
    """名前がほぼ同じヘッジあり/なしの2候補を基準価額で自動判別できること。"""
    sec_id = _seed_fund(store, "架空ゴールドファンド(為替ヘッジなし)")
    results = [
        {"name": "架空ゴールドファンド(為替ヘッジなし)", "ref": "JP1:001",
         "company": "架空アセット", "category": "4"},
        {"name": "架空ゴールドファンド(為替ヘッジあり)", "ref": "JP2:002",
         "company": "架空アセット", "category": "4"},
    ]
    navs = {
        "JP1:001": {date(2026, 8, 3): D("42902")},
        "JP2:002": {date(2026, 8, 3): D("31111")},
    }
    out = fal.suggest_links(
        store, search=_fake_search(results), fetch_history=_fake_history(navs)
    )
    assert len(out) == 1
    s = out[0]
    assert s["security_id"] == sec_id
    assert s["status"] == "auto"
    assert s["best_ref"] == "JP1:001"
    assert s["candidates"][0]["nav_match"] is True
    assert s["candidates"][0]["reported_price"] == "42902"


def test_suggest_candidates_when_no_nav_match(store: Store):
    _seed_fund(store, "架空バランスファンド")
    results = [
        {"name": "架空バランスファンド(8資産均等型)", "ref": "JP1:001",
         "company": "架空アセット", "category": "4"},
    ]
    navs = {"JP1:001": {date(2026, 8, 3): D("11111")}}  # 不一致
    out = fal.suggest_links(
        store, search=_fake_search(results), fetch_history=_fake_history(navs)
    )
    assert out[0]["status"] == "candidates"
    assert out[0]["best_ref"] is None
    assert out[0]["candidates"][0]["nav_match"] is False


def test_suggest_never_auto_without_reported_price(store: Store):
    """記載基準価額が無い（手動登録の）投信は名前が完全一致でも自動確定しない。"""
    _seed_fund(store, "架空インデックスファンド", reported=None)
    results = [
        {"name": "架空インデックスファンド", "ref": "JP1:001",
         "company": "架空アセット", "category": "4"},
    ]
    out = fal.suggest_links(
        store, search=_fake_search(results), fetch_history=_fake_history({})
    )
    assert out[0]["status"] == "candidates"
    assert out[0]["candidates"][0]["nav_match"] is None


def test_suggest_none_when_no_results(store: Store):
    _seed_fund(store, "どこにも無いファンド")
    out = fal.suggest_links(
        store, search=_fake_search([]), fetch_history=_fake_history({})
    )
    assert out[0]["status"] == "none"
    assert out[0]["candidates"] == []


def test_suggest_skips_linked_and_non_funds(store: Store):
    from asset_summary.importers.base import make_name_key

    store.create_security(
        Security(name="株式銘柄", name_key="かぶ", asset_class=AssetClass.STOCK_JP,
                 code="9999", price_source_status=PriceSourceStatus.UNLINKED)
    )
    store.create_security(
        Security(name="連携済み投信", name_key=make_name_key("連携済み投信"),
                 asset_class=AssetClass.FUND_JP,
                 price_source_type=PriceSourceType.TOUSHIN,
                 price_source_ref="JP0:000",
                 price_source_status=PriceSourceStatus.LINKED)
    )
    out = fal.suggest_links(
        store, search=_fake_search([]), fetch_history=_fake_history({})
    )
    assert out == []


# ----------------------------------------------------------------------
# API（apply）
# ----------------------------------------------------------------------

def test_apply_endpoint_links_and_fetches_history(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import asset_summary.web.app as web_app

    app = web_app.create_app(str(tmp_path / "t.db"))
    client = TestClient(app)
    store: Store = app.state.store

    sec_id = _seed_fund(store, "架空ファンド")
    called: dict = {}

    def fake_ensure(store_, secs, start, end, warn=None):
        called["refs"] = [s.price_source_ref for s in secs]

    monkeypatch.setattr(web_app, "ensure_price_history", fake_ensure)
    r = client.post(
        "/api/fund-links/apply",
        json={"links": [{"security_id": sec_id, "ref": "JP1:001"}]},
    )
    assert r.status_code == 200
    assert r.json()["linked"] == 1
    sec = store.get_security(sec_id)
    assert sec.price_source_status == PriceSourceStatus.LINKED
    assert sec.price_source_ref == "JP1:001"
    assert called["refs"] == ["JP1:001"]


def test_apply_endpoint_validates_input(tmp_path):
    from fastapi.testclient import TestClient

    import asset_summary.web.app as web_app

    client = TestClient(web_app.create_app(str(tmp_path / "t.db")))
    assert client.post("/api/fund-links/apply", json={}).status_code == 400
    assert (
        client.post(
            "/api/fund-links/apply",
            json={"links": [{"security_id": 999, "ref": "JP1:001"}]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/fund-links/apply",
            json={"links": [{"security_id": "abc", "ref": "JP1:001"}]},
        ).status_code
        == 400
    )


def test_suggest_endpoint_serializes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import asset_summary.web.app as web_app
    from asset_summary.core import fund_autolink

    app = web_app.create_app(str(tmp_path / "t.db"))
    client = TestClient(app)

    def fake_suggest(store, warn=None, **kw):
        return [{"security_id": 1, "name": "架空", "status": "none",
                 "best_ref": None, "candidates": []}]

    monkeypatch.setattr(fund_autolink, "suggest_links", fake_suggest)
    r = client.post("/api/fund-links/suggest")
    assert r.status_code == 200
    body = r.json()
    assert body["suggestions"][0]["status"] == "none"
    assert "warnings" in body


# ----------------------------------------------------------------------
# dedupe_same_fund（同一ファンドに連携された重複銘柄の自動統合）
# ----------------------------------------------------------------------

def _seed_linked_fund(
    store: Store, name: str, ref: str, *,
    account: str = "テスト証券", quantity: str | None = "10000",
    inactive: bool = False,
) -> int:
    from asset_summary.importers.base import make_name_key

    sec_id = store.create_security(
        Security(
            name=name,
            name_key=make_name_key(name),
            asset_class=AssetClass.FUND_JP,
            unit=Unit.KUCHI,
            price_unit_divisor=10000,
            price_source_type=PriceSourceType.TOUSHIN,
            price_source_ref=ref,
            price_source_status=PriceSourceStatus.LINKED,
            inactive=inactive,
        )
    )
    if quantity is not None:
        acct = store.get_or_create_account(account)
        store.upsert_snapshot(
            HoldingSnapshot(
                account_id=acct.id,
                security_id=sec_id,
                as_of_date=date(2026, 8, 4),
                quantity=D(quantity),
                origin="mf",
            )
        )
    return sec_id


def test_dedupe_merges_same_ref_and_prefers_clean_name(store: Store):
    """ref（ISIN:協会コード）が同じ2銘柄は自動統合。証券会社コード付きの
    切り詰め名より素の名前が生き残る（保有数が多くても）。"""
    clean = _seed_linked_fund(
        store, "架空・全世界株式(オールカントリー)", "JP1:001", quantity="100000"
    )
    dirty = _seed_linked_fund(
        store, "架空・全世界株オール(8782)", "JP1:001",
        account="別の証券", quantity="900000",
    )
    out = fal.dedupe_same_fund(store)
    assert len(out) == 1
    assert out[0]["target_id"] == clean
    assert out[0]["merged_ids"] == [dirty]
    assert out[0]["merged_names"] == ["架空・全世界株オール(8782)"]
    assert store.get_security(dirty) is None
    # 旧名は alias として学習される（次回取込から自動で当たる）
    from asset_summary.importers.base import make_name_key

    assert store.resolve_security(
        name_key=make_name_key("架空・全世界株オール(8782)")
    ) == clean
    # 保有は1銘柄に集まる
    assert {l.security_id for l in store.current_holdings()} == {clean}


def test_dedupe_prefers_active_over_inactive(store: Store):
    """売却済み登録（inactive）の殻より、現役の銘柄が生き残る。
    inactive を残すと価格取得が止まるため、名前の綺麗さより優先する。"""
    shell = _seed_linked_fund(
        store, "架空・全世界株式(オールカントリー)", "JP1:001",
        quantity=None, inactive=True,
    )
    live = _seed_linked_fund(store, "架空・全世界株オール(8782)", "JP1:001")
    out = fal.dedupe_same_fund(store)
    assert out[0]["target_id"] == live
    assert store.get_security(shell) is None
    assert store.get_security(live).inactive is False


def test_dedupe_skips_incompatible_pair_with_warning(store: Store):
    """属性が食い違う組（単位違い等）は壊さずに残し、警告だけ流す。"""
    a = _seed_linked_fund(store, "架空ファンドA", "JP1:001")
    from asset_summary.importers.base import make_name_key

    b = store.create_security(
        Security(
            name="架空ファンドA(口数単位違い)",
            name_key=make_name_key("架空ファンドA(口数単位違い)"),
            asset_class=AssetClass.FUND_JP,
            unit=Unit.KUCHI,
            price_unit_divisor=1,  # 1口あたり基準価額の銘柄は統合できない
            price_source_type=PriceSourceType.TOUSHIN,
            price_source_ref="JP1:001",
            price_source_status=PriceSourceStatus.LINKED,
        )
    )
    warnings: list[str] = []
    out = fal.dedupe_same_fund(store, warn=warnings.append)
    assert out == []
    assert store.get_security(a) is not None
    assert store.get_security(b) is not None
    assert any("自動統合できませんでした" in w for w in warnings)


def test_dedupe_noop_without_duplicates(store: Store):
    _seed_linked_fund(store, "架空ファンドA", "JP1:001")
    _seed_linked_fund(store, "架空ファンドB", "JP2:002")
    assert fal.dedupe_same_fund(store) == []


def test_put_link_triggers_auto_merge(tmp_path, monkeypatch):
    """設定画面から手動で連携した瞬間に、同じファンドの既存銘柄と統合される。
    連携した銘柄自身が統合で消えても、価格履歴は統合先に対して取得される。"""
    from fastapi.testclient import TestClient

    import asset_summary.web.app as web_app

    app = web_app.create_app(str(tmp_path / "t.db"))
    client = TestClient(app)
    store: Store = app.state.store

    survivor = _seed_linked_fund(
        store, "架空・全世界株式(オールカントリー)", "JP1:001"
    )
    newcomer = _seed_fund(store, "架空・全世界株オール(8782)")  # 未連携

    called: dict = {}

    def fake_ensure(store_, secs, start, end, warn=None):
        called["ids"] = [s.id for s in secs]

    monkeypatch.setattr(web_app, "ensure_price_history", fake_ensure)
    r = client.put(
        f"/api/securities/{newcomer}",
        json={"price_source_type": "toushin", "price_source_ref": "JP1:001"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["merged"][0]["target_id"] == survivor
    assert body["merged"][0]["merged_ids"] == [newcomer]
    assert store.get_security(newcomer) is None
    assert called["ids"] == [survivor]  # 履歴取得は統合先に付け替わる


def test_apply_endpoint_auto_merges_duplicates(tmp_path, monkeypatch):
    """自動判定の適用で2銘柄が同じファンドに連携されたら、その場で1つになる。"""
    from fastapi.testclient import TestClient

    import asset_summary.web.app as web_app

    app = web_app.create_app(str(tmp_path / "t.db"))
    client = TestClient(app)
    store: Store = app.state.store

    a = _seed_fund(store, "架空・全世界株式(オールカントリー)")
    b = _seed_fund(store, "架空・全世界株オール(8782)")
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a_, **k: None)

    r = client.post(
        "/api/fund-links/apply",
        json={"links": [
            {"security_id": a, "ref": "JP1:001"},
            {"security_id": b, "ref": "JP1:001"},
        ]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] == 2
    assert body["merged"][0]["target_id"] == a
    assert body["merged"][0]["merged_names"] == ["架空・全世界株オール(8782)"]
    assert store.get_security(b) is None
    funds = store.list_securities(asset_class="fund_jp")
    assert [s.id for s in funds] == [a]


def test_startup_dedupes_already_linked_duplicates(tmp_path, monkeypatch):
    """両方とも連携済みの既存DBは、アプリ起動時（lifespan）に自動統合される。"""
    from fastapi.testclient import TestClient

    import asset_summary.web.app as web_app

    monkeypatch.setenv("AS_MF_INBOX_DIR", "off")  # 受信フォルダ監視は無効化
    app = web_app.create_app(str(tmp_path / "t.db"))
    store: Store = app.state.store
    keep = _seed_linked_fund(
        store, "架空・全世界株式(オールカントリー)", "JP1:001"
    )
    gone = _seed_linked_fund(
        store, "架空・全世界株オール(8782)", "JP1:001", account="別の証券"
    )
    with TestClient(app):  # with で lifespan（startup/shutdown）が走る
        pass
    assert store.get_security(gone) is None
    assert {l.security_id for l in store.current_holdings()} == {keep}


# ----------------------------------------------------------------------
# 取込直後の自動連携（新規投信を ISIN まで辿って既存銘柄へ寄せる）
# ----------------------------------------------------------------------


def _unlinked_fund(store, name, reported=None, as_of=date(2026, 9, 2)):
    acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    sid = store.create_security(Security(
        name=name, name_key=name, asset_class=AssetClass.FUND_JP, unit=Unit.KUCHI,
        price_unit_divisor=10000, price_source_status=PriceSourceStatus.UNLINKED))
    store.upsert_snapshot(HoldingSnapshot(
        account_id=acct.id, security_id=sid, lot_seq=0, as_of_date=as_of,
        quantity=D("1000"), avg_cost=D("10000"), reported_price=reported, origin="mf"))
    return sid


def test_suggest_reports_why_it_could_not_link(store: Store):
    """連携できなかった理由を、協会に届かなかったのか該当が無いのかで分ける。"""
    sid = _unlinked_fund(store, "架空グローバル株式ファンド", reported=D("12345"))
    hit = [{"name": "架空グローバル株式ファンド", "ref": "JP1:1", "company": "架空投信"}]

    def run(search, history):
        out = fal.suggest_links(store, search=search, fetch_history=history,
                                security_ids=[sid])
        return out[0]["status"], out[0]["reason"]

    # 協会へ届かない
    assert run(lambda q, warn=None: ts.SearchResult(reachable=False),
               lambda r, warn=None: HistoryResult(reachable=False)) == (
        "unavailable", "search_unreachable")
    # 届いたが該当なし
    assert run(lambda q, warn=None: ts.SearchResult(items=[]),
               lambda r, warn=None: HistoryResult()) == ("none", "not_found")
    # 候補はあるが基準価額が取れない
    assert run(lambda q, warn=None: ts.SearchResult(items=hit),
               lambda r, warn=None: HistoryResult(reachable=False)) == (
        "candidates", "nav_unavailable")
    # 基準価額が一致 → 自動確定
    assert run(lambda q, warn=None: ts.SearchResult(items=hit),
               lambda r, warn=None: HistoryResult(
                   prices={date(2026, 9, 2): D("12345")})) == ("auto", "nav_matched")


def test_search_distinguishes_unreachable_from_empty(monkeypatch):
    """provider 層で「届かない」と「該当なし」を区別する。"""
    monkeypatch.setattr(ts.base, "request", lambda *a, **k: None)
    assert ts.search_funds("q").reachable is False
    assert ts.search("q") == []          # 既存の呼び出し方は変わらない

    class _Resp:
        def json(self):
            return {"searchResultInfo": {"resultInfoMapList": []}}

    monkeypatch.setattr(ts.base, "request", lambda *a, **k: _Resp())
    got = ts.search_funds("q")
    assert got.reachable is True and got.items == []


def test_import_autolinks_new_fund_and_merges_into_existing(store: Store, monkeypatch):
    """取込で表記揺れの新銘柄ができても、ISIN まで辿って既存銘柄へ統合する。"""
    from asset_summary.web import app as web_app

    # 既存: 連携済みの正しい銘柄
    acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    keep = store.create_security(Security(
        name="架空グローバル株式ファンド(為替ヘッジなし)", name_key="既存",
        asset_class=AssetClass.FUND_JP, unit=Unit.KUCHI, price_unit_divisor=10000,
        price_source_type=PriceSourceType.TOUSHIN, price_source_ref="JP1:1",
        price_source_status=PriceSourceStatus.LINKED))
    store.upsert_snapshot(HoldingSnapshot(
        account_id=acct.id, security_id=keep, lot_seq=0, as_of_date=date(2026, 9, 1),
        quantity=D("1000"), avg_cost=D("10000"), origin="mf"))
    # 取込で生まれた表記揺れの新銘柄（未連携）
    dup = _unlinked_fund(store, "架空グローバル株式ファンド(為替ヘッジな", reported=D("12345"))

    monkeypatch.setattr(ts, "search_funds", lambda q, warn=None: ts.SearchResult(
        items=[{"name": "架空グローバル株式ファンド(為替ヘッジなし)", "ref": "JP1:1",
                "company": "架空投信"}]))
    monkeypatch.setattr(ts, "fetch_history", lambda r, warn=None: HistoryResult(
        prices={date(2026, 9, 2): D("12345")}))
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)

    warnings: list[str] = []
    out = web_app._autolink_new_funds(store, [dup], warnings)

    assert out["attempted"] == 1
    assert [x["security_id"] for x in out["linked"]] == [dup]
    assert out["unresolved"] == []
    # ISIN が同じなので既存銘柄へ統合され、重複は消える
    assert store.get_security(dup) is None
    assert store.get_security(keep) is not None


def test_import_autolink_records_reason_when_it_cannot_link(store: Store, monkeypatch):
    """連携できなかったときは理由を添えて返す（協会へ届かないケース）。"""
    from asset_summary.web import app as web_app

    dup = _unlinked_fund(store, "架空未知ファンド", reported=D("12345"))
    monkeypatch.setattr(ts, "search_funds",
                        lambda q, warn=None: ts.SearchResult(reachable=False))
    monkeypatch.setattr(ts, "fetch_history",
                        lambda r, warn=None: HistoryResult(reachable=False))

    warnings: list[str] = []
    out = web_app._autolink_new_funds(store, [dup], warnings)
    assert out["linked"] == []
    assert out["unresolved"] == [
        {"security_id": dup, "name": "架空未知ファンド",
         "status": "unavailable", "reason": "search_unreachable"}
    ]
    assert store.get_security(dup) is not None      # 銘柄は残す（手動で連携できる）


def test_import_autolink_skips_when_too_many_new_funds(store: Store, monkeypatch):
    """新規が多いときは取込を待たせない（設定ページへ誘導）。"""
    from asset_summary.web import app as web_app

    ids = [_unlinked_fund(store, f"架空ファンド{i}") for i in range(6)]
    called = []
    monkeypatch.setattr(ts, "search_funds",
                        lambda q, warn=None: called.append(q) or ts.SearchResult())
    warnings: list[str] = []
    out = web_app._autolink_new_funds(store, ids, warnings)
    assert out["skipped"] is True and out["linked"] == []
    assert called == []                              # 協会へ照会しない
    assert any("設定ページ" in w for w in warnings)
