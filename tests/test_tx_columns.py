"""ヘッダ検出と列割当の検証。

このファイルの要は test_adversarial_price_columns —
「ヘッダ語彙が当たったから正しい」ではなく「算術が合うから正しい」を
確かめるもので、書式非依存性が本当に成り立っているかはここで決まる。
"""

from __future__ import annotations

import pytest

from asset_summary.importers.txlog.columns import hungarian
from asset_summary.importers.txlog.contracts import (
    EMPTY_UNIVERSE,
    SAMPLE_ROWS,
    sample_rows,
    CanonicalField as F,
)
from asset_summary.importers.txlog.engine import detect_format, fingerprint, parse_grid
from asset_summary.importers.txlog.grid import load_grid, load_pasted
from asset_summary.importers.txlog.header import data_row_indices, find_region
from tests.fixtures.tx_grids import (
    DAIWA_LIKE_UNIVERSE,
    LAYOUT_G,
    LAYOUT_H,
    FUND_UNIVERSE,
    LAYOUT_A,
    LAYOUT_A_REORDERED,
    LAYOUT_B,
    LAYOUT_C,
    LAYOUT_D,
    LAYOUT_E,
    LAYOUT_F,
    STOCK_UNIVERSE,
    csv_bytes,
    xlsx_bytes,
)


def _detect(text: str, universe=EMPTY_UNIVERSE, encoding: str = "utf-8"):
    grid = load_grid(csv_bytes(text, encoding))
    return grid, detect_format(grid, universe)


def _map(fmt) -> dict[str, int]:
    return {c.field.value: c.index for c in fmt.columns if c.field is not F.IGNORE}


# ----------------------------------------------------------------------
# ヘッダ行の検出
# ----------------------------------------------------------------------


def test_header_found_after_preamble_lines():
    grid = load_grid(csv_bytes(LAYOUT_A))
    region = find_region(grid, EMPTY_UNIVERSE)
    assert region.header_row == 3
    assert region.headers[0] == "約定日"
    assert ("preamble" in {r for _, r in region.dropped}) or any(
        why == "preamble" for _, why in region.dropped
    )


def test_trailing_total_row_is_dropped():
    grid = load_grid(csv_bytes(LAYOUT_A))
    region = find_region(grid, EMPTY_UNIVERSE)
    assert any(why == "total_row" for _, why in region.dropped)
    assert len(data_row_indices(region)) == 5


def test_headerless_layout_is_recognised_as_headerless():
    grid = load_grid(csv_bytes(LAYOUT_B))
    region = find_region(grid, FUND_UNIVERSE)
    assert region.header_row is None
    # データ行が 1 行も捨てられていないこと（先頭行をヘッダと誤認しない）
    assert len(data_row_indices(region)) == 4


def test_repeated_header_mid_file_is_dropped():
    text = (
        "約定日,銘柄名,取引区分,数量,単価\n"
        "2026/01/05,架空商事,買付,100,2500\n"
        "約定日,銘柄名,取引区分,数量,単価\n"
        "2026/02/10,架空商事,売却,40,2800\n"
    )
    grid = load_grid(csv_bytes(text))
    region = find_region(grid, EMPTY_UNIVERSE)
    assert any(why == "repeated_header" for _, why in region.dropped)
    assert len(data_row_indices(region)) == 2


def test_unit_row_below_header_is_dropped():
    text = (
        "約定日,銘柄名,数量,単価\n"
        "(年月日),(名称),(株),(円)\n"
        "2026/01/05,架空商事,100,2500\n"
        "2026/02/10,架空商事,40,2800\n"
    )
    grid = load_grid(csv_bytes(text))
    region = find_region(grid, EMPTY_UNIVERSE)
    assert any(why == "unit_row" for _, why in region.dropped)
    assert len(data_row_indices(region)) == 2


def test_two_row_header_is_joined():
    grid = load_grid(csv_bytes(LAYOUT_F))
    region = find_region(grid, EMPTY_UNIVERSE)
    assert region.headers[0] == "約定日"
    assert len(data_row_indices(region)) == 3


# ----------------------------------------------------------------------
# 列の割当
# ----------------------------------------------------------------------


def test_layout_a_maps_every_column():
    _grid, fmt = _detect(LAYOUT_A, STOCK_UNIVERSE, "cp932")
    mapping = _map(fmt)
    assert mapping == {
        "trade_date": 0, "settle_date": 1, "security_code": 2, "security_name": 3,
        "tx_type": 4, "quantity": 5, "unit_price": 6, "gross_amount": 7,
        "fee": 8, "tax": 9, "net_amount": 10, "account_type": 11,
    }
    assert fmt.divisor == 1
    assert fmt.sign_convention == "by_type"
    assert fmt.confidence > 0.8


def test_layout_a_identities_all_pass():
    _grid, fmt = _detect(LAYOUT_A, STOCK_UNIVERSE, "cp932")
    checks = {c.name: c for c in fmt.identities}
    assert checks["qty*price=gross"].pass_rate == 1.0
    assert checks["net-gross=fee+tax"].pass_rate == 1.0


def test_headerless_layout_uses_known_securities_to_find_the_name_column():
    """ヘッダが無くても、既存銘柄に解決できる列が銘柄列だと分かる。"""
    _grid, fmt = _detect(LAYOUT_B, FUND_UNIVERSE)
    mapping = _map(fmt)
    assert mapping["security_name"] == 1
    assert mapping["trade_date"] == 0
    assert mapping["quantity"] == 3


def test_fund_divisor_is_discovered_by_arithmetic():
    """投信の 1万口あたり基準価額は、語彙ではなく算術で判明する。"""
    _grid, fmt = _detect(LAYOUT_B, FUND_UNIVERSE)
    assert fmt.divisor == 10000
    assert _map(fmt)["unit_price"] == 4
    check = next(c for c in fmt.identities if c.name == "qty*price=gross")
    assert check.pass_rate == 1.0


def test_signed_quantity_layout_without_a_type_column():
    _grid, fmt = _detect(LAYOUT_C)
    assert fmt.sign_convention == "signed_quantity"
    assert "tx_type" not in _map(fmt)
    assert _map(fmt)["quantity"] == 3


def test_adversarial_price_columns_resolved_by_arithmetic():
    """『取得単価』と『単価』が並ぶとき、検算が合う方だけを選ぶ。

    ヘッダ語彙はどちらも単価に見える。数量×単価＝約定代金 が成り立つのは
    後者だけなので、算術が決め手になる — これが書式非依存性の実体。
    """
    _grid, fmt = _detect(LAYOUT_E)
    assert _map(fmt)["unit_price"] == 5          # '単価'（検算が合う）
    assert _map(fmt).get("unit_price") != 4      # '取得単価'（合わない）
    check = next(c for c in fmt.identities if c.name == "qty*price=gross")
    assert check.pass_rate == 1.0
    # 選ばれなかった候補は代替として残り、UI で選び直せる
    col4 = next(c for c in fmt.columns if c.index == 4)
    assert col4.field is F.IGNORE or col4.alternatives


def test_reordered_columns_with_an_extra_column_still_map():
    _grid, fmt = _detect(LAYOUT_D, STOCK_UNIVERSE)
    mapping = _map(fmt)
    assert mapping["trade_date"] == 5
    assert mapping["quantity"] == 4
    assert mapping["security_name"] == 1
    assert mapping["note"] == 0


def test_leading_code_in_name_column_is_split():
    _grid, fmt = _detect(LAYOUT_D, STOCK_UNIVERSE)
    name_col = next(c for c in fmt.columns if c.field is F.SECURITY_NAME)
    assert name_col.split_leading_code


def test_amount_column_is_not_mistaken_for_a_security_code():
    """4 桁の金額列が normalize_code を通ってしまう問題への歯止め。"""
    text = (
        "約定日,銘柄名,取引区分,数量,単価,約定代金\n"
        "2026/01/05,架空商事,買付,2,1500,3000\n"
        "2026/02/10,架空商事,買付,3,1200,3600\n"
        "2026/03/11,架空商事,売却,1,1800,1800\n"
    )
    _grid, fmt = _detect(text, STOCK_UNIVERSE)
    assert "security_code" not in _map(fmt)


def test_pasted_grid_is_detected_like_a_file():
    grid = load_pasted(
        "約定日\t銘柄名\t取引区分\t数量\t単価\t約定代金\n"
        "2026/01/05\t架空商事\t買付\t100\t2500\t250000\n"
        "2026/02/10\t架空商事\t売却\t40\t2800\t112000\n"
        "2026/03/11\t架空商事\t買付\t60\t2900\t174000\n"
    )
    fmt = detect_format(grid, STOCK_UNIVERSE)
    assert _map(fmt)["quantity"] == 3
    assert next(c for c in fmt.identities if c.name == "qty*price=gross").pass_rate == 1.0


def test_xlsx_typed_dates_are_used():
    pytest.importorskip("openpyxl")
    from datetime import date

    data = xlsx_bytes(
        [
            ["約定日", "銘柄名", "取引区分", "数量", "単価", "約定代金"],
            [date(2026, 1, 5), "架空商事", "買付", 100, 2500, 250000],
            [date(2026, 2, 10), "架空商事", "売却", 40, 2800, 112000],
            [date(2026, 3, 11), "架空商事", "買付", 60, 2900, 174000],
        ]
    )
    grid = load_grid(data)
    fmt = detect_format(grid, STOCK_UNIVERSE)
    assert _map(fmt)["trade_date"] == 0
    assert _map(fmt)["quantity"] == 3


# ----------------------------------------------------------------------
# 利用者による上書き
# ----------------------------------------------------------------------


def test_user_override_wins_over_detection():
    grid = load_grid(csv_bytes(LAYOUT_E))
    fmt = detect_format(grid, EMPTY_UNIVERSE, overrides={4: "unit_price"})
    assert _map(fmt)["unit_price"] == 4


# ----------------------------------------------------------------------
# 指紋（書式プロファイル）
# ----------------------------------------------------------------------


def test_fingerprint_is_stable_under_column_reordering():
    a = fingerprint(("約定日", "銘柄名", "数量", "単価"))
    b = fingerprint(("単価", "数量", "銘柄名", "約定日"))
    assert a == b and a is not None


def test_fingerprint_changes_when_a_column_is_added():
    a = fingerprint(("約定日", "銘柄名", "数量", "単価"))
    b = fingerprint(("約定日", "銘柄名", "数量", "単価", "備考"))
    assert a != b


def test_reordered_layout_gets_a_different_fingerprint_but_still_maps():
    _g1, f1 = _detect(LAYOUT_A, STOCK_UNIVERSE, "cp932")
    _g2, f2 = _detect(LAYOUT_A_REORDERED, STOCK_UNIVERSE)
    assert f1.fingerprint != f2.fingerprint          # 列が増えているので別書式
    assert _map(f2)["quantity"] == 3                 # それでも判定はできる
    assert _map(f2)["trade_date"] == 1


# ----------------------------------------------------------------------
# ハンガリアン法
# ----------------------------------------------------------------------


def test_hungarian_finds_the_optimal_assignment():
    # 貪欲だと行0が列0を取って全体で損をする配置
    cost = [[1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [3.0, 6.0, 9.0]]
    picks = hungarian(cost)
    assert sorted(picks) == [0, 1, 2]
    assert sum(cost[i][picks[i]] for i in range(3)) == 10.0  # 最小は (2,1,0)=3+4+3


def test_hungarian_handles_more_columns_than_rows():
    cost = [[5.0, 1.0, 9.0], [4.0, 8.0, 2.0]]
    picks = hungarian(cost)
    assert picks[0] == 1 and picks[1] == 2


def test_hungarian_on_empty_input():
    assert hungarian([]) == []


# ----------------------------------------------------------------------
# 値と単位を別列に分け、空欄を "-" で埋める書式（レイアウト G）
#
# 実ファイルで最初に外した書式。原因は "-" を「値がある」と数えていたことで、
# 全部日付の列が「日付率 31%」に見えて拒否され、別のフィールドに流れていた。
# ----------------------------------------------------------------------


def test_null_placeholders_do_not_hide_a_date_column():
    """7 割が "-" でも、残りが全部日付なら日付列と判る。"""
    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    region = find_region(grid, DAIWA_LIKE_UNIVERSE)
    rows = data_row_indices(region)
    from asset_summary.importers.txlog.shapes import column_stats

    st = column_stats([grid.cell(r, 0) for r in rows], DAIWA_LIKE_UNIVERSE)
    assert st.n_nonempty == len(rows)      # "-" も非空ではある
    assert st.n_present < st.n_nonempty    # が、値としては数えない
    assert st.date_rate == 1.0             # 値のあるものは全部日付


def test_daiwa_like_layout_maps_the_core_columns():
    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    fmt = detect_format(grid, DAIWA_LIKE_UNIVERSE)
    mapping = _map(fmt)
    assert mapping["trade_date"] == 0
    assert mapping["settle_date"] == 1
    assert mapping["security_name"] == 2
    assert mapping["tx_type"] == 3
    assert mapping["quantity"] == 4
    assert mapping["unit_price"] == 6


def test_settlement_amount_is_net_not_gross():
    """『精算金額』は受渡金額。約定代金の列が無い書式で取り違えない。

    数量×単価がたまたま一部の行で精算金額と一致するため、検算の合格率だけで
    順位を付けると約定代金に化ける。合格していない検算は順位に使わない。
    """
    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    fmt = detect_format(grid, DAIWA_LIKE_UNIVERSE)
    mapping = _map(fmt)
    assert mapping["net_amount"] == 8
    assert "gross_amount" not in mapping


def test_unit_column_supplies_the_currency():
    """単位列の『米ドル / 円』を通貨として拾う。

    拾わないと外貨建ての取引が黙って円扱いになり、約150倍ずれたまま
    取得原価に紛れ込む。
    """
    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    fmt = detect_format(grid, DAIWA_LIKE_UNIVERSE)
    assert _map(fmt).get("currency") in (7, 9)

    result = parse_grid(grid, DAIWA_LIKE_UNIVERSE, fmt=fmt)
    currencies = {t.currency for t in result.transactions}
    assert currencies == {"JPY", "USD"}


def test_full_width_space_in_tx_type_is_classified():
    """『売　付』『買　付』『預　入』のような全角スペース入りを取り違えない。"""
    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    result = parse_grid(grid, DAIWA_LIKE_UNIVERSE)
    kinds = [t.tx_type for t in result.transactions]
    assert "sell" in kinds and "buy" in kinds
    assert "transfer_in" in kinds          # 預　入（証券の入庫）
    assert kinds.count("reinvest") == 3    # 再投資 / 再投資買付


def test_rows_without_a_trade_date_are_still_importable():
    """配当・振替に約定日が無いのは異常ではない。受渡日で代用し減点しない。

    減点すると、この書式では 7 割の行が「要確認」に落ちて既定で除外される。
    """
    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    result = parse_grid(grid, DAIWA_LIKE_UNIVERSE)
    from asset_summary.importers.txlog.contracts import CONFIDENCE_INCLUDE_THRESHOLD

    dividends = [t for t in result.transactions if t.tx_type == "dividend"]
    assert dividends
    assert all(t.trade_date is not None for t in dividends)
    assert all(t.confidence >= CONFIDENCE_INCLUDE_THRESHOLD for t in dividends)
    # 判別できない行だけが要確認に残る（架空データでは『振　替』の1件）
    low = [t for t in result.transactions if t.confidence < CONFIDENCE_INCLUDE_THRESHOLD]
    assert all(t.tx_type == "other" for t in low)


def test_short_unit_values_are_not_mistaken_for_accounts():
    """『株』のような 1 文字の単位が既存口座名に部分一致して口座列に化けない。"""
    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    fmt = detect_format(grid, DAIWA_LIKE_UNIVERSE)
    assert _map(fmt).get("account") != 5


# ----------------------------------------------------------------------
# 使わない列が大量に並ぶ書式（レイアウト H）
#
# 行き場の無いフィールドを無理に収めると、本命の割当が犠牲になる。
# 実ファイルでは口座の列が無いのに『口座』が銘柄名列を奪っていた。
# ----------------------------------------------------------------------


def test_wide_layout_keeps_the_security_name_column():
    """既存銘柄に一致する列を、行き場の無いフィールドに明け渡さない。"""
    grid = load_grid(csv_bytes(LAYOUT_H, "cp932"))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    mapping = _map(fmt)
    assert mapping["security_name"] == 3      # 『銘柄名』
    assert mapping["security_code"] == 2
    assert mapping.get("account") is None     # 口座の列は無い


def test_a_merely_similar_header_does_not_take_the_security_name():
    """『市場名称』は『名称』を含むだけ。中身は東証で銘柄ではない。"""
    grid = load_grid(csv_bytes(LAYOUT_H, "cp932"))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    market = next(c for c in fmt.columns if c.header == "市場名称")
    assert market.field is F.IGNORE


def test_unrelated_numeric_columns_are_left_alone():
    """全部 0 の『名義書換料』を約定代金に仕立てない。

    数値列はどれも「数値である」以上の手がかりが無く、割り当てないより
    割り当てたほうがスコアが上がるので、検算の裏づけが無いときは
    見出しのある列だけ残す。
    """
    grid = load_grid(csv_bytes(LAYOUT_H, "cp932"))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    mapping = _map(fmt)
    assert "gross_amount" not in mapping      # 約定代金の列は無い
    assert mapping["net_amount"] == 11        # 『受渡金額［円］』
    for header in ("建単価［円］", "建手数料［円］", "貸株料", "名義書換料［円］（税抜）"):
        col = next(c for c in fmt.columns if c.header == header)
        assert col.field is F.IGNORE, f"{header} が {col.field.value} に割り当てられた"


def test_stale_identity_results_are_not_reported():
    """外した列で計算した検算結果を画面に出さない。"""
    grid = load_grid(csv_bytes(LAYOUT_H, "cp932"))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    assert all(c.tested == 0 or c.name != "qty*price=gross" for c in fmt.identities)
    assert not any("一致しません" in w for w in fmt.warnings)


def test_wide_layout_classifies_every_row():
    grid = load_grid(csv_bytes(LAYOUT_H, "cp932"))
    result = parse_grid(grid, STOCK_UNIVERSE)
    kinds = [t.tx_type for t in result.transactions]
    assert kinds == ["buy", "buy", "buy", "transfer_in", "transfer_out"]
    assert all(t.confidence >= 0.7 for t in result.transactions)

def test_sampling_spans_the_whole_file_not_just_the_head():
    """判定用の抽出はファイル全体から満遍なく取る。

    先頭 200 行だけ見ると、長い履歴では古い時期の性格しか見えない。実データ
    （数千行の長期履歴）では先頭 200 行が MRF ばかりで銘柄コードが
    9 桁の協会コードだったため、証券コードらしさが 0.478 と拒否のしきい値 0.5 を
    わずかに下回り、見出しが「銘柄コード」と完全一致しているのに列が捨てられて
    いた（全体で測れば 0.765）。その結果、9 桁コードの銘柄が 4 桁コードの
    同じ銘柄に結びつかなかった。
    """
    rows = list(range(1000))
    picked = sample_rows(rows)
    assert len(picked) == SAMPLE_ROWS
    assert picked[0] == 0
    assert picked[-1] >= 990          # 末尾側も入る
    assert len(set(picked)) == len(picked)
    assert picked == sorted(picked)


def test_short_files_are_sampled_whole():
    rows = list(range(12))
    assert sample_rows(rows) == rows
