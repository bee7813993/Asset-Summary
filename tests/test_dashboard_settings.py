"""資産クラス単位の総資産包含設定と、ダッシュボード構成の保存。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from asset_summary.core import portfolio
from asset_summary.core.models import AssetClass
from asset_summary.web.app import DEFAULT_DASHBOARD_LAYOUT, create_app

D = Decimal


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "dash.db")))


# ----------------------------------------------------------------------
# 設定キーと除外ロジック
# ----------------------------------------------------------------------

def test_include_setting_key_keeps_legacy_points_key():
    """'point' は旧DBの 'include_points' を使い続ける（設定が消えないように）。"""
    assert portfolio.include_setting_key(AssetClass.POINT) == "include_points"
    assert portfolio.include_setting_key("point") == "include_points"
    assert portfolio.include_setting_key(AssetClass.PENSION) == "include_pension"
    assert portfolio.include_setting_key(AssetClass.REAL_ESTATE) == "include_real_estate"


def test_excluded_classes_defaults_to_none():
    assert portfolio._excluded_classes({}) == set()


@pytest.mark.parametrize(
    "key,cls",
    [
        ("include_points", AssetClass.POINT),
        ("include_pension", AssetClass.PENSION),
        ("include_real_estate", AssetClass.REAL_ESTATE),
        ("include_metal", AssetClass.METAL),
        ("include_cash", AssetClass.CASH),
    ],
)
def test_any_class_can_be_excluded(key, cls):
    assert portfolio._excluded_classes({key: "0"}) == {cls}


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

def test_meta_exposes_include_classes_and_layout(client: TestClient):
    s = client.get("/api/meta").json()["settings"]
    assert s["include_classes"]["real_estate"] is True
    assert s["include_classes"]["point"] is True
    assert [w["id"] for w in s["dashboard_layout"]] == [
        w["id"] for w in DEFAULT_DASHBOARD_LAYOUT
    ]


def test_include_classes_roundtrip(client: TestClient):
    r = client.put("/api/settings", json={"include_classes": {"real_estate": False}})
    assert r.status_code == 200
    s = client.get("/api/meta").json()["settings"]
    assert s["include_classes"]["real_estate"] is False
    assert s["include_classes"]["cash"] is True


def test_legacy_points_key_and_new_key_agree(client: TestClient):
    """旧キーで保存しても新しい include_classes に反映されること。"""
    client.put("/api/settings", json={"include_points": False})
    s = client.get("/api/meta").json()["settings"]
    assert s["include_classes"]["point"] is False
    assert s["include_points"] is False
    # 新形式で戻せる
    client.put("/api/settings", json={"include_classes": {"point": True}})
    s2 = client.get("/api/meta").json()["settings"]
    assert s2["include_classes"]["point"] is True
    assert s2["include_points"] is True


def test_include_classes_rejects_unknown_class(client: TestClient):
    r = client.put("/api/settings", json={"include_classes": {"nope": False}})
    assert r.status_code == 400
    assert client.put("/api/settings", json={"include_classes": "x"}).status_code == 400


def test_excluding_class_changes_total(client: TestClient):
    sec = client.post(
        "/api/securities", json={"name": "現金テスト", "asset_class": "cash"}
    ).json()["id"]
    client.post(
        "/api/holdings",
        json={"security_id": sec, "account_name": "銀行", "quantity": "500000"},
    )
    assert D(client.get("/api/summary?currency=JPY").json()["total_value"]) == D("500000")
    client.put("/api/settings", json={"include_classes": {"cash": False}})
    s = client.get("/api/summary?currency=JPY").json()
    assert D(s["total_value"]) == D("0")
    # 行は残り、対象外と分かる
    assert s["holdings"][0]["in_total"] is False


def test_merge_cash_setting_roundtrip(client: TestClient):
    assert client.get("/api/meta").json()["settings"]["merge_cash"] is True
    assert client.put("/api/settings", json={"merge_cash": False}).status_code == 200
    assert client.get("/api/meta").json()["settings"]["merge_cash"] is False
    client.put("/api/settings", json={"merge_cash": True})
    assert client.get("/api/meta").json()["settings"]["merge_cash"] is True


def test_merge_cash_combines_holdings_rows(client: TestClient):
    """既定では預金・現金は「A 他N件」の1行、オフにすると銀行ごとの行に戻る。"""
    sec = client.post(
        "/api/securities", json={"name": "現金・預金", "asset_class": "cash"}
    ).json()["id"]
    for bank, qty in (("架空ネット銀行", "800000"), ("別の銀行", "200000")):
        client.post(
            "/api/holdings",
            json={"security_id": sec, "account_name": bank, "quantity": qty},
        )

    s = client.get("/api/summary?currency=JPY").json()
    cash_rows = [h for h in s["holdings_by_security"] if h["asset_class"] == "cash"]
    assert len(cash_rows) == 1
    assert cash_rows[0]["account"] == "架空ネット銀行 他1件"
    assert D(cash_rows[0]["value"]) == D("1000000")
    assert [a["account"] for a in cash_rows[0]["accounts"]] == [
        "架空ネット銀行", "別の銀行"
    ]
    assert {c["class"]: c["holding_count"] for c in s["classes"]} == {"cash": 1}
    # 銘柄詳細は設定によらず口座横断の合算（内訳は accounts 表に出る）
    detail = client.get(f"/api/security/{sec}").json()
    assert D(detail["tiles"]["value"]) == D("1000000")
    assert len(detail["accounts"]) == 2

    client.put("/api/settings", json={"merge_cash": False})
    s2 = client.get("/api/summary?currency=JPY").json()
    cash_rows2 = [h for h in s2["holdings_by_security"] if h["asset_class"] == "cash"]
    assert sorted(h["account"] for h in cash_rows2) == [
        "別の銀行", "架空ネット銀行"
    ]
    assert {c["class"]: c["holding_count"] for c in s2["classes"]} == {"cash": 2}
    detail2 = client.get(f"/api/security/{sec}").json()
    assert D(detail2["tiles"]["value"]) == D("1000000")


def test_dashboard_layout_roundtrip(client: TestClient):
    layout = [
        {"id": "portfolios", "visible": True},
        {"id": "holdings", "visible": False},
    ]
    assert client.put("/api/settings", json={"dashboard_layout": layout}).status_code == 200
    saved = client.get("/api/meta").json()["settings"]["dashboard_layout"]
    # 指定した順が先頭に来て、未指定のウィジェットは既定値で補完される
    assert saved[0] == {"id": "portfolios", "visible": True}
    assert saved[1] == {"id": "holdings", "visible": False}
    assert {w["id"] for w in saved} == {w["id"] for w in DEFAULT_DASHBOARD_LAYOUT}


def test_dashboard_layout_rejects_bad_input(client: TestClient):
    assert client.put(
        "/api/settings", json={"dashboard_layout": [{"id": "bogus"}]}
    ).status_code == 400
    assert client.put(
        "/api/settings", json={"dashboard_layout": "nope"}
    ).status_code == 400


def test_chip_classes_default_set(client: TestClient):
    """未設定なら既定セット（現金・年金・ポイント）。"""
    chips = client.get("/api/meta").json()["settings"]["dashboard_chip_classes"]
    assert chips == ["cash", "pension", "point"]


def test_chip_classes_roundtrip_and_clear(client: TestClient):
    r = client.put(
        "/api/settings", json={"dashboard_chip_classes": ["cash", "real_estate", "cash"]}
    )
    assert r.status_code == 200
    saved = client.get("/api/meta").json()["settings"]["dashboard_chip_classes"]
    assert saved == ["cash", "real_estate"]        # 重複は除去
    # 空リストは「1つも出さない」、null は既定セットに戻す
    client.put("/api/settings", json={"dashboard_chip_classes": []})
    assert client.get("/api/meta").json()["settings"]["dashboard_chip_classes"] == []
    client.put("/api/settings", json={"dashboard_chip_classes": None})
    assert client.get("/api/meta").json()["settings"]["dashboard_chip_classes"] == [
        "cash", "pension", "point"
    ]


def test_chip_classes_corrupt_value_falls_back(client: TestClient):
    client.app.state.store.set_setting("dashboard_chip_classes", "{broken")
    assert client.get("/api/meta").json()["settings"]["dashboard_chip_classes"] == [
        "cash", "pension", "point"
    ]


def test_chip_classes_rejects_bad_input(client: TestClient):
    assert client.put(
        "/api/settings", json={"dashboard_chip_classes": ["nope"]}
    ).status_code == 400
    assert client.put(
        "/api/settings", json={"dashboard_chip_classes": "cash"}
    ).status_code == 400


def test_portfolios_widget_hidden_by_default(client: TestClient):
    """Myポートフォリオはタグのドリルダウンで代替できるため既定で非表示。"""
    layout = client.get("/api/meta").json()["settings"]["dashboard_layout"]
    by_id = {w["id"]: w["visible"] for w in layout}
    assert by_id["portfolios"] is False
    assert by_id["tags"] is False
    assert by_id["holdings"] is True


def test_dashboard_layout_survives_corrupt_value(client: TestClient):
    """保存値が壊れていても既定構成にフォールバックすること。"""
    client.app.state.store.set_setting("dashboard_layout", "{not json")
    saved = client.get("/api/meta").json()["settings"]["dashboard_layout"]
    assert [w["id"] for w in saved] == [w["id"] for w in DEFAULT_DASHBOARD_LAYOUT]
