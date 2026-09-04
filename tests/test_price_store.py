from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from asset_summary.core import price_store as ps
from asset_summary.core.store import Store

D = Decimal


def d(s: str) -> date:
    return date.fromisoformat(s)


# ----------------------------------------------------------------------
# 純関数: merge_ranges / subtract_ranges / clamp_history_end
# ----------------------------------------------------------------------


def test_merge_ranges_empty():
    assert ps.merge_ranges([]) == []


def test_merge_ranges_overlap():
    out = ps.merge_ranges(
        [(d("2026-01-05"), d("2026-01-10")), (d("2026-01-01"), d("2026-01-07"))]
    )
    assert out == [(d("2026-01-01"), d("2026-01-10"))]


def test_merge_ranges_adjacent():
    # end+1日 == start は結合（週末再取得問題の解）
    out = ps.merge_ranges(
        [(d("2026-01-01"), d("2026-01-10")), (d("2026-01-11"), d("2026-01-20"))]
    )
    assert out == [(d("2026-01-01"), d("2026-01-20"))]


def test_merge_ranges_disjoint_kept():
    out = ps.merge_ranges(
        [(d("2026-01-01"), d("2026-01-10")), (d("2026-01-13"), d("2026-01-20"))]
    )
    assert out == [
        (d("2026-01-01"), d("2026-01-10")),
        (d("2026-01-13"), d("2026-01-20")),
    ]


def test_merge_ranges_contained_and_invalid():
    out = ps.merge_ranges(
        [
            (d("2026-01-01"), d("2026-01-31")),
            (d("2026-01-10"), d("2026-01-12")),  # 完全包含
            (d("2026-02-05"), d("2026-02-01")),  # start > end は無視
        ]
    )
    assert out == [(d("2026-01-01"), d("2026-01-31"))]


def test_subtract_ranges_no_coverage():
    assert ps.subtract_ranges(d("2026-01-01"), d("2026-01-10"), []) == [
        (d("2026-01-01"), d("2026-01-10"))
    ]


def test_subtract_ranges_partial():
    covered = [(d("2026-01-03"), d("2026-01-05")), (d("2026-01-08"), d("2026-01-20"))]
    gaps = ps.subtract_ranges(d("2026-01-01"), d("2026-01-10"), covered)
    assert gaps == [
        (d("2026-01-01"), d("2026-01-02")),
        (d("2026-01-06"), d("2026-01-07")),
    ]


def test_subtract_ranges_fully_covered():
    covered = [(d("2025-12-01"), d("2026-02-01"))]
    assert ps.subtract_ranges(d("2026-01-01"), d("2026-01-10"), covered) == []


def test_subtract_ranges_inverted_request():
    assert ps.subtract_ranges(d("2026-01-10"), d("2026-01-01"), []) == []


def test_clamp_history_end():
    today = d("2026-08-04")
    # 当日・未来は昨日へクランプ
    assert ps.clamp_history_end(d("2026-08-04"), today) == d("2026-08-03")
    assert ps.clamp_history_end(d("2026-09-01"), today) == d("2026-08-03")
    # 過去日はそのまま
    assert ps.clamp_history_end(d("2026-08-01"), today) == d("2026-08-01")


# ----------------------------------------------------------------------
# fetched_ranges（DB）
# ----------------------------------------------------------------------


def test_record_range_merges_in_db(store: Store):
    ps.record_range(store, "yahoo", "9433.T", d("2026-01-01"), d("2026-01-10"))
    ps.record_range(store, "yahoo", "9433.T", d("2026-01-11"), d("2026-01-20"))
    assert ps.get_ranges(store, "yahoo", "9433.T") == [
        (d("2026-01-01"), d("2026-01-20"))
    ]
    ps.record_range(store, "yahoo", "9433.T", d("2026-02-01"), d("2026-02-05"))
    assert ps.get_ranges(store, "yahoo", "9433.T") == [
        (d("2026-01-01"), d("2026-01-20")),
        (d("2026-02-01"), d("2026-02-05")),
    ]
    # 橋渡し範囲で全結合
    ps.record_range(store, "yahoo", "9433.T", d("2026-01-15"), d("2026-01-31"))
    assert ps.get_ranges(store, "yahoo", "9433.T") == [
        (d("2026-01-01"), d("2026-02-05"))
    ]


def test_record_range_isolated_by_source_id(store: Store):
    ps.record_range(store, "yahoo", "9433.T", d("2026-01-01"), d("2026-01-10"))
    ps.record_range(store, "yahoo", "GC=F", d("2026-03-01"), d("2026-03-05"))
    assert ps.get_ranges(store, "yahoo", "9433.T") == [
        (d("2026-01-01"), d("2026-01-10"))
    ]
    assert ps.get_ranges(store, "yahoo", "GC=F") == [
        (d("2026-03-01"), d("2026-03-05"))
    ]


def test_record_range_ignores_inverted(store: Store):
    ps.record_range(store, "yahoo", "X", d("2026-01-10"), d("2026-01-01"))
    assert ps.get_ranges(store, "yahoo", "X") == []


def test_missing_ranges_against_db(store: Store):
    assert ps.missing_ranges(store, "fx", "USD", d("2026-01-01"), d("2026-01-10")) == [
        (d("2026-01-01"), d("2026-01-10"))
    ]
    ps.record_range(store, "fx", "USD", d("2026-01-01"), d("2026-01-05"))
    assert ps.missing_ranges(store, "fx", "USD", d("2026-01-01"), d("2026-01-10")) == [
        (d("2026-01-06"), d("2026-01-10"))
    ]
    ps.record_range(store, "fx", "USD", d("2026-01-06"), d("2026-01-10"))
    assert (
        ps.missing_ranges(store, "fx", "USD", d("2026-01-01"), d("2026-01-10")) == []
    )


def test_prelisting_clamp_scenario(store: Store):
    """上場前区間も被覆記録すれば再取得しない（yahoo range=maxフォールバックの帰結）。"""
    # 2020-01-01 上場の銘柄に 2019 年を要求 → 全範囲を被覆記録（価格行なし）
    ps.record_range(store, "yahoo", "521A.T", d("2019-01-01"), d("2019-12-31"))
    assert (
        ps.missing_ranges(store, "yahoo", "521A.T", d("2019-06-01"), d("2019-12-31"))
        == []
    )


def test_fetched_today(store: Store):
    assert not ps.fetched_today(store, "toushin", "JP000:0001")
    ps.record_range(store, "toushin", "JP000:0001", d("2026-01-01"), d("2026-01-31"))
    assert ps.fetched_today(store, "toushin", "JP000:0001")
    # 別IDには影響しない
    assert not ps.fetched_today(store, "toushin", "JP000:0002")


# ----------------------------------------------------------------------
# daily_prices / spot_cache
# ----------------------------------------------------------------------


def test_save_daily_prices_roundtrip_and_upsert(store: Store):
    ps.save_daily_prices(
        store,
        "fx",
        "USD",
        {d("2026-08-01"): D("150.5"), d("2026-07-31"): D("160.24")},
        "JPY",
    )
    series, ccy = store.get_price_rows("fx", "USD")
    assert series == {"2026-07-31": D("160.24"), "2026-08-01": D("150.5")}
    assert ccy == "JPY"
    # 同一日付は上書き
    ps.save_daily_prices(store, "fx", "USD", {d("2026-08-01"): D("151")}, "JPY")
    series, _ = store.get_price_rows("fx", "USD")
    assert series["2026-08-01"] == D("151")
    assert len(series) == 2


def test_get_latest_price_returns_newest_row(store: Store):
    """現在値の解決は最新1件だけを引く（全期間を読むと銘柄数ぶん律速する）。"""
    assert store.get_latest_price("toushin", "X") is None
    ps.save_daily_prices(
        store,
        "toushin",
        "X",
        {d("2026-08-03"): D("12345"), d("2026-08-01"): D("11000"),
         d("2026-08-02"): D("12000")},
        "JPY",
    )
    assert store.get_latest_price("toushin", "X") == ("2026-08-03", D("12345"), "JPY")
    # 系列違いを取り違えない
    ps.save_daily_prices(store, "toushin", "Y", {d("2026-08-04"): D("999")}, "JPY")
    assert store.get_latest_price("toushin", "X")[1] == D("12345")
    assert store.get_latest_price("toushin", "Y")[1] == D("999")
    # get_price_rows（全期間）と最新値が一致すること
    series, ccy = store.get_price_rows("toushin", "X")
    assert (max(series), series[max(series)], ccy) == store.get_latest_price("toushin", "X")


def test_latest_price_date(store: Store):
    assert ps.latest_price_date(store, "toushin", "X") is None
    ps.save_daily_prices(
        store, "toushin", "X", {d("2026-08-01"): D("1"), d("2026-08-03"): D("2")}
    )
    assert ps.latest_price_date(store, "toushin", "X") == d("2026-08-03")


def test_spot_cache_ttl(store: Store):
    ps.put_spot(store, "yahoo", "9433.T", D("2882.5"), "JPY", now=1000.0)
    assert ps.get_cached_spot(store, "yahoo", "9433.T", ttl=300.0, now=1200.0) == (
        D("2882.5"),
        "JPY",
    )
    # TTL 超過で None
    assert ps.get_cached_spot(store, "yahoo", "9433.T", ttl=300.0, now=1301.0) is None
    # 未登録は None
    assert ps.get_cached_spot(store, "yahoo", "7203.T", now=1000.0) is None
    # 上書きで復活
    ps.put_spot(store, "yahoo", "9433.T", D("2900"), "USD", now=2000.0)
    assert ps.get_cached_spot(store, "yahoo", "9433.T", ttl=300.0, now=2100.0) == (
        D("2900"),
        "USD",
    )
