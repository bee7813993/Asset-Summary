"""取引履歴取込の共通データ契約（純粋なデータ定義のみ）。

各段（読取り→ヘッダ検出→列割当→行変換）の間を流れる値をここに集約する。
取引種別そのもの（TxType）は他所からも使うので core.models にある。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

# 判定に使うサンプル行数の上限（大きなファイルでも一定時間で終わらせる）
SAMPLE_ROWS = 200


def sample_rows(rows: list[int]) -> list[int]:
    """判定に使う行を **ファイル全体から満遍なく** 選ぶ。

    先頭から 200 行だけ見ると、長い履歴では古い時期の性格しか見えない。
    実際に数千行の長期履歴で、先頭 200 行は最初期の MRF ばかりで
    銘柄コードが 9 桁の協会コードだった。証券コードらしさが
    0.478 と拒否のしきい値 0.5 をわずかに下回り、**見出しが「銘柄コード」と
    完全一致しているのに列が捨てられていた**（全体で測れば 0.765）。
    その結果、5 桁コードの銘柄が 4 桁コードの同じ銘柄に結びつかなかった。

    等間隔で拾えば、書式が途中で変わっていても両方の時期が入る。
    """
    if len(rows) <= SAMPLE_ROWS:
        return list(rows)
    step = len(rows) / SAMPLE_ROWS
    return [rows[int(i * step)] for i in range(SAMPLE_ROWS)]

# 行の信頼度がこれを下回ると既定で取込対象から外す（matching.py と同じ規約）
CONFIDENCE_INCLUDE_THRESHOLD = 0.7

# 信頼度の減点幅。mf_pdf.py と同じ名前・同じ値にして 2 つの取込を読み比べやすくする。
PENALTY_CHECK_FAILED = 0.35
PENALTY_MISSING_FIELD = 0.25
PENALTY_MINOR = 0.1
PENALTY_UNRESOLVED_SECURITY = 0.3
PENALTY_UNKNOWN_TYPE = 0.2


class CanonicalField(str, Enum):
    """列に割り当てる正準フィールド。IGNORE はどこにも使わない列。"""

    TRADE_DATE = "trade_date"
    SETTLE_DATE = "settle_date"
    SECURITY_NAME = "security_name"
    SECURITY_CODE = "security_code"
    TX_TYPE = "tx_type"
    QUANTITY = "quantity"
    UNIT_PRICE = "unit_price"
    GROSS_AMOUNT = "gross_amount"
    NET_AMOUNT = "net_amount"
    FEE = "fee"
    TAX = "tax"
    ACCOUNT = "account"
    ACCOUNT_TYPE = "account_type"
    CURRENCY = "currency"
    EXCHANGE_RATE = "exchange_rate"
    NOTE = "note"
    IGNORE = "_"


# 数量・単価・約定代金・受渡金額は算術検算で総当たり確定させる（columns.py）。
# ヘッダ語彙が当てにならない書式でも、この 4 つは数値どうしの関係だけで決まる。
NUMERIC_QUARTET = (
    CanonicalField.QUANTITY,
    CanonicalField.UNIT_PRICE,
    CanonicalField.GROSS_AMOUNT,
    CanonicalField.NET_AMOUNT,
)

# 残りはスコア行列の最適割当（ハンガリアン法）で決まる
CATEGORICAL_FIELDS = (
    CanonicalField.TRADE_DATE,
    CanonicalField.SETTLE_DATE,
    CanonicalField.SECURITY_NAME,
    CanonicalField.SECURITY_CODE,
    CanonicalField.TX_TYPE,
    CanonicalField.ACCOUNT,
    CanonicalField.ACCOUNT_TYPE,
    CanonicalField.CURRENCY,
    CanonicalField.EXCHANGE_RATE,
    CanonicalField.NOTE,
)

# 手数料・税額は割当問題に混ぜない。複数列（手数料＋消費税 等）を合算しうるため、
# 割当後に残った数値列から拾って加算する。
ADDITIVE_FIELDS = (CanonicalField.FEE, CanonicalField.TAX)


@dataclass(frozen=True)
class SourceMeta:
    """入力の素性。プレビューにそのまま出して利用者に確認させる。"""

    kind: str = "csv"                       # csv | xlsx | paste
    encoding: str | None = None
    delimiter: str | None = None            # 複数空白区切りのときは None
    delimiter_mode: str = "char"            # char | multispace | single_column
    sheet_name: str | None = None
    sheet_names: tuple[str, ...] = ()
    filename: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SheetGrid:
    """表の生テキスト。NFKC 正規化はしない（プレビューは原文を見せる）。

    types は xlsx のときだけ埋まる（'date' | 'number' | 'text'）。
    日付セルが型として分かるぶん判定が正確になるので捨てずに持ち回る。
    """

    rows: tuple[tuple[str, ...], ...] = ()
    types: tuple[tuple[str, ...], ...] | None = None
    meta: SourceMeta = field(default_factory=SourceMeta)

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def cell(self, r: int, c: int) -> str:
        if 0 <= r < len(self.rows):
            row = self.rows[r]
            if 0 <= c < len(row):
                return row[c]
        return ""

    def cell_type(self, r: int, c: int) -> str:
        if self.types is None:
            return ""
        if 0 <= r < len(self.types):
            row = self.types[r]
            if 0 <= c < len(row):
                return row[c]
        return ""

    def column(self, c: int, start: int, end: int) -> list[str]:
        return [self.cell(r, c) for r in range(start, min(end, self.height))]


@dataclass(frozen=True)
class TableRegion:
    """ヘッダ行とデータ行の範囲。header_row=None はヘッダ無し書式（対応する）。"""

    header_row: int | None = None
    header_rows: tuple[int, ...] = ()
    data_start: int = 0
    data_end: int = 0
    header_score: float = 0.0
    headers: tuple[str, ...] = ()
    dropped: tuple[tuple[int, str], ...] = ()   # (行番号, 理由)


@dataclass(frozen=True)
class KnownSecurity:
    """判定に必要な最小限の銘柄情報（Security から写す）。"""

    id: int
    code: str | None
    name: str
    name_key: str
    price_unit_divisor: int = 1
    currency: str = "JPY"
    asset_class: str = "stock_jp"


@dataclass(frozen=True)
class KnownUniverse:
    """すでに DB にある銘柄・口座。列判定のいちばん強い信号になる。

    「当日の評価額を取得できている時点で銘柄は固定できている」— 証券会社ごとに
    銘柄名の表記が揺れても、既存の銘柄集合へ解決できる列が銘柄列である、という
    判定に使う。ヘッダ語彙より重い重みを与える（columns.W_KNOWN）。
    """

    securities: tuple[KnownSecurity, ...] = ()
    by_code: Mapping[str, KnownSecurity] = field(default_factory=dict)
    by_name_key: Mapping[str, KnownSecurity] = field(default_factory=dict)
    by_alias: Mapping[str, KnownSecurity] = field(default_factory=dict)
    known_codes: frozenset[str] = frozenset()
    account_names: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not self.securities


EMPTY_UNIVERSE = KnownUniverse()


@dataclass(frozen=True)
class IdentityCheck:
    """算術検算の結果。書式非依存性の要なのでプレビューにも出す。"""

    name: str            # 'qty*price=gross' | 'net-gross=fee+tax'
    tested: int = 0
    passed: int = 0
    divisor: int | None = None

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.tested) if self.tested else 0.0

    @property
    def conclusive(self) -> bool:
        """検算できるだけの行数があったか（少なすぎる検算は根拠にしない）。"""
        return self.tested >= MIN_IDENTITY_ROWS


# これ未満の行数しか検算できなかった識別式は、判定の根拠として採用しない。
# 3 行で妥協しているのは、数量×単価＝約定代金 が 3 行そろって成り立つのは
# 偶然では起きないため。5 行を要求すると、行数の少ないファイルで検算そのものが
# 効かなくなり（投信の divisor 判定が丸ごと落ちる）、判定が語彙頼みに戻る。
MIN_IDENTITY_ROWS = 3
# 検算の合格率がこれ以上なら「その割当は正しい」とみなす
IDENTITY_PASS_RATE = 0.9

# 価格の裏取りで銘柄を自動で結びつけるのに要る「一致した日数」の下限。
# 基準価額が何日も表示桁まで揃うのは同じ銘柄のときだけで、3 日そろえば
# 名前が略されていても別銘柄の取り違えはまず起きない（MIN_IDENTITY_ROWS
# と同じ考え方）。
MIN_PRICE_VERIFY_DAYS = 3

# 長期の履歴には、価格の訂正や特別な基準価額で数日だけ食い違う行が混じる。
# 全日一致を要求すると、88 日中 84 日そろっている銘柄がその 4 日のせいで
# 落ちる。そこで **検証できた日数が十分あるときに限り** 少しの食い違いを許す。
# 日数が少ないうちは全日一致のままなので、証拠が薄いところで緩むことはない。
#
# 実測（長期履歴の全銘柄 × 既存銘柄、数千組の総当たり）では
# この帯に入った 21 組すべてが正しい対応で、別銘柄の取り違えは 0 件だった。
PRICE_VERIFY_PASS_RATE = 0.9
MIN_PRICE_VERIFY_DAYS_WITH_STRAYS = 20


@dataclass(frozen=True)
class ColumnAssignment:
    """1 列の割当結果と、その根拠（プレビューで利用者に見せて直させる）。"""

    index: int
    header: str = ""
    field: CanonicalField = CanonicalField.IGNORE
    score: float = 0.0
    evidence: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, float], ...] = ()
    split_leading_code: bool = False     # '1234 架空商事' のような複合列

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "header": self.header,
            "field": self.field.value,
            "score": round(self.score, 3),
            "evidence": list(self.evidence),
            "alternatives": [
                {"field": f, "score": round(s, 3)} for f, s in self.alternatives
            ],
            "split_leading_code": self.split_leading_code,
        }


@dataclass(frozen=True)
class DetectedFormat:
    """列判定の全体像。remap ではこれを差し替えて再解析する。"""

    region: TableRegion = field(default_factory=TableRegion)
    columns: tuple[ColumnAssignment, ...] = ()
    identities: tuple[IdentityCheck, ...] = ()
    divisor: int = 1                     # 1=株 / 10000=投信（算術検算で判明する）
    sign_convention: str = "by_type"     # by_type | signed_quantity | signed_net | unsigned
    date_order: str = "ymd"              # ymd | dmy | mdy
    extra_fee_columns: tuple[int, ...] = ()
    extra_tax_columns: tuple[int, ...] = ()
    confidence: float = 0.0
    fingerprint: str | None = None
    profile_applied: str | None = None
    warnings: tuple[str, ...] = ()

    def column_for(self, f: CanonicalField) -> int | None:
        for col in self.columns:
            if col.field == f:
                return col.index
        return None

    def mapping(self) -> dict[int, CanonicalField]:
        return {c.index: c.field for c in self.columns if c.field != CanonicalField.IGNORE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "header_row": self.region.header_row,
            "headers": list(self.region.headers),
            "data_start": self.region.data_start,
            "data_end": self.region.data_end,
            "columns": [c.to_dict() for c in self.columns],
            "identities": [
                {
                    "name": i.name,
                    "tested": i.tested,
                    "passed": i.passed,
                    "pass_rate": round(i.pass_rate, 3),
                    "divisor": i.divisor,
                }
                for i in self.identities
            ],
            "divisor": self.divisor,
            "sign_convention": self.sign_convention,
            "date_order": self.date_order,
            "extra_fee_columns": list(self.extra_fee_columns),
            "extra_tax_columns": list(self.extra_tax_columns),
            "confidence": round(self.confidence, 3),
            "fingerprint": self.fingerprint,
            "profile_applied": self.profile_applied,
            "dropped": [{"row": r, "reason": why} for r, why in self.region.dropped],
            "warnings": list(self.warnings),
        }


@dataclass
class ParsedTx:
    """1 取引行の中間表現。

    ここでは「ファイルに何と書いてあったか」だけを持つ。現在保有との差分は
    取らない（スナップショットとの突合は core/cost_basis.py の仕事）。
    こうしておけば、台帳をどう永続化するかを後から変えても解析側は動かない。
    """

    row_index: int
    tx_type: str = "other"                  # core.models.TxType の値
    trade_date: date | None = None
    settle_date: date | None = None
    security_name_raw: str = ""
    security_code_raw: str | None = None
    account_raw: str = ""
    account_type_raw: str | None = None
    quantity: Decimal | None = None         # 符号つき増減（売却は負）
    unit_price: Decimal | None = None
    gross_amount: Decimal | None = None
    net_amount: Decimal | None = None
    fee: Decimal | None = None
    tax: Decimal | None = None
    split_ratio: Decimal | None = None
    currency: str = "JPY"
    exchange_rate: Decimal | None = None
    note: str | None = None
    confidence: float = 1.0
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TxParseReport:
    """warnings-as-data。判定エンジンは例外を投げず、すべてここへ積む。"""

    detection: dict[str, Any] = field(default_factory=dict)
    row_count: int = 0
    parsed_count: int = 0
    skipped_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TxParseResult:
    source_kind: str = "broker_csv"
    transactions: list[ParsedTx] = field(default_factory=list)
    report: TxParseReport = field(default_factory=TxParseReport)
