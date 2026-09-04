"""Shared importer contracts and normalization helpers.

すべてのインポータ（MF PDF、将来のスクショOCR・CSV）は ParseResult を返し、
以降の照合・プレビュー・確定処理を共用する。
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class SectionType(str, Enum):
    DEPOSIT = "deposit"    # 預金・現金
    STOCK = "stock"        # 株式(現物)
    FUND = "fund"          # 投資信託
    CRYPTO = "crypto"      # 暗号資産（取込対象外・検算のみ）
    PENSION = "pension"    # 年金
    POINT = "point"        # ポイント
    UNKNOWN = "unknown"


class ParsedHolding(BaseModel):
    """1保有行の共通中間表現。"""

    section: SectionType
    institution: str                    # 保有金融機関（折返し結合済み）。年金は '年金'
    name_raw: str
    code_raw: str | None = None
    quantity: Decimal | None = None
    avg_cost: Decimal | None = None     # 平均取得単価（年金は取得価額の総額）
    price: Decimal | None = None        # 現在値/基準価額
    value_jpy: Decimal | None = None    # 評価額/残高（円）
    pl_jpy: Decimal | None = None       # 評価損益（円）
    meta: dict[str, Any] = Field(default_factory=dict)  # 換算レート・有効期限・currency_hint等
    confidence: float = 1.0
    warnings: list[str] = Field(default_factory=list)


class SectionReport(BaseModel):
    name: str
    section: SectionType
    expected_total: Decimal | None = None   # PDFの「合計：N円」
    computed_total: Decimal | None = None   # パース行の合計
    ok: bool = True
    row_count: int = 0


class ParseReport(BaseModel):
    grand_total_expected: Decimal | None = None  # 資産総額
    grand_total_computed: Decimal | None = None
    grand_total_ok: bool = True
    sections: list[SectionReport] = Field(default_factory=list)
    unparsed_lines: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ParseResult(BaseModel):
    source_kind: str                     # 'mf_pdf' | 将来 'screenshot' | 'csv'
    holdings: list[ParsedHolding] = Field(default_factory=list)
    report: ParseReport = Field(default_factory=ParseReport)


class HoldingsImporter(Protocol):
    source_kind: str

    def parse(self, path: Path) -> ParseResult: ...


# ----------------------------------------------------------------------
# normalization helpers
# ----------------------------------------------------------------------

_CODE_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")
_TRAILING_BRACKET_NUM_RE = re.compile(r"[（(]\d{1,6}[）)]\s*$")


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def normalize_code(raw: str | None, known_codes: set[str] | None = None) -> str | None:
    """証券コードの正規化。

    - NFKC・空白除去・大文字化
    - 生コードが既知コードに一致すればそのまま（切詰め優先ルールの例外）
    - 5文字末尾'0'かつ先頭4文字が有効形式なら4文字へ切詰め（大和証券系 28400→2840）
    - 有効形式（^[0-9][0-9A-Z]{3}$）でなければ None
    """
    if not raw:
        return None
    code = nfkc(raw).strip().upper().replace(" ", "")
    if not code:
        return None
    if known_codes and code in known_codes:
        return code
    if len(code) == 5 and code.endswith("0") and _CODE_RE.match(code[:4]):
        return code[:4]
    if _CODE_RE.match(code):
        return code
    return None


def make_name_key(name: str) -> str:
    """投信等の名前ベース照合キー。

    NFKC → 小文字化 → 全空白除去 → 中黒除去 → 末尾の丸括弧数字を除去。
    折返し結合のスペース復元ヒューリスティックに依存しない（空白は全除去）。
    """
    s = nfkc(name)
    s = _TRAILING_BRACKET_NUM_RE.sub("", s)
    s = s.lower()
    s = re.sub(r"\s+", "", s)
    s = s.replace("・", "").replace("･", "")
    return s


_NUM_RE = re.compile(r"^[-+−▲]?[0-9,，]+(?:\.[0-9]+)?$")


def clean_number(text: str | None) -> Decimal | None:
    """'1,234円' '-5.84%' '2,256ポイント' 等から Decimal を取り出す。失敗時 None。"""
    if text is None:
        return None
    s = nfkc(str(text)).strip()
    if not s:
        return None
    s = re.sub(r"(円|%|％|ポイント|マイル|コイン|口|株|g)$", "", s).strip()
    neg = False
    if s.startswith(("▲", "△")):
        neg = True
        s = s[1:]
    s = s.replace("−", "-").replace("，", ",").replace(",", "")
    if not s:
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if neg else value


def read_csv_text(path: Path) -> str:
    """将来のCSV取込用: utf-8-sig → cp932 の順で自動判別（Crypto-Summary踏襲）。"""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
