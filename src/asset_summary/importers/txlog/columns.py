"""列 → 正準フィールドの割当（純関数）。

3 系統の独立した信号でスコア行列を作り、2 段階で解く:

1. 分類系の列（日付・銘柄・区分・口座…）はスコアが信用できるので、
   ハンガリアン法で最適割当を求める。
2. 数量・単価・約定代金・受渡金額の 4 つはスコアが当てにならない
   （どれも「数値の列」でしかない）ので、**総当たりして算術検算で決める**。
   数量×単価＝約定代金、約定代金−手数料−税額＝受渡金額 が成り立つ組合せを選ぶ。

2 が、ヘッダが無い書式・語彙に無い書式でも列を確定できる理由。
mf_pdf.py の 3 層検算と同じ発想を、列の同定そのものに使っている。
"""

from __future__ import annotations

import itertools
from typing import Sequence
from decimal import Decimal

from .contracts import (
    ADDITIVE_FIELDS,
    CATEGORICAL_FIELDS,
    IDENTITY_PASS_RATE,
    MIN_IDENTITY_ROWS,
    NUMERIC_QUARTET,
    sample_rows,
    CanonicalField as F,
    ColumnAssignment,
    IdentityCheck,
    KnownUniverse,
    SheetGrid,
    TableRegion,
)
from .shapes import ColumnStats, column_stats, parse_amount
from .vocab import header_scores

# スコアの重み。既知データとの一致をヘッダ語彙より重くしているのが要点 —
# 「当日の評価額を取得できている時点で銘柄は固定できている」という前提を
# そのままスコアに落としている。
W_HEADER = 1.0
W_SHAPE = 1.0
W_KNOWN = 2.5

# 総当たりを許す数値列の数の上限（これを超えたらヘッダ優先の割当に落とす）
MAX_BRUTE_NUMERIC = 8

# これ未満のスコアなら、そのフィールドは割り当てない。
# 銘柄名が既存銘柄に一度も当たらない書式でも 0.35 は出る（形状の下駄）ので、
# それは通しつつ、根拠が実質ゼロの割当だけを落とす高さにしてある。
MIN_ASSIGN_SCORE = 0.3

# 投信の基準価額は 1万口あたり。株なら 1。算術検算で自動的に決まる。
DIVISOR_CANDIDATES = (1, 10000)


def _sample(grid: SheetGrid, region: TableRegion, col: int, rows: list[int]) -> list[str]:
    return [grid.cell(r, col) for r in sample_rows(rows)]


def _sample_types(grid: SheetGrid, col: int, rows: list[int]) -> list[str]:
    return [grid.cell_type(r, col) for r in sample_rows(rows)]


# ----------------------------------------------------------------------
# スコア行列
# ----------------------------------------------------------------------


def _shape_score(field: F, st: ColumnStats) -> tuple[float, bool, str]:
    """(形状スコア0-1, 禁止か, 根拠)。

    禁止（veto）はスコアが低いのとは違う ハードな不許可。これが無いと
    「4桁の金額列」が銘柄コードとして通ってしまう。
    """
    if field in (F.TRADE_DATE, F.SETTLE_DATE):
        if st.date_rate < 0.6:
            return (0.0, True, "日付として読めない")
        return (st.date_rate, False, f"日付らしさ {st.date_rate:.0%}")

    if field is F.SECURITY_NAME:
        if st.number_rate > 0.8 or st.date_rate > 0.5:
            return (0.0, True, "数値・日付の列")
        base = max(st.known_security_rate, st.leading_code_gain)
        if base <= 0 and st.mean_len >= 3 and st.number_rate < 0.3:
            # 既存銘柄に当たらない（初回取込や古い銘柄）ときの弱い当たり。
            # ただし行数のわりに値の種類が極端に少ない列は分類であって銘柄名
            # ではない（『商品』は数千行に 9 種類、『市場名称』は東証/JNX/JAX）。
            few_kinds = st.n_present >= 20 and st.distinct <= 15
            base = 0.15 if few_kinds else 0.35
        return (base, False, f"既存銘柄に一致 {st.known_security_rate:.0%}")

    if field is F.SECURITY_CODE:
        if st.code_rate < 0.5:
            return (0.0, True, "証券コードの形をしていない")
        return (st.code_rate, False, f"コードらしさ {st.code_rate:.0%}")

    if field is F.TX_TYPE:
        if st.number_rate > 0.7:
            return (0.0, True, "数値の列")
        return (st.txtype_rate, False, f"取引区分の語に一致 {st.txtype_rate:.0%}")

    if field is F.ACCOUNT_TYPE:
        if st.number_rate > 0.7:
            return (0.0, True, "数値の列")
        return (st.accounttype_rate, False, f"口座区分の語に一致 {st.accounttype_rate:.0%}")

    if field is F.CURRENCY:
        if st.currency_rate < 0.5:
            return (0.0, True, "通貨コードではない")
        return (st.currency_rate, False, f"通貨コード {st.currency_rate:.0%}")

    if field is F.ACCOUNT:
        if st.number_rate > 0.5:
            return (0.0, True, "数値の列")
        # 中身が 特定/一般/NISA なら口座「区分」であって口座名ではない。
        # 見出しが『口座』だけだとどちらとも取れるので、値で決める。
        if st.accounttype_rate >= 0.8:
            return (0.0, True, "口座区分の値が並んでいる")
        return (st.known_account_rate, False, f"既存口座に一致 {st.known_account_rate:.0%}")

    if field is F.EXCHANGE_RATE:
        if st.number_rate < 0.6:
            return (0.0, True, "数値として読めない")
        return (0.2, False, "数値")

    if field is F.NOTE:
        if st.number_rate > 0.8 or st.date_rate > 0.5:
            return (0.0, True, "数値・日付の列")
        return (0.15, False, "自由記述")

    if field in NUMERIC_QUARTET or field in ADDITIVE_FIELDS:
        if st.number_rate < 0.6:
            return (0.0, True, "数値として読めない")
        # 数値列どうしはここでは決まらない。算術検算に委ねる。
        hint = 0.2
        if field in ADDITIVE_FIELDS:
            hint += 0.3 * st.zero_rate          # 手数料・税額は 0 が混ざる
        if field is F.QUANTITY:
            hint += 0.2 * st.int_rate
        return (min(hint, 1.0), False, "数値")

    return (0.0, False, "")


def build_score_matrix(
    grid: SheetGrid,
    region: TableRegion,
    universe: KnownUniverse,
    rows: list[int],
) -> tuple[dict[int, ColumnStats], dict[tuple[int, F], float], dict[tuple[int, F], list[str]]]:
    width = max(grid.width, len(region.headers))
    stats: dict[int, ColumnStats] = {}
    scores: dict[tuple[int, F], float] = {}
    evidence: dict[tuple[int, F], list[str]] = {}

    for col in range(width):
        values = _sample(grid, region, col, rows)
        types = _sample_types(grid, col, rows)
        st = column_stats(values, universe, types=types)
        stats[col] = st
        label = region.headers[col] if col < len(region.headers) else ""
        head = header_scores(label) if label else {}

        for field in list(F):
            if field is F.IGNORE:
                continue
            shape, vetoed, why = _shape_score(field, st)
            if vetoed:
                continue
            h = head.get(field, 0.0)
            # 既知データとの一致は「DB に元々ある集合」に照らした信号にだけ与える。
            # 取引区分・口座区分は語彙との一致であり、それは既に形状スコアなので、
            # ここで足すと二重計上になる（'備考' 見出しの列に NISA と書いてあると
            # 見出しを無視して口座区分に化ける）。
            known = 0.0
            if field is F.SECURITY_NAME:
                known = max(st.known_security_rate, st.leading_code_gain)
            elif field is F.SECURITY_CODE:
                known = st.known_security_rate
            elif field is F.ACCOUNT:
                known = st.known_account_rate

            # 証券コードは「4桁の数値」だけでは決められない。ヘッダか既知銘柄の
            # 裏づけを要求する（金額列が 4 桁だと素通りしてしまうため）。
            if field is F.SECURITY_CODE and h <= 0 and st.known_security_rate < 0.3:
                continue
            # 為替レートと備考は形だけでは他と区別がつかない。見出しの裏づけを
            # 要求しないと、為替レートが数量列を先に取ってしまい（分類系は数値の
            # 総当たりより先に解くため）、備考が銘柄名を取ってしまう。
            if field in (F.EXCHANGE_RATE, F.NOTE) and h <= 0:
                continue

            total = W_HEADER * h + W_SHAPE * shape + W_KNOWN * known
            if total <= 0:
                continue
            scores[(col, field)] = total
            note: list[str] = []
            if h:
                note.append(f"見出し『{label}』")
            if why:
                note.append(why)
            evidence[(col, field)] = note
    return stats, scores, evidence


# ----------------------------------------------------------------------
# ハンガリアン法（分類系フィールドの最適割当）
# ----------------------------------------------------------------------


def hungarian(cost: list[list[float]]) -> list[int]:
    """行 i に列 result[i] を割り当てる（総コスト最小）。未割当は -1。

    SciPy は依存に無いし、20x10 程度の行列のために足す理由も無いので自前で持つ。
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    if m == 0:
        return [-1] * n
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            if delta == inf:
                break
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        if j0 == 0:
            continue
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    result = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


def assign_categorical(
    columns: list[int],
    scores: dict[tuple[int, F], float],
) -> dict[F, int]:
    """分類系フィールドを列へ最適割当する。

    候補の無い組合せのコストは 0（＝割り当てても何も得られない）にする。
    大きな罰点を置くと「そのフィールドをどこにも割り当てない」が選べなくなり、
    行き場の無いフィールドを収めるために本命の割当が犠牲になる。
    楽天証券の書式では、口座の列が無いのに『口座』を置き場所（銘柄名 0.06）へ
    押し込むために、既存銘柄に 88% 一致する銘柄名列が明け渡されていた。
    """
    fields = [f for f in CATEGORICAL_FIELDS if any((c, f) in scores for c in columns)]
    if not fields or not columns:
        return {}
    cost = [
        [(-scores[(c, f)] if (c, f) in scores else 0.0) for c in columns]
        for f in fields
    ]
    if len(fields) > len(columns):
        # 行 > 列 だと解けないので、ダミー列で埋める（同じく 0＝得るものなし）
        pad = len(fields) - len(columns)
        for row in cost:
            row.extend([0.0] * pad)
    picks = hungarian(cost)
    out: dict[F, int] = {}
    for i, f in enumerate(fields):
        j = picks[i] if i < len(picks) else -1
        if 0 <= j < len(columns) and scores.get((columns[j], f), 0.0) >= MIN_ASSIGN_SCORE:
            out[f] = columns[j]
    return out


# ----------------------------------------------------------------------
# 算術検算（書式非依存性の要）
# ----------------------------------------------------------------------


def _amounts(grid: SheetGrid, col: int | None, rows: list[int]) -> list[Decimal | None]:
    if col is None:
        return [None] * len(rows)
    return [parse_amount(grid.cell(r, col)) for r in rows]


def _summed(
    grid: SheetGrid, cols: Sequence[int] | None, rows: list[int]
) -> list[Decimal | None]:
    """複数列の合計（手数料＋消費税、所得税＋住民税 のように割れている書式用）。"""
    if not cols:
        return [None] * len(rows)
    out: list[Decimal | None] = []
    for r in rows:
        total: Decimal | None = None
        for col in cols:
            value = parse_amount(grid.cell(r, col))
            if value is not None:
                total = value if total is None else total + value
        out.append(total)
    return out


def check_identities(
    grid: SheetGrid,
    rows: list[int],
    *,
    qty: int | None,
    price: int | None,
    gross: int | None,
    net: int | None,
    fee: Sequence[int] | None,
    tax: Sequence[int] | None,
    divisor: int,
) -> list[IdentityCheck]:
    """数量×単価＝約定代金 と |受渡−約定|＝手数料＋税額 を検算する。

    符号の規約はまだ確定していないので、両辺とも絶対値で見る。
    """
    q = _amounts(grid, qty, rows)
    p = _amounts(grid, price, rows)
    g = _amounts(grid, gross, rows)
    n = _amounts(grid, net, rows)
    f = _summed(grid, fee, rows)
    t = _summed(grid, tax, rows)
    out: list[IdentityCheck] = []

    if qty is not None and price is not None and gross is not None:
        tested = passed = 0
        for qi, pi, gi in zip(q, p, g):
            if qi is None or pi is None or gi is None or qi == 0 or pi == 0:
                continue
            tested += 1
            expected = abs(qi) * abs(pi) / Decimal(divisor)
            # 単価は表示上まるめられているので、許容差は数量に比例させる
            # （mf_pdf._check_row が表示のまるめから許容差を導くのと同じ考え）
            tol = Decimal(1) + abs(qi) * Decimal("0.5") / Decimal(divisor)
            if abs(expected - abs(gi)) <= tol:
                passed += 1
        out.append(IdentityCheck("qty*price=gross", tested, passed, divisor))

    if gross is not None and net is not None:
        tested = passed = 0
        for gi, ni, fi, ti in zip(g, n, f, t):
            if gi is None or ni is None:
                continue
            # 全部ゼロの行は |0−0| = 0+0 で必ず通る。関係の無い空列どうしを
            # 選んでも「検算に合格」してしまい、本物の割当を押しのけるので、
            # 何も語っていない行は数に入れない。
            if gi == 0 and ni == 0:
                continue
            tested += 1
            costs = (fi or Decimal(0)) + (ti or Decimal(0))
            if abs(abs(abs(ni) - abs(gi)) - abs(costs)) <= Decimal(1):
                passed += 1
        out.append(IdentityCheck("net-gross=fee+tax", tested, passed, None))
    return out


def _identity_rank(checks: list[IdentityCheck]) -> int:
    """検算に「合格した」式の数。合格率は返さない。

    以前は平均合格率も順位に混ぜていたが、それだと **落ちた検算(0.6) が
    「検算できない(0.0)」より上位**になり、見出しの証拠を押しのけてしまう。
    大和証券の『精算金額』（受渡金額）が、たまたま一部の行で
    数量×単価と一致したせいで約定代金に化けたのがこれ。
    合格した式が無いなら順位は付けず、見出しと形状のスコアに委ねる。
    """
    return sum(
        1 for c in checks if c.conclusive and c.pass_rate >= IDENTITY_PASS_RATE
    )


def solve_numeric(
    grid: SheetGrid,
    rows: list[int],
    numeric_columns: list[int],
    scores: dict[tuple[int, F], float],
    *,
    fee: Sequence[int] | None,
    tax: Sequence[int] | None,
    pinned: dict[F, int] | None = None,
) -> tuple[dict[F, int], list[IdentityCheck], int]:
    """数量・単価・約定代金・受渡金額を総当たりで確定する。

    候補は「数値列 ∪ {割り当てない}」の単射。列が 7 本でも 840 通りなので
    総当たりで足り、貪欲＋再試行より遥かに見通しがよくテストしやすい。

    pinned は見出しで既に確定している列。手数料がゼロだと受渡金額と約定代金が
    同じ値になり、検算ではどちらか決められない。見出しが完全一致していて
    その列が一意なら、そちらを信じる（検算に決めさせると『受渡金額』が
    約定代金に化ける）。
    """
    pinned = dict(pinned or {})
    free_fields = [f for f in NUMERIC_QUARTET if f not in pinned]
    pool: list[int | None] = [None] + [
        c for c in numeric_columns if c not in set(pinned.values())
    ]
    best: tuple[tuple[int, float], float, dict[F, int], list[IdentityCheck], int] | None = None

    if len(numeric_columns) > MAX_BRUTE_NUMERIC:
        # 現実にはまず起きないが、起きたらヘッダ優先の割当に落とす
        picked = {}
        for f in NUMERIC_QUARTET:
            cands = [(scores.get((c, f), 0.0), c) for c in numeric_columns
                     if c not in picked.values()]
            cands = [(s, c) for s, c in cands if s > 0]
            if cands:
                picked[f] = max(cands)[1]
        checks = check_identities(
            grid, rows, qty=picked.get(F.QUANTITY), price=picked.get(F.UNIT_PRICE),
            gross=picked.get(F.GROSS_AMOUNT), net=picked.get(F.NET_AMOUNT),
            fee=fee, tax=tax, divisor=1,
        )
        return (picked, checks, 1)

    for combo in itertools.permutations(pool, len(free_fields)):
        used = [c for c in combo if c is not None]
        if len(set(used)) != len(used):
            continue
        mapping = dict(pinned)
        mapping.update({f: c for f, c in zip(free_fields, combo) if c is not None})
        if not mapping:
            continue
        matrix = sum(scores.get((c, f), 0.0) for f, c in mapping.items())
        # 割り当てなかったフィールドは小さく減点（列があるなら使うのが自然）
        matrix -= 0.05 * (len(NUMERIC_QUARTET) - len(mapping))

        for divisor in DIVISOR_CANDIDATES:
            checks = check_identities(
                grid, rows,
                qty=mapping.get(F.QUANTITY), price=mapping.get(F.UNIT_PRICE),
                gross=mapping.get(F.GROSS_AMOUNT), net=mapping.get(F.NET_AMOUNT),
                fee=fee, tax=tax, divisor=divisor,
            )
            rank = _identity_rank(checks)
            key = (rank, matrix)
            if best is None or key > (best[0], best[1]):
                best = (rank, matrix, mapping, checks, divisor)
            # 検算に使える行が 1 行も無いときだけ打ち切る。「合格しなかった」で
            # 打ち切ると、株として読むと合わない投信（divisor=10000）に永久に
            # たどり着けない。
            if divisor == 1 and not any(
                c.name == "qty*price=gross" and c.tested > 0 for c in checks
            ):
                break

    if best is None:
        return ({}, [], 1)
    return (best[2], best[3], best[4])


# ----------------------------------------------------------------------
# 符号の規約
# ----------------------------------------------------------------------


def detect_sign_convention(
    stats: dict[int, ColumnStats],
    *,
    tx_type_col: int | None,
    quantity_col: int | None,
    net_col: int | None,
) -> tuple[str, list[str]]:
    """売買の向きをどこから読むか。

    取引区分の列が無い書式では数量の符号で表す。どちらも無ければ
    **推測しない** — 「全部買付」と決め打つと保有数が黙って壊れるため、
    unsigned を返して行を既定で取込対象から外す。
    """
    warnings: list[str] = []
    if tx_type_col is not None and stats.get(tx_type_col, ColumnStats()).txtype_rate >= 0.5:
        return ("by_type", warnings)

    if quantity_col is not None:
        qs = stats.get(quantity_col, ColumnStats())
        if 0.05 <= qs.negative_rate <= 0.95:
            return ("signed_quantity", warnings)

    if net_col is not None:
        ns = stats.get(net_col, ColumnStats())
        if 0.05 <= ns.negative_rate <= 0.95:
            return ("signed_net", warnings)

    if tx_type_col is not None:
        return ("by_type", warnings)

    warnings.append(
        "取引区分の列が見つからず、数量の符号からも売買を判定できませんでした。"
        "行ごとに種別を指定してください"
    )
    return ("unsigned", warnings)


# ----------------------------------------------------------------------
# 追加の手数料・税額列
# ----------------------------------------------------------------------


def collect_cost_columns(
    region: TableRegion,
    stats: dict[int, ColumnStats],
) -> tuple[list[int], list[int]]:
    """手数料・税額の列を見出しから拾う（複数列ありうる）。

    手数料＋消費税、所得税＋住民税 のように割れている書式があるので、割当問題
    には混ぜず合計として扱う。数量・単価の総当たりより先に確定させておかないと、
    受渡金額の検算（約定代金との差＝手数料＋税額）が合わなくなる。

    消費税は手数料側に入れてある — 売買手数料にかかる消費税は取得費の一部で、
    利益にかかる源泉徴収税額とは性格が違うため。
    """
    fees: list[int] = []
    taxes: list[int] = []
    for col in sorted(stats):
        st = stats[col]
        if not st.is_numeric:
            continue
        label = region.headers[col] if col < len(region.headers) else ""
        if not label:
            continue
        head = header_scores(label)
        if head.get(F.FEE, 0) >= 0.7:
            fees.append(col)
        elif head.get(F.TAX, 0) >= 0.7:
            taxes.append(col)
    return (fees, taxes)
