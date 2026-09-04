"""前日終値（前日比の基準）の解決規則。

「いまスポットとして採用している値の日付より前の、最後の終値」が基準。
ソースごとにスポットの基準日が違う（投信・手動評価は日次系列の最新行が
そのままスポット）ため、一律「昨日」では誤る — そこを固定する。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from asset_summary.core import prices
from asset_summary.core.models import (
    AssetClass,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.core.store import Store

D = Decimal
TODAY = date(2026, 8, 13)


def _sec(store: Store, **kw) -> Security:
    defaults = dict(
        id=None,
        code="7203",
        name="テスト銘柄",
        name_key="test",
        asset_class=AssetClass.STOCK_JP,
        currency="JPY",
        unit=Unit.SHARE,
        price_unit_divisor=1,
        price_source_type=PriceSourceType.YAHOO,
        price_source_ref="7203.T",
        price_source_status=PriceSourceStatus.LINKED,
    )
    defaults.update(kw)
    sec = Security(**defaults)
    sec.id = store.create_security(sec)
    return sec


def _put(store: Store, source: str, ref: str, days_ago: int, price: str, ccy="JPY"):
    store.upsert_daily_price(
        source, ref, (TODAY - timedelta(days=days_ago)).isoformat(), D(price), currency=ccy
    )


# ---- store.get_price_before ----


def test_get_price_before_skips_the_given_day(store: Store):
    _put(store, "yahoo", "7203.T", 0, "3000")
    _put(store, "yahoo", "7203.T", 1, "2900")
    got = store.get_price_before("yahoo", "7203.T", TODAY.isoformat())
    assert got == ((TODAY - timedelta(days=1)).isoformat(), D("2900"), "JPY")


def test_get_price_before_returns_none_when_nothing_earlier(store: Store):
    _put(store, "yahoo", "7203.T", 0, "3000")
    assert store.get_price_before("yahoo", "7203.T", TODAY.isoformat()) is None


# ---- fetch_prev_close ----


def test_prev_close_for_live_source_is_last_row_before_today(store: Store):
    sec = _sec(store)
    _put(store, "yahoo", "7203.T", 1, "2900")
    _put(store, "yahoo", "7203.T", 2, "2800")
    prev, as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert prev[sec.id] == D("2900")
    assert as_of[sec.id] == (TODAY - timedelta(days=1)).isoformat()


def test_prev_close_for_toushin_is_the_row_before_the_latest(store: Store):
    """基準価額はT+1公表。スポット＝最新行なので、その1つ前が前日終値。"""
    sec = _sec(
        store,
        price_source_type=PriceSourceType.TOUSHIN,
        price_source_ref="ISIN:0331418A",
        asset_class=AssetClass.FUND_JP,
    )
    _put(store, "toushin", "ISIN:0331418A", 1, "24500")  # ← これがスポット
    _put(store, "toushin", "ISIN:0331418A", 2, "24300")  # ← 前日終値
    prev, as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert prev[sec.id] == D("24300")
    assert as_of[sec.id] == (TODAY - timedelta(days=2)).isoformat()


def test_prev_close_for_manual_source_is_the_row_before_the_latest(store: Store):
    sec = _sec(
        store,
        price_source_type=PriceSourceType.MANUAL,
        price_source_ref=None,
        price_source_status=PriceSourceStatus.MANUAL,
        asset_class=AssetClass.REAL_ESTATE,
    )
    _put(store, "manual", str(sec.id), 1, "30000000")
    _put(store, "manual", str(sec.id), 2, "29000000")
    prev, _as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert prev[sec.id] == D("29000000")


def test_no_day_change_for_an_index_linked_property(store: Store):
    """指数を紐付けても不動産の前日比は出さない。

    指数は月次・数ヶ月遅れなので、そこから作った日次差分を「今日の増減」として
    示すのは中身の無い主張になる。導出系列は読み出し時計算で daily_prices に
    書かないため、前日比の基準にはアンカー（査定額）しか見えず、
    PREV_MAX_GAP_DAYS がこれを殺す —— 偶然に頼らずここで固定する。
    """
    sec = _sec(
        store,
        price_source_type=PriceSourceType.MANUAL,
        price_source_ref="re_index:nanto:condo",
        price_source_status=PriceSourceStatus.MANUAL,
        asset_class=AssetClass.REAL_ESTATE,
    )
    _put(store, "manual", str(sec.id), 40, "50000000")
    _put(store, "manual", str(sec.id), 400, "48000000")
    # 指数は日次に見えるほど密（月次を内挿するため）だが、前日比には使わせない
    _put(store, "re_index", "nanto:condo", 45, "220")
    _put(store, "re_index", "nanto:condo", 75, "218")

    prev, _as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert sec.id not in prev


def test_prev_close_rejects_a_basis_that_is_too_old(store: Store):
    """何ヶ月も前の登録値を「前日」と呼ばない（手動評価の年金・不動産対策）。"""
    sec = _sec(store)
    _put(store, "yahoo", "7203.T", prices.PREV_MAX_GAP_DAYS + 1, "2900")
    prev, _as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert sec.id not in prev


def test_prev_close_accepts_a_gap_within_the_limit(store: Store):
    """週末・連休ぶんのズレは許す。"""
    sec = _sec(store)
    _put(store, "yahoo", "7203.T", prices.PREV_MAX_GAP_DAYS, "2900")
    prev, _as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert prev[sec.id] == D("2900")


def test_prev_close_missing_history_is_absent_not_zero(store: Store):
    sec = _sec(store)
    prev, as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert prev == {} and as_of == {}


def test_prev_close_for_metal_composes_futures_and_fx(store: Store):
    sec = _sec(
        store,
        price_source_type=PriceSourceType.METAL,
        price_source_ref="XAU",
        asset_class=AssetClass.METAL,
    )
    _put(store, "yahoo", "GC=F", 1, "2400", ccy="USD")
    _put(store, "fx", "USD", 1, "150")
    prev, _as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    from asset_summary.core.providers import metal

    assert prev[sec.id] == metal.spot_from_components(D("2400"), D("150"))


def test_prev_close_for_metal_needs_both_components(store: Store):
    sec = _sec(
        store,
        price_source_type=PriceSourceType.METAL,
        price_source_ref="XAU",
        asset_class=AssetClass.METAL,
    )
    _put(store, "yahoo", "GC=F", 1, "2400", ccy="USD")  # FX が無い
    prev, _as_of = prices.fetch_prev_close(store, [sec], today=TODAY)
    assert sec.id not in prev


def test_prev_close_shares_one_lookup_across_securities_with_same_ref(store: Store):
    a = _sec(store, code="A", name="A", name_key="a")
    b = _sec(store, code="B", name="B", name_key="b")
    _put(store, "yahoo", "7203.T", 1, "2900")
    prev, _as_of = prices.fetch_prev_close(store, [a, b], today=TODAY)
    assert prev[a.id] == prev[b.id] == D("2900")


# ---- fetch_prev_fx_rates ----


def test_prev_fx_rates_jpy_is_one_and_others_come_from_history(store: Store):
    _put(store, "fx", "USD", 1, "148.5")
    out = prices.fetch_prev_fx_rates(store, ["JPY", "USD", "EUR"], today=TODAY)
    assert out["JPY"] == D("1")
    assert out["USD"] == D("148.5")
    assert "EUR" not in out  # 履歴が無い通貨は載せない
