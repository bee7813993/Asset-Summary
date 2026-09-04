"""マネーフォワードME 資産内訳PDF の座標ベースパーサ。

pdfplumber ``extract_words()`` の word リスト（dict: text/x0/x1/top/bottom）を
入力とする純関数 ``parse_words()`` を中核とし、PDF入出力は薄いラッパに分離する。
テストは合成 word リストで行う（実PDFは統合テストのみ）。

設計書「MF PDF インポート」節のとおり:
- セクション見出し完全一致（NFKC後）で状態遷移。状態はページを跨いで持続。
- ヘッダ行再出現時は列スパン（隣接ヘッダx範囲の間隙中点で境界・両端はページ余白）
  のみ再計算。ヘッダ語は列幅に応じて複数行に割れる（銘柄コ/ード、保有/金融/機関、
  列が1文字幅まで潰れると 保/有/金/融/機/関 の縦組みになる）ため、x範囲が重なる語を
  「縦ラン」としてまとめ、連結したテキストをヘッダ語と照合する。
- レコード検出 = アンカーセル。アンカー無し行は直前レコードの折返し断片。
- 3層検算: 行内(±2円) → セクション(合計:N円) → 総額(資産総額)。
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .base import (
    ParsedHolding,
    ParseReport,
    ParseResult,
    SectionReport,
    SectionType,
    clean_number,
    nfkc,
)

SOURCE_KIND = "mf_pdf"

LINE_CLUSTER_TOL = 2.0        # top座標の行クラスタ許容
FRAGMENT_MAX_GAP = 14.0       # 折返し断片とみなす最大行間（実測 約10.3pt）
ROW_CHECK_TOL = Decimal("2")  # 行内検算の許容誤差（円）
HEADER_MAX_WRAP_LINES = 8     # ヘッダ縦ラン蓄積の上限行数（暴走防止）
HEADER_RUN_MIN_OVERLAP = 1.0  # 縦ランに追記する最小x重なり幅(pt)
# 同一セル内で語が切れたとみなす最大の語間(pt)。実測（2026-08-10のPDF・株式/投信の
# 隣接語590組）ではセル内の切れ目は 3.31pt、列の区切りは最小 11.25pt で、
# 両者の間には重なりがない。
CELL_GAP_MAX = 6.0

# 検算不一致・欠損の confidence 減点
PENALTY_CHECK_FAILED = 0.35
PENALTY_MISSING_FIELD = 0.25
PENALTY_MINOR = 0.1

SECTION_HEADINGS: dict[str, SectionType] = {
    "預金・現金": SectionType.DEPOSIT,
    "株式(現物)": SectionType.STOCK,
    "投資信託": SectionType.FUND,
    "暗号資産": SectionType.CRYPTO,
    "年金": SectionType.PENSION,
    "ポイント": SectionType.POINT,
}

SECTION_LABELS: dict[SectionType, str] = {v: k for k, v in SECTION_HEADINGS.items()}

# セクションごとのヘッダ語 → 論理列名（NFKC正規化後の完全一致）
_DEPOSIT_HEADERS = {
    "種類・名称": "name",
    "残高": "balance",
    "保有金融機関": "institution",
    "変更": "_",
    "削除": "_",
}
_HEADER_TOKENS: dict[SectionType, dict[str, str]] = {
    SectionType.DEPOSIT: _DEPOSIT_HEADERS,
    SectionType.CRYPTO: _DEPOSIT_HEADERS,
    SectionType.STOCK: {
        "銘柄コード": "code",
        "銘柄名": "name",
        "保有数": "quantity",
        "平均取得単価": "avg_cost",
        "現在値": "price",
        "評価額": "value",
        "前日比": "day_change",
        "評価損益": "pl",
        "評価損益率": "pl_pct",
        "保有金融機関": "institution",
        "取得日": "acquired",
        "変更": "_",
        "削除": "_",
    },
    SectionType.FUND: {
        "銘柄名": "name",
        "保有数": "quantity",
        "平均取得単価": "avg_cost",
        "基準価額": "price",
        "評価額": "value",
        "前日比": "day_change",
        "評価損益": "pl",
        "評価損益率": "pl_pct",
        "保有金融機関": "institution",
        "取得日": "acquired",
        "変更": "_",
        "削除": "_",
    },
    SectionType.PENSION: {
        "名称": "name",
        "取得価額": "acq_value",
        "現在価値": "value",
        "評価損益": "pl",
        "評価損益率": "pl_pct",
        "取得日": "acquired",
        "変更": "_",
        "削除": "_",
    },
    SectionType.POINT: {
        "名称": "name",
        "種類": "ptype",
        "ポイント・マイル数": "quantity",
        "換算レート": "rate",
        "現在の価値": "value",
        "有効期限": "expiry",
        "保有金融機関": "institution",
        "変更": "_",
        "削除": "_",
    },
}

# テキスト列（折返し断片の結合先として許可する列）
_TEXT_COLUMNS = {"name", "institution", "ptype", "code", "expiry", "acquired"}

# 通貨ヒント（預金名称 → ISO通貨コード）。円換算済みJPY残高として保存し、記録のみ。
_CURRENCY_HINTS = (
    ("豪ドル", "AUD"),
    ("米ドル", "USD"),
    ("ユーロ", "EUR"),
    ("英ポンド", "GBP"),
    ("ポンド", "GBP"),
    ("ドル", "USD"),
)

# 最終ページのフッタナビ開始の目印（以降の行はすべて無視する）。
# 出現順はPDFによって変わるため、最初に当たったものでフッタ判定に入る。
_FOOTER_MARKERS = (
    "サービスについて",
    "プレスキット",
    "特定商取引法",
    "電子決済等代行業",
)

# 印刷時に全ページへ入る装飾。表と同じ列域に落ちるので、行になる前に語ごと落とす。
# 落とさないと折返し断片として直前のレコードに結合され、銘柄名が汚れる。
# - ヘルプ・サポート: ページ番号が語の中に挟まる（例 'ヘルプ・サポー1/8ト'）
# - フッタのURL: 左端＝名称列に落ちる
_PAGE_CHROME_WORD_RE = re.compile(r"^ヘルプ[・･]?サポー.*ト$|^https?://")
# ページ冒頭の日時見出し（例 '2026/08/24 16:37 マネーフォワード ME'）は行単位で無視する
_PAGE_HEADER_RE = re.compile(
    r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\s+マネーフォワード\s*ME$"
)

_TOTAL_RE = re.compile(r"^合計[::]?\s*(.+?)\s*$")
_GRAND_TOTAL_RE = re.compile(r"^資産総額[::]?\s*(.+?)\s*$")
_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


def _is_pua(ch: str) -> bool:
    return 0xE000 <= ord(ch) <= 0xF8FF or 0xF0000 <= ord(ch) <= 0x10FFFD


def _strip_pua(text: str) -> str:
    return "".join(ch for ch in text if not _is_pua(ch))


def _is_doubled_label(text: str) -> bool:
    """円グラフの二重文字ラベル（同一文字2連続×3回以上）。例: 株株式式現現物物"""
    if len(text) < 6 or len(text) % 2 != 0:
        return False
    return all(text[i] == text[i + 1] for i in range(0, len(text), 2))


def _is_ascii_visible(ch: str) -> bool:
    return "!" <= ch <= "~"


def join_display(parts: Iterable[str]) -> str:
    """表示名の結合。境界の両側がASCII英数記号のときのみ半角スペースを挿入。"""
    out = ""
    for part in parts:
        if not part:
            continue
        if out and _is_ascii_visible(out[-1]) and _is_ascii_visible(part[0]):
            out += " "
        out += part
    return out


def cluster_lines(words: list[dict[str, Any]], tol: float = LINE_CLUSTER_TOL) -> list[dict[str, Any]]:
    """top座標で行クラスタ。返値: [{"top": float, "words": [word,...]}]（x0順）"""
    lines: list[dict[str, Any]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1]["top"] - w["top"]) <= tol:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
    return lines


class _ColumnSpans:
    """ヘッダ行から求めた列スパン。単語中心x → 論理列名。"""

    def __init__(self, columns: list[tuple[str, float, float]]):
        # columns: [(論理列名, header_x0, header_x1)] x0昇順
        self.columns = columns
        self.boundaries: list[float] = [
            (columns[i][2] + columns[i + 1][1]) / 2 for i in range(len(columns) - 1)
        ]

    def column_of(self, word: dict[str, Any]) -> str:
        center = (word["x0"] + word["x1"]) / 2
        for i, b in enumerate(self.boundaries):
            if center < b:
                return self.columns[i][0]
        return self.columns[-1][0]

    def header_range(self, col: str) -> tuple[float, float] | None:
        for name, x0, x1 in self.columns:
            if name == col:
                return (x0, x1)
        return None


class _HeaderRun:
    """ヘッダ語の縦ラン。同じ列に縦積みされた語をx範囲の重なりでまとめる。

    列幅が足りないとMFのPDFはヘッダ語を折返す。折返し方は列幅次第で
    「保有/金融/機関」にも「保/有/金/融/機/関」にもなるため、行ごとの完全一致では
    なくランの連結テキストで照合する。
    """

    __slots__ = ("x0", "x1", "parts")

    def __init__(self, word: dict[str, Any]):
        self.x0: float = word["x0"]
        self.x1: float = word["x1"]
        self.parts: list[str] = [word["text"]]

    def overlap(self, word: dict[str, Any]) -> float:
        return min(self.x1, word["x1"]) - max(self.x0, word["x0"])

    def add(self, word: dict[str, Any]) -> None:
        self.x0 = min(self.x0, word["x0"])
        self.x1 = max(self.x1, word["x1"])
        self.parts.append(word["text"])

    def text(self) -> str:
        return nfkc("".join(self.parts)).strip()


def _start_header_runs(
    line_words: list[dict[str, Any]], tokens: dict[str, str]
) -> list[_HeaderRun] | None:
    """ヘッダ行なら縦ランの初期状態を返す。未一致語も後続行の受け皿として残す。"""
    matched = sum(1 for w in line_words if nfkc(w["text"]).strip() in tokens)
    if matched < 3:
        return None
    return [_HeaderRun(w) for w in sorted(line_words, key=lambda w: w["x0"])]


def _absorb_header_line(
    runs: list[_HeaderRun], line_words: list[dict[str, Any]]
) -> None:
    """後続行の語を最も重なるランへ追記（重なるランが無ければ新ラン）。"""
    for w in sorted(line_words, key=lambda w: w["x0"]):
        best: _HeaderRun | None = None
        best_overlap = HEADER_RUN_MIN_OVERLAP
        for run in runs:
            ov = run.overlap(w)
            if ov > best_overlap:
                best, best_overlap = run, ov
        if best is None:
            runs.append(_HeaderRun(w))
        else:
            best.add(w)
    runs.sort(key=lambda r: r.x0)


def _spans_from_runs(
    runs: list[_HeaderRun], tokens: dict[str, str]
) -> _ColumnSpans | None:
    """ランの連結テキストをヘッダ語と照合して列スパンを組む。"""
    columns = [
        (col, run.x0, run.x1)
        for run in runs
        if (col := tokens.get(run.text())) is not None
    ]
    if len(columns) < 3:
        return None
    columns.sort(key=lambda c: c[1])
    return _ColumnSpans(columns)


class _Record:
    def __init__(self, section: SectionType, page: int, top: float):
        self.section = section
        self.page = page
        self.top = top
        self.last_line_top = top
        self.cells: dict[str, list[str]] = {}

    def add(self, col: str, text: str) -> None:
        self.cells.setdefault(col, []).append(text)

    def cell(self, col: str) -> str:
        return nfkc(join_display(self.cells.get(col, []))).strip()

    def cell_number(self, col: str) -> Decimal | None:
        return clean_number(self.cell(col))


def _assign_columns(
    section: SectionType, spans: _ColumnSpans, words: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """列割当（中心x基準）+ 実測ずれの補正。

    - 株式: 銘柄コード列はヘッダx1より右から始まる語を銘柄名列へ寄せる
      （短い銘柄名先頭語が見かけ上コード列に落ちるため）。
    - 預金/暗号資産: 残高セルの小さな右寄せ数値が金融機関列に落ちるため、
      「N円」形の語は残高列へ寄せる。
    - 最後に、直前の語とほぼ地続き（CELL_GAP_MAX未満）の語を同じ列へ戻す
      （長い銘柄名が列幅を超えて隣の列へせり出すため。下の詳細を参照）。
    """
    assigned = [(spans.column_of(w), w) for w in words]
    if section == SectionType.STOCK:
        rng = spans.header_range("code")
        if rng:
            fixed = []
            for col, w in assigned:
                if col == "code" and w["x0"] > rng[1] + 1.0:
                    col = "name"
                fixed.append((col, w))
            assigned = fixed
    if section in (SectionType.DEPOSIT, SectionType.CRYPTO):
        has_balance = any(col == "balance" for col, _ in assigned)
        if not has_balance:
            fixed = []
            for col, w in assigned:
                text = nfkc(w["text"]).strip()
                if (
                    col == "institution"
                    and text.endswith("円")
                    and clean_number(text) is not None
                    and not has_balance
                ):
                    col = "balance"
                    has_balance = True
                fixed.append((col, w))
            assigned = fixed

    # セルの中身が列幅に収まらないと、はみ出した部分の中心xが隣の列に入り、
    # 中心x基準の割当だけでは別のセルに落ちる。実際 2026-08-10 のPDFでは
    # 「ステート・ストリート・スパイダー|ゴー」の "ゴー" が保有数へ落ち、
    # 保有数セルが 'ゴー500' になって行ごと解析不能になった。
    # セル内の語の切れ目は文字送りぶんしか空かないので、直前の語と地続きなら
    # 同じセルとみなす。境界の判定は列スパンのままにして、割当だけを直す。
    fixed = []
    for i, (col, word) in enumerate(assigned):
        if i and word["x0"] - assigned[i - 1][1]["x1"] < CELL_GAP_MAX:
            col = fixed[i - 1][0]
        fixed.append((col, word))
    return fixed


def _has_anchor(section: SectionType, cells: dict[str, list[str]]) -> bool:
    def num(col: str) -> Decimal | None:
        return clean_number(nfkc("".join(cells.get(col, []))))

    def yen(col: str) -> bool:
        text = nfkc("".join(cells.get(col, []))).strip()
        return text.endswith("円") and clean_number(text) is not None

    if section in (SectionType.DEPOSIT, SectionType.CRYPTO):
        return yen("balance")
    if section in (SectionType.STOCK, SectionType.FUND):
        return num("quantity") is not None and yen("value")
    if section == SectionType.PENSION:
        # 取得価額は必須にしない。確定拠出年金の「待機資金」（入金済み・買付前）は
        # 取得原価が存在せず、現在価値の列だけが埋まる。
        return yen("value")
    if section == SectionType.POINT:
        return num("quantity") is not None and yen("value")
    return False


class _Parser:
    def __init__(self) -> None:
        self.holdings: list[ParsedHolding] = []
        self.report = ParseReport()
        self.section = SectionType.UNKNOWN
        self.spans: _ColumnSpans | None = None
        self.record: _Record | None = None
        self.section_reports: dict[SectionType, SectionReport] = {}
        self.grand_expected: Decimal | None = None
        # ヘッダ縦ラン蓄積中の状態（アンカー行に達するまで後続行を吸収する）
        self.header_runs: list[_HeaderRun] | None = None
        self.header_lines = 0

    # -- section bookkeeping -------------------------------------------

    def _section_report(self, section: SectionType) -> SectionReport:
        rep = self.section_reports.get(section)
        if rep is None:
            rep = SectionReport(
                name=SECTION_LABELS.get(section, section.value), section=section
            )
            self.section_reports[section] = rep
        return rep

    def _enter_section(self, section: SectionType) -> None:
        self._finalize_record()
        self.section = section
        self.spans = None
        self.header_runs = None
        self.header_lines = 0
        self._section_report(section)

    # -- header accumulation -------------------------------------------

    def _finish_header(self, tokens: dict[str, str]) -> None:
        """蓄積中の縦ランを列スパンとして確定する。"""
        runs, self.header_runs = self.header_runs, None
        self.header_lines = 0
        if runs is None:
            return
        spans = _spans_from_runs(runs, tokens)
        if spans is not None:
            self.spans = spans

    def _absorb_header_line(
        self, tokens: dict[str, str], line_words: list[dict[str, Any]]
    ) -> bool:
        """蓄積中に来た行を処理。Trueならヘッダの一部として消費した。

        暫定スパンでアンカーが立つ行＝最初のデータ行なので、そこで蓄積を打ち切る。
        """
        runs = self.header_runs
        if runs is None:
            return False
        if self.header_lines < HEADER_MAX_WRAP_LINES:
            spans = _spans_from_runs(runs, tokens)
            if spans is not None:
                cells: dict[str, list[str]] = {}
                for col, word in _assign_columns(self.section, spans, line_words):
                    cells.setdefault(col, []).append(word["text"])
                if not _has_anchor(self.section, cells):
                    _absorb_header_line(runs, line_words)
                    self.header_lines += 1
                    return True
        self._finish_header(tokens)
        return False

    # -- record finalization -------------------------------------------

    def _finalize_record(self) -> None:
        rec, self.record = self.record, None
        if rec is None:
            return
        holding = _build_holding(rec)
        if holding is None:
            return
        self.holdings.append(holding)
        rep = self._section_report(rec.section)
        rep.row_count += 1
        if holding.value_jpy is not None:
            rep.computed_total = (rep.computed_total or Decimal("0")) + holding.value_jpy

    # -- line processing -----------------------------------------------

    def feed_page(self, page_no: int, words: list[dict[str, Any]]) -> None:
        footer_mode = False
        for line in cluster_lines(_filter_words(words)):
            if footer_mode:
                continue
            line_words = line["words"]
            if not line_words:
                continue
            text = nfkc(" ".join(w["text"] for w in line_words)).strip()
            if not text:
                continue
            if any(marker in text for marker in _FOOTER_MARKERS):
                footer_mode = True
                continue
            if "©" in text or _PAGE_HEADER_RE.match(text):
                continue
            self._feed_line(page_no, line["top"], line_words, text)
        # 折返し断片はページを跨がない（ヘッダが再掲されるため）
        tokens = _HEADER_TOKENS.get(self.section)
        if tokens and self.header_runs is not None:
            self._finish_header(tokens)
        self._finalize_record()

    def _feed_line(
        self, page_no: int, top: float, line_words: list[dict[str, Any]], text: str
    ) -> None:
        # セクション見出し（単語1つの行・完全一致）
        if len(line_words) == 1 and text in SECTION_HEADINGS:
            self._enter_section(SECTION_HEADINGS[text])
            return

        if self.section == SectionType.UNKNOWN:
            m = _GRAND_TOTAL_RE.match(text)
            if m:
                self.grand_expected = clean_number(m.group(1))
            return  # セクション開始前のナビ・説明文はノイズ

        tokens = _HEADER_TOKENS.get(self.section)

        # セクション合計「合計:N円」
        m = _TOTAL_RE.match(text)
        if m:
            if tokens and self.header_runs is not None:
                self._finish_header(tokens)
            self._finalize_record()
            total = clean_number(m.group(1))
            if total is not None:
                self._section_report(self.section).expected_total = total
            return

        if tokens:
            # ヘッダ蓄積中なら折返し行を吸収。消費しなければ列スパンが確定して下へ。
            if self.header_runs is not None and self._absorb_header_line(
                tokens, line_words
            ):
                return
            runs = _start_header_runs(line_words, tokens)
            if runs is not None:
                self._finalize_record()
                self.header_runs = runs
                self.header_lines = 0
                return

        if self.spans is None:
            self._unparsed(page_no, top, text)
            return

        assigned = _assign_columns(self.section, self.spans, line_words)
        cells: dict[str, list[str]] = {}
        for col, w in assigned:
            cells.setdefault(col, []).append(w["text"])

        if _has_anchor(self.section, cells):
            self._finalize_record()
            rec = _Record(self.section, page_no, top)
            for col, w in assigned:
                if col != "_":
                    rec.add(col, w["text"])
            self.record = rec
            return

        # 折返し断片: 直前レコードへ結合（近接行・テキスト列のみ）
        rec = self.record
        if (
            rec is not None
            and rec.page == page_no
            and top - rec.last_line_top <= FRAGMENT_MAX_GAP
            and all(col in _TEXT_COLUMNS or col == "_" for col, _ in assigned)
        ):
            for col, w in assigned:
                if col != "_":
                    rec.add(col, w["text"])
            rec.last_line_top = top
            return

        self._unparsed(page_no, top, text)

    def _unparsed(self, page_no: int, top: float, text: str) -> None:
        self.report.unparsed_lines.append(
            {"page": page_no, "top": round(top, 2), "text": text}
        )

    # -- result --------------------------------------------------------

    def result(self) -> ParseResult:
        self._finalize_record()
        report = self.report
        report.sections = list(self.section_reports.values())
        computed_grand = Decimal("0")
        any_computed = False
        for rep in report.sections:
            if rep.computed_total is None and rep.row_count == 0:
                rep.computed_total = Decimal("0")
            if rep.expected_total is not None:
                rep.ok = rep.computed_total == rep.expected_total
                if not rep.ok:
                    report.warnings.append(
                        f"セクション「{rep.name}」の合計が一致しません"
                        f"（期待 {rep.expected_total}円 / 集計 {rep.computed_total}円）"
                    )
            if rep.computed_total is not None:
                computed_grand += rep.computed_total
                any_computed = True
        report.grand_total_expected = self.grand_expected
        report.grand_total_computed = computed_grand if any_computed else None
        if self.grand_expected is not None:
            report.grand_total_ok = report.grand_total_computed == self.grand_expected
            if not report.grand_total_ok:
                report.warnings.append(
                    f"資産総額が一致しません（期待 {self.grand_expected}円 / "
                    f"集計 {report.grand_total_computed}円）"
                )
        if report.unparsed_lines:
            report.warnings.append(
                f"解析できなかった行が {len(report.unparsed_lines)} 件あります"
            )
        return ParseResult(source_kind=SOURCE_KIND, holdings=self.holdings, report=report)


def _filter_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ノイズ単語の除去（ページ共通の装飾・円グラフ二重文字ラベル）。"""
    out = []
    for w in words:
        text = _strip_pua(str(w["text"])).strip()
        if not text:
            continue
        norm = nfkc(text)
        if _PAGE_CHROME_WORD_RE.search(norm):
            continue
        if _is_doubled_label(norm):
            continue
        out.append({**w, "text": text})
    return out


def _warn(holding: ParsedHolding, message: str, penalty: float) -> None:
    holding.warnings.append(message)
    holding.confidence = max(0.0, round(holding.confidence - penalty, 4))


def _check_row(holding: ParsedHolding, divisor: int) -> None:
    """行内検算 quantity×price÷divisor ≒ value_jpy。

    許容誤差 = ±2円 + 数量×0.5÷divisor。PDFの現在値/基準価額は円未満が
    四捨五入表示のため、表示丸め由来の誤差（最大 数量×0.5÷divisor 円）を許容する。
    """
    if holding.quantity is None or holding.price is None or holding.value_jpy is None:
        return
    computed = holding.quantity * holding.price / Decimal(divisor)
    tol = ROW_CHECK_TOL + holding.quantity.copy_abs() * Decimal("0.5") / Decimal(divisor)
    if abs(computed - holding.value_jpy) > tol:
        _warn(
            holding,
            f"行内検算が一致しません（{holding.quantity}×{holding.price}÷{divisor}"
            f"≒{computed:.0f}円 ≠ 評価額 {holding.value_jpy}円）",
            PENALTY_CHECK_FAILED,
        )


def _build_holding(rec: _Record) -> ParsedHolding | None:
    section = rec.section
    name = rec.cell("name")
    meta: dict[str, Any] = {}

    if section in (SectionType.DEPOSIT, SectionType.CRYPTO):
        balance = rec.cell_number("balance")
        if section == SectionType.DEPOSIT:
            for token, ccy in _CURRENCY_HINTS:
                if token in name:
                    meta["currency_hint"] = ccy
                    break
        holding = ParsedHolding(
            section=section,
            institution=rec.cell("institution"),
            name_raw=name,
            value_jpy=balance,
            meta=meta,
        )
        if balance is None:
            _warn(holding, "残高を読み取れませんでした", PENALTY_MISSING_FIELD)
        return holding

    if section in (SectionType.STOCK, SectionType.FUND):
        for col, key in (("day_change", "day_change"), ("pl_pct", "pl_pct"),
                         ("acquired", "acquired_on")):
            val = rec.cell(col)
            if val:
                meta[key] = val
        holding = ParsedHolding(
            section=section,
            institution=rec.cell("institution"),
            name_raw=name,
            code_raw=rec.cell("code") or None,
            quantity=rec.cell_number("quantity"),
            avg_cost=rec.cell_number("avg_cost"),
            price=rec.cell_number("price"),
            value_jpy=rec.cell_number("value"),
            pl_jpy=rec.cell_number("pl"),
            meta=meta,
        )
        for field, label in (("quantity", "保有数"), ("avg_cost", "平均取得単価"),
                             ("price", "現在値" if section == SectionType.STOCK else "基準価額"),
                             ("value_jpy", "評価額")):
            if getattr(holding, field) is None:
                _warn(holding, f"{label}を読み取れませんでした", PENALTY_MISSING_FIELD)
        _check_row(holding, 10000 if section == SectionType.FUND else 1)
        return holding

    if section == SectionType.PENSION:
        acq = rec.cell_number("acq_value")
        value = rec.cell_number("value")
        pl = rec.cell_number("pl")
        if rec.cell("pl_pct"):
            meta["pl_pct"] = rec.cell("pl_pct")
        holding = ParsedHolding(
            section=section,
            institution="年金",
            name_raw=name,
            avg_cost=acq,          # 年金は取得価額の総額
            value_jpy=value,
            pl_jpy=pl,
            meta=meta,
        )
        if value is None:
            _warn(holding, "現在価値を読み取れませんでした", PENALTY_MISSING_FIELD)
        elif acq is None:
            # 待機資金は取得価額も損益も持たない（原価が無いので損益も出ない）。
            # 損益だけ出ている行で取得価額が欠けているなら、そちらは読み取り失敗。
            if pl is not None:
                _warn(holding, "取得価額を読み取れませんでした", PENALTY_MISSING_FIELD)
        elif pl is not None and abs(acq + pl - value) > ROW_CHECK_TOL:
            _warn(
                holding,
                f"行内検算が一致しません（取得価額 {acq}円 + 損益 {pl}円 ≠ 現在価値 {value}円）",
                PENALTY_CHECK_FAILED,
            )
        return holding

    if section == SectionType.POINT:
        quantity = rec.cell_number("quantity")
        rate = rec.cell_number("rate")
        value = rec.cell_number("value")
        if rec.cell("ptype"):
            meta["point_type"] = rec.cell("ptype")
        if rate is not None:
            meta["rate"] = str(rate)
        if rec.cell("expiry"):
            meta["expiry"] = rec.cell("expiry")
        holding = ParsedHolding(
            section=section,
            institution=rec.cell("institution"),
            name_raw=name,
            quantity=quantity,
            value_jpy=value,
            meta=meta,
        )
        if quantity is None or value is None:
            _warn(holding, "ポイント数または現在の価値を読み取れませんでした", PENALTY_MISSING_FIELD)
        elif rate is not None and abs(quantity * rate - value) > ROW_CHECK_TOL:
            _warn(
                holding,
                f"行内検算が一致しません（{quantity}×{rate} ≠ 現在の価値 {value}円）",
                PENALTY_CHECK_FAILED,
            )
        return holding

    return None


def parse_words(pages: list[list[dict[str, Any]]]) -> ParseResult:
    """wordリスト（ページごと）→ ParseResult の純関数。ページ番号は1始まり。"""
    parser = _Parser()
    for i, words in enumerate(pages, start=1):
        parser.feed_page(i, words)
    return parser.result()


def _parse_pdf_date(value: str | None) -> date | None:
    if not value:
        return None
    m = _PDF_DATE_RE.search(str(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def extract_pages(data: bytes) -> tuple[list[list[dict[str, Any]]], date | None]:
    """PDFバイト列 → (ページごとのwordリスト, suggested_as_of)。"""
    import pdfplumber

    pages: list[list[dict[str, Any]]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        meta = pdf.metadata or {}
        suggested = _parse_pdf_date(meta.get("CreationDate")) or _parse_pdf_date(
            meta.get("ModDate")
        )
        for page in pdf.pages:
            pages.append(
                [
                    {
                        "text": w["text"],
                        "x0": float(w["x0"]),
                        "x1": float(w["x1"]),
                        "top": float(w["top"]),
                        "bottom": float(w["bottom"]),
                    }
                    for w in page.extract_words()
                ]
            )
    return pages, suggested


def parse_pdf(data: bytes) -> tuple[ParseResult, date | None]:
    """PDFバイト列を解析。返値: (ParseResult, suggested_as_of)。"""
    pages, suggested = extract_pages(data)
    return parse_words(pages), suggested


class MfPdfImporter:
    """HoldingsImporter Protocol 実装。"""

    source_kind = SOURCE_KIND

    def parse(self, path: Path) -> ParseResult:
        result, _ = parse_pdf(Path(path).read_bytes())
        return result
