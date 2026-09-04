"""不動産の評価額導出（純関数）のテスト。DBもネットワークも使わない。"""

from __future__ import annotations

from decimal import Decimal

from asset_summary.core import re_index

D = Decimal


# 「素朴な式」= V_i * I(d)/I(t_i)。次のアンカーで不連続に飛ぶ方。
def naive(v0: Decimal, i0: Decimal, iu: Decimal) -> Decimal:
    return v0 * iu / i0


# ----------------------------------------------------------------------
# ref のパース
# ----------------------------------------------------------------------


def test_make_ref_roundtrips():
    ref = re_index.make_ref("nanto", "condo")
    assert ref == "re_index:nanto:condo"
    assert re_index.parse_ref(ref) == "nanto:condo"
    assert re_index.split_source_id("nanto:condo") == ("nanto", "condo")


def test_parse_ref_rejects_non_index_refs():
    # 未連携投信が manual に昇格したときの ISIN などを拾わないこと
    assert re_index.parse_ref(None) is None
    assert re_index.parse_ref("") is None
    assert re_index.parse_ref("JP90C000AAA1") is None
    assert re_index.parse_ref("re_index:") is None
    assert re_index.parse_ref("re_index:atlantis:condo") is None
    assert re_index.parse_ref("re_index:nanto:castle") is None


# ----------------------------------------------------------------------
# expand_monthly（月次 → 日次）
# ----------------------------------------------------------------------


MONTHLY = {"2025-11-01": D("100"), "2025-12-01": D("130")}


def test_expand_monthly_is_exact_on_observation_days():
    out = re_index.expand_monthly(MONTHLY, "2025-11-01", "2025-12-01")
    assert out["2025-11-01"] == D("100")
    assert out["2025-12-01"] == D("130")


def test_expand_monthly_ramps_evenly_within_the_month():
    # 11月は30日。(130-100)/30 = 1/日
    out = re_index.expand_monthly(MONTHLY, "2025-11-01", "2025-12-01")
    assert out["2025-11-02"] == D("101")
    assert out["2025-11-03"] == D("102")
    # 連続する2つの差分が等しい＝月内に段差が無い
    d1 = out["2025-11-03"] - out["2025-11-02"]
    d2 = out["2025-11-04"] - out["2025-11-03"]
    assert d1 == d2


def test_expand_monthly_absent_before_first_and_flat_after_last():
    out = re_index.expand_monthly(MONTHLY, "2025-10-28", "2026-01-05")
    # 最初の観測より前は backfill しない
    assert "2025-10-28" not in out
    assert "2025-10-31" not in out
    # 最終観測より後は定数（トレンドを外挿しない）
    assert out["2025-12-02"] == D("130")
    assert out["2026-01-05"] == D("130")


def test_expand_monthly_empty():
    assert re_index.expand_monthly({}, "2025-01-01", "2025-01-05") == {}


# ----------------------------------------------------------------------
# derive_series: アンカーを厳密に通ること
# ----------------------------------------------------------------------


def test_passes_exactly_through_every_anchor():
    anchors = {
        "2024-01-01": D("50000000"),
        "2025-01-01": D("52000000"),
        "2026-01-01": D("51000000"),
    }
    index = {
        "2024-01-01": D("100"),
        "2024-07-01": D("108"),
        "2025-01-01": D("104"),
        "2025-07-01": D("119"),
        "2026-01-01": D("112"),
    }
    out = re_index.derive_series(anchors, index, "2024-01-01", "2026-01-01")
    for day, value in anchors.items():
        assert out[day] == value


def test_does_not_use_the_naive_formula():
    # 素朴な式なら t1 で 120 に飛ぶ。チェーンリンクなら厳密に 110。
    anchors = {"2024-01-01": D("100"), "2024-02-01": D("110")}
    index = {"2024-01-01": D("100"), "2024-02-01": D("120")}
    out = re_index.derive_series(anchors, index, "2024-01-01", "2024-02-01")

    assert out["2024-02-01"] == D("110")
    # 前日も 120 付近ですら無い（＝残差が最後の1日で吸収されているのではない）
    assert D("109") < out["2024-01-31"] < D("111")


def test_follows_index_shape_between_anchors():
    # 指数が非線形（凸）なとき、導出値は素朴な曲線と純線形補間の「間」に来る。
    anchors = {"2024-01-01": D("100"), "2024-03-01": D("110")}
    index = {
        "2024-01-01": D("100"),
        "2024-02-01": D("130"),
        "2024-03-01": D("140"),
    }
    out = re_index.derive_series(anchors, index, "2024-01-01", "2024-03-01")

    mid = out["2024-02-01"]
    w = D(31) / D(60)  # 1/1 から 2/1 までの経過割合
    pure_linear = D("100") + D("10") * w
    naive_mid = naive(D("100"), D("100"), D("130"))

    assert mid == D("114.5")
    assert pure_linear < mid < naive_mid


# ----------------------------------------------------------------------
# derive_series: 範囲外（延長と遡及）
# ----------------------------------------------------------------------


def test_extends_past_the_last_anchor_with_the_index():
    anchors = {"2025-01-01": D("50000000")}
    index = {"2025-01-01": D("100"), "2025-07-01": D("110")}
    out = re_index.derive_series(anchors, index, "2025-01-01", "2025-07-01")
    assert out["2025-01-01"] == D("50000000")
    assert out["2025-07-01"] == D("55000000")  # +10%


def test_goes_flat_after_the_last_index_month():
    # 指数の公表が止まっている状況。最終指数月より先は横ばいになる。
    anchors = {"2025-01-01": D("50000000")}
    index = {"2025-01-01": D("100"), "2025-12-01": D("110")}
    out = re_index.derive_series(anchors, index, "2025-01-01", "2026-08-01")
    assert out["2025-12-01"] == D("55000000")
    assert out["2026-03-01"] == D("55000000")
    assert out["2026-08-01"] == D("55000000")


def test_scales_backwards_before_the_first_anchor():
    anchors = {"2025-07-01": D("55000000")}
    index = {"2025-01-01": D("100"), "2025-07-01": D("110")}
    out = re_index.derive_series(anchors, index, "2025-01-01", "2025-07-01")
    assert out["2025-01-01"] == D("50000000")


# ----------------------------------------------------------------------
# derive_series: 縮退ケース
# ----------------------------------------------------------------------


def test_no_anchors_returns_empty_even_with_an_index():
    # 指数は水準であって価格ではない。査定額が無い物件の評価額は「不明」で 0 ではない。
    index = {"2025-01-01": D("100"), "2025-07-01": D("110")}
    assert re_index.derive_series({}, index, "2025-01-01", "2025-07-01") == {}
    assert re_index.derive_series({}, {}, "2025-01-01", "2025-07-01") == {}


def test_single_anchor_without_index_is_flat():
    # 従来の挙動そのもの（単点ユーザーに退行が無いこと）
    anchors = {"2025-01-01": D("52000000")}
    out = re_index.derive_series(anchors, {}, "2024-06-01", "2025-06-01")
    assert set(out.values()) == {D("52000000")}


def test_without_index_interpolates_linearly_between_anchors():
    anchors = {"2024-01-01": D("100"), "2024-01-11": D("200")}
    out = re_index.derive_series(anchors, {}, "2023-12-30", "2024-01-13")
    assert out["2024-01-01"] == D("100")
    assert out["2024-01-06"] == D("150")  # ちょうど中間
    assert out["2024-01-11"] == D("200")
    # 両端の外側は平ら
    assert out["2023-12-30"] == D("100")
    assert out["2024-01-13"] == D("200")


def test_partial_index_degrades_per_interval_and_stays_continuous():
    # 指数は 2026-01 以降しか無い。最初の区間は線形、次の区間は指数ベース。
    anchors = {
        "2024-01-01": D("100"),
        "2026-01-01": D("110"),
        "2026-06-01": D("120"),
    }
    index = {"2026-01-01": D("100"), "2026-06-01": D("105")}
    out = re_index.derive_series(anchors, index, "2024-01-01", "2026-06-01")

    assert out["2024-01-01"] == D("100")
    assert out["2026-01-01"] == D("110")
    assert out["2026-06-01"] == D("120")
    # 区間の境目で折れない（左から近づいた値が境界値にほぼ一致）
    assert abs(out["2025-12-31"] - D("110")) < D("0.02")


def test_non_positive_index_levels_are_ignored():
    anchors = {"2024-01-01": D("100"), "2024-03-01": D("110")}
    broken = {"2024-01-01": D("0"), "2024-02-01": D("-5"), "2024-03-01": D("0")}
    assert re_index.derive_series(
        anchors, broken, "2024-01-01", "2024-03-01"
    ) == re_index.derive_series(anchors, {}, "2024-01-01", "2024-03-01")


def test_a_single_bad_index_point_is_dropped_not_fatal():
    anchors = {"2024-01-01": D("100"), "2024-03-01": D("110")}
    index = {"2024-01-01": D("100"), "2024-02-01": D("0"), "2024-03-01": D("140")}
    out = re_index.derive_series(anchors, index, "2024-01-01", "2024-03-01")
    # 2月の点は落ちるが、1月→3月の内挿で系列は成立する
    assert out["2024-01-01"] == D("100")
    assert out["2024-03-01"] == D("110")


def test_anchors_given_out_of_order():
    anchors = {"2024-03-01": D("110"), "2024-01-01": D("100")}
    out = re_index.derive_series(anchors, {}, "2024-01-01", "2024-03-01")
    assert out["2024-01-01"] == D("100")
    assert out["2024-03-01"] == D("110")


def test_zero_valued_anchor_does_not_blow_up():
    anchors = {"2024-01-01": D("0"), "2024-01-11": D("100")}
    index = {"2024-01-01": D("100"), "2024-02-01": D("110")}
    out = re_index.derive_series(anchors, index, "2024-01-01", "2024-01-11")
    assert out["2024-01-01"] == D("0")
    assert out["2024-01-11"] == D("100")


def test_derive_window_is_capped():
    # end だけ渡すと最初のアンカーまで遡るが、上限を超えて materialize しない
    anchors = {"1990-01-01": D("100"), "2026-01-01": D("200")}
    out = re_index.derive_series(anchors, {}, None, "2026-01-01")
    assert len(out) == re_index.MAX_DERIVE_DAYS + 1
    assert "1990-01-01" not in out


def test_empty_window_returns_empty():
    anchors = {"2024-01-01": D("100")}
    assert re_index.derive_series(anchors, {}, "2024-05-01", "2024-01-01") == {}


# ----------------------------------------------------------------------
# spot_from_anchor
# ----------------------------------------------------------------------


def test_spot_from_anchor_matches_the_series():
    anchors = {"2025-01-01": D("50000000")}
    index = {"2025-01-01": D("100"), "2025-07-01": D("110")}
    assert re_index.spot_from_anchor(anchors, index, "2025-07-01") == D("55000000")
    assert re_index.spot_from_anchor({}, index, "2025-07-01") is None
