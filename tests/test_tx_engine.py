"""取引区分の分類と、グリッド → ParsedTx の一貫した動作の検証。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from asset_summary.core.models import TxType
from asset_summary.importers.txlog.contracts import (
    CONFIDENCE_INCLUDE_THRESHOLD,
    EMPTY_UNIVERSE,
)
from asset_summary.importers.txlog.engine import detect_format, parse_grid
from asset_summary.importers.txlog.grid import load_grid
from asset_summary.importers.txlog.vocab import classify_tx_type
from tests.fixtures.tx_grids import (
    FUND_UNIVERSE,
    LAYOUT_A,
    LAYOUT_B,
    LAYOUT_C,
    STOCK_UNIVERSE,
    csv_bytes,
)


def _parse(text: str, universe=EMPTY_UNIVERSE, encoding: str = "utf-8"):
    grid = load_grid(csv_bytes(text, encoding))
    return parse_grid(grid, universe)


# ----------------------------------------------------------------------
# 取引区分の分類
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("買付", TxType.BUY),
        ("購入", TxType.BUY),
        ("積立買付", TxType.BUY),
        ("金額買付", TxType.BUY),
        ("売却", TxType.SELL),
        ("売付", TxType.SELL),
        ("解約", TxType.SELL),
        ("償還", TxType.SELL),
        ("配当金", TxType.DIVIDEND),
        ("普通分配金", TxType.DIVIDEND),
        ("株式分割", TxType.SPLIT),
        ("併合", TxType.SPLIT),
        ("入庫", TxType.TRANSFER_IN),
        ("出庫", TxType.TRANSFER_OUT),
        ("よくわからない摘要", TxType.OTHER),
        ("", TxType.OTHER),
    ],
)
def test_tx_type_vocabulary(raw, expected):
    assert classify_tx_type(raw)[0] is expected


def test_reinvestment_beats_dividend():
    """『分配金再投資』は配当ではなく買付。素朴な先頭一致だと取り違える。"""
    assert classify_tx_type("分配金再投資")[0] is TxType.REINVEST
    assert classify_tx_type("収益分配金再投資")[0] is TxType.REINVEST


def test_split_sell_is_a_sell_not_a_split():
    """『分割売却』は売却。共起を見ないと分割に化ける。"""
    assert classify_tx_type("分割売却")[0] is TxType.SELL


def test_special_distribution_is_return_of_capital():
    """特別分配金は元本の払い戻しで、取得原価を減らす（配当ではない）。"""
    assert classify_tx_type("特別分配金")[0] is TxType.RETURN_OF_CAPITAL
    assert classify_tx_type("元本払戻金")[0] is TxType.RETURN_OF_CAPITAL


# ----------------------------------------------------------------------
# 行の変換
# ----------------------------------------------------------------------


def test_layout_a_rows_are_signed_and_complete():
    result = _parse(LAYOUT_A, STOCK_UNIVERSE, "cp932")
    txs = result.transactions
    assert len(txs) == 5

    buy = txs[0]
    assert buy.tx_type == "buy"
    assert buy.quantity == Decimal("100")       # 買いは正
    assert buy.net_amount == Decimal("-250275")  # 買いは現金が出る
    assert buy.fee == Decimal("275")
    assert buy.account_type_raw == "特定"
    assert buy.confidence >= CONFIDENCE_INCLUDE_THRESHOLD

    sell = txs[2]
    assert sell.tx_type == "sell"
    assert sell.quantity == Decimal("-50")      # 売りは負
    assert sell.net_amount == Decimal("139546")
    assert sell.tax == Decimal("300")


def test_total_and_preamble_rows_never_become_transactions():
    result = _parse(LAYOUT_A, STOCK_UNIVERSE, "cp932")
    assert all(t.security_name_raw in ("架空商事", "架空製作所") for t in result.transactions)
    reasons = {s["reason"] for s in result.report.skipped_rows}
    assert "total_row" in reasons and "preamble" in reasons


def test_fund_rows_keep_the_nav_and_units():
    result = _parse(LAYOUT_B, FUND_UNIVERSE)
    txs = result.transactions
    assert len(txs) == 4
    assert txs[0].quantity == Decimal("10000")     # 口数
    assert txs[0].unit_price == Decimal("16000")   # 1万口あたり基準価額
    assert txs[2].tx_type == "reinvest"
    assert txs[3].quantity == Decimal("-5000")


def test_signed_quantity_layout_infers_buy_and_sell():
    result = _parse(LAYOUT_C)
    txs = result.transactions
    assert [t.tx_type for t in txs] == ["buy", "sell", "buy", "sell", "buy"]
    assert txs[1].quantity == Decimal("-40")


def test_dividend_rows_do_not_move_quantity():
    text = (
        "約定日,銘柄名,取引区分,数量,単価,約定代金,受渡金額\n"
        "2026/01/05,架空商事,買付,100,2500,250000,250000\n"
        "2026/02/10,架空商事,買付,100,2600,260000,260000\n"
        "2026/03/11,架空商事,買付,100,2700,270000,270000\n"
        "2026/06/30,架空商事,配当金,,,,4500\n"
    )
    result = _parse(text, STOCK_UNIVERSE)
    dividend = result.transactions[-1]
    assert dividend.tx_type == "dividend"
    assert dividend.quantity is None
    assert dividend.net_amount == Decimal("4500")


def test_unknown_tx_type_is_kept_as_other_with_the_raw_text():
    text = (
        "約定日,銘柄名,取引区分,数量,単価,約定代金\n"
        "2026/01/05,架空商事,買付,100,2500,250000\n"
        "2026/02/10,架空商事,買付,100,2600,260000\n"
        "2026/03/11,架空商事,謎の取引,100,2700,270000\n"
    )
    result = _parse(text, STOCK_UNIVERSE)
    odd = result.transactions[-1]
    assert odd.tx_type == "other"
    assert odd.raw["tx_type_raw"] == "謎の取引"
    assert any("取引区分" in w for w in odd.warnings)
    assert odd.confidence < 1.0


def test_row_arithmetic_mismatch_lowers_confidence():
    text = (
        "約定日,銘柄名,取引区分,数量,単価,約定代金\n"
        "2026/01/05,架空商事,買付,100,2500,250000\n"
        "2026/02/10,架空商事,買付,100,2600,260000\n"
        "2026/03/11,架空商事,買付,100,2700,270000\n"
        "2026/04/11,架空商事,買付,100,2800,999999\n"
    )
    result = _parse(text, STOCK_UNIVERSE)
    bad = result.transactions[-1]
    assert any("行内検算" in w for w in bad.warnings)
    assert bad.confidence < result.transactions[0].confidence


def test_no_type_column_and_no_signs_refuses_to_guess():
    """売買の向きが分からないとき『全部買付』と決め打たない。

    黙って推測すると保有数が取り返しのつかない形で壊れる。取込対象から
    外して人に判断させる方が安い。
    """
    text = (
        "約定日,銘柄名,数量,単価,約定代金\n"
        "2026/01/05,架空商事,100,2500,250000\n"
        "2026/02/10,架空商事,100,2600,260000\n"
        "2026/03/11,架空商事,100,2700,270000\n"
    )
    grid = load_grid(csv_bytes(text))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    assert fmt.sign_convention == "unsigned"
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    assert all(t.tx_type == "other" for t in result.transactions)
    assert all(t.confidence < CONFIDENCE_INCLUDE_THRESHOLD for t in result.transactions)
    assert any("取引区分" in w for w in fmt.warnings)


def test_extra_fee_columns_are_summed():
    text = (
        "約定日,銘柄名,取引区分,数量,単価,約定代金,手数料,消費税,受渡金額\n"
        "2026/01/05,架空商事,買付,100,2500,250000,250,25,250275\n"
        "2026/02/10,架空商事,買付,100,2600,260000,260,26,260286\n"
        "2026/03/11,架空商事,買付,100,2700,270000,270,27,270297\n"
    )
    result = _parse(text, STOCK_UNIVERSE)
    assert result.transactions[0].fee == Decimal("275")   # 250 + 25


# ----------------------------------------------------------------------
# 壊れた入力でも例外を投げない（warnings-as-data）
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n\n",
        "a",
        "a,b,c",
        ",,,\n,,,\n",
        "約定日,銘柄名\n",
        "1\n2\n3\n",
        "約定日,銘柄名,数量\n不正,データ,ここ\n",
    ],
)
def test_degenerate_input_never_raises(text):
    result = _parse(text, STOCK_UNIVERSE)
    assert isinstance(result.transactions, list)
    assert isinstance(result.report.warnings, list)


def test_report_carries_the_detection_for_the_preview_ui():
    result = _parse(LAYOUT_A, STOCK_UNIVERSE, "cp932")
    detection = result.report.detection
    assert detection["header_row"] == 3
    assert detection["divisor"] == 1
    assert len(detection["columns"]) == 12
    # 各列に根拠と代替候補が付いていること（利用者が直せるように）
    trade = next(c for c in detection["columns"] if c["field"] == "trade_date")
    assert trade["evidence"]
    assert "identities" in detection and detection["identities"]


# ----------------------------------------------------------------------
# 現金の移動と証券の移動の区別
#
# 「振替出金」を証券の出庫と同じ扱いにすると、cost_basis の再生で保有数が
# 減る。口座への入出金で持ち株が消えることになるので、必ず分ける。
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 現金の移動（保有には無関係）
        ("振替出金", TxType.CASH_OUT),
        ("自動振替出金", TxType.CASH_OUT),
        ("出金", TxType.CASH_OUT),
        ("振替入金", TxType.CASH_IN),
        ("自動振替入金", TxType.CASH_IN),
        ("入金", TxType.CASH_IN),
        # 証券そのものの移動（数量が動く）
        ("預入", TxType.TRANSFER_IN),
        ("入庫", TxType.TRANSFER_IN),
        ("振替入庫", TxType.TRANSFER_IN),
        ("出庫", TxType.TRANSFER_OUT),
        ("振替出庫", TxType.TRANSFER_OUT),
    ],
)
def test_cash_and_security_movements_are_distinguished(raw, expected):
    assert classify_tx_type(raw)[0] is expected


def test_cash_rows_never_carry_a_quantity():
    """入出金の行は保有数を動かさない。数量欄に何かあっても無視する。"""
    text = (
        "約定日,銘柄名,取引区分,数量,単価,約定代金,手数料,受渡金額\n"
        "2026/01/05,架空商事,買付,100,2000,200000,275,200275\n"
        "2026/01/06,架空商事,買付,100,2100,210000,289,210289\n"
        "2026/01/07,架空商事,買付,100,2200,220000,302,220302\n"
        "2026/01/10,架空ネクスト銀行普通預り金,自動振替出金,999,,,,500000\n"
    )
    result = _parse(text, STOCK_UNIVERSE)
    cash = result.transactions[-1]
    assert cash.tx_type == "cash_out"
    assert cash.quantity is None                   # 数量欄の 999 は無視する
    assert cash.net_amount == Decimal("-500000")   # 出金は負


def test_ambiguous_transfer_takes_its_direction_from_the_amount():
    """向きの語が無い『振替』は金額の符号で入出金を決める。"""
    text = (
        "約定日,銘柄名,取引区分,数量,単価,受渡金額\n"
        "2026/01/05,架空商事,買付,100,2000,200000\n"
        "2026/01/06,架空商事,買付,100,2100,210000\n"
        "2026/01/07,架空商事,買付,100,2200,220000\n"
        "2026/01/10,源泉所得税還付,振替,,,1703\n"
        "2026/01/11,資金移動,振替,,,-2000\n"
    )
    result = _parse(text, STOCK_UNIVERSE)
    assert result.transactions[-2].tx_type == "cash_in"
    assert result.transactions[-1].tx_type == "cash_out"


def test_daiwa_like_layout_separates_cash_from_securities():
    from tests.fixtures.tx_grids import DAIWA_LIKE_UNIVERSE, LAYOUT_G

    grid = load_grid(csv_bytes(LAYOUT_G, "cp932"))
    result = parse_grid(grid, DAIWA_LIKE_UNIVERSE)
    kinds = [t.tx_type for t in result.transactions]
    # 銀行への資金移動は入出金。証券の預入は入庫のまま。
    assert "cash_out" in kinds
    assert kinds.count("transfer_in") == 1
    assert all(
        t.quantity is None for t in result.transactions if t.tx_type.startswith("cash")
    )


# ----------------------------------------------------------------------
# 分類の列と項目が紛らわしい書式（実在する証券会社の形を参考に、中身は架空）
#
#   - 『商品』は分類（株式/投信）であって銘柄名ではない
#   - 『口座』の中身が 特定/一般/NISA なら口座区分であって口座名ではない
#   - 信用取引を現物の売買にすると保有数が壊れる
#   - 入出金の行に銘柄が無いのは異常ではない
# ----------------------------------------------------------------------

MONEX_LIKE = (
    "約定日,受渡日,口座,商品,取引,銘柄コード,銘柄名,数量,単価,手数料,受渡金額\n"
    "2026/01/05,2026/01/07,特定,株式,お買付,1234,架空商事,100,2500,275,-250275\n"
    "2026/02/10,2026/02/12,NISA,株式,お買付,1234,架空商事,200,2600,0,-520000\n"
    "2026/03/03,2026/03/05,特定,株式,ご売却,5678,架空製作所,100,1350,148,134652\n"
    "2026/03/10,2026/03/10,,,ご入金,,,0,0,0,200000\n"
    "2026/04/07,2026/04/09,特定,株式,半年新規買い,1234,架空商事,300,2400,264,-720264\n"
    "2026/05/12,2026/05/14,特定,株式,半年返済売り,1234,架空商事,300,2600,286,779714\n"
)


def _monex(universe=STOCK_UNIVERSE):
    grid = load_grid(csv_bytes(MONEX_LIKE, "cp932"))
    return grid, detect_format(grid, universe)


def test_product_category_is_not_the_security_name():
    """『商品』は株式/投信という分類。本物の『銘柄名』列を使う。"""
    _grid, fmt = _monex()
    mapping = {c.field.value: c.index for c in fmt.columns if c.field.value != "_"}
    assert mapping["security_name"] == 6      # 『銘柄名』
    product = next(c for c in fmt.columns if c.header == "商品")
    assert product.field.value == "_"


def test_an_account_column_holding_account_types_is_read_as_such():
    """見出しが『口座』でも中身が 特定/一般/NISA なら口座区分。"""
    _grid, fmt = _monex()
    mapping = {c.field.value: c.index for c in fmt.columns if c.field.value != "_"}
    assert mapping["account_type"] == 2
    assert "account" not in mapping


@pytest.mark.parametrize(
    "raw", ["半年新規買い", "半年返済売り", "無期新規売り", "信用返済"]
)
def test_margin_trades_are_not_treated_as_cash_trades(raw):
    """建玉の売買を現物にすると、持っていない株を持つことになる。"""
    assert classify_tx_type(raw)[0] is TxType.OTHER


def test_physical_settlement_of_margin_moves_real_holdings():
    """現渡・現引は信用の返済だが、現物を実際に動かす。

    現渡は持っている現物を渡して返済（＝処分）、現引は代金を払って現物を
    受け取る（＝取得）。対象外にすると現物の増減がファイル上で合わなくなる
    （実データでは、現物 +400/-200 に現渡 -200 で ±0 が完結する銘柄や、
    現引 +100 株がそのまま保有中の取得記録になっている銘柄があった）。
    """
    assert classify_tx_type("半年現渡")[0] is TxType.SELL
    assert classify_tx_type("現渡")[0] is TxType.SELL
    assert classify_tx_type("無期現引")[0] is TxType.BUY
    assert classify_tx_type("品渡")[0] is TxType.SELL


def test_physical_settlement_prices_are_not_market_evidence():
    """現渡・現引の単価は建単価で、その日の時価ではない（入出庫と同じ理屈）。"""
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,買付,100,2500,250000",
        "2026/02/10,架空商事,半年現渡,100,1800,180000",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    gw = next(t for t in result.transactions if t.raw.get("sell_kind") == "現渡")
    assert gw.tx_type == "sell"
    assert gw.raw.get("off_market_price") is True
    assert gw.quantity == Decimal("-100")


def test_margin_rows_say_why_they_are_excluded():
    grid, fmt = _monex()
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    margin = [t for t in result.transactions if t.raw.get("margin")]
    assert len(margin) == 2
    assert all(t.tx_type == "other" for t in margin)
    assert all(any("信用取引" in w for w in t.warnings) for t in margin)
    # 現物の売買として数量を持たせない
    assert all(t.quantity == 0 or t.quantity is None or t.tx_type == "other"
               for t in margin)


def test_cash_rows_without_a_security_are_not_flagged():
    """入出金に銘柄が無いのは当然。減点すると入出金が丸ごと要確認に落ちる。"""
    grid, fmt = _monex()
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    cash = [t for t in result.transactions if t.tx_type.startswith("cash")]
    assert cash
    assert all(not any("銘柄" in w for w in t.warnings) for t in cash)


def test_ordinary_rows_still_complain_about_a_missing_security():
    text = (
        "約定日,取引,銘柄名,数量,単価,受渡金額\n"
        "2026/01/05,お買付,架空商事,100,2500,-250000\n"
        "2026/02/10,お買付,架空商事,200,2600,-520000\n"
        "2026/03/03,お買付,,100,2700,-270000\n"
    )
    result = _parse(text, STOCK_UNIVERSE)
    assert any("銘柄" in w for w in result.transactions[-1].warnings)

def test_buyback_is_a_sale_not_a_purchase():
    """『買取』は証券会社が買い取る側の呼び方で、利用者から見れば売却。

    _BUY の単独 "買" に食われて買付になっていた。実データでは数十行あり、
    買付計と買取計が同数で残 0 になるはずの銘柄が、買付として数えると
    保有 2 倍になる。
    """
    assert classify_tx_type("買取")[0] is TxType.SELL
    assert classify_tx_type("買取請求")[0] is TxType.SELL
    assert classify_tx_type("買増")[0] is TxType.BUY


def test_a_payout_out_of_an_accumulation_account_is_a_transfer():
    """『累投』は口座の性格で、行の動作ではない。動作を表す語がある側を採る。

    『国内累投ＮＩＳＡ払出（特定へ）』は同じ数量の『入庫』と対に
    なっている。再投資（＝買付）と読むと対の入庫とあわせて保有が 2 倍に増える。
    """
    assert classify_tx_type("国内累投ＮＩＳＡ払出（特定へ）")[0] is TxType.TRANSFER_OUT
    assert classify_tx_type("ＭＲＦ再投資")[0] is TxType.REINVEST
    assert classify_tx_type("再投資買付")[0] is TxType.REINVEST

# ----------------------------------------------------------------------
# 取引区分が空欄の行の向き推定
# ----------------------------------------------------------------------


def _blank_type_csv(*rows: str) -> bytes:
    head = "約定日,銘柄名,取引区分,数量,単価,約定代金" + chr(10)
    return csv_bytes(head + "".join(r + chr(10) for r in rows))


def test_blank_type_cells_are_inferred_as_buys_when_sell_is_impossible():
    """空欄行を売却と読むと保有がマイナスに落ちるなら、買付と確定できる。

    マネックスは投信のつみたて買付で種別の欄を空にする（長い履歴では数百行）。
    ボタンで人に「買付」と指定させていたが、無いものは売れないという算術で
    ファイル自身から決められる。
    """
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,,100,2500,250000",
        "2026/02/10,架空商事,,100,2600,260000",
        "2026/02/20,架空商事,,100,2650,265000",
        "2026/03/11,架空商事,売却,50,2700,135000",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    blanks = [t for t in result.transactions if t.raw.get("inferred_type")]
    assert [t.tx_type for t in blanks] == ["buy", "buy", "buy"]
    assert all(t.confidence >= CONFIDENCE_INCLUDE_THRESHOLD for t in blanks)
    assert all(t.quantity == Decimal("100") for t in blanks)
    # このフィクスチャに受渡金額の列は無い（あれば買いなので負に揃える）
    assert all(t.net_amount is None or t.net_amount < 0 for t in blanks)
    assert all(any("推定" in w for w in t.warnings) for t in blanks)
    # 明記された売却はそのまま
    assert result.transactions[3].tx_type == "sell"


def test_the_proof_extends_to_groups_where_sell_would_be_feasible():
    """空欄は書式の性質なので、証明できた行が十分あれば残りにも同じ読みを使う。

    実データでは入庫が先にある銘柄（数百行）がこれに当たる。銘柄単独では
    売却の可能性を排除できないが、同じファイルの空欄行の多数が買付と
    確定している。
    """
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,,100,2500,250000",
        "2026/02/10,架空商事,,100,2600,260000",
        "2026/03/11,架空商事,,100,2700,270000",
        "2026/01/04,架空鉱業,入庫,500,1000,500000",
        "2026/02/15,架空鉱業,,200,1100,220000",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    mining = [t for t in result.transactions if t.security_name_raw == "架空鉱業"]
    blank_mining = [t for t in mining if t.raw.get("inferred_type")]
    assert [t.tx_type for t in blank_mining] == ["buy"]
    assert any("同じファイル" in w for w in blank_mining[0].warnings)


def test_too_few_proven_rows_do_not_establish_a_format_property():
    """証明できた行が少なすぎるときは書式の性質とまでは言えず、推定しない。"""
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,,100,2500,250000",
        "2026/01/04,架空鉱業,入庫,500,1000,500000",
        "2026/02/15,架空鉱業,,200,1100,220000",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    assert all(not t.raw.get("inferred_type") for t in result.transactions)


def test_rows_that_do_not_settle_like_a_trade_are_not_inferred():
    """数量×単価と金額が合わない空欄行は約定の形をしていないので触らない。"""
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,,100,2500,250000",
        "2026/02/10,架空商事,,100,2600,260000",
        "2026/03/11,架空商事,,100,2700,270000",
        "2026/04/01,架空商事,,999,2500,1",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    odd = [t for t in result.transactions if t.quantity == Decimal("999")]
    assert odd and odd[0].tx_type == "other"

# ----------------------------------------------------------------------
# ノイズになる行の扱い（MRF・増減ゼロ・保証金振替）
# ----------------------------------------------------------------------


def test_all_zero_rows_stay_parsed_for_evidence():
    """数量も金額もすべて 0 の行は、パース段階では捨てない。

    プレビューで隠すのはサービス層の仕事（tx_service）。パーサが捨てると
    ゼロ行の銘柄コードが失われ、3 代にわたる改称の数珠つなぎを復元でき
    なくなる。実データではつなぎの十数行がすべてゼロ額の分配金行だった。
    """
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,買付,100,2500,250000",
        "2026/02/01,日興ＭＲＦ,ＭＲＦ再投資,0,0,0",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    assert len(result.transactions) == 2


def test_mrf_rows_are_treated_as_cash():
    """MRF は投信の形をした実質普通預金。買付・売却で来ても入出金として扱う。"""
    grid = load_grid(_blank_type_csv(
        "2026/01/05,日興ＭＲＦ,お買付,200000,1,200000",
        "2026/01/06,日興ＭＲＦ,ご売却,200000,1,200000",
        "2026/02/01,日興ＭＲＦ,ＭＲＦ再投資,55,1,55",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    assert all(t.tx_type in ("cash_in", "cash_out") for t in result.transactions)


def test_margin_collateral_transfer_is_cash_not_an_unresolved_other():
    """『振替（信用保証金へ）』は保証金の資金移動。銘柄も数量も無い。

    信用の字だけで弾くと「その他・銘柄未確定 ⚠」になり、利用者がどう
    対処すべきか分からない行になる（実データで数十行）。
    """
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,買付,100,2500,250000",
        "2026/02/21,,振替（信用保証金へ）,0,0,300000",
        "2026/03/03,,振替（信用保証金から）,0,0,18228",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    transfers = [t for t in result.transactions if not t.security_name_raw]
    assert transfers and all(t.tx_type in ("cash_in", "cash_out") for t in transfers)
    assert all(not t.raw.get("margin") for t in transfers)
    assert all(not any("信用" in w for w in t.warnings) for t in transfers)


def test_real_margin_trades_are_still_excluded():
    """建玉の売買（新規買・返済売）は引き続き対象外。"""
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,買付,100,2500,250000",
        "2026/02/10,架空商事,半年新規買い,100,2600,260000",
        "2026/03/11,架空商事,半年返済売り,100,2700,270000",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    margin = [t for t in result.transactions if t.raw.get("margin")]
    assert len(margin) == 2
    assert all(t.tx_type == "other" for t in margin)
    assert all(any("信用取引" in w for w in t.warnings) for t in margin)

def test_the_leading_action_word_wins_over_a_parenthesized_qualifier():
    """日本の取引ラベルは動作が先頭、括弧内は補足。

    『ご入金（カード積立）』はカード積立のための入金であって買付ではない。
    括弧まで含めて見ると補足の「積立」が買付に化け、行の形の検査で現金に
    戻される遠回りの末に、普通の入金に ⚠ が付いていた。
    """
    assert classify_tx_type("ご入金（カード積立）")[0] is TxType.CASH_IN
    assert classify_tx_type("還付金（配当等）")[0] is TxType.CASH_IN
    # 動作が括弧の側にある書式は全体で分類する
    assert classify_tx_type("投信（買付）")[0] is TxType.BUY
    assert classify_tx_type("買付（NISA）")[0] is TxType.BUY


def test_a_card_deposit_row_is_plain_cash_without_warnings():
    """利用者が示した実例の形: 銘柄も数量も無い『ご入金（カード積立）』。"""
    grid = load_grid(_blank_type_csv(
        "2026/01/05,架空商事,買付,100,2500,250000",
        "2009/03/19,,ご入金（カード積立）,0,0,15000",
    ))
    fmt = detect_format(grid, STOCK_UNIVERSE)
    result = parse_grid(grid, STOCK_UNIVERSE, fmt=fmt)
    deposit = next(t for t in result.transactions if not t.security_name_raw)
    assert deposit.tx_type == "cash_in"
    assert deposit.warnings == []
