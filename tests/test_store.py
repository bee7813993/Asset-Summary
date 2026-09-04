from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    ImportBatch,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.core.store import ConflictError, Store


def _sec(name: str = "テスト工業", code: str | None = "9999", **kw) -> Security:
    defaults = dict(
        name=name,
        name_key=name.lower(),
        asset_class=AssetClass.STOCK_JP,
        code=code,
    )
    defaults.update(kw)
    return Security(**defaults)


def _snap(sec_id: int, acct_id: int, **kw) -> HoldingSnapshot:
    defaults = dict(
        account_id=acct_id,
        security_id=sec_id,
        as_of_date=date(2026, 8, 1),
        quantity=Decimal("100"),
        avg_cost=Decimal("1000"),
    )
    defaults.update(kw)
    return HoldingSnapshot(**defaults)


def test_account_get_or_create_idempotent(store: Store):
    a1 = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    a2 = store.get_or_create_account("架空証券")
    assert a1.id == a2.id
    assert a2.kind == "broker"


def test_security_roundtrip_and_resolution(store: Store):
    sec_id = store.create_security(_sec())
    assert store.resolve_security(code="9999") == sec_id
    assert store.resolve_security(name_key="テスト工業".lower()) == sec_id
    # alias resolution
    store.add_alias("てすとこうぎょう", sec_id)
    assert store.resolve_security(name_key="てすとこうぎょう") == sec_id
    # code has priority
    other = store.create_security(_sec(name="別銘柄", code="8888"))
    assert store.resolve_security(code="8888", name_key="てすとこうぎょう") == other


def test_snapshot_upsert_and_current_view(store: Store):
    acct = store.get_or_create_account("テスト証券")
    sec_id = store.create_security(_sec())
    store.upsert_snapshot(_snap(sec_id, acct.id, as_of_date=date(2026, 7, 1), quantity=Decimal("50")))
    store.upsert_snapshot(_snap(sec_id, acct.id, as_of_date=date(2026, 8, 1), quantity=Decimal("100")))
    cur = store.current_holdings()
    assert len(cur) == 1
    assert cur[0].quantity == Decimal("100")
    # same-day upsert replaces
    store.upsert_snapshot(_snap(sec_id, acct.id, as_of_date=date(2026, 8, 1), quantity=Decimal("120")))
    cur = store.current_holdings()
    assert len(cur) == 1
    assert cur[0].quantity == Decimal("120")
    assert len(store.all_snapshots()) == 2


def test_holdings_as_of_takes_the_last_snapshot_up_to_that_day(store: Store):
    """前日比の基準。その日までの最新スナップショットだけを返す。"""
    acct = store.get_or_create_account("テスト証券")
    sec_id = store.create_security(_sec())
    for day, qty in ((date(2026, 8, 1), "50"), (date(2026, 8, 5), "80"),
                     (date(2026, 8, 7), "100")):
        store.upsert_snapshot(_snap(sec_id, acct.id, as_of_date=day, quantity=Decimal(qty)))

    assert [s.quantity for s in store.holdings_as_of(date(2026, 8, 6))] == [Decimal("80")]
    assert [s.quantity for s in store.holdings_as_of(date(2026, 8, 5))] == [Decimal("80")]
    assert [s.quantity for s in store.holdings_as_of(date(2026, 8, 4))] == [Decimal("50")]
    # まだ記録の無い日には何も返さない（呼び出し側が遡及ルールで埋める）
    assert store.holdings_as_of(date(2026, 7, 31)) == []


def test_latest_snapshot_date(store: Store):
    """前日比の基準日を決めるための「データ側の最新地点」。"""
    acct = store.get_or_create_account("テスト証券")
    sec_id = store.create_security(_sec())
    assert store.latest_snapshot_date() is None
    store.upsert_snapshot(_snap(sec_id, acct.id, as_of_date=date(2026, 8, 1)))
    store.upsert_snapshot(_snap(sec_id, acct.id, as_of_date=date(2026, 8, 7)))
    assert store.latest_snapshot_date() == date(2026, 8, 7)


def test_lot_separation(store: Store):
    acct = store.get_or_create_account("テスト証券")
    sec_id = store.create_security(_sec())
    store.upsert_snapshot(_snap(sec_id, acct.id, lot_seq=0, quantity=Decimal("10")))
    store.upsert_snapshot(_snap(sec_id, acct.id, lot_seq=1, quantity=Decimal("20")))
    lots = store.latest_lots(acct.id, sec_id)
    assert [lot.lot_seq for lot in lots] == [0, 1]
    assert sum(lot.quantity for lot in lots) == Decimal("30")


def test_delete_security_with_snapshots_conflicts(store: Store):
    acct = store.get_or_create_account("テスト証券")
    sec_id = store.create_security(_sec())
    store.upsert_snapshot(_snap(sec_id, acct.id))
    with pytest.raises(ConflictError):
        store.delete_security(sec_id)


def test_batch_lifecycle_and_rollback(store: Store):
    acct = store.get_or_create_account("テスト証券")
    sec_id = store.create_security(_sec())
    batch = ImportBatch(id="batch-1", file_sha256="abc")
    store.create_batch(batch)
    store.upsert_snapshot(_snap(sec_id, acct.id, batch_id="batch-1"))
    store.update_batch("batch-1", status="committed", as_of_date=date(2026, 8, 1))
    assert store.find_committed_batch_by_sha("abc") is not None
    batches = store.list_batches()
    assert batches[0]["row_count"] == 1
    deleted = store.delete_batch("batch-1")
    assert deleted == 1
    assert store.current_holdings() == []
    assert store.find_committed_batch_by_sha("abc") is None


def test_duplicate_committed_sha_blocked(store: Store):
    store.create_batch(ImportBatch(id="b1", file_sha256="x", status="committed"))
    store.create_batch(ImportBatch(id="b2", file_sha256="x", status="previewed"))
    with pytest.raises(Exception):
        store.update_batch("b2", status="committed")


def test_price_rows_and_series_priority(store: Store):
    sec_id = store.create_security(
        _sec(
            code=None,
            name="テストファンド",
            name_key="てすとふぁんど",
            asset_class=AssetClass.FUND_JP,
            unit=Unit.KUCHI,
            price_unit_divisor=10000,
            price_source_type=PriceSourceType.TOUSHIN,
            price_source_ref="JP000:0001",
            price_source_status=PriceSourceStatus.LINKED,
        )
    )
    sec = store.get_security(sec_id)
    # mf_reported only → fallback series
    store.upsert_daily_price("mf_reported", str(sec_id), "2026-08-01", Decimal("10000"))
    series, ccy = store.price_series_for_security(sec)
    assert series == {"2026-08-01": Decimal("10000")}
    # provider series takes precedence
    store.upsert_daily_price("toushin", "JP000:0001", "2026-08-02", Decimal("11000"))
    series, ccy = store.price_series_for_security(sec)
    assert series == {"2026-08-02": Decimal("11000")}
    # manual overlays provider
    store.upsert_daily_price("manual", str(sec_id), "2026-08-03", Decimal("12000"))
    series, _ = store.price_series_for_security(sec)
    assert series["2026-08-03"] == Decimal("12000")
    assert ccy == "JPY"


# ----------------------------------------------------------------------
# 不動産: 手動評価の日次導出（core.re_index との結合）
# ----------------------------------------------------------------------


def _estate(store: Store, ref: str | None = None) -> Security:
    sec_id = store.create_security(
        _sec(
            code=None,
            name="自宅マンション",
            name_key="じたくまんしょん",
            asset_class=AssetClass.REAL_ESTATE,
            unit=Unit.UNIT,
            price_source_type=PriceSourceType.MANUAL,
            price_source_status=PriceSourceStatus.MANUAL,
            price_source_ref=ref,
        )
    )
    return store.get_security(sec_id)


def test_estate_without_index_interpolates_between_valuations(store: Store):
    sec = _estate(store)
    store.upsert_daily_price("manual", str(sec.id), "2026-01-01", Decimal("50000000"))
    store.upsert_daily_price("manual", str(sec.id), "2026-01-11", Decimal("51000000"))

    series, ccy = store.price_series_for_security(sec, end="2026-01-11")
    assert ccy == "JPY"
    # 疎な2点ではなく日次になっている（階段が消える）
    assert len(series) == 11
    assert series["2026-01-01"] == Decimal("50000000")
    assert series["2026-01-06"] == Decimal("50500000")
    assert series["2026-01-11"] == Decimal("51000000")


def test_estate_with_index_follows_it_and_extends(store: Store):
    sec = _estate(store, ref="re_index:nanto:condo")
    store.upsert_daily_price("manual", str(sec.id), "2026-01-01", Decimal("50000000"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-01-01", Decimal("100"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-07-01", Decimal("110"))

    series, ccy = store.price_series_for_security(sec, end="2026-07-01")
    # 指数の通貨（無次元なので JPY 固定で保存）を拾ってしまわないこと
    assert ccy == "JPY"
    assert series["2026-01-01"] == Decimal("50000000")
    assert series["2026-07-01"] == Decimal("55000000")


def test_estate_index_ref_is_ignored_when_unlinked(store: Store):
    # price_source_ref が指数以外（未連携投信から昇格した ISIN 等）なら無視する
    sec = _estate(store, ref="JP90C000AAA1")
    store.upsert_daily_price("manual", str(sec.id), "2026-01-01", Decimal("50000000"))
    series, _ = store.price_series_for_security(sec, end="2026-01-03")
    assert set(series.values()) == {Decimal("50000000")}


def test_estate_bounded_start_matches_unbounded(store: Store):
    """start は表示の窓。導出の基準になるアンカーを削ってはいけない。"""
    sec = _estate(store, ref="re_index:nanto:condo")
    store.upsert_daily_price("manual", str(sec.id), "2026-01-01", Decimal("50000000"))
    store.upsert_daily_price("manual", str(sec.id), "2026-03-01", Decimal("53000000"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-01-01", Decimal("100"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-03-01", Decimal("104"))

    full, _ = store.price_series_for_security(sec, end="2026-03-01")
    windowed, _ = store.price_series_for_security(
        sec, start="2026-02-20", end="2026-03-01"
    )
    assert windowed
    for day, value in windowed.items():
        assert full[day] == value


def test_estate_with_no_valuation_has_no_series(store: Store):
    """査定額が無ければ指数があっても系列は空（評価額は「不明」で 0 ではない）。"""
    sec = _estate(store, ref="re_index:nanto:condo")
    store.upsert_daily_price("re_index", "nanto:condo", "2026-01-01", Decimal("100"))
    store.upsert_daily_price("re_index", "nanto:condo", "2026-07-01", Decimal("110"))
    series, _ = store.price_series_for_security(sec, end="2026-07-01")
    assert series == {}


def test_settings_defaults(store: Store):
    s = store.get_settings()
    assert s["include_pension"] == "1"
    assert s["include_points"] == "1"
    store.set_setting("include_points", "0")
    assert store.get_settings()["include_points"] == "0"
