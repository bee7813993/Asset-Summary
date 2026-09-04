"""取引台帳とスナップショットの突合（引き算による期首ロットの復元）の検証。

このファイルの要は test_partial_coverage_with_a_sell_inside_the_window。
「Q_A×C_A から買付コストを引く」という素朴な式は売却があると壊れるので、
その反例を固定してある。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from asset_summary.core.cost_basis import (
    Anchor,
    build_anchors,
    reconcile_all,
    reconcile_closed,
    reconcile_group,
)
from asset_summary.core.models import (
    AssetClass,
    Coverage,
    HoldingSnapshot,
    PriceSourceStatus,
    Security,
    Unit,
)
from tests.fixtures.tx_factories import (
    buy,
    dividend,
    reinvest,
    roc,
    sell,
    split,
    transfer_in,
)

D = Decimal


def anchor(qty, avg_cost=None, *, divisor=1, as_of="2026-08-01", **kw) -> Anchor:
    return Anchor(
        account_id=1,
        security_id=10,
        as_of_date=date.fromisoformat(as_of),
        quantity=D(str(qty)),
        avg_cost=None if avg_cost is None else D(str(avg_cost)),
        divisor=divisor,
        **kw,
    )


def _codes(result) -> set[str]:
    return {w.code for w in result.warnings}


# ----------------------------------------------------------------------
# 完全被覆
# ----------------------------------------------------------------------


def test_full_coverage_computes_the_average_from_the_ledger_alone():
    txs = [buy("2026-01-05", 100, 2500), buy("2026-02-10", 100, 2700)]
    result = reconcile_group(txs, anchor(200, 2600))
    assert result.coverage is Coverage.FULL
    assert result.avg_cost == D("2600")
    assert result.residual_quantity == 0
    assert result.covered_quantity == D("200")
    assert result.applies_to_pl


def test_full_coverage_includes_buy_fees_in_the_cost_basis():
    txs = [buy("2026-01-05", 100, 2500, fee=275)]
    result = reconcile_group(txs, anchor(100))
    # (250,000 + 275) / 100
    assert result.avg_cost == D("2502.75")


def test_full_coverage_works_even_when_the_snapshot_has_no_avg_cost():
    """MF に取得単価が無い保有こそ、この機能でいちばん得をする。

    現状そういうロットは損益計算から丸ごと外れている（pl_excluded_count）。
    取引履歴が全期間を覆っていれば、初めて損益に載せられる。
    """
    txs = [buy("2026-01-05", 100, 2500), buy("2026-02-10", 100, 2700)]
    result = reconcile_group(txs, anchor(200, avg_cost=None))
    assert result.coverage is Coverage.FULL
    assert result.avg_cost == D("2600")
    assert result.applies_to_pl


def test_position_zeroed_mid_window_counts_as_full_coverage():
    """途中で一度ゼロになれば、その前の履歴が無くても以降は完全に説明できる。

    b == 0 ⟺ 残余ゼロ ⟺ 完全被覆 という不変条件の確認。
    """
    txs = [
        buy("2026-01-05", 100, 2000),
        sell("2026-02-10", 100, 2200),      # ここでゼロ
        buy("2026-03-11", 50, 3000),
    ]
    result = reconcile_group(txs, anchor(50, 3000))
    assert result.coverage is Coverage.FULL
    assert result.residual_quantity == 0
    assert result.avg_cost == D("3000")


# ----------------------------------------------------------------------
# 部分被覆 — 引き算の中核
# ----------------------------------------------------------------------


def test_partial_coverage_recovers_the_opening_lot():
    # 期首 200株@1250 + 期間内 100株@2000 → 300株、加重平均 1500
    txs = [buy("2026-01-05", 100, 2000)]
    result = reconcile_group(txs, anchor(300, 1500))
    assert result.coverage is Coverage.PARTIAL
    assert result.residual_quantity == D("200")
    assert result.residual_avg_cost == D("1250")
    assert result.covered_quantity == D("100")


def test_partial_coverage_with_a_sell_inside_the_window():
    """売却が挟まると単純な引き算は壊れる。

    期首 100株@1000 → 期間内に 100株@2000 買い → 50株 売り。
    MF は Q_A=150 / C_A=1500（総原価 225,000）を報告する。
    素朴な式 (225,000 − 200,000) / 100 は期首単価 250 を出すが、真の値は 1000。
    """
    txs = [buy("2026-01-05", 100, 2000), sell("2026-02-10", 50, 2400)]
    result = reconcile_group(txs, anchor(150, 1500))
    assert result.coverage is Coverage.PARTIAL
    assert result.residual_avg_cost == D("1000")
    assert result.residual_avg_cost != D("250")          # 素朴な引き算の答え
    # 期首ロットも売却で按分して減る（200株中50株売却 → 3/4 が残る）
    assert result.residual_quantity == D("75")
    assert result.covered_quantity == D("75")


def test_partial_coverage_average_equals_the_snapshot_by_construction():
    """部分被覆では再計算値は MF と一致する（循環）。

    残余原価を Q_A×C_A から逆算する以上これは恒等式であって近似ではない。
    だから損益への反映は完全被覆に限る。この性質を消さないよう固定しておく。
    """
    txs = [buy("2026-01-05", 100, 2000), sell("2026-02-10", 50, 2400)]
    result = reconcile_group(txs, anchor(150, 1500))
    assert result.avg_cost == D("1500")                  # ＝ MF の値そのもの
    assert not result.applies_to_pl                      # 損益には反映しない


def test_partial_without_anchor_cost_cannot_solve_the_residual():
    txs = [buy("2026-01-05", 100, 2000)]
    result = reconcile_group(txs, anchor(300, avg_cost=None))
    assert result.coverage is Coverage.PARTIAL_UNCOSTED
    assert result.avg_cost is None
    assert "NO_ANCHOR_COST" in _codes(result)
    assert not result.applies_to_pl


# ----------------------------------------------------------------------
# 不整合の検出
# ----------------------------------------------------------------------


def test_negative_residual_is_detected_and_blocks_the_override():
    """CSV がスナップショットより多く買っている＝二重取込か銘柄誤照合。"""
    txs = [buy("2026-01-05", 400, 2000)]
    result = reconcile_group(txs, anchor(300, 1500))
    assert result.coverage is Coverage.UNRECONCILED
    assert "NEGATIVE_RESIDUAL" in _codes(result)
    assert result.avg_cost is None
    assert not result.applies_to_pl


def test_negative_residual_cost_is_detected():
    # 期間内に高値で大量に買っており、逆算すると期首単価が負になる
    txs = [buy("2026-01-05", 100, 5000)]
    result = reconcile_group(txs, anchor(200, 1000))
    assert result.coverage is Coverage.UNRECONCILED
    assert "NEGATIVE_RESIDUAL_COST" in _codes(result)


def test_transactions_after_the_snapshot_are_excluded_and_warned():
    txs = [buy("2026-01-05", 100, 2000), buy("2026-09-01", 50, 3000)]
    result = reconcile_group(txs, anchor(100, 2000, as_of="2026-08-01"))
    assert "TX_AFTER_SNAPSHOT" in _codes(result)
    assert result.coverage is Coverage.FULL
    assert result.tx_count == 1


def test_currency_mismatch_blocks_the_override():
    """円建て列を外貨建て銘柄に当ててしまうと約150倍ずれる。黙って通さない。"""
    txs = [buy("2026-01-05", 10, 150, currency="JPY")]
    result = reconcile_group(txs, anchor(10, 150, currency="USD"))
    assert result.coverage is Coverage.UNRECONCILED
    assert "CURRENCY_MISMATCH" in _codes(result)
    assert result.avg_cost is None


# ----------------------------------------------------------------------
# 実現損益
# ----------------------------------------------------------------------


def test_realized_pl_excludes_withholding_tax_but_subtracts_the_sell_fee():
    """源泉徴収税額は利益にかかる税であって取得費ではない。

    引いてしまうと特定口座年間取引報告書と突き合わせられなくなる。
    """
    txs = [buy("2026-01-05", 100, 1500), sell("2026-03-03", 50, 2500, fee=500, tax=1000)]
    result = reconcile_group(txs, anchor(50, 1500))
    sale = result.realized[0]
    # 125,000 − 500 − (50 × 1500) = 49,500
    assert sale.realized == D("49500")
    assert sale.cost == D("75000")
    assert sale.tax == D("1000")
    assert result.realized_pl == D("49500")
    assert result.withheld_tax == D("1000")


def test_selling_does_not_change_the_average_cost():
    txs = [buy("2026-01-05", 100, 2000), sell("2026-02-10", 40, 3000)]
    result = reconcile_group(txs, anchor(60, 2000))
    assert result.avg_cost == D("2000")


def test_no_sales_means_no_realized_pl():
    result = reconcile_group([buy("2026-01-05", 100, 2000)], anchor(100, 2000))
    assert result.realized_pl is None
    assert result.realized == []


# ----------------------------------------------------------------------
# 分割・再投資・分配金
# ----------------------------------------------------------------------


def test_split_by_ratio_preserves_the_cost_pool():
    txs = [buy("2026-01-05", 100, 3000), split("2026-02-01", ratio=3)]
    result = reconcile_group(txs, anchor(300, 1000))
    assert result.coverage is Coverage.FULL
    assert result.avg_cost == D("1000")          # 300,000 / 300


def test_split_as_a_quantity_delta_matches_the_ratio_form():
    by_ratio = reconcile_group(
        [buy("2026-01-05", 100, 3000), split("2026-02-01", ratio=3)], anchor(300, 1000)
    )
    by_delta = reconcile_group(
        [buy("2026-01-05", 100, 3000), split("2026-02-01", delta=200)], anchor(300, 1000)
    )
    assert by_ratio.avg_cost == by_delta.avg_cost
    assert by_ratio.residual_quantity == by_delta.residual_quantity


def test_split_inside_a_partial_window_solves_the_opening_in_pre_split_units():
    # 期首 100株、期間内に 1:2 分割 → 200株。買付は無い。
    txs = [split("2026-02-01", ratio=2)]
    result = reconcile_group(txs, anchor(200, 500))
    assert result.coverage is Coverage.PARTIAL
    assert result.residual_quantity == D("200")   # 分割後の株数で残る
    assert result.residual_avg_cost == D("1000")  # 分割前の単価で逆算される


def test_reinvestment_increases_both_quantity_and_cost():
    txs = [buy("2026-01-05", 10000, 16000, divisor=10000),
           reinvest("2026-05-02", 500, 16800, divisor=10000)]
    result = reconcile_group(txs, anchor(10500, divisor=10000))
    assert result.coverage is Coverage.FULL
    # (16,000 + 840) / 10,500 口 × 10,000 = 16,038.09…
    assert result.avg_cost == pytest.approx(D("16038.0952"), abs=D("0.001"))


def test_dividend_moves_neither_quantity_nor_cost():
    txs = [buy("2026-01-05", 100, 2000), dividend("2026-06-30", 4500, tax=914)]
    result = reconcile_group(txs, anchor(100, 2000))
    assert result.avg_cost == D("2000")
    assert result.income_total == D("4500")
    assert result.withheld_tax == D("914")
    assert result.realized_pl is None


def test_return_of_capital_reduces_cost_without_touching_quantity():
    """特別分配金は元本の払い戻し。入れないと毎月分配型で MF と食い違う。"""
    txs = [buy("2026-01-05", 10000, 16000, divisor=10000), roc("2026-05-02", 2000)]
    result = reconcile_group(txs, anchor(10000, divisor=10000))
    assert result.coverage is Coverage.FULL
    # (16,000 − 2,000) / 10,000 口 × 10,000 = 14,000
    assert result.avg_cost == D("14000")


def test_transfer_in_without_a_cost_is_warned():
    txs = [transfer_in("2026-01-05", 100)]
    result = reconcile_group(txs, anchor(100, 2000))
    assert "TRANSFER_WITHOUT_COST" in _codes(result)


# ----------------------------------------------------------------------
# 投信の divisor
# ----------------------------------------------------------------------


def test_fund_divisor_is_applied_to_the_cost():
    txs = [buy("2026-01-05", 100000, 12345, divisor=10000)]
    result = reconcile_group(txs, anchor(100000, divisor=10000))
    assert result.coverage is Coverage.FULL
    assert result.avg_cost == D("12345")
    assert result.events[0].amount == D("123450")   # 100,000口 × 12,345 / 10,000


# ----------------------------------------------------------------------
# 取得日
# ----------------------------------------------------------------------


def test_acquired_on_comes_from_the_ledger_when_fully_covered():
    txs = [buy("2026-01-05", 100, 2000), buy("2026-03-11", 100, 2200)]
    result = reconcile_group(txs, anchor(200, 2100))
    assert result.acquired_on == date(2026, 1, 5)
    assert result.acquired_on_src == "csv"


def test_acquired_on_falls_back_to_the_mf_value_when_partially_covered():
    """残余は CSV より前から持っているので、CSV の最古の買付は最古ではない。

    MF PDF が取得日をすでに raw に持っているので、そちらを使う。
    """
    txs = [buy("2026-01-05", 100, 2000)]
    result = reconcile_group(
        txs, anchor(300, 1500, mf_acquired_on=date(2019, 3, 14))
    )
    assert result.acquired_on == date(2019, 3, 14)
    assert result.acquired_on_src == "mf_raw"


# ----------------------------------------------------------------------
# 売り切った銘柄
# ----------------------------------------------------------------------


def test_closed_position_reports_realized_pl_without_an_override():
    txs = [buy("2026-01-05", 100, 2000), sell("2026-03-03", 100, 2500)]
    result = reconcile_closed(
        txs, account_id=1, security_id=10, as_of_date=date(2026, 8, 1)
    )
    assert result.realized_pl == D("50000")
    assert "CLOSED_POSITION" in _codes(result)
    assert not result.applies_to_pl


def test_closed_position_that_does_not_zero_out_is_flagged():
    txs = [buy("2026-01-05", 100, 2000), sell("2026-03-03", 40, 2500)]
    result = reconcile_closed(
        txs, account_id=1, security_id=10, as_of_date=date(2026, 8, 1)
    )
    assert "CLOSED_MISMATCH" in _codes(result)


# ----------------------------------------------------------------------
# 錨の組み立て（ロット粒度）
# ----------------------------------------------------------------------


def _sec(security_id=10, divisor=1, currency="JPY") -> Security:
    return Security(
        id=security_id, code=None, name="架空商事", name_key="架空商事",
        asset_class=AssetClass.STOCK_JP, currency=currency, unit=Unit.SHARE,
        price_unit_divisor=divisor, price_source_status=PriceSourceStatus.UNLINKED,
    )


def _lot(lot_seq=0, qty="100", avg=None, raw=None, as_of="2026-08-01") -> HoldingSnapshot:
    return HoldingSnapshot(
        account_id=1, security_id=10, lot_seq=lot_seq,
        as_of_date=date.fromisoformat(as_of),
        quantity=D(qty), avg_cost=None if avg is None else D(avg),
        origin="mf", raw=raw or {},
    )


def test_single_lot_gets_lot_scope():
    anchors, _ = build_anchors([_lot(avg="1500")], {10: _sec()})
    assert len(anchors) == 1
    assert anchors[0].lot_scope == "lot"
    assert anchors[0].quantity == D("100")
    assert anchors[0].avg_cost == D("1500")


def test_multiple_lots_are_grouped_with_a_weighted_average():
    lots = [_lot(0, "100", "1000"), _lot(1, "300", "2000")]
    anchors, _ = build_anchors(lots, {10: _sec()})
    a = anchors[0]
    assert a.lot_scope == "group"
    assert a.quantity == D("400")
    assert a.avg_cost == D("1750")       # (100×1000 + 300×2000) / 400
    # 合計原価が保存されること
    assert a.quantity * a.avg_cost == D("700000")


def test_group_scope_never_overrides_pl():
    """複数ロットに 1 つの平均単価を被せると各ロットの表示が実態とずれる。

    合計原価は正しくても、ロットごとの内訳は保証できないので損益には触れない。
    """
    lots = [_lot(0, "100", "1000"), _lot(1, "100", "2000")]
    anchors, _ = build_anchors(lots, {10: _sec()})
    result = reconcile_group([buy("2026-01-05", 200, 1500)], anchors[0])
    assert result.coverage is Coverage.FULL
    assert not result.applies_to_pl      # group scope なので反映しない


def test_mixed_cost_availability_refuses_to_produce_an_average():
    lots = [_lot(0, "100", "1000"), _lot(1, "100", None)]
    anchors, warnings = build_anchors(lots, {10: _sec()})
    assert anchors[0].avg_cost is None
    assert any(w.code == "LOT_MIXED_COST" for w in warnings)


def test_mf_acquired_on_is_read_from_the_snapshot_raw():
    """MF PDF が取り込んだ取得日は raw に入ったまま誰も読んでいなかった。"""
    lot = _lot(avg="1500", raw={"meta": {"acquired_on": "2019/03/14"}})
    anchors, _ = build_anchors([lot], {10: _sec()})
    assert anchors[0].mf_acquired_on == date(2019, 3, 14)


def test_sold_out_lots_are_not_anchored():
    anchors, _ = build_anchors([_lot(qty="0", avg="1500")], {10: _sec()})
    assert anchors == []


# ----------------------------------------------------------------------
# 全体
# ----------------------------------------------------------------------


def test_reconcile_all_covers_both_held_and_closed_positions():
    lots = [_lot(avg="1500")]
    securities = {10: _sec(), 11: _sec(11)}
    txs = [
        buy("2026-01-05", 100, 1500),
        buy("2026-02-01", 50, 900, security_id=11),
        sell("2026-03-01", 50, 1200, security_id=11),
    ]
    results, _warnings = reconcile_all(txs, lots, securities)
    by_sec = {r.security_id: r for r in results}
    assert by_sec[10].coverage is Coverage.FULL
    assert by_sec[11].realized_pl == D("15000")


def test_reconcile_all_ignores_unmatched_transactions():
    lots = [_lot(avg="1500")]
    txs = [buy("2026-01-05", 100, 1500)]
    txs[0].security_id = None
    results, _ = reconcile_all(txs, lots, {10: _sec()})
    assert results == []
