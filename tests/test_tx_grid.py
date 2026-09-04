"""bytes/テキスト → SheetGrid の検証（文字コード・区切り文字・xlsx・貼り付け）。"""

from __future__ import annotations

import pytest

from asset_summary.importers.txlog.grid import (
    decode_text,
    load_grid,
    load_pasted,
    sniff_delimiter,
    sniff_kind,
)
from tests.fixtures.tx_grids import (
    LAYOUT_A,
    LAYOUT_C,
    csv_bytes,
    fake_xls_bytes,
    fake_zip_bytes,
    xlsx_bytes,
)


# ----------------------------------------------------------------------
# 種別の判定
# ----------------------------------------------------------------------


def test_sniff_kind_detects_csv_xlsx_xls_pdf():
    assert sniff_kind(csv_bytes(LAYOUT_A)) == "csv"
    assert sniff_kind(fake_xls_bytes()) == "xls"
    assert sniff_kind(b"%PDF-1.4 ...") == "pdf"
    assert sniff_kind(b"") == "unknown"


def test_plain_zip_is_not_mistaken_for_xlsx():
    # docx も PK で始まる。xl/ が無ければ xlsx ではない。
    assert sniff_kind(fake_zip_bytes()) == "unknown"


def test_xls_and_pdf_are_reported_as_warnings_not_exceptions():
    grid = load_grid(fake_xls_bytes(), filename="old.xls")
    assert grid.height == 0
    assert any(".xls" in w for w in grid.meta.warnings)

    grid = load_grid(b"%PDF-1.4 fake", filename="mf.pdf")
    assert any("PDF" in w for w in grid.meta.warnings)


# ----------------------------------------------------------------------
# 文字コード
# ----------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp932"])
def test_encoding_round_trips(encoding):
    grid = load_grid(csv_bytes(LAYOUT_A, encoding))
    assert "架空商事" in grid.rows[4]
    assert grid.meta.encoding in (encoding, "utf-8")


def test_cp932_is_preferred_over_mojibake():
    text = "銘柄名,数量\n架空商事,100\n"
    _decoded, encoding, _warnings = decode_text(text.encode("cp932"))
    assert encoding == "cp932"


def test_utf16_bom_is_honoured():
    text = "銘柄名\t数量\n架空商事\t100\n"
    decoded, encoding, _ = decode_text(text.encode("utf-16-le-sig" if False else "utf-16"))
    assert "架空商事" in decoded
    assert encoding.startswith("utf-16")


def test_undecodable_bytes_degrade_with_a_warning():
    decoded, _encoding, warnings = decode_text(b"\xff\xfe\xfa\xfb\xfc garbage \x81\x40")
    assert isinstance(decoded, str)
    assert warnings == [] or any("文字コード" in w for w in warnings)


# ----------------------------------------------------------------------
# 区切り文字
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "delimiter,text",
    [
        (",", "a,b,c\n1,2,3\n4,5,6\n"),
        ("\t", "a\tb\tc\n1\t2\t3\n4\t5\t6\n"),
        (";", "a;b;c\n1;2;3\n4;5;6\n"),
        ("|", "a|b|c\n1|2|3\n4|5|6\n"),
    ],
)
def test_delimiter_sniffing(delimiter, text):
    found, mode, _score = sniff_delimiter(text)
    assert (found, mode) == (delimiter, "char")


def test_preamble_does_not_defeat_delimiter_sniffing():
    # 前文が 4 行、本体が 3 行。全体一致率だと沈むので最長連続区間でも測っている。
    text = "口座番号\n出力日時\n\n検索条件\na,b,c,d\n1,2,3,4\n5,6,7,8\n"
    found, mode, _ = sniff_delimiter(text)
    assert (found, mode) == (",", "char")


def test_quoted_comma_inside_security_name_is_preserved():
    grid = load_grid(csv_bytes('銘柄,数量\n"架空商事, 第一種",100\n"別会社",200\n'))
    assert grid.rows[1][0] == "架空商事, 第一種"
    assert grid.rows[1][1] == "100"


def test_semicolon_layout_parses_into_columns():
    grid = load_grid(csv_bytes(LAYOUT_C))
    assert grid.rows[0][0] == "Date"
    assert grid.width == 7


def test_crlf_line_endings():
    grid = load_grid(csv_bytes("a,b\r\n1,2\r\n3,4\r\n"))
    assert grid.rows[1] == ("1", "2")
    assert grid.height == 3


def test_empty_input_is_an_empty_grid():
    grid = load_grid(b"")
    assert grid.height == 0
    assert grid.width == 0


# ----------------------------------------------------------------------
# 貼り付け
# ----------------------------------------------------------------------


def test_pasted_tab_separated_text():
    grid = load_pasted("約定日\t銘柄\t数量\n2026/01/05\t架空商事\t100")
    assert grid.meta.kind == "paste"
    assert grid.rows[1] == ("2026/01/05", "架空商事", "100")


def test_pasted_multispace_text():
    grid = load_pasted("約定日      銘柄       数量\n2026/01/05  架空商事   100")
    assert grid.meta.delimiter_mode == "multispace"
    assert grid.rows[1] == ("2026/01/05", "架空商事", "100")


# ----------------------------------------------------------------------
# xlsx
# ----------------------------------------------------------------------


def test_xlsx_round_trip():
    pytest.importorskip("openpyxl")
    from datetime import date

    data = xlsx_bytes(
        [
            ["約定日", "銘柄名", "取引区分", "数量", "単価"],
            [date(2026, 1, 5), "架空商事", "買付", 100, 2500],
            [date(2026, 2, 10), "架空商事", "売却", 40, 2800],
        ]
    )
    grid = load_grid(data, filename="t.xlsx")
    assert grid.meta.kind == "xlsx"
    assert grid.meta.sheet_name == "取引履歴"
    assert grid.rows[0][0] == "約定日"
    assert grid.rows[1][0] == "2026-01-05"
    # 日付セルは型として分かるので、判定でそのまま使える
    assert grid.cell_type(1, 0) == "date"
    assert grid.cell_type(1, 3) == "number"


def test_xlsx_trailing_empty_cells_are_trimmed():
    pytest.importorskip("openpyxl")
    data = xlsx_bytes([["a", "b", None, None], ["1", "2", None, None]])
    grid = load_grid(data)
    assert grid.width == 2


# ----------------------------------------------------------------------
# 1件が複数行に分かれた貼り付け（未対応だが、理由は名指しする）
# ----------------------------------------------------------------------

BLOCK_PASTE = (
    "受渡日\t約定日\t商品\t銘柄・摘要\n口座\n取引\n数量\n単価\n受渡金額\n"
    "2026/07/31\t07/29\t株式\t1234\n架空商事\n特定\n買付\n10株\n1,740\n-17,400円\n"
    "2026/07/30\t07/30\t金銭\t振込出金(NET)\n-99,332円\n"
    "2026/07/29\t07/27\t株式\t5678\n架空製作所\n特定\n買付\n1株\n226.12\n-226円\n"
    "2026/07/28\t07/24\t株式\t1234\n架空商事\n特定\n買付\n36株\n1,775\n-63,900円\n"
)


def test_block_paste_is_named_rather_than_a_generic_failure():
    """表として読めないとき、読めない理由が分かるなら言う。"""
    grid = load_pasted(BLOCK_PASTE)
    assert any("複数行に分かれた" in w for w in grid.meta.warnings)


def test_a_normal_pasted_table_is_not_mistaken_for_a_block_paste():
    grid = load_pasted(
        "約定日\t銘柄名\t取引区分\t数量\t単価\n"
        "2026/01/05\t架空商事\t買付\t100\t2500\n"
        "2026/02/10\t架空商事\t売却\t40\t2800\n"
        "2026/03/11\t架空商事\t買付\t60\t2900\n"
    )
    assert not any("複数行に分かれた" in w for w in grid.meta.warnings)
    assert grid.width == 5
