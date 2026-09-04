"""値の形状から列の性質を推定する（純関数）。

ヘッダ語彙が当てにならない書式のための第2の信号。日付らしさ・数値らしさ・
証券コードらしさを列単位の比率として出し、columns.py のスコア行列に渡す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..base import clean_number, make_name_key, nfkc, normalize_code
from .contracts import KnownUniverse
from .vocab import looks_like_account_type, looks_like_currency, looks_like_tx_type

# ----------------------------------------------------------------------
# 日付
# ----------------------------------------------------------------------

# 和暦の元号（開始日）。大和・野村系の書式でいまだに出てくる。
_ERAS = {
    "令和": (2018, date(2019, 5, 1)),   # 令和N年 = 2018+N
    "R": (2018, date(2019, 5, 1)),
    "平成": (1988, date(1989, 1, 8)),
    "H": (1988, date(1989, 1, 8)),
    "昭和": (1925, date(1926, 12, 25)),
    "S": (1925, date(1926, 12, 25)),
    "大正": (1911, date(1912, 7, 30)),
    "T": (1911, date(1912, 7, 30)),
    "明治": (1867, date(1868, 1, 25)),
    "M": (1867, date(1868, 1, 25)),
}

_WAREKI_RE = re.compile(
    r"^(令和|平成|昭和|大正|明治|R|H|S|T|M)\s*(元|\d{1,2})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})\s*日?$"
)
_YMD_RE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$")
_SHORT_RE = re.compile(r"^(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})$")
_YY_RE = re.compile(r"^(\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})$")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")

# 8桁数値を日付とみなす年の範囲。口座番号を日付と誤認しないための歯止め。
_MIN_YEAR = 1990


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(cell: str, *, order: str = "ymd", today: date | None = None) -> date | None:
    """よくある日付表記を date に。判別できなければ None。

    order は 03/04/2026 のように月日が曖昧なときだけ効く（ymd|dmy|mdy）。
    """
    if cell is None:
        return None
    s = nfkc(str(cell)).strip()
    if not s:
        return None
    # 時刻付き（'2026/01/05 09:00:12'）は日付部分だけ見る
    s = re.split(r"[ T]", s, maxsplit=1)[0] if re.search(r"[ T]\d{1,2}:", s) else s

    m = _YMD_RE.match(s)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _WAREKI_RE.match(s)
    if m:
        era, yy, mo, dd = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        base = _ERAS.get(era)
        if base is not None:
            year = base[0] + (1 if yy == "元" else int(yy))
            return _safe_date(year, mo, dd)

    m = _COMPACT_RE.match(s)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        limit = (today or date.today()).year + 1
        if _MIN_YEAR <= y <= limit:
            return _safe_date(y, mo, dd)
        return None

    # 3 成分すべてが 1-2 桁なら YY/MM/DD と読む（日本の証券会社の書式）。
    # '26/01/05' は 2026-01-05 であって 2005年1月26日ではない。ただし 2 番目が
    # 13 以上なら月になり得ないので、その場合は下の DD/MM/YY 解釈へ落とす。
    m = _YY_RE.match(s)
    if m:
        yy, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo <= 12 and dd <= 31:
            return _safe_date(2000 + yy, mo, dd)

    m = _SHORT_RE.match(s)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        year = int(c) if len(c) == 4 else 2000 + int(c)
        # 12 を超える成分があれば、それが日。曖昧なときだけ order に従う。
        if a > 12 >= b:
            return _safe_date(year, b, a)
        if b > 12 >= a:
            return _safe_date(year, a, b)
        return _safe_date(year, b, a) if order == "dmy" else _safe_date(year, a, b)
    return None


def is_date_like(cell: str, *, cell_type: str = "") -> bool:
    if cell_type == "date":
        return True
    return parse_date(cell) is not None


# ----------------------------------------------------------------------
# 数値
# ----------------------------------------------------------------------

# 会計表記の負数 (1,234) と、Excel がテキスト化した数値の先頭 '
_PAREN_NEG_RE = re.compile(r"^\((.+)\)$")
_DASH_ONLY = {"-", "‐", "―", "—", "–", "ー", "−", "", "--", "n/a", "na"}

# 「値が無い」ことを表す埋め草。空欄の代わりにこれを置く書式が多い
# （大和証券の取引履歴は約定日・数量・単価の無い行を "-" で埋める）。
# 統計の母数から外さないと、実際には全部日付の列が「日付率 31%」に見えて
# 拒否され、まったく別のフィールドに流れてしまう。
_NULL_TOKENS = _DASH_ONLY | {"---", "n.a.", "なし", "－", "ｰ", "・", "*", "‐‐"}


def is_null_token(cell: str) -> bool:
    """「値が無い」ことを表す埋め草か。空文字も含む。"""
    if cell is None:
        return True
    return nfkc(str(cell)).strip().lower() in _NULL_TOKENS


def parse_amount(cell: str) -> Decimal | None:
    """金額・数量を Decimal に。base.clean_number を包んで書式差を吸収する。

    clean_number は mf_pdf が使っているので触らない。ここでは
    会計表記の括弧負数と Excel のテキスト化数値だけ足す。
    """
    if cell is None:
        return None
    s = nfkc(str(cell)).strip()
    if not s:
        return None
    if s.lower() in _DASH_ONLY:
        return None
    if s.startswith("'"):          # Excel のテキスト化数値
        s = s[1:].strip()
    neg = False
    m = _PAREN_NEG_RE.match(s)
    if m:
        neg = True
        s = m.group(1).strip()
    if s.startswith("+"):
        s = s[1:].strip()
    value = clean_number(s)
    if value is None:
        return None
    return -value if neg else value


def is_number_like(cell: str, *, cell_type: str = "") -> bool:
    if cell_type == "number":
        return True
    return parse_amount(cell) is not None


def code_of(cell: str, known_codes: frozenset[str] | set[str] | None = None) -> str | None:
    """証券コードらしさ。base.normalize_code に委譲（大和の5桁末尾0対応込み）。"""
    if not cell:
        return None
    return normalize_code(str(cell), set(known_codes) if known_codes else None)


# 銘柄名の先頭に付いたコード（'1234 架空商事' / '1234:架空商事'）
_LEADING_CODE_RE = re.compile(r"^\s*([0-9][0-9A-Za-z]{3,4})\s*[:：\s\-－]\s*(.+)$")
# 銘柄名の末尾に付いたコード（'架空商事 (1234)'）は make_name_key が落とすので不要


def split_leading_code(cell: str) -> tuple[str | None, str]:
    """'1234 架空商事' → ('1234', '架空商事')。分けられなければ (None, 原文)。"""
    s = nfkc(str(cell or "")).strip()
    m = _LEADING_CODE_RE.match(s)
    if m:
        return (m.group(1), m.group(2).strip())
    return (None, s)


# ----------------------------------------------------------------------
# 列単位の統計
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnStats:
    n: int = 0
    n_nonempty: int = 0      # 空欄でないセル数（埋め草 "-" を含む）
    n_present: int = 0       # 実際に値のあるセル数（各種 rate の母数）
    date_rate: float = 0.0
    number_rate: float = 0.0
    int_rate: float = 0.0
    code_rate: float = 0.0
    negative_rate: float = 0.0
    zero_rate: float = 0.0
    distinct_rate: float = 0.0
    distinct: int = 0
    mean_len: float = 0.0
    max_len: int = 0
    median_abs: Decimal | None = None
    known_security_rate: float = 0.0
    known_account_rate: float = 0.0
    txtype_rate: float = 0.0
    accounttype_rate: float = 0.0
    currency_rate: float = 0.0
    leading_code_gain: float = 0.0   # 先頭コードを剥がすと銘柄一致がどれだけ増えるか

    @property
    def is_numeric(self) -> bool:
        return self.number_rate >= 0.6

    @property
    def is_date(self) -> bool:
        return self.date_rate >= 0.6


def _resolve_known(value: str, universe: KnownUniverse) -> bool:
    """既存銘柄に解決できるか（完全一致のみ。曖昧照合は行単位で後からやる）。

    列判定の段階でファジーマッチまで回すと計算量が跳ねるうえ、必要も無い。
    ある列だけが 6 割解決して他が 0 なら、その列が銘柄列であることは十分決まる。
    """
    if not value or universe.is_empty:
        return False
    code = code_of(value, universe.known_codes)
    if code and code in universe.by_code:
        return True
    key = make_name_key(value)
    if not key:
        return False
    return key in universe.by_alias or key in universe.by_name_key


def _account_contains(value: str, universe: KnownUniverse) -> bool:
    """既存の口座名との部分一致。短すぎる値では成立させない。

    素朴な包含判定だと '株' のような 1 文字の単位が口座名に紛れ込んで一致し、
    単位列が口座列に化ける。短い側が 2 文字以上で、かつ長い側の半分以上の
    長さを占めるときだけ「同じ口座を指している」とみなす。
    """
    key = make_name_key(value)
    if len(key) < 2:
        return False
    for name in universe.account_names:
        other = make_name_key(name)
        if len(other) < 2:
            continue
        short, long = (key, other) if len(key) <= len(other) else (other, key)
        if short in long and len(short) * 2 >= len(long):
            return True
    return False


def _median_abs(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(abs(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def column_stats(
    values: list[str],
    universe: KnownUniverse,
    *,
    types: list[str] | None = None,
) -> ColumnStats:
    """1列分のサンプル値から統計を出す。"""
    n = len(values)
    if n == 0:
        return ColumnStats()
    types = types or [""] * n

    nonempty = 0
    dates = numbers = ints = codes = negs = zeros = 0
    known_sec = known_acct = txtypes = accttypes = currencies = 0
    lengths: list[int] = []
    numeric_values: list[Decimal] = []
    seen: set[str] = set()
    leading_hits = 0
    plain_hits = 0

    present = 0
    for value, cell_type in zip(values, types):
        text = nfkc(str(value or "")).strip()
        if not text:
            continue
        nonempty += 1
        # 埋め草は「その形をしていない値」ではなく「値が無い」。母数から外す。
        if is_null_token(text):
            continue
        present += 1
        seen.add(text)
        lengths.append(len(text))

        if is_date_like(text, cell_type=cell_type):
            dates += 1
        amount = parse_amount(text) if cell_type != "date" else None
        if amount is not None:
            numbers += 1
            numeric_values.append(amount)
            if amount == amount.to_integral_value():
                ints += 1
            if amount < 0:
                negs += 1
            if amount == 0:
                zeros += 1
        if code_of(text, universe.known_codes):
            codes += 1
        if _resolve_known(text, universe):
            known_sec += 1
            plain_hits += 1
        else:
            lead_code, rest = split_leading_code(text)
            if lead_code and (_resolve_known(lead_code, universe) or _resolve_known(rest, universe)):
                leading_hits += 1
        if text in universe.account_names:
            known_acct += 1
        elif _account_contains(text, universe):
            known_acct += 1
        if looks_like_tx_type(text):
            txtypes += 1
        if looks_like_account_type(text):
            accttypes += 1
        if looks_like_currency(text):
            currencies += 1

    denom = float(present) if present else 1.0
    return ColumnStats(
        n=n,
        n_nonempty=nonempty,
        n_present=present,
        date_rate=dates / denom,
        number_rate=numbers / denom,
        int_rate=ints / max(numbers, 1),
        code_rate=codes / denom,
        negative_rate=negs / max(numbers, 1),
        zero_rate=zeros / max(numbers, 1),
        distinct_rate=len(seen) / denom,
        distinct=len(seen),
        mean_len=(sum(lengths) / len(lengths)) if lengths else 0.0,
        max_len=max(lengths) if lengths else 0,
        median_abs=_median_abs(numeric_values),
        known_security_rate=known_sec / denom,
        known_account_rate=known_acct / denom,
        txtype_rate=txtypes / denom,
        accounttype_rate=accttypes / denom,
        currency_rate=currencies / denom,
        leading_code_gain=leading_hits / denom,
    )


def detect_date_order(values: list[str]) -> str:
    """03/04/2026 のような曖昧表記の並びを列全体から決める。

    どれか 1 行でも 12 を超える成分があれば確定する。全行が曖昧なら 'ymd'
    （日本の証券会社の既定）を返し、呼び出し側が警告を出す。
    """
    saw_first_gt12 = saw_second_gt12 = False
    for value in values:
        s = nfkc(str(value or "")).strip()
        m = _SHORT_RE.match(s)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            saw_first_gt12 = True
        if b > 12:
            saw_second_gt12 = True
    if saw_first_gt12 and not saw_second_gt12:
        return "dmy"
    if saw_second_gt12 and not saw_first_gt12:
        return "mdy"
    return "ymd"


def date_order_ambiguous(values: list[str]) -> bool:
    """曖昧表記が含まれるのに決め手が無いか（警告用）。"""
    ambiguous = False
    for value in values:
        s = nfkc(str(value or "")).strip()
        m = _SHORT_RE.match(s)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 12 and b <= 12:
            ambiguous = True
        else:
            return False
    return ambiguous


def parse_ratio(cell: str) -> Decimal | None:
    """'1:3' '1対3' '3' のような分割比率を「新÷旧」の Decimal に。"""
    if not cell:
        return None
    s = nfkc(str(cell)).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[:：対]\s*(\d+(?:\.\d+)?)$", s)
    if m:
        try:
            old, new = Decimal(m.group(1)), Decimal(m.group(2))
        except InvalidOperation:
            return None
        return (new / old) if old else None
    value = parse_amount(s)
    return value if value and value > 0 else None
