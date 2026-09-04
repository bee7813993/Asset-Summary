"""グリッド → ヘッダ行とデータ行の範囲（純関数）。

日本の証券会社の CSV は先頭に前文（口座番号・出力日時・空行）が入り、末尾に
「合計」行が付き、ページ送りでヘッダが途中に再出現することがある。
ヘッダが完全に無い書式も珍しくないので、見つからないこと自体を正常系として扱う。
"""

from __future__ import annotations

from .contracts import SheetGrid, TableRegion
from .shapes import is_date_like, is_number_like
from .vocab import header_scores, is_total_row_label, normalize_label

# ヘッダを探す範囲（先頭からの行数）
HEADER_SEARCH_ROWS = 30
# これを下回ったら「ヘッダ無し」とみなす
HEADER_MIN_SCORE = 2.5
# 2 行に割れたヘッダを結合してみる閾値
HEADER_JOIN_SCORE = 1.2


def _cells(grid: SheetGrid, r: int) -> list[str]:
    return [c for c in (grid.rows[r] if r < grid.height else ())]


def _nonempty(cells: list[str]) -> list[str]:
    return [c for c in cells if c and c.strip()]


def _modal_width(grid: SheetGrid) -> int:
    freq: dict[int, int] = {}
    for row in grid.rows:
        n = len(_nonempty(list(row)))
        if n >= 2:
            freq[n] = freq.get(n, 0) + 1
    if not freq:
        return grid.width
    return max(freq, key=lambda k: (freq[k], k))


def _numeric_fraction(cells: list[str]) -> float:
    vals = _nonempty(cells)
    if not vals:
        return 0.0
    return sum(1 for c in vals if is_number_like(c)) / len(vals)


def _date_fraction(cells: list[str], types: list[str] | None = None) -> float:
    vals = [c for c in cells if c and c.strip()]
    if not vals:
        return 0.0
    types = types or [""] * len(cells)
    hits = 0
    for c, t in zip(cells, types):
        if c and c.strip() and is_date_like(c, cell_type=t):
            hits += 1
    return hits / len(vals)


def _vocab_fraction(cells: list[str]) -> float:
    vals = _nonempty(cells)
    if not vals:
        return 0.0
    return sum(1 for c in vals if header_scores(c)) / len(vals)


def _known_security_hit(cells: list[str], universe) -> bool:
    from .shapes import _resolve_known  # 局所 import（循環回避）

    return any(_resolve_known(c, universe) for c in _nonempty(cells))


def _row_types(grid: SheetGrid, r: int) -> list[str]:
    if grid.types is None or r >= len(grid.types):
        return []
    return list(grid.types[r])


def score_header_row(grid: SheetGrid, r: int, modal_width: int, universe) -> float:
    """1 行がヘッダらしいかのスコア。閾値との比較は呼び出し側。"""
    cells = _cells(grid, r)
    vals = _nonempty(cells)
    if len(vals) < 2:
        return 0.0

    score = 0.0
    score += 3.0 * _vocab_fraction(cells)
    if len(vals) == modal_width:
        score += 1.0
    # 日付や裸の数値が入っている行はヘッダではない。加点の減衰ではなく減点にする —
    # 減衰だと、行数が少なくて下記の落差が効かないファイルで最初のデータ行が
    # ヘッダに化ける。
    score -= 2.5 * _date_fraction(cells, _row_types(grid, r))
    score -= 2.0 * _numeric_fraction(cells)

    # ヘッダ語は短くて重複しない。銘柄名データは長くて繰り返す。
    if (all(c and c.strip() for c in cells[:modal_width])
            and len(set(vals)) == len(vals)
            and max((len(c) for c in vals), default=0) <= 12):
        score += 1.5

    # 下の行が数値中心でこの行がそうでない、という落差がいちばん効く。
    # 語彙に無いヘッダを拾えるのはこの項のおかげ。
    below = [_numeric_fraction(_cells(grid, rr)) for rr in range(r + 1, min(r + 6, grid.height))]
    below = [b for b in below if b > 0]
    if below:
        contrast = (sum(below) / len(below)) - _numeric_fraction(cells)
        if contrast > 0:
            score += 2.0 * min(contrast, 1.0)

    if _known_security_hit(cells, universe):
        score -= 2.0          # 既存銘柄が並ぶ行はデータ行
    if any(is_total_row_label(c) for c in vals):
        score -= 1.5
    return score


def _vocab_strength(cells: list[str]) -> float:
    """見出しとしての強さ（各セルの最良スコアの合計）。

    _vocab_fraction は「当たったセルの割合」なので、'銘柄名' と '約定銘柄名' の
    ように両方当たる候補どうしを比べられない。結合のしかたを選ぶにはこちらを使う。
    """
    total = 0.0
    for c in cells:
        scores = header_scores(c) if c and c.strip() else {}
        if scores:
            total += max(scores.values())
    return total


def _join_two_rows(grid: SheetGrid, top: int, bottom: int, width: int) -> list[str]:
    """2 行に割れたヘッダを結合する。

    結合セル（xlsx は非先頭が空になる）を横に補完してから繋ぐ形と、素直に
    上下を繋ぐ形の 2 通りを作り、見出しとして強い方を採る。補完だけに決め打つと
    '約定' が 1 列にしか掛かっていない書式で '約定銘柄名' のような見出しを
    でっち上げてしまう。
    """
    upper = _cells(grid, top)
    lower = _cells(grid, bottom)

    def cell(row: list[str], i: int) -> str:
        return row[i].strip() if i < len(row) else ""

    direct = [f"{cell(upper, i)}{cell(lower, i)}" for i in range(width)]

    filled: list[str] = []
    last = ""
    for i in range(width):
        cur = cell(upper, i)
        if cur:
            last = cur
        filled.append(last)
    spanned = [
        f"{filled[i]}{cell(lower, i)}" if cell(lower, i) else filled[i]
        for i in range(width)
    ]

    return direct if _vocab_strength(direct) >= _vocab_strength(spanned) else spanned


def _is_partial_header(cells: list[str], header_width: int) -> bool:
    """見出しの一部だけが載った行か（結合セルの先頭・折り返しの上段）。

    データ行を巻き込まないよう、数値も日付も無く、埋まっているセル数が
    見出し行より少ないことを条件にする。
    """
    vals = _nonempty(cells)
    if not vals or len(vals) >= max(header_width, 1):
        return False
    return _numeric_fraction(cells) == 0.0 and _date_fraction(cells) == 0.0


def _is_unit_row(cells: list[str]) -> bool:
    """'(円)' '(株)' だけが並ぶ単位行。"""
    vals = _nonempty(cells)
    if not vals:
        return False
    return all(len(c) <= 6 and (c.startswith(("(", "（")) and c.endswith((")", "）")))
               for c in vals)


def find_region(grid: SheetGrid, universe) -> TableRegion:
    """ヘッダ行を特定し、データ行の範囲と捨てた行を返す。"""
    if grid.height == 0:
        return TableRegion(header_row=None, data_start=0, data_end=0)

    modal_width = _modal_width(grid)
    limit = min(HEADER_SEARCH_ROWS, grid.height)

    best_row, best_score = None, 0.0
    for r in range(limit):
        s = score_header_row(grid, r, modal_width, universe)
        if s > best_score:
            best_row, best_score = r, s

    dropped: list[tuple[int, str]] = []
    headers: tuple[str, ...] = ()
    header_rows: tuple[int, ...] = ()

    if best_row is None or best_score < HEADER_MIN_SCORE:
        # ヘッダ無し書式。値の形状と既存銘柄だけで列を決める。
        header_row = None
        data_start = 0
    else:
        header_row = best_row
        header_rows = (best_row,)
        headers = tuple(_cells(grid, best_row)[:modal_width])
        data_start = best_row + 1

        # 2 行に割れたヘッダ（'約定' / '日' が上下に分かれている等）。
        # 割れ方は上下どちらにもなり得る — 見出しの本体が下の行にあり、上の行に
        # '約定' だけが残っている書式では、下方向しか見ないと '日' のままになる。
        base_strength = _vocab_strength(list(headers))

        if best_row + 1 < grid.height:
            nxt = score_header_row(grid, best_row + 1, modal_width, universe)
            if nxt >= HEADER_JOIN_SCORE:
                joined = _join_two_rows(grid, best_row, best_row + 1, modal_width)
                if _vocab_strength(joined) > base_strength:
                    headers = tuple(joined)
                    header_rows = (best_row, best_row + 1)
                    data_start = best_row + 2

        if header_rows == (best_row,) and best_row > 0 and _is_partial_header(
            _cells(grid, best_row - 1), len(_nonempty(list(headers)))
        ):
            joined = _join_two_rows(grid, best_row - 1, best_row, modal_width)
            if _vocab_strength(joined) >= base_strength:
                headers = tuple(joined)
                header_rows = (best_row - 1, best_row)

        for r in range(0, header_rows[0]):
            if _nonempty(_cells(grid, r)):
                dropped.append((r, "preamble"))

        if data_start < grid.height and _is_unit_row(_cells(grid, data_start)):
            dropped.append((data_start, "unit_row"))
            data_start += 1

    # 末尾から、空行・合計行・短すぎる行を落とす
    data_end = grid.height
    while data_end > data_start:
        cells = _cells(grid, data_end - 1)
        vals = _nonempty(cells)
        if not vals:
            data_end -= 1
            continue
        if any(is_total_row_label(c) for c in vals[:2]):
            dropped.append((data_end - 1, "total_row"))
            data_end -= 1
            continue
        if len(vals) < 2:
            dropped.append((data_end - 1, "trailer"))
            data_end -= 1
            continue
        break

    # 途中の空行・繰り返しヘッダ・小見出しも落とす（行番号だけ記録して読み飛ばす）
    header_key = tuple(normalize_label(h) for h in headers) if headers else ()
    for r in range(data_start, data_end):
        cells = _cells(grid, r)
        vals = _nonempty(cells)
        if not vals:
            dropped.append((r, "blank"))
            continue
        if header_key and tuple(normalize_label(c) for c in cells[:len(header_key)]) == header_key:
            dropped.append((r, "repeated_header"))
            continue
        if len(vals) < 2:
            dropped.append((r, "subtitle"))
            continue
        if any(is_total_row_label(c) for c in vals[:2]):
            dropped.append((r, "total_row"))

    return TableRegion(
        header_row=header_row,
        header_rows=header_rows,
        data_start=data_start,
        data_end=data_end,
        header_score=best_score,
        headers=headers,
        dropped=tuple(sorted(set(dropped))),
    )


def data_row_indices(region: TableRegion) -> list[int]:
    """実際に解析対象とする行番号（捨てた行を除く）。"""
    skip = {r for r, _ in region.dropped}
    return [r for r in range(region.data_start, region.data_end) if r not in skip]
