from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from asset_summary.importers.base import SectionType
from asset_summary.importers.mf_pdf import (
    MfPdfImporter,
    _parse_pdf_date,
    join_display,
    parse_pdf,
    parse_words,
)
from tests.fixtures.mf_words import (
    fund_header,
    heading,
    pension_header,
    sample_pages,
    stock_header,
    stock_header_vertical,
    total_line,
    w,
)

REAL_PDF = Path(__file__).resolve().parents[1] / "data" / "マネーフォワード ME.pdf"


@pytest.fixture(scope="module")
def parsed():
    return parse_words(sample_pages())


def _by_section(parsed, section: SectionType):
    return [h for h in parsed.holdings if h.section == section]


# ----------------------------------------------------------------------
# 3層検算・セクション遷移
# ----------------------------------------------------------------------


def test_grand_total_and_sections(parsed):
    rep = parsed.report
    assert rep.grand_total_expected == Decimal("320399")
    assert rep.grand_total_computed == Decimal("320399")
    assert rep.grand_total_ok
    by_name = {s.section: s for s in rep.sections}
    assert set(by_name) == {
        SectionType.DEPOSIT,
        SectionType.STOCK,
        SectionType.FUND,
        SectionType.CRYPTO,
        SectionType.PENSION,
        SectionType.POINT,
    }
    for s in rep.sections:
        assert s.ok, f"{s.name}: expected={s.expected_total} computed={s.computed_total}"
    assert by_name[SectionType.DEPOSIT].row_count == 3
    assert by_name[SectionType.STOCK].row_count == 2
    assert by_name[SectionType.FUND].row_count == 2
    assert by_name[SectionType.CRYPTO].row_count == 1
    assert by_name[SectionType.PENSION].row_count == 1
    assert by_name[SectionType.POINT].row_count == 2
    assert by_name[SectionType.STOCK].expected_total == Decimal("79000")


def test_section_persists_across_pages(parsed):
    # 2件目の株式レコードは2ページ目（見出しの再出現なし）にある
    stocks = _by_section(parsed, SectionType.STOCK)
    assert len(stocks) == 2
    assert stocks[1].name_raw == "テスト電機"


# ----------------------------------------------------------------------
# 預金・現金
# ----------------------------------------------------------------------


def test_deposit_rows(parsed):
    rows = _by_section(parsed, SectionType.DEPOSIT)
    assert [h.name_raw for h in rows] == ["テスト支店普通", "代表口座-豪ドル普通", "現金"]
    # 全角数字・全角カンマの正規化
    assert rows[0].value_jpy == Decimal("1234")
    # 機関名の折返し結合（非ASCII境界はスペースなし）
    assert rows[0].institution == "架空ネット銀行"
    # 小さな右寄せ残高が機関列に落ちるケースの補正
    assert rows[1].value_jpy == Decimal("4")
    # 通貨ヒントは meta のみ（残高は円のまま）
    assert rows[1].meta.get("currency_hint") == "AUD"
    assert rows[2].institution == "架空証券"
    assert all(h.confidence == 1.0 for h in rows)


# ----------------------------------------------------------------------
# 株式(現物): 2行ヘッダ・列スパン再計算・負損益・全角数字
# ----------------------------------------------------------------------


def test_stock_first_row_code_column_fixup(parsed):
    h = _by_section(parsed, SectionType.STOCK)[0]
    assert h.code_raw == "1234"
    # 短い先頭語 "AB" はコード列に食い込むが銘柄名に補正され、
    # ASCII境界のみ半角スペースが入る
    assert h.name_raw == "AB GOLD+ゴールド"
    assert h.quantity == Decimal("100")
    assert h.avg_cost == Decimal("500")
    assert h.price == Decimal("600")
    assert h.value_jpy == Decimal("60000")
    assert h.pl_jpy == Decimal("10000")
    assert h.institution == "架空証券"
    assert h.confidence == 1.0 and not h.warnings


def test_stock_second_row_with_shifted_header(parsed):
    # 2ページ目はヘッダが+20ptシフト → 列スパンの再計算を検証
    h = _by_section(parsed, SectionType.STOCK)[1]
    assert h.code_raw == "567A0"
    assert h.quantity == Decimal("10")          # 全角「１０」
    assert h.pl_jpy == Decimal("-1000")         # ▲表記の負損益
    assert h.value_jpy == Decimal("19000")
    assert h.meta.get("pl_pct") == "-5.00%"
    assert h.meta.get("day_change") == "0円"
    assert h.institution == "架空証券"


# ----------------------------------------------------------------------
# 投資信託: divisor=10000 検算・検算NG時の confidence
# ----------------------------------------------------------------------


def test_fund_row_check_with_divisor(parsed):
    f1 = _by_section(parsed, SectionType.FUND)[0]
    assert f1.name_raw == "架空インデックスファンド"  # 折返し結合
    assert f1.quantity == Decimal("10000")
    assert f1.value_jpy == Decimal("16000")     # 10,000×16,000÷10000
    assert f1.confidence == 1.0 and not f1.warnings


def test_fund_row_check_ng_lowers_confidence(parsed):
    f2 = _by_section(parsed, SectionType.FUND)[1]
    assert f2.name_raw == "検算エラー投信"
    assert f2.warnings and "行内検算" in f2.warnings[0]
    assert f2.confidence < 0.7


# ----------------------------------------------------------------------
# 暗号資産・年金・ポイント
# ----------------------------------------------------------------------


def test_crypto_rows_parsed_for_reconciliation(parsed):
    rows = _by_section(parsed, SectionType.CRYPTO)
    assert len(rows) == 1
    assert rows[0].name_raw == "BTC残高"
    assert rows[0].value_jpy == Decimal("500")
    assert rows[0].institution == "架空コイン"


def test_pension_row(parsed):
    h = _by_section(parsed, SectionType.PENSION)[0]
    assert h.institution == "年金"               # 機関列なし → 固定
    assert h.name_raw == "架空DCファンド(確定拠出)"
    assert h.avg_cost == Decimal("100000")      # 取得価額の総額
    assert h.value_jpy == Decimal("120000")
    assert h.pl_jpy == Decimal("20000")
    assert h.confidence == 1.0 and not h.warnings


def _pension_doc(rows, total):
    """年金セクションだけの文書。rows は行ごとの word リスト。"""
    words = []
    words += heading("年金", 10)
    words += total_line(total, 20)
    words += pension_header(30)
    for row in rows:
        words += row
    return [words]


def test_pension_reserve_without_acquisition_cost():
    """確定拠出年金の待機資金は取得価額が無く、現在価値の列だけが埋まる。

    入金済み・買付前の資金なので取得原価が存在しない。読み取り失敗ではないので
    警告も減点もせず、年金セクションの合計に算入する。
    """
    result = parse_words(_pension_doc([
        [w("架空DCファンド", 19, 78, 60),
         w("100,000円", 110, 154, 60), w("120,000円", 164, 208, 60),
         w("20,000円", 220, 257, 60), w("20.00%", 285, 314, 60)],
        [w("待機資金", 19, 50, 80), w("20,000円", 164, 208, 80)],
    ], "140,000"))

    assert not result.report.unparsed_lines      # 旧実装では行ごと捨てられた
    assert len(result.holdings) == 2
    reserve = result.holdings[1]
    assert reserve.name_raw == "待機資金"
    assert reserve.avg_cost is None              # 取得原価なし＝損益を出さない
    assert reserve.value_jpy == Decimal("20000")
    assert reserve.pl_jpy is None
    assert reserve.confidence == 1.0 and not reserve.warnings
    assert result.report.sections[0].ok          # 合計に算入される


def test_pension_row_with_pl_but_no_cost_warns():
    """損益が出ているのに取得価額が無いのは読み取り失敗なので警告する。"""
    result = parse_words(_pension_doc([
        [w("架空DCファンド", 19, 78, 60),
         w("120,000円", 164, 208, 60), w("20,000円", 220, 257, 60)],
    ], "120,000"))
    h = result.holdings[0]
    assert h.avg_cost is None and h.pl_jpy == Decimal("20000")
    assert h.warnings and "取得価額" in h.warnings[0]
    assert h.confidence < 1.0


def _page_chrome_doc():
    """全ページに印刷用のヘッダ・フッタが入る新形式（2026-08-24 のPDF）。

    実PDF実測: ページ冒頭に日時見出し、末尾に 'ヘルプ・サポー{n}/{N}ト'（ページ番号が
    語の中に挟まる）と 'https://moneyforward.com/bs/portfolio'（左端＝名称列）。
    """
    words = []
    words += [w("2026/08/24", 34, 64, 15.52), w("16:37", 65, 80, 15.52),
              w("マネーフォワード", 503, 551, 15.75), w("ME", 552, 561, 17.02)]
    words += heading("投資信託", 30)
    words += total_line("16,000", 40)
    words += fund_header(50)
    words += [
        w("架空インデッ", 19, 78, 780),
        w("10,000", 95, 121, 780), w("15,000", 145, 170, 780), w("16,000", 181, 206, 780),
        w("16,000円", 226, 260, 780), w("0円", 277, 289, 780),
        w("1,000円", 306, 335, 780), w("6.67%", 354, 378, 780),
        w("架空", 386, 416, 780),
    ]
    words += [w("クスファンド・", 19, 78, 789), w("証券", 386, 416, 789)]
    words += [w("外国REIT", 19, 60, 798)]
    # 以降が装飾。結合されると銘柄名が汚れる（'ヘルプ…' は変更/削除列に落ちて
    # 踏み台になり、直後のURLが名称列へ入る）
    words += [w("ヘルプ・サポー1/8ト", 507, 565, 807)]
    # 銘柄名/保有数の境界は 77.5。URLの中心を名称列側に置き、実PDFと同じ経路にする
    words += [w("https://moneyforward.com/bs/portfolio", 19, 100, 811)]
    return [words]


def test_page_chrome_does_not_leak_into_names():
    result = parse_words(_page_chrome_doc())
    assert len(result.holdings) == 1
    h = result.holdings[0]
    # 旧実装では 'https://moneyforward.com/bs/portfolio' が末尾に付き、
    # 別銘柄として登録されてしまった
    assert h.name_raw == "架空インデックスファンド・外国REIT"
    assert h.institution == "架空証券"
    assert h.value_jpy == Decimal("16000")
    assert h.confidence == 1.0 and not h.warnings
    # 装飾は行としても残さない（毎回「解析できなかった行」が出るのを防ぐ）
    assert not result.report.unparsed_lines
    assert result.report.sections[0].ok


def test_point_rows(parsed):
    p1, p2 = _by_section(parsed, SectionType.POINT)
    assert p1.name_raw == "架空ポイント"          # 折返し結合
    assert p1.quantity == Decimal("1000")
    assert p1.value_jpy == Decimal("1000")
    assert p1.meta.get("point_type") == "ポイント"
    assert p1.meta.get("rate") == "1.00"
    assert p1.meta.get("expiry") == "2027/01/31"
    assert p2.quantity == Decimal("200")
    assert p2.value_jpy == Decimal("400")
    assert "expiry" not in p2.meta


# ----------------------------------------------------------------------
# ノイズ除去・unparsed_lines
# ----------------------------------------------------------------------


def test_noise_removed_and_unparsed_lines(parsed):
    rep = parsed.report
    assert len(rep.unparsed_lines) == 1
    entry = rep.unparsed_lines[0]
    assert entry["page"] == 3
    assert entry["text"] == "謎のデータ行"
    assert "top" in entry
    all_text = " ".join(u["text"] for u in rep.unparsed_lines)
    for noise in ("ヘルプ・サポート", "株株式式現現物物", "サービスについて", "運営会社", "©"):
        assert noise not in all_text
    # ノイズが保有行として混入していないこと
    names = [h.name_raw for h in parsed.holdings]
    assert "運営会社" not in names


def test_pua_footer_word_dropped():
    pages = [
        heading("預金・現金", 10)
        + total_line("100", 20)
        + [
            w("種類・名称", 38, 71, 30), w("残高", 138, 152, 30), w("保有金融機関", 205, 245, 30),
            w("普通預金", 19, 48, 50), w("100円", 163, 192, 50), w("架空銀行", 200, 230, 50),
            w("ヘルプ・サポート", 496, 562, 815),  # 私用領域文字つきフッタ
        ]
    ]
    result = parse_words(pages)
    assert len(result.holdings) == 1
    assert not result.report.unparsed_lines


# ----------------------------------------------------------------------
# 行内検算の許容誤差（表示丸め）
# ----------------------------------------------------------------------


def _mini_stock_doc(qty: str, price: str, value: str):
    words = []
    words += heading("株式(現物)", 10)
    words += stock_header(30)
    words += [
        w("1234", 19, 38, 60), w("架空銘柄", 53, 97, 60),
        w(qty, 134, 148, 60), w("100", 184, 198, 60), w(price, 213, 227, 60),
        w(value, 248, 281, 60),
    ]
    return [words]


def test_row_check_allows_display_rounding():
    # 実価格224.5が「225」と丸め表示されるケース: 10×225=2250 vs 2245
    result = parse_words(_mini_stock_doc("10", "225", "2,245円"))
    h = result.holdings[0]
    assert not h.warnings and h.confidence == 1.0


def test_row_check_fails_beyond_tolerance():
    result = parse_words(_mini_stock_doc("10", "300", "2,245円"))
    h = result.holdings[0]
    assert h.warnings and h.confidence < 0.7


# ----------------------------------------------------------------------
# 銘柄名が列幅からはみ出して隣の列へ食い込むケース
# ----------------------------------------------------------------------


def _overflowing_name_doc():
    """長い銘柄名の末尾が保有数の列域まで伸びる文書。

    2026-08-10 の実PDF実測をそのまま縮尺: 銘柄名の語間は 3.31pt しか空かず、
    はみ出した "ゴー" の中心xが 銘柄名/保有数 の境界を越える。
    """
    words = []
    words += heading("株式(現物)", 10)
    words += total_line("60,000", 20)
    words += stock_header(30)
    # 銘柄名/保有数の境界は 112.0（ヘッダ 銘柄名 75-96 と 保有数 128-148 の中点）
    words += [
        w("1234", 19, 38, 60),
        w("ステート・ストリート・スパイダー", 53, 105, 60),   # 中心79 → 銘柄名
        w("ゴー", 108.31, 118, 60),      # 間隔3.31pt・中心113.2で境界112を越える
        w("100", 134, 148, 60),          # 間隔16pt・列の区切りなので結合しない
        w("500", 184, 198, 60), w("600", 213, 227, 60),
        w("60,000円", 248, 281, 60), w("0円", 298, 310, 60),
        w("10,000円", 318, 356, 60), w("20.00%", 374, 402, 60),
        w("架空", 411, 426, 60),
    ]
    words += [w("ルド", 53, 72, 70), w("ETF(為替ヘッジなし)", 75.31, 140, 70),
              w("証券", 411, 426, 70)]
    return [words]


def test_name_overflowing_into_next_column():
    result = parse_words(_overflowing_name_doc())
    assert not result.report.unparsed_lines      # 旧実装では行ごと捨てられた
    assert len(result.holdings) == 1
    h = result.holdings[0]
    assert h.name_raw == "ステート・ストリート・スパイダーゴールドETF(為替ヘッジなし)"
    assert h.quantity == Decimal("100")          # 旧実装では 'ゴー100' で読めなかった
    assert h.avg_cost == Decimal("500")
    assert h.value_jpy == Decimal("60000")
    assert h.institution == "架空証券"
    assert h.confidence == 1.0 and not h.warnings
    assert result.report.sections[0].ok


def test_column_gap_still_separates_adjacent_cells():
    """列の区切り（実測11.25pt以上）は結合しない。"""
    result = parse_words(_mini_stock_doc("10", "225", "2,245円"))
    h = result.holdings[0]
    assert h.code_raw == "1234"
    assert h.name_raw == "架空銘柄"              # コード列と銘柄名が混ざらない
    assert h.quantity == Decimal("10")


# ----------------------------------------------------------------------
# 保有金融機関の縦組み（1文字幅まで潰れた列）
# ----------------------------------------------------------------------


def _vertical_stock_row(top, code, name_parts, qty, avg, price, value, pl, pct, inst):
    """機関名が1文字ずつ縦に積まれた株式行。銘柄名も折返して同じ行に同居する。

    実PDFの実測に合わせ、行送りは 10.3pt。name_parts[0] と inst[0] がアンカー行、
    以降は1行ずつ下に積まれる。
    """
    head, *tail = name_parts
    words = [
        w(code, 19, 38, top), w(head, 62, 118, top),
        w(qty, 134, 148, top), w(avg, 184, 198, top), w(price, 213, 227, top),
        w(value, 248, 281, top), w("0円", 298, 310, top),
        w(pl, 318, 356, top), w(pct, 374, 402, top),
        w(inst[0], 414, 424, top),
    ]
    for i, ch in enumerate(inst[1:]):
        line_top = top + 10.3 * (i + 1)
        words.append(w(ch, 414, 424, line_top))
        if i < len(tail):  # 銘柄名の折返し断片は機関名の文字と同居する
            words.append(w(tail[i], 62, 118, line_top))
    return words


def _vertical_institution_doc():
    """保有金融機関が1文字幅に潰れた 株式(現物) セクション。"""
    words = []
    words += heading("株式(現物)", 10)
    words += total_line("400,000", 20)
    words += stock_header_vertical(30)
    words += _vertical_stock_row(
        90, "1234", ["IF ABCD+ゴー", "ルド"],
        "100", "500", "600", "60,000円", "10,000円", "20.00%", "架空コネクト証券",
    )
    # 銘柄名が3行に割れ、機関名7文字と同居するケース
    words += _vertical_stock_row(
        200, "5678", ["架空プラチナ上場", "信託(現物国内", "保管型)"],
        "40", "8,000", "8,500", "340,000円", "20,000円", "6.25%", "架空ネット証券",
    )
    return [words]


def test_vertical_institution_column():
    result = parse_words(_vertical_institution_doc())
    assert len(result.holdings) == 2
    h1, h2 = result.holdings
    # 縦組みの機関名がすべて結合される
    assert h1.institution == "架空コネクト証券"
    assert h2.institution == "架空ネット証券"
    # 機関名と同居していた銘柄名の折返し断片も失われない
    assert h1.name_raw == "IF ABCD+ゴールド"
    assert h2.name_raw == "架空プラチナ上場信託(現物国内保管型)"
    assert h1.code_raw == "1234"
    assert h1.quantity == Decimal("100")
    assert h1.value_jpy == Decimal("60000")
    assert h2.quantity == Decimal("40")
    assert h2.value_jpy == Decimal("340000")
    # 機関名の文字が評価損益率へ紛れ込んでいないこと（旧実装では "20.00%大" になった）
    assert h1.meta.get("pl_pct") == "20.00%"
    assert h2.meta.get("pl_pct") == "6.25%"
    assert all(h.confidence == 1.0 and not h.warnings for h in result.holdings)
    assert not result.report.unparsed_lines
    assert result.report.sections[0].ok


# ----------------------------------------------------------------------
# 表示名結合・PDF日付
# ----------------------------------------------------------------------


def test_join_display_ascii_boundary_rule():
    assert join_display(["IF", "FANG+ゴールド"]) == "IF FANG+ゴールド"
    assert join_display(["ソフトバンクグル", "ープ"]) == "ソフトバンクグループ"
    assert join_display(["eMAXIS", "Slim", "米"]) == "eMAXIS Slim米"
    assert join_display(["", "abc", "", "def"]) == "abc def"


def test_parse_pdf_date():
    assert _parse_pdf_date("D:20260804033448+00'00'") == date(2026, 8, 4)
    assert _parse_pdf_date("D:20261301000000") is None  # 不正な月
    assert _parse_pdf_date("garbage") is None
    assert _parse_pdf_date(None) is None


# ----------------------------------------------------------------------
# 実PDF統合テスト（data/ にPDFがある場合のみ・金額定数は書かない）
# ----------------------------------------------------------------------


@pytest.mark.skipif(not REAL_PDF.exists(), reason="実PDFなし")
def test_real_pdf_three_layer_verification():
    result, suggested = parse_pdf(REAL_PDF.read_bytes())
    rep = result.report
    assert rep.grand_total_ok
    assert len(rep.sections) == 6
    for s in rep.sections:
        assert s.ok, f"セクション検算NG: {s.name}"
        assert s.row_count > 0
    assert all(h.confidence >= 0.7 for h in result.holdings)
    assert suggested is not None
    assert not rep.unparsed_lines


@pytest.mark.skipif(not REAL_PDF.exists(), reason="実PDFなし")
def test_real_pdf_importer_protocol():
    importer = MfPdfImporter()
    result = importer.parse(REAL_PDF)
    assert result.source_kind == "mf_pdf"
    assert result.report.grand_total_ok
