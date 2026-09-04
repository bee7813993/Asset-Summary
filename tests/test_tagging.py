"""タグ配分と Myポートフォリオ集計のテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from asset_summary.core import tagging
from asset_summary.core.store import Store
from asset_summary.web.app import create_app

D = Decimal


def _holding(sec_id: int, value: str, **kw) -> dict:
    h = {
        "id": sec_id,
        "value": D(value),
        "cost": None,
        "costed_value": None,
        "in_total": True,
        "currency": "JPY",
        "asset_class": "fund_jp",
        "account": "テスト証券",
    }
    h.update(kw)
    return h


TAGS = [
    {"id": 1, "name": "世界株", "color": "#1"},
    {"id": 2, "name": "国内株式", "color": "#2"},
    {"id": 3, "name": "ゴールド", "color": "#3"},
]


# ----------------------------------------------------------------------
# 按分の中核
# ----------------------------------------------------------------------

def test_weighted_split_sums_to_total():
    """オルカン的な按分: 1銘柄を複数タグに分けても合計が総額と一致する。"""
    holdings = [_holding(10, "1000000")]
    tag_map = {10: {1: D("95"), 2: D("5")}}
    out = tagging.summarize_by_tag(holdings, tag_map, TAGS)
    by_name = {t["name"]: t["value"] for t in out["tags"]}
    assert by_name["世界株"] == D("950000")
    assert by_name["国内株式"] == D("50000")
    assert sum(t["value"] for t in out["tags"]) == D("1000000")
    assert out["total_value"] == D("1000000")


def test_untagged_remainder_is_bucketed():
    holdings = [_holding(10, "1000000")]
    tag_map = {10: {1: D("60")}}   # 40%未配分
    out = tagging.summarize_by_tag(holdings, tag_map, TAGS)
    by_name = {t["name"]: t["value"] for t in out["tags"]}
    assert by_name["世界株"] == D("600000")
    assert by_name["未分類"] == D("400000")


def test_completely_untagged_security():
    out = tagging.summarize_by_tag([_holding(10, "500")], {}, TAGS)
    assert out["tags"][0]["name"] == "未分類"
    assert out["tags"][0]["value"] == D("500")


def test_untagged_sorts_last():
    holdings = [_holding(10, "100"), _holding(11, "1000000")]
    tag_map = {10: {1: D("100")}}   # 11 は未分類（金額は大きい）
    out = tagging.summarize_by_tag(holdings, tag_map, TAGS)
    assert out["tags"][-1]["name"] == "未分類"


# ----------------------------------------------------------------------
# ポートフォリオの計上割合
# ----------------------------------------------------------------------

def _pf(**kw) -> dict:
    p = {"id": 1, "name": "P", "tag_ids": [], "include_security_ids": [],
         "exclude_security_ids": []}
    p.update(kw)
    return p


def test_ratio_tag_match_is_prorated():
    """タグ条件のポートフォリオは、一致したタグの配分率ぶんだけ計上する。"""
    weights = {1: D("95"), 2: D("5")}
    assert tagging.portfolio_ratio(10, weights, _pf(tag_ids=[2])) == D("0.05")
    assert tagging.portfolio_ratio(10, weights, _pf(tag_ids=[1])) == D("0.95")
    assert tagging.portfolio_ratio(10, weights, _pf(tag_ids=[1, 2])) == D("1")
    assert tagging.portfolio_ratio(10, weights, _pf(tag_ids=[3])) == D("0")


def test_ratio_explicit_include_is_full():
    weights = {1: D("95"), 2: D("5")}
    assert tagging.portfolio_ratio(10, weights, _pf(include_security_ids=[10])) == D("1")


def test_ratio_exclude_wins():
    weights = {1: D("100")}
    p = _pf(tag_ids=[1], include_security_ids=[10], exclude_security_ids=[10])
    assert tagging.portfolio_ratio(10, weights, p) == D("0")


def test_portfolio_prorated_total():
    """タグ[国内株式]のポートフォリオにオルカンの5%分だけが入ること。"""
    holdings = [_holding(10, "1000000"), _holding(11, "200000")]
    tag_map = {10: {1: D("95"), 2: D("5")}, 11: {2: D("100")}}
    out = tagging.summarize_by_tag(holdings, tag_map, TAGS, _pf(tag_ids=[2]))
    assert out["total_value"] == D("250000")     # 50,000 + 200,000
    by_name = {t["name"]: t["value"] for t in out["tags"]}
    assert by_name["国内株式"] == D("250000")
    assert "世界株" not in by_name                # 対象外タグは出さない


def test_portfolio_explicit_include_shows_all_its_tags():
    holdings = [_holding(10, "1000000")]
    tag_map = {10: {1: D("95"), 2: D("5")}}
    out = tagging.summarize_by_tag(
        holdings, tag_map, TAGS, _pf(tag_ids=[], include_security_ids=[10])
    )
    assert out["total_value"] == D("1000000")
    by_name = {t["name"]: t["value"] for t in out["tags"]}
    assert by_name["世界株"] == D("950000")
    assert by_name["国内株式"] == D("50000")


def test_portfolio_pl_is_prorated():
    holdings = [
        _holding(10, "1000000", cost=D("800000"), costed_value=D("1000000"))
    ]
    tag_map = {10: {1: D("50"), 2: D("50")}}
    out = tagging.summarize_by_tag(holdings, tag_map, TAGS, _pf(tag_ids=[1]))
    assert out["total_value"] == D("500000")
    assert out["total_cost"] == D("400000")
    assert out["total_pl"] == D("100000")


def test_unallocated_is_reported():
    """配分が100%未満の銘柄が、未配分額つきで報告されること。"""
    holdings = [
        _holding(10, "1000000"),                       # 60%だけ配分
        _holding(11, "300000"),                        # タグ未設定
        _holding(12, "500000"),                        # 完全配分
    ]
    tag_map = {10: {1: D("60")}, 12: {1: D("100")}}
    out = tagging.summarize_by_tag(holdings, tag_map, TAGS)
    un = {u["security_id"]: u for u in out["unallocated"]}
    assert set(un) == {10, 11}                          # 12は含まれない
    assert un[10]["allocated_pct"] == D("60")
    assert un[10]["unallocated_value"] == D("400000")
    assert un[11]["allocated_pct"] == D("0")
    assert un[11]["unallocated_value"] == D("300000")
    # 未配分額の大きい順
    assert [u["security_id"] for u in out["unallocated"]] == [10, 11]


def test_unallocated_merges_same_security_across_accounts():
    holdings = [_holding(10, "100000"), _holding(10, "200000")]
    out = tagging.summarize_by_tag(holdings, {10: {1: D("50")}}, TAGS)
    assert len(out["unallocated"]) == 1
    assert out["unallocated"][0]["unallocated_value"] == D("150000")


def test_excluded_class_holdings_skipped():
    holdings = [_holding(10, "100", in_total=False)]
    out = tagging.summarize_by_tag(holdings, {10: {1: D("100")}}, TAGS)
    assert out["total_value"] == D("0")


def test_group_by_uses_portfolio_value():
    holdings = [
        _holding(10, "1000000", currency="JPY"),
        _holding(11, "500000", currency="USD"),
    ]
    tag_map = {10: {1: D("50")}, 11: {1: D("100")}}
    out = tagging.summarize_by_tag(holdings, tag_map, TAGS, _pf(tag_ids=[1]))
    rows = tagging.group_by(out["holdings"], "currency")
    by_ccy = {r["key"]: r["value"] for r in rows}
    assert by_ccy["JPY"] == D("500000")    # 50%按分
    assert by_ccy["USD"] == D("500000")


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "tag.db")))


def _seed(client: TestClient) -> tuple[int, int]:
    sec = client.post(
        "/api/securities", json={"name": "オルカン", "asset_class": "fund_jp"}
    ).json()["id"]
    client.post(
        "/api/holdings",
        json={"security_id": sec, "account_name": "テスト証券", "quantity": "10000",
              "avg_cost": "8000"},
    )
    client.post(f"/api/securities/{sec}/manual-price",
                json={"date": "2026-08-01", "value": "10000"})
    return sec, sec


def test_tag_color_can_be_changed_without_recreating(client: TestClient):
    """色は作り直さずに更新できること（名前・銘柄割当は保持される）。"""
    tag = client.post("/api/tags", json={"name": "世界株", "color": "#3fb950"}).json()["id"]
    sec, _ = _seed(client)
    client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": tag, "weight": "100"}]},
    )
    assert client.put(f"/api/tags/{tag}", json={"color": "#39c5cf"}).status_code == 200
    row = client.get("/api/tags").json()["tags"][0]
    assert row["color"] == "#39c5cf"
    assert row["name"] == "世界株"
    assert row["security_count"] == 1          # 割当は消えない
    # タグ別サマリーの色も追随する
    by_tag = client.get("/api/tag-summary?currency=JPY").json()["by_tag"]
    assert next(t for t in by_tag if t["tag_id"] == tag)["color"] == "#39c5cf"


def test_tag_name_and_color_update_independently(client: TestClient):
    tag = client.post("/api/tags", json={"name": "リート", "color": "#f0883e"}).json()["id"]
    client.put(f"/api/tags/{tag}", json={"name": "REIT"})
    row = client.get("/api/tags").json()["tags"][0]
    assert (row["name"], row["color"]) == ("REIT", "#f0883e")   # 色は据え置き
    client.put(f"/api/tags/{tag}", json={"color": "#a371f7"})
    row = client.get("/api/tags").json()["tags"][0]
    assert (row["name"], row["color"]) == ("REIT", "#a371f7")   # 名前は据え置き


def test_tag_crud_and_duplicate_name(client: TestClient):
    r = client.post("/api/tags", json={"name": "世界株", "color": "#3fb950"})
    assert r.status_code == 200
    tag_id = r.json()["id"]
    assert client.post("/api/tags", json={"name": "世界株"}).status_code == 409
    assert client.put(f"/api/tags/{tag_id}", json={"name": "全世界株"}).status_code == 200
    assert client.get("/api/tags").json()["tags"][0]["name"] == "全世界株"
    assert client.delete(f"/api/tags/{tag_id}").status_code == 200
    assert client.get("/api/tags").json()["tags"] == []


def test_allocation_rejects_over_100(client: TestClient):
    sec, _ = _seed(client)
    a = client.post("/api/tags", json={"name": "A"}).json()["id"]
    b = client.post("/api/tags", json={"name": "B"}).json()["id"]
    r = client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": a, "weight": "70"},
                              {"tag_id": b, "weight": "40"}]},
    )
    assert r.status_code == 400
    assert "100" in r.json()["detail"]


def test_allocation_and_tag_summary(client: TestClient):
    sec, _ = _seed(client)
    world = client.post("/api/tags", json={"name": "世界株"}).json()["id"]
    jp = client.post("/api/tags", json={"name": "国内株式"}).json()["id"]
    client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": world, "weight": "95"},
                              {"tag_id": jp, "weight": "5"}]},
    )
    s = client.get("/api/tag-summary?currency=JPY").json()
    by_name = {t["name"]: D(t["value"]) for t in s["by_tag"]}
    assert by_name["世界株"] == D("9500")   # 10000口 × 10000円 ÷ 10000 = 10,000円 の95%
    assert by_name["国内株式"] == D("500")
    assert D(s["total_value"]) == D("10000")
    assert s["unallocated"] == []           # 100%配分済みなので警告対象なし


def test_unallocated_surfaced_by_api(client: TestClient):
    sec, _ = _seed(client)
    tag = client.post("/api/tags", json={"name": "世界株"}).json()["id"]
    client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": tag, "weight": "40"}]},
    )
    s = client.get("/api/tag-summary?currency=JPY").json()
    assert len(s["unallocated"]) == 1
    u = s["unallocated"][0]
    assert u["security_id"] == sec
    assert D(u["allocated_pct"]) == D("40")
    assert D(u["unallocated_value"]) == D("6000")
    assert D(u["value"]) == D("10000")


def test_portfolio_crud_and_detail(client: TestClient):
    sec, _ = _seed(client)
    jp = client.post("/api/tags", json={"name": "国内株式"}).json()["id"]
    world = client.post("/api/tags", json={"name": "世界株"}).json()["id"]
    client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": world, "weight": "95"},
                              {"tag_id": jp, "weight": "5"}]},
    )
    pid = client.post(
        "/api/portfolios", json={"name": "日本株だけ", "tag_ids": [jp]}
    ).json()["id"]
    assert client.post("/api/portfolios", json={"name": "日本株だけ"}).status_code == 409

    d = client.get(f"/api/portfolios/{pid}?currency=JPY").json()
    assert D(d["total_value"]) == D("500")     # 5%だけ計上
    assert d["by_tag"][0]["name"] == "国内株式"
    assert D(d["holdings"][0]["portfolio_ratio"]) == D("5")
    assert d["by_currency"][0]["key"] == "JPY"
    assert d["by_class"][0]["label"] == "投資信託"
    assert d["by_account"][0]["key"] == "テスト証券"

    lst = client.get("/api/portfolios?currency=JPY").json()["portfolios"]
    assert D(lst[0]["value"]) == D("500")

    # 個別追加すると全額計上に変わる
    client.put(f"/api/portfolios/{pid}", json={"include_security_ids": [sec]})
    d2 = client.get(f"/api/portfolios/{pid}?currency=JPY").json()
    assert D(d2["total_value"]) == D("10000")

    # 除外は最優先
    client.put(f"/api/portfolios/{pid}", json={"exclude_security_ids": [sec]})
    assert D(client.get(f"/api/portfolios/{pid}?currency=JPY").json()["total_value"]) == D("0")

    assert client.delete(f"/api/portfolios/{pid}").status_code == 200
    assert client.get(f"/api/portfolios/{pid}").status_code == 404


def test_tag_holdings_drilldown_matches_summary(client: TestClient):
    """タグのドリルダウン合計が、タグ別サマリーの金額と一致すること。"""
    sec, _ = _seed(client)
    world = client.post("/api/tags", json={"name": "世界株"}).json()["id"]
    jp = client.post("/api/tags", json={"name": "国内株式"}).json()["id"]
    client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": world, "weight": "95"},
                              {"tag_id": jp, "weight": "5"}]},
    )
    summary = client.get("/api/tag-summary?currency=JPY").json()
    by_id = {t["tag_id"]: D(t["value"]) for t in summary["by_tag"]}

    d = client.get(f"/api/tags/{jp}/holdings?currency=JPY").json()
    assert d["tag"]["name"] == "国内株式"
    assert D(d["total_value"]) == by_id[jp] == D("500")
    assert len(d["holdings"]) == 1
    assert D(d["holdings"][0]["portfolio_ratio"]) == D("5")
    assert D(d["holdings"][0]["portfolio_value"]) == D("500")
    # 口座別・クラス別も按分後の額で出る
    assert D(d["by_account"][0]["value"]) == D("500")

    d2 = client.get(f"/api/tags/{world}/holdings?currency=JPY").json()
    assert D(d2["total_value"]) == by_id[world] == D("9500")


def test_tag_holdings_unknown_tag_is_404(client: TestClient):
    assert client.get("/api/tags/9999/holdings").status_code == 404


def test_deleting_tag_removes_allocations_and_portfolio_refs(client: TestClient):
    sec, _ = _seed(client)
    tag = client.post("/api/tags", json={"name": "一時タグ"}).json()["id"]
    client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": tag, "weight": "100"}]},
    )
    pid = client.post(
        "/api/portfolios", json={"name": "P", "tag_ids": [tag]}
    ).json()["id"]
    client.delete(f"/api/tags/{tag}")
    assert client.get("/api/security-tags").json()["allocations"] == {}
    assert client.get(f"/api/portfolios/{pid}").json()["portfolio"]["tag_ids"] == []


def test_tags_endpoints_validate(client: TestClient):
    assert client.post("/api/tags", json={"name": "  "}).status_code == 400
    assert client.put("/api/tags/999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/tags/999").status_code == 404
    assert client.get("/api/portfolios/999").status_code == 404
    sec, _ = _seed(client)
    assert client.put(
        f"/api/securities/{sec}/tags",
        json={"allocations": [{"tag_id": 999, "weight": "10"}]},
    ).status_code == 404
    assert client.put(
        f"/api/securities/{sec}/tags", json={"allocations": "nope"}
    ).status_code == 400


# ----------------------------------------------------------------------
# タグの自動配分（ルールベース）
# ----------------------------------------------------------------------

def _suggestion_for(client: TestClient, sec_id: int) -> dict:
    d = client.post("/api/tag-rules/suggest").json()
    return next(s for s in d["suggestions"] if s["security_id"] == sec_id)


def test_autoalloc_suggest_classifies_new_change_unchanged(client: TestClient):
    world = client.post("/api/tags", json={"name": "世界株"}).json()["id"]
    jp = client.post("/api/tags", json={"name": "国内株式"}).json()["id"]
    sec, _ = _seed(client)   # 「オルカン」fund_jp → 世界株95/国内株式5

    s = _suggestion_for(client, sec)
    assert s["status"] == "new"
    assert s["rule_id"] == "world.allcountry"
    assert s["matched_by"] == "keyword"
    assert s["fallback"] is False
    assert {(a["tag_id"], a["weight"]) for a in s["suggested"]} == {
        (world, "95"), (jp, "5"),
    }

    # 手で違う配分にすると change になる
    client.put(f"/api/securities/{sec}/tags",
               json={"allocations": [{"tag_id": world, "weight": "100"}]})
    assert _suggestion_for(client, sec)["status"] == "change"

    # ルール通りの配分なら unchanged（100 と 100.0 の表記差も同一視）
    client.put(f"/api/securities/{sec}/tags",
               json={"allocations": [{"tag_id": world, "weight": "95.0"},
                                     {"tag_id": jp, "weight": "5"}]})
    assert _suggestion_for(client, sec)["status"] == "unchanged"


def test_autoalloc_suggest_no_rule_and_missing_tag(client: TestClient):
    no_rule = client.post(
        "/api/securities", json={"name": "ひふみプラス", "asset_class": "fund_jp"}
    ).json()["id"]
    gold = client.post(
        "/api/securities", json={"name": "架空ゴールドファンド", "asset_class": "fund_jp"}
    ).json()["id"]

    d = client.post("/api/tag-rules/suggest").json()
    by_id = {s["security_id"]: s for s in d["suggestions"]}
    assert by_id[no_rule]["status"] == "no-rule"
    assert by_id[no_rule]["suggested"] == []
    # ルールは一致するが「ゴールド」タグが未作成 → missing-tag として報告
    assert by_id[gold]["status"] == "missing-tag"
    assert by_id[gold]["missing_tags"] == ["ゴールド"]
    assert "ゴールド" in d["missing_tags"]
    assert d["counts"]["missing-tag"] >= 1


def test_autoalloc_fallback_flagged_for_plain_stock(client: TestClient):
    client.post("/api/tags", json={"name": "国内株式"})
    stock = client.post(
        "/api/securities", json={"name": "架空商事", "asset_class": "stock_jp"}
    ).json()["id"]
    s = _suggestion_for(client, stock)
    assert s["status"] == "new"
    assert s["rule_id"] == "fb.stock_jp"
    assert s["matched_by"] == "class"
    assert s["fallback"] is True
    assert s["fund_shape_warning"] is False


def test_autoalloc_apply_only_touches_requested_ids(client: TestClient):
    world = client.post("/api/tags", json={"name": "世界株"}).json()["id"]
    jp = client.post("/api/tags", json={"name": "国内株式"}).json()["id"]
    client.post("/api/tags", json={"name": "ハイリスク"})
    sec, _ = _seed(client)
    other = client.post(
        "/api/securities",
        json={"name": "iFreeNEXT FANG+インデックス", "asset_class": "fund_jp"},
    ).json()["id"]
    # other には手で入れた配分があり、apply の対象にしない → 触られないこと
    client.put(f"/api/securities/{other}/tags",
               json={"allocations": [{"tag_id": world, "weight": "60"}]})

    r = client.post("/api/tag-rules/apply", json={"security_ids": [sec]})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 1
    assert body["skipped"] == []

    allocs = client.get("/api/security-tags").json()["allocations"]
    assert {(a["tag_id"], D(a["weight"])) for a in allocs[str(sec)]} == {
        (world, D("95")), (jp, D("5")),
    }
    assert allocs[str(other)] == [{"tag_id": world, "weight": "60"}]


def test_autoalloc_apply_recomputes_server_side(client: TestClient):
    """suggest と apply の間にタグ名が変わったら、古い tag_id を書かず skip する。"""
    world = client.post("/api/tags", json={"name": "世界株"}).json()["id"]
    client.post("/api/tags", json={"name": "国内株式"})
    sec, _ = _seed(client)
    s = _suggestion_for(client, sec)
    assert s["status"] == "new"

    client.put(f"/api/tags/{world}", json={"name": "改名済みタグ"})
    r = client.post("/api/tag-rules/apply", json={"security_ids": [sec]})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 0
    assert body["skipped"][0]["reason"] == "missing-tag"
    assert body["skipped"][0]["missing_tags"] == ["世界株"]
    assert body["warnings"]
    # 配分は一切書かれていない
    assert client.get("/api/security-tags").json()["allocations"] == {}


def test_autoalloc_apply_no_rule_is_skipped(client: TestClient):
    sec = client.post(
        "/api/securities", json={"name": "ひふみプラス", "asset_class": "fund_jp"}
    ).json()["id"]
    body = client.post("/api/tag-rules/apply", json={"security_ids": [sec]}).json()
    assert body["applied"] == 0
    assert body["skipped"][0]["reason"] == "no-rule"


def test_autoalloc_apply_validates(client: TestClient):
    assert client.post("/api/tag-rules/apply", json={}).status_code == 400
    assert client.post(
        "/api/tag-rules/apply", json={"security_ids": []}
    ).status_code == 400
    assert client.post(
        "/api/tag-rules/apply", json={"security_ids": [9999]}
    ).status_code == 404
