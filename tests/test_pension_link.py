"""年金（iDeCo・企業型DC）×投信協会連携の口数逆算。

MF PDF の年金セクションは取得価額と評価額しか持たない（口数・基準価額なし）。
基準価額に連携すると 評価額÷NAV×10000 で口数が逆算でき、以後は日々の
基準価額で自動評価される — その一連の流れを固定する。
数値は実在ファンドの公表桁数を模した架空データ。
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from fastapi.testclient import TestClient

import asset_summary.web.app as web_app
from asset_summary.core import fund_autolink as fal
from asset_summary.core import prices as core_prices
from asset_summary.core.models import (
    Account,
    AssetClass,
    HoldingSnapshot,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.core.portfolio import daily_series, summarize
from asset_summary.core.store import Store
from asset_summary.importers import matching
from asset_summary.importers.base import make_name_key
from tests.fixtures import factories

D = Decimal

FUND = "ＳＢＩ・全世界株式インデックス・ファンド"
REF = "JP90C000TEST:0331TEST"
# 実例と同じ関係: 1,441,776口 × 35,754円/万口 = 5,154,925.9円 → 記載 5,154,925円
NAV = D("35754")
VALUE = D("5154925")
UNITS = D("1441776")
ACQ = D("4409657")
AS_OF = date(2026, 8, 22)


def _pension_sec(store: Store, name: str = FUND, linked: bool = True) -> int:
    return store.create_security(
        Security(
            name=name,
            name_key=make_name_key(name),
            asset_class=AssetClass.PENSION,
            unit=Unit.UNIT,
            price_unit_divisor=1,
            price_source_type=(
                PriceSourceType.TOUSHIN if linked else PriceSourceType.NONE
            ),
            price_source_ref=REF if linked else None,
            price_source_status=(
                PriceSourceStatus.LINKED if linked else PriceSourceStatus.NOT_REQUIRED
            ),
        )
    )


def _pension_lot(store: Store, sec_id: int, value: Decimal | None = VALUE, **kw) -> None:
    acct = store.get_or_create_account("年金", kind="pension")
    defaults = dict(
        account_id=acct.id,
        security_id=sec_id,
        as_of_date=AS_OF,
        quantity=D("1"),
        avg_cost=ACQ,
        reported_value_jpy=value,
        origin="mf",
    )
    defaults.update(kw)
    store.upsert_snapshot(HoldingSnapshot(**defaults))


def _seed_navs(store: Store, days: dict[str, str] | None = None) -> None:
    for day, nav in (days or {"2026-08-20": "35600", "2026-08-21": str(NAV)}).items():
        store.upsert_daily_price("toushin", REF, day, D(nav))


# ----------------------------------------------------------------------
# derive_pension_units（純関数）
# ----------------------------------------------------------------------

def test_derive_units_finds_integer_units():
    series = {"2026-08-20": D("35600"), "2026-08-21": NAV}
    units, confident = fal.derive_pension_units(VALUE, series, AS_OF.isoformat())
    assert units == UNITS
    assert confident is True
    # 逆算した口数で評価額が円未満まで再現できる
    assert abs(units * NAV / 10000 - VALUE) < 1


def test_derive_units_falls_back_to_ratio_when_no_day_reproduces():
    # どの日の NAV でも整数口で評価額を再現できない → 直近NAVの比例口数
    series = {"2026-08-21": D("99999")}
    units, confident = fal.derive_pension_units(VALUE, series, AS_OF.isoformat())
    assert confident is False
    assert units == (VALUE * 10000 / D("99999")).quantize(D("0.0001"))


def test_derive_units_ignores_navs_after_as_of_and_empty():
    assert fal.derive_pension_units(VALUE, {}, AS_OF.isoformat()) is None
    # 基準日より後の NAV しか無い（未来の値では逆算しない）
    assert fal.derive_pension_units(VALUE, {"2026-08-25": str(NAV)}, AS_OF.isoformat()) is None
    assert fal.derive_pension_units(D("0"), {"2026-08-21": str(NAV)}) is None


# ----------------------------------------------------------------------
# derive_pension_quantities（連携済み年金の一括変換）
# ----------------------------------------------------------------------

def test_derive_quantities_converts_linked_pension(store: Store):
    sec_id = _pension_sec(store)
    _pension_lot(store, sec_id)
    _seed_navs(store)

    out = fal.derive_pension_quantities(store)

    assert out == [{"security_id": sec_id, "name": FUND, "lots": 1}]
    sec = store.get_security(sec_id)
    assert sec.price_unit_divisor == 10000
    assert sec.unit == Unit.KUCHI
    lot = store.current_holdings()[0]
    assert lot.quantity == UNITS
    assert lot.avg_cost == ACQ            # 取得価額（総額）はそのまま
    assert lot.reported_value_jpy == VALUE
    # 冪等: もう一度呼んでも何も変わらない
    assert fal.derive_pension_quantities(store) == []


def test_derive_quantities_skips_unlinked_and_no_series(store: Store):
    unlinked = _pension_sec(store, name="連携していない年金", linked=False)
    _pension_lot(store, unlinked)
    assert fal.derive_pension_quantities(store) == []

    linked = _pension_sec(store)
    _pension_lot(store, linked)
    # 系列が無ければ何もしない（quantity=1 のまま → 評価は記載値フォールバック）
    assert fal.derive_pension_quantities(store) == []
    assert all(l.quantity in (D("1"),) for l in store.current_holdings())


def test_derive_quantities_uses_manual_anchor_and_removes_it(store: Store):
    """手動登録の年金（評価額を manual 価格で持つ）も連携で口数に変換できる。"""
    sec_id = store.create_security(
        Security(
            name="手動登録の企業型DC",
            name_key=make_name_key("手動登録の企業型DC"),
            asset_class=AssetClass.PENSION,
            unit=Unit.UNIT,
            price_unit_divisor=1,
            price_source_type=PriceSourceType.TOUSHIN,
            price_source_ref=REF,
            price_source_status=PriceSourceStatus.LINKED,
        )
    )
    _pension_lot(store, sec_id, value=None)  # reported_value なし
    store.upsert_daily_price("manual", str(sec_id), AS_OF.isoformat(), VALUE)
    _seed_navs(store)

    out = fal.derive_pension_quantities(store)

    assert out[0]["lots"] == 1
    assert store.current_holdings()[0].quantity == UNITS
    # 変換後、手動評価額は削除される（NAV系列を上書きして汚さないように）
    prices, _ = store.get_price_rows("manual", str(sec_id))
    assert prices == {}


# ----------------------------------------------------------------------
# 評価: 口数導出前は記載値のまま・導出後は NAV で日々評価
# ----------------------------------------------------------------------

def _summarize_pension(store: Store, spot: dict[int, Decimal]):
    secs = store.securities_by_id()
    accounts = {a.id: a for a in store.list_accounts()}
    return summarize(
        store.current_holdings(), secs, accounts, spot=spot, fx={},
        settings={"include_pension": "1", "include_points": "1"},
    )


def test_underived_pension_is_not_valued_at_one_unit(store: Store):
    """連携済みでも quantity=1 のままなら記載評価額で評価する（1口×NAVにしない）。"""
    sec_id = _pension_sec(store)
    _pension_lot(store, sec_id)  # quantity=1 のまま
    out = _summarize_pension(store, spot={sec_id: NAV})
    h = out["holdings"][0]
    assert h["value"] == VALUE          # 3.5円ではなく記載値
    assert h["has_price"] is False


def test_derived_pension_is_valued_by_nav(store: Store):
    sec_id = _pension_sec(store)
    _pension_lot(store, sec_id)
    _seed_navs(store)
    fal.derive_pension_quantities(store)

    nav_today = D("36000")  # 取込後に基準価額が動いた
    out = _summarize_pension(store, spot={sec_id: nav_today})
    h = out["holdings"][0]
    assert h["value"] == UNITS * nav_today / 10000
    assert h["has_price"] is True
    assert h["pl"] == h["value"] - ACQ  # 損益も日々追随する


def test_daily_series_guards_underived_pension(store: Store):
    sec_id = _pension_sec(store)
    _pension_lot(store, sec_id)
    secs = store.securities_by_id()
    accounts = {a.id: a for a in store.list_accounts()}
    series = {sec_id: ({AS_OF.isoformat(): NAV}, "JPY")}
    out = daily_series(
        store.all_snapshots(), secs, accounts, series, {}, AS_OF, AS_OF,
        settings={"include_pension": "1", "include_points": "1"},
    )
    assert out[0]["value"] == VALUE     # 1口×NAV=3.5円 に潰れない


# ----------------------------------------------------------------------
# MF 取込プレビューでの逆算（以後の取込は自動で実口数になる）
# ----------------------------------------------------------------------

def test_build_matches_derives_units_for_linked_pension(store: Store):
    sec_id = _pension_sec(store)
    _pension_lot(store, sec_id)
    _seed_navs(store)
    fal.derive_pension_quantities(store)

    # 翌月の取込: 掛金買付で評価額が増えている（NAVも動いた）
    new_nav = D("36000")
    store.upsert_daily_price("toushin", REF, "2026-09-19", new_nav)
    new_units = D("1455000")
    new_value = (new_units * new_nav / 10000).to_integral_value(rounding="ROUND_FLOOR")
    result = factories.make_result(
        factories.pension(FUND, acq="4529657", value=str(new_value))
    )
    rows, diff, _sections = matching.build_matches(store, result)

    row = next(r for r in rows if r["section"] == "pension")
    assert row["security_id"] == sec_id
    assert row["new_quantity"] == str(new_units)      # 1 ではなく逆算した口数
    assert row["old_quantity"] == str(UNITS)          # 既存ロットと正しく突き合う
    assert row["status"] == "qty_changed"


def test_build_matches_keeps_quantity_one_when_unlinked(store: Store):
    sec_id = _pension_sec(store, linked=False)
    _pension_lot(store, sec_id)
    result = factories.make_result(factories.pension(FUND, acq=str(ACQ), value=str(VALUE)))
    rows, _diff, _sections = matching.build_matches(store, result)
    row = next(r for r in rows if r["section"] == "pension")
    assert row["new_quantity"] == "1"


# ----------------------------------------------------------------------
# suggest_links が年金を連携対象に含める
# ----------------------------------------------------------------------

def test_suggest_links_includes_pension_as_candidates(store: Store):
    sec_id = _pension_sec(store, linked=False)
    _pension_lot(store, sec_id)
    results = [{"name": FUND, "ref": REF, "company": "架空アセット", "category": "4"}]
    out = fal.suggest_links(
        store,
        search=lambda q, warn=None, **kw: results,
        fetch_history=lambda ref, warn=None, **kw: None,
    )
    assert [s["security_id"] for s in out] == [sec_id]
    # 年金は基準価額の記載が無いため自動確定にはならない（候補から人が選ぶ）
    assert out[0]["status"] == "candidates"
    assert out[0]["candidates"][0]["nav_match"] is None
    # 取込が1回分しか無ければ値動き照合もできない
    assert out[0]["candidates"][0]["movement_match"] is None


# ----------------------------------------------------------------------
# 値動き照合: 取込2回分以上あれば候補NAVの騰落率で裏を取れる
# ----------------------------------------------------------------------


def test_suggest_pension_movement_verifies_candidates(store: Store):
    """「前回評価額×候補NAVの騰落率+掛金増分 ≒ 今回評価額」で候補を裏取りする。

    正しいファンドは名前が大きく略されていても値動き一致で拾われて先頭に、
    名前だけ似た別ファンドは値動き不一致で後ろに回る。
    """
    from asset_summary.core.providers.base import HistoryResult

    sec_id = _pension_sec(store, linked=False)
    u1 = D("1441776")
    nav_t1, nav_t2 = D("35000"), NAV        # 期間中 +2.15%
    v1 = (u1 * nav_t1 / 10000).to_integral_value(rounding="ROUND_FLOOR")
    # 期中に掛金 109,652円で 31,063口を買付
    du, cost_add = D("31063"), D("109652")
    v2 = ((u1 + du) * nav_t2 / 10000).to_integral_value(rounding="ROUND_FLOOR")
    _pension_lot(store, sec_id, value=v1, avg_cost=D("4300000"),
                 as_of_date=date(2026, 7, 20))
    _pension_lot(store, sec_id, value=v2, avg_cost=D("4300000") + cost_add,
                 as_of_date=AS_OF)

    results = [
        # 正しいファンド（正式名が略称で名前スコアは低い）
        {"name": "雪だるま（全世界株式）", "ref": REF, "company": "架空A", "category": "4"},
        # 名前は似ているが値動きが違う別ファンド（期間中 -5%）
        {"name": FUND, "ref": "JP90C000WRNG:0331WRNG", "company": "架空B", "category": "4"},
    ]
    navs = {
        REF: {date(2026, 7, 17): nav_t1, date(2026, 8, 21): nav_t2},
        "JP90C000WRNG:0331WRNG": {
            date(2026, 7, 17): D("20000"), date(2026, 8, 21): D("19000"),
        },
    }
    out = fal.suggest_links(
        store,
        search=lambda q, warn=None, **kw: results,
        fetch_history=lambda ref, warn=None, **kw: HistoryResult(navs[ref], "JPY", []),
    )
    s = out[0]
    assert s["status"] == "candidates"          # 値動き一致でも自動確定はしない
    by_ref = {c["ref"]: c for c in s["candidates"]}
    assert by_ref[REF]["movement_match"] is True
    assert by_ref[REF]["movement_periods"] == 1
    assert by_ref["JP90C000WRNG:0331WRNG"]["movement_match"] is False
    # 名前スコアが低くても値動き一致の候補は落とさず、先頭に並べる
    assert s["candidates"][0]["ref"] == REF


def test_build_matches_warns_when_value_not_reproducible(store: Store):
    """連携先のNAVで評価額を整数口で再現できない取込は警告を出す（誤連携の兆候）。"""
    sec_id = _pension_sec(store)
    _pension_lot(store, sec_id)
    # 評価額を整数口で再現できないNAV（誤ったファンドに連携された想定）
    store.upsert_daily_price("toushin", REF, "2026-08-21", D("99999"))

    result = factories.make_result(factories.pension(FUND, acq=str(ACQ), value=str(VALUE)))
    rows, _diff, _sections = matching.build_matches(store, result)

    assert any("再現できません" in w for w in result.report.warnings)
    row = next(r for r in rows if r["section"] == "pension")
    assert row["new_quantity"] != "1"           # 比例口数では取り込む（評価は追随する）


# ----------------------------------------------------------------------
# API 一巡: 連携 → 逆算 → サマリーが NAV 評価になる
# ----------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = web_app.create_app(str(tmp_path / "t.db"))
    # toushin / manual はローカルの daily_prices を読むだけ（ネットワーク不要）
    monkeypatch.setattr(web_app, "fetch_spot", core_prices.fetch_spot)
    monkeypatch.setattr(web_app, "fetch_fx_rates", lambda store, ccys, warn=None: {})
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "ensure_fx_history", lambda *a, **k: None)
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


def test_api_relink_pension_to_another_fund_rederives_units(app, client):
    """誤連携 → 正しいファンドへ連携し直すと、口数が確定値で引き直される。"""
    store: Store = app.state.store
    sec_id = _pension_sec(store, linked=False)
    _pension_lot(store, sec_id)
    _seed_navs(store)
    wrong_ref = "JP90C000WRNG:0331WRNG"
    # 評価額を整数口で再現できないNAV（誤ったファンド）
    store.upsert_daily_price("toushin", wrong_ref, "2026-08-21", D("99999"))

    r1 = client.put(
        f"/api/securities/{sec_id}",
        json={"price_source_type": "toushin", "price_source_ref": wrong_ref},
    )
    assert r1.status_code == 200
    wrong_units = store.current_holdings()[0].quantity
    assert wrong_units == (VALUE * 10000 / D("99999")).quantize(D("0.0001"))
    # 誤連携の兆候（整数口で再現できない）は警告として返る
    assert any("再現できません" in w for w in r1.json()["warnings"])

    r2 = client.put(
        f"/api/securities/{sec_id}",
        json={"price_source_type": "toushin", "price_source_ref": REF},
    )
    assert r2.status_code == 200
    sec = store.get_security(sec_id)
    assert sec.price_source_ref == REF
    assert store.current_holdings()[0].quantity == UNITS  # 確定値で上書き


def test_api_unlink_restores_class_default_status(app, client):
    """連携の解除。年金は「不要」に戻り（未連携の警告対象にしない）、投信は「未連携」。"""
    store: Store = app.state.store
    sec_id = _pension_sec(store)
    _pension_lot(store, sec_id)
    _seed_navs(store)
    fal.derive_pension_quantities(store)

    res = client.put(
        f"/api/securities/{sec_id}",
        json={"price_source_type": "none", "price_source_ref": None},
    )
    assert res.status_code == 200
    sec = store.get_security(sec_id)
    assert sec.price_source_type == PriceSourceType.NONE
    assert sec.price_source_ref is None
    assert sec.price_source_status == PriceSourceStatus.NOT_REQUIRED
    # 口数はそのまま残り、評価は記載値へ安全にフォールバックする
    assert store.current_holdings()[0].quantity == UNITS
    summary = client.get("/api/summary?currency=JPY").json()
    row = next(h for h in summary["holdings"] if h["id"] == sec_id)
    assert D(row["value"]) == VALUE
    assert row["has_price"] is False

    # 投信の解除は従来どおり「未連携」（価格ソースが必要なクラスのため）
    fund_id = store.create_security(
        Security(
            name="解除テスト投信", name_key=make_name_key("解除テスト投信"),
            asset_class=AssetClass.FUND_JP, unit=Unit.KUCHI,
            price_unit_divisor=10000,
            price_source_type=PriceSourceType.TOUSHIN,
            price_source_ref=REF,
            price_source_status=PriceSourceStatus.LINKED,
        )
    )
    client.put(
        f"/api/securities/{fund_id}",
        json={"price_source_type": "none", "price_source_ref": None},
    )
    assert store.get_security(fund_id).price_source_status == PriceSourceStatus.UNLINKED


def test_api_link_pension_derives_units_and_values_by_nav(app, client):
    store: Store = app.state.store
    sec_id = _pension_sec(store, linked=False)
    _pension_lot(store, sec_id)
    _seed_navs(store)

    res = client.put(
        f"/api/securities/{sec_id}",
        json={"price_source_type": "toushin", "price_source_ref": REF},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["pension_units"] == [
        {"security_id": sec_id, "name": FUND, "lots": 1}
    ]

    lot = store.current_holdings()[0]
    assert lot.quantity == UNITS

    summary = client.get("/api/summary?currency=JPY").json()
    row = next(h for h in summary["holdings"] if h["id"] == sec_id)
    assert row["asset_class"] == "pension"
    assert D(row["quantity"]) == UNITS
    assert D(row["price"]) == NAV                       # 現在値=最新の基準価額
    assert D(row["value"]) == UNITS * NAV / 10000       # 記載値ではなく NAV 評価
    assert D(row["pl"]) == UNITS * NAV / 10000 - ACQ
