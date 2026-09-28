"""取引台帳とスナップショットの突合（純粋な計算。DB には触れない）。

## なぜ「引き算」なのか

証券会社の取引履歴は直近数年分しか落とせないことが多い。一方でマネーフォワード
ME の PDF は「いま何株持っているか」を正確に持っている。そこで
**スナップショットを総量の錨（アンカー）**とし、取引で説明できる分を差し引いた
残りを「取得日不明の期首ロット」として復元する。

## 単純な引き算では足りない

期首原価を

    残余原価 = Q_A × C_A − Σ(期間内の買付コスト)

で出すのは、**期間内に売却が無いときしか**正しくない。移動平均法では売却が
原価プールを按分して減らすため、買った分をそのまま引くと期首原価がずれる。

    例: 期首 100株@1000 → 期間内に 100株@2000 買い → 50株 売り
        MF は Q_A=150 / C_A=1500（総原価 225,000）と報告する
        単純な引き算: (225,000 − 200,000) / 100 = 250   ← 誤り
        正しい期首単価:                          1000

## 解き方

未知の期首単価を c0 とすると、数量も原価プールも c0 の一次式になる。

    パス1（数量）  q_A = α·q0 + β        → q0 = (Q_A − β) / α
    パス2（原価）  P_A = a + b·c0        → c0 = (Q_A·C_A/D − a) / b

副産物として **b == 0 ⟺ 期首ロットが 1 株も残っていない ⟺ 完全被覆** が
成り立つ。CSV が全期間を覆う場合と、期間内に一度ポジションがゼロになった場合の
両方を 1 つの判定で拾える。パス3 で c0 を代入した具体再生を行い、売却ごとの
実現損益とロット内訳を出す。

## 循環について

部分被覆のとき、残余原価は Q_A×C_A から逆算する。したがって再計算後の平均取得
単価は **MF の値と数学的に一致する**。この場合に増えるのは取得日・ロット内訳・
実現損益であって平均取得単価ではない。よって損益計算への反映は完全被覆に限る
（models.CostBasisOverride.applies_to_pl）。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .models import Coverage, HoldingSnapshot, Security, Transaction, TxType

if TYPE_CHECKING:
    from .transfers import TransferLink

ZERO = Decimal("0")

# 数量がこの範囲に収まれば 0 とみなす（端株・口数の丸め対策）
QTY_EPSILON = Decimal("0.0000001")
# 金額の突合の許容差（円）
MONEY_EPSILON = Decimal("1")


# ----------------------------------------------------------------------
# 警告
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CostWarning:
    code: str
    message: str
    values: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "values": dict(self.values)}


# ----------------------------------------------------------------------
# 入出力の形
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """突合の錨。スナップショットから作る「いま何株をいくらで持っているか」。"""

    account_id: int
    security_id: int
    as_of_date: date
    quantity: Decimal
    avg_cost: Decimal | None = None            # None = MF に取得単価が無い
    lot_seqs: tuple[int, ...] = (0,)
    lot_scope: str = "lot"                     # lot | group
    mf_acquired_on: date | None = None         # MF PDF が持っていた取得日
    divisor: int = 1
    currency: str = "JPY"


@dataclass(frozen=True)
class RealizedSale:
    trade_date: date
    quantity: Decimal
    proceeds: Decimal          # 譲渡価額（税引前・手数料控除前）
    fee: Decimal
    cost: Decimal              # 移動平均で按分した取得費
    realized: Decimal          # proceeds − fee − cost（税は引かない）
    tax: Decimal               # 源泉徴収税額（参考。実現損益からは引かない）


@dataclass(frozen=True)
class LotEvent:
    """UI のロット内訳・タイムライン用の 1 行。"""

    trade_date: date | None
    kind: str                  # opening | buy | sell | split | dividend | roc
    quantity: Decimal | None
    unit_cost: Decimal | None
    amount: Decimal | None
    note: str | None = None


@dataclass
class GroupResult:
    account_id: int
    security_id: int
    as_of_date: date
    lot_seqs: tuple[int, ...] = (0,)
    lot_scope: str = "lot"
    coverage: Coverage = Coverage.UNRECONCILED
    avg_cost: Decimal | None = None
    acquired_on: date | None = None
    acquired_on_src: str | None = None
    covered_quantity: Decimal = ZERO
    residual_quantity: Decimal = ZERO
    residual_avg_cost: Decimal | None = None
    realized: list[RealizedSale] = field(default_factory=list)
    realized_pl: Decimal | None = None
    income_total: Decimal = ZERO
    withheld_tax: Decimal = ZERO
    events: list[LotEvent] = field(default_factory=list)
    tx_count: int = 0
    first_tx_date: date | None = None
    last_tx_date: date | None = None
    warnings: list[CostWarning] = field(default_factory=list)

    @property
    def applies_to_pl(self) -> bool:
        return (
            self.coverage is Coverage.FULL
            and self.avg_cost is not None
            and self.lot_scope == "lot"
        )


# ----------------------------------------------------------------------
# 取引の読み取り補助
# ----------------------------------------------------------------------


def _abs(value: Decimal | None) -> Decimal:
    return abs(value) if value is not None else ZERO


def gross_of(tx: Transaction) -> Decimal:
    """約定代金。無ければ受渡金額と費用から復元し、それも無ければ数量×単価。

    **ゼロは「情報なし」と同じに扱う。** 入庫・出庫の受渡金額は 0 円と
    書かれる（現金は動かない）が、単価欄には引き継ぎ取得価額が入っている —
    NISA 期間満了の払出なら移管日の時価、他社からの移管なら元の取得単価。
    受渡 0 を「取得費 0」と読むと、移管された株が無償で増えたことになり、
    平均取得単価が薄まる（実データでは、MF の表示に対して 3 割以上低い
    平均取得単価に化けた銘柄があった）。
    """
    if tx.gross_amount:
        return abs(tx.gross_amount)
    fee, tax = _abs(tx.fee), _abs(tx.tax)
    if tx.net_amount:
        net = abs(tx.net_amount)
        # 買いは 受渡 = 代金 + 費用、売りは 受渡 = 代金 − 費用
        if tx.tx_type in (TxType.BUY, TxType.REINVEST):
            return net - fee - tax
        return net + fee + tax
    if tx.quantity is not None and tx.unit_price is not None:
        return abs(tx.quantity) * abs(tx.unit_price)
    return ZERO


def _needs_divisor(tx: Transaction) -> bool:
    """gross_of が数量×単価へフォールバックしたか（基準価額は 1万口あたり）。"""
    return not tx.gross_amount and not tx.net_amount and tx.unit_price is not None


def acquisition_cost(tx: Transaction, divisor: int) -> Decimal:
    """取得費 = 約定代金 + 買付手数料（消費税込み）。

    買付にかかった手数料は取得費に含める（税法上も、MF の平均取得単価も同じ扱い）。
    """
    gross = gross_of(tx)
    if _needs_divisor(tx):
        gross = gross / Decimal(divisor)
    return gross + _abs(tx.fee)


def sale_proceeds(tx: Transaction, divisor: int) -> Decimal:
    gross = gross_of(tx)
    if _needs_divisor(tx):
        gross = gross / Decimal(divisor)
    return gross


def _split_ratio(tx: Transaction) -> Decimal | None:
    if tx.split_ratio is not None and tx.split_ratio > 0:
        return tx.split_ratio
    return None


def _ordered(txs: Iterable[Transaction]) -> list[Transaction]:
    return sorted(
        txs,
        key=lambda t: (t.trade_date, t.id if t.id is not None else 0),
    )


def _quantity_step(tx: Transaction) -> tuple[Decimal, Decimal] | None:
    """取引が保有数をどう動かすか: (倍率, 増減) で 後 = 前 × 倍率 + 増減。

    保有数を動かさない取引（配当・元本払戻・現金の出入りなど）は None。
    数量の符号はファイルの書き方に左右されるので、種別で決める。
    """
    if tx.tx_type in (TxType.BUY, TxType.REINVEST, TxType.TRANSFER_IN):
        return (Decimal(1), _abs(tx.quantity))
    if tx.tx_type in (TxType.SELL, TxType.TRANSFER_OUT):
        return (Decimal(1), -_abs(tx.quantity))
    if tx.tx_type is TxType.SPLIT:
        ratio = _split_ratio(tx)
        if ratio is not None:
            return (ratio, ZERO)
        if tx.quantity is not None:
            return (Decimal(1), tx.quantity)
    return None


# ----------------------------------------------------------------------
# 突合本体
# ----------------------------------------------------------------------


def reconcile_group(
    txs: Sequence[Transaction],
    anchor: Anchor,
) -> GroupResult:
    """1 つの (口座, 銘柄) について、取引とスナップショットを突き合わせる。"""
    divisor = Decimal(anchor.divisor or 1)
    result = GroupResult(
        account_id=anchor.account_id,
        security_id=anchor.security_id,
        as_of_date=anchor.as_of_date,
        lot_seqs=anchor.lot_seqs,
        lot_scope=anchor.lot_scope,
    )

    usable: list[Transaction] = []
    for tx in _ordered(txs):
        if tx.trade_date > anchor.as_of_date:
            result.warnings.append(
                CostWarning(
                    "TX_AFTER_SNAPSHOT",
                    f"{tx.trade_date} の取引はスナップショットの基準日"
                    f"（{anchor.as_of_date}）より後のため突合から除きました。"
                    "マネーフォワードME の PDF を取り込み直すと反映されます",
                    {"trade_date": tx.trade_date.isoformat()},
                )
            )
            continue
        if tx.currency != anchor.currency:
            result.warnings.append(
                CostWarning(
                    "CURRENCY_MISMATCH",
                    f"取引の通貨（{tx.currency}）が銘柄の建値通貨（{anchor.currency}）と"
                    "違うため、取得原価は再計算していません",
                    {"tx_currency": tx.currency, "security_currency": anchor.currency},
                )
            )
            result.coverage = Coverage.UNRECONCILED
            return result
        usable.append(tx)

    result.tx_count = len(usable)
    if usable:
        result.first_tx_date = usable[0].trade_date
        result.last_tx_date = usable[-1].trade_date

    result.income_total = sum(
        (_abs(t.net_amount) for t in usable if t.tx_type is TxType.DIVIDEND), ZERO
    )
    result.withheld_tax = sum((_abs(t.tax) for t in usable), ZERO)

    # ---- パス1: 数量。q_A = α·q0 + β
    alpha, beta = Decimal(1), ZERO
    for tx in usable:
        step = _quantity_step(tx)
        if step is not None:
            mult, delta = step
            alpha *= mult
            beta = beta * mult + delta

    if alpha == 0:
        result.warnings.append(
            CostWarning("BAD_SPLIT", "分割の比率が 0 のため突合できませんでした")
        )
        return result

    q0 = (anchor.quantity - beta) / alpha

    if q0 < -QTY_EPSILON:
        result.warnings.append(
            CostWarning(
                "NEGATIVE_RESIDUAL",
                f"取引履歴が示す保有数がスナップショット（{anchor.quantity}）を"
                f"{-q0} 超えています。二重取込・銘柄の誤照合・売却行の欠落・"
                "分割の二重計上のいずれかが疑われます",
                {"residual": str(q0), "anchor_quantity": str(anchor.quantity)},
            )
        )
        result.coverage = Coverage.UNRECONCILED
        return result
    if abs(q0) <= QTY_EPSILON:
        q0 = ZERO

    # ---- パス2: 原価プール。P_A = a + b·c0
    q = q0
    a, b = ZERO, q0 / divisor
    for tx in usable:
        if tx.tx_type in (TxType.BUY, TxType.REINVEST):
            a += acquisition_cost(tx, anchor.divisor)
            q += _abs(tx.quantity)
        elif tx.tx_type is TxType.TRANSFER_IN:
            # 現金の動きが無い入庫。原価が書いてあれば取得費として扱う。
            cost = acquisition_cost(tx, anchor.divisor)
            if cost:
                a += cost
            else:
                result.warnings.append(
                    CostWarning(
                        "TRANSFER_WITHOUT_COST",
                        f"{tx.trade_date} の入庫に取得価額が無いため、数量だけ加えました",
                    )
                )
            q += _abs(tx.quantity)
        elif tx.tx_type in (TxType.SELL, TxType.TRANSFER_OUT):
            qs = _abs(tx.quantity)
            if q > 0:
                remain = (q - qs) / q
                a *= remain
                b *= remain
            q -= qs
        elif tx.tx_type is TxType.SPLIT:
            ratio = _split_ratio(tx)
            if ratio is not None:
                q *= ratio
            elif tx.quantity is not None:
                q += tx.quantity
        elif tx.tx_type is TxType.RETURN_OF_CAPITAL:
            # 特別分配金は元本の払い戻し。数量は動かさず取得原価だけ減る。
            a -= _abs(tx.net_amount)

    # ---- 被覆の判定と期首単価の逆算
    #  b == 0 ⟺ 期首ロットが 1 株も残っていない ⟺ 完全被覆
    c0: Decimal | None = None
    if b == 0:
        result.coverage = Coverage.FULL
        if anchor.quantity > 0:
            result.avg_cost = a * divisor / anchor.quantity
        else:
            result.avg_cost = None
    elif anchor.avg_cost is None:
        result.coverage = Coverage.PARTIAL_UNCOSTED
        result.warnings.append(
            CostWarning(
                "NO_ANCHOR_COST",
                "取引履歴より前から保有している分があり、マネーフォワードME 側にも"
                "平均取得単価が無いため、取得原価を確定できませんでした",
            )
        )
    else:
        target = anchor.quantity * anchor.avg_cost / divisor
        c0 = (target - a) / b
        if c0 < 0:
            result.warnings.append(
                CostWarning(
                    "NEGATIVE_RESIDUAL_COST",
                    f"取引履歴から逆算した期首の取得単価が負（{c0:.2f}）になりました。"
                    "取引の重複か、売却行の欠落が疑われます",
                    {"residual_avg_cost": str(c0)},
                )
            )
            result.coverage = Coverage.UNRECONCILED
            return result
        result.coverage = Coverage.PARTIAL
        # 部分被覆では残余を錨から逆算しているので、再計算値は定義上 MF と一致する
        result.avg_cost = anchor.avg_cost
        result.residual_avg_cost = c0

    # ---- パス3: c0 を入れた具体再生。実現損益とロット内訳を出す。
    _replay(result, usable, q0, c0, anchor)

    result.residual_quantity = max(result.residual_quantity, ZERO)
    result.covered_quantity = anchor.quantity - result.residual_quantity
    result.acquired_on, result.acquired_on_src = _acquired_on(result, usable, anchor)
    return result


def _replay(
    result: GroupResult,
    txs: list[Transaction],
    q0: Decimal,
    c0: Decimal | None,
    anchor: Anchor,
) -> None:
    """期首単価を確定させたうえでの具体再生。"""
    divisor = Decimal(anchor.divisor or 1)
    q = q0
    pool = (q0 * c0 / divisor) if (c0 is not None and q0) else ZERO
    residual = q0
    realized_total = ZERO
    has_sale = False

    if q0 > 0:
        result.events.append(
            LotEvent(
                trade_date=anchor.mf_acquired_on,
                kind="opening",
                quantity=q0,
                unit_cost=c0,
                amount=pool if c0 is not None else None,
                note="取得日不明（取引履歴より前から保有）",
            )
        )

    for tx in txs:
        if tx.tx_type in (TxType.BUY, TxType.REINVEST, TxType.TRANSFER_IN):
            qty = _abs(tx.quantity)
            cost = acquisition_cost(tx, anchor.divisor)
            q += qty
            pool += cost
            result.events.append(
                LotEvent(
                    trade_date=tx.trade_date,
                    kind="buy",
                    quantity=qty,
                    unit_cost=tx.unit_price,
                    amount=cost,
                    note="再投資" if tx.tx_type is TxType.REINVEST else None,
                )
            )
        elif tx.tx_type in (TxType.SELL, TxType.TRANSFER_OUT):
            qty = _abs(tx.quantity)
            if q <= 0:
                continue
            share = qty / q
            removed = pool * share
            residual -= residual * share      # 期首ロットも按分して減る
            proceeds = (
                sale_proceeds(tx, anchor.divisor)
                if tx.tx_type is TxType.SELL
                else ZERO
            )
            fee = _abs(tx.fee)
            # 実現損益 = 譲渡価額 − 譲渡費用 − 取得費。源泉徴収税額は引かない
            # （利益にかかる税であって取得費ではない。年間取引報告書と突き合わせ
            #  られなくなる）。
            gain = proceeds - fee - removed
            q -= qty
            pool -= removed
            if tx.tx_type is TxType.SELL:
                has_sale = True
                realized_total += gain
                result.realized.append(
                    RealizedSale(
                        trade_date=tx.trade_date,
                        quantity=qty,
                        proceeds=proceeds,
                        fee=fee,
                        cost=removed,
                        realized=gain,
                        tax=_abs(tx.tax),
                    )
                )
            result.events.append(
                LotEvent(
                    trade_date=tx.trade_date,
                    kind="sell",
                    quantity=-qty,
                    unit_cost=tx.unit_price,
                    amount=proceeds,
                    note="出庫" if tx.tx_type is TxType.TRANSFER_OUT else None,
                )
            )
        elif tx.tx_type is TxType.SPLIT:
            ratio = _split_ratio(tx)
            if ratio is not None:
                q *= ratio
                residual *= ratio
                note = f"分割 1:{ratio}"
            elif tx.quantity is not None and q:
                grew = (q + tx.quantity) / q
                q += tx.quantity
                residual *= grew
                note = "分割（数量差分）"
            else:
                continue
            # 分割は原価プールを動かさない（単価だけが下がる）
            result.events.append(
                LotEvent(tx.trade_date, "split", None, None, None, note)
            )
        elif tx.tx_type is TxType.DIVIDEND:
            result.events.append(
                LotEvent(tx.trade_date, "dividend", None, None, _abs(tx.net_amount))
            )
        elif tx.tx_type is TxType.RETURN_OF_CAPITAL:
            amount = _abs(tx.net_amount)
            pool -= amount
            if pool < 0:
                pool = ZERO
                result.warnings.append(
                    CostWarning(
                        "ROC_EXCEEDS_COST",
                        f"{tx.trade_date} の特別分配金が取得原価を超えたため 0 で止めました",
                    )
                )
            result.events.append(
                LotEvent(tx.trade_date, "roc", None, None, amount, "特別分配金")
            )

    result.residual_quantity = residual
    result.realized_pl = realized_total if has_sale else None

    # 完全被覆のときだけ、再生した原価から平均取得単価を出せる（錨に依存しない）
    if result.coverage is Coverage.FULL and anchor.quantity > 0:
        result.avg_cost = pool * divisor / anchor.quantity


def _acquired_on(
    result: GroupResult, txs: list[Transaction], anchor: Anchor
) -> tuple[date | None, str | None]:
    """取得日。完全被覆なら CSV の最古の買付、そうでなければ MF が持っていた取得日。

    部分被覆では残余が CSV より前から存在するので、CSV の最古の買付は
    「いちばん古い取得日」ではない。MF PDF が取得日をすでに持っている
    （holding_snapshots.raw の meta.acquired_on）ので、そちらを使う。
    """
    buys = [t.trade_date for t in txs
            if t.tx_type in (TxType.BUY, TxType.REINVEST, TxType.TRANSFER_IN)]
    if result.coverage is Coverage.FULL and buys:
        return (min(buys), "csv")
    if anchor.mf_acquired_on is not None:
        return (anchor.mf_acquired_on, "mf_raw")
    if buys:
        return (min(buys), "csv")
    return (None, None)


def reconcile_closed(
    txs: Sequence[Transaction],
    *,
    account_id: int,
    security_id: int,
    as_of_date: date,
    divisor: int = 1,
    currency: str = "JPY",
) -> GroupResult:
    """スナップショットに無い銘柄（取込前に売り切った銘柄）。

    錨が無いので取得原価は上書きしないが、実現損益は出せる。
    """
    anchor = Anchor(
        account_id=account_id,
        security_id=security_id,
        as_of_date=as_of_date,
        quantity=ZERO,
        avg_cost=None,
        divisor=divisor,
        currency=currency,
    )
    result = reconcile_group(txs, anchor)
    if result.coverage is Coverage.FULL and abs(result.residual_quantity) <= QTY_EPSILON:
        result.warnings.append(
            CostWarning(
                "CLOSED_POSITION",
                "スナップショットに無い銘柄です（取込前に売却済みとみなしました）",
            )
        )
    else:
        result.warnings.append(
            CostWarning(
                "CLOSED_MISMATCH",
                "スナップショットに無い銘柄ですが、取引を再生しても保有数が 0 に"
                "なりませんでした。実現損益は参考値です",
            )
        )
    return result


# ----------------------------------------------------------------------
# 錨の組み立て
# ----------------------------------------------------------------------


def _mf_acquired_on(lot: HoldingSnapshot) -> date | None:
    """MF PDF が取り込んだ取得日を raw から取り出す。

    mf_pdf は 取得日 列を meta['acquired_on'] に入れており、それが
    holding_snapshots.raw に JSON で残っている。これまで誰も読んでいなかった。
    """
    meta = (lot.raw or {}).get("meta") or {}
    text = meta.get("acquired_on")
    if not text:
        return None
    return parse_loose_date(str(text))


def parse_loose_date(text: str) -> date | None:
    """'2023/4/5' '23/04/05' '2023-04-05' 程度を緩く読む。"""
    import re

    s = str(text).strip()
    m = re.match(r"^(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})$", s)
    if m:
        try:
            return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def build_anchors(
    lots: Sequence[HoldingSnapshot],
    securities: dict[int, Security],
) -> tuple[list[Anchor], list[CostWarning]]:
    """現在ロットから (口座, 銘柄) 単位の錨を作る。

    MF PDF は株式・投信の lot_label を設定せず、lot_seq は取込時に平均取得単価の
    近さで機械的に割り当てられたものなので、CSV の「特定 / 一般 / NISA」を
    確実に対応づける術がない。したがって突合は (口座, 銘柄) の合計で行い、
    ロットが複数あるときは損益への反映を見送る（lot_scope='group'）。
    """
    warnings: list[CostWarning] = []
    groups: dict[tuple[int, int], list[HoldingSnapshot]] = {}
    for lot in lots:
        if lot.quantity == 0:
            continue
        groups.setdefault((lot.account_id, lot.security_id), []).append(lot)

    anchors: list[Anchor] = []
    for (account_id, security_id), members in sorted(groups.items()):
        sec = securities.get(security_id)
        if sec is None:
            continue
        total_qty = sum((m.quantity for m in members), ZERO)
        if total_qty <= 0:
            continue

        costed = [m for m in members if m.avg_cost is not None]
        avg_cost: Decimal | None = None
        if costed:
            costed_qty = sum((m.quantity for m in costed), ZERO)
            if costed_qty > 0:
                avg_cost = sum(
                    (m.quantity * m.avg_cost for m in costed), ZERO
                ) / costed_qty

        scope = "lot" if len(members) == 1 else "group"
        if len(members) > 1 and costed and len(costed) != len(members):
            # 一部のロットにしか取得単価が無い。合成した平均を全ロットに被せると
            # 合計原価が狂うので、損益には反映しない。
            warnings.append(
                CostWarning(
                    "LOT_MIXED_COST",
                    "同じ銘柄で取得単価のあるロットと無いロットが混在しているため、"
                    "取得原価は損益に反映しません（取得日と取引履歴は表示します）",
                    {"account_id": account_id, "security_id": security_id},
                )
            )
            avg_cost = None

        acquired = next(
            (d for d in (_mf_acquired_on(m) for m in members) if d is not None), None
        )
        as_of = max(m.as_of_date for m in members)

        anchors.append(
            Anchor(
                account_id=account_id,
                security_id=security_id,
                as_of_date=as_of,
                quantity=total_qty,
                avg_cost=avg_cost,
                lot_seqs=tuple(sorted(m.lot_seq for m in members)),
                lot_scope=scope,
                mf_acquired_on=acquired,
                divisor=sec.price_unit_divisor,
                currency=sec.currency,
            )
        )
    return (anchors, warnings)


def reconcile_all(
    transactions: Sequence[Transaction],
    lots: Sequence[HoldingSnapshot],
    securities: dict[int, Security],
) -> tuple[list[GroupResult], list[CostWarning]]:
    """台帳全体を突き合わせる。スナップショットに無い銘柄も拾う。"""
    anchors, warnings = build_anchors(lots, securities)
    by_group: dict[tuple[int, int], list[Transaction]] = {}
    for tx in transactions:
        if tx.security_id is None:
            continue
        by_group.setdefault((tx.account_id, tx.security_id), []).append(tx)

    results: list[GroupResult] = []
    seen: set[tuple[int, int]] = set()
    for anchor in anchors:
        key = (anchor.account_id, anchor.security_id)
        seen.add(key)
        txs = by_group.get(key)
        if not txs:
            continue
        results.append(reconcile_group(txs, anchor))

    latest = max((lot.as_of_date for lot in lots), default=None)
    for key, txs in sorted(by_group.items()):
        if key in seen:
            continue
        sec = securities.get(key[1])
        if sec is None or latest is None:
            continue
        results.append(
            reconcile_closed(
                txs,
                account_id=key[0],
                security_id=key[1],
                as_of_date=max(latest, max(t.trade_date for t in txs)),
                divisor=sec.price_unit_divisor,
                currency=sec.currency,
            )
        )
    return (results, warnings)


# ----------------------------------------------------------------------
# 推移グラフ用: 最初のスナップショットより前の保有数
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class QuantityPath:
    """口座×銘柄の、最初のスナップショットより前の保有数（取引履歴から逆算）。

    before（最初のスナップショットの日）より前にだけ使う。それ以降は
    スナップショットが正。changes は (約定日, その日からの保有数) の昇順で、
    最初の約定日より前は opening ＝ 取引履歴より前から持っていた分（期首ロット）。
    期首ロットはいつから持っていたか判らないので、記録ではなく遡った数量として
    扱う。完全被覆なら opening は 0 で、最初の買付より前は持っていない。

    スナップショットの無い口座×銘柄（売り切った・移管した、今は持っていない保有）
    では before が date.max で、全期間を取引履歴の数量で描く。
    """

    before: date
    changes: tuple[tuple[date, Decimal], ...]
    opening: Decimal

    def at(self, day: date) -> tuple[Decimal, bool]:
        """(その日の保有数, 記録ではなく遡った数量か)。"""
        i = bisect_right(self.changes, day, key=lambda c: c[0])
        if i == 0:
            return (self.opening, self.opening > QTY_EPSILON)
        return (self.changes[i - 1][1], False)


def quantity_paths(
    transactions: Sequence[Transaction],
    snapshots: Sequence[HoldingSnapshot],
    links: Sequence["TransferLink"] = (),
) -> dict[tuple[int, int], QuantityPath]:
    """口座×銘柄ごとに、最初のスナップショットより前の保有数を取引履歴から戻す。

    推移グラフ（銘柄詳細の保有数・評価額）で、最初の取込より前を「最初の
    保有数のまま」ではなく実際の数量で描くための材料。スナップショットを錨に、
    約定を新しい順に打ち消しながらさかのぼる。錨の数量はそのまま残るので、
    取込の前後で線はつながる。

    links（口座間の移管。core/transfers.py）の移管先は、最初のスナップショット
    より前を「移管の日から移ってきた数量、それより前は持っていない」で描く。
    移管先の取引履歴のうち移管が届いたものとして数えた入庫（link.absorbed。元の
    取得日の日付で記録された入庫など）は外し、移管の日の仮の入庫に置き換える。

    スナップショットの無い口座×銘柄（売り切った・移管した、今は持っていない
    保有）は、今は 0 という事実を錨に取引履歴だけで全期間を描く。

    次の口座×銘柄は作らない（呼び出し側は従来どおり最初の保有数で遡る）:
    - 最初のスナップショットより前の約定も移管も無い（さかのぼる材料が無い）
    - ロットごとに記録の始まる日が違う（後から現れたロットが買付なのか、
      既存の保有が行を分けただけなのかを決められない）
    - さかのぼる途中で保有数が負になる（二重取込・売却行の欠落・銘柄の誤照合。
      スナップショットと合わない数字は描かない）。スナップショットの無い
      口座×銘柄では、取引履歴の上でまだ持っていることになるもの
    """
    snaps_by_group: dict[tuple[int, int], list[HoldingSnapshot]] = {}
    for s in snapshots:
        snaps_by_group.setdefault((s.account_id, s.security_id), []).append(s)
    absorbed = {k for link in links for k in link.absorbed}
    txs_by_group: dict[tuple[int, int], list[Transaction]] = {}
    for tx in transactions:
        if tx.security_id is None or _quantity_step(tx) is None:
            continue
        if absorbed and _tx_key(tx) in absorbed:
            continue    # 移管の日の仮の入庫として数える
        txs_by_group.setdefault((tx.account_id, tx.security_id), []).append(tx)
    links_into: dict[tuple[int, int], list[TransferLink]] = {}
    links_out: dict[tuple[int, int], list[TransferLink]] = {}
    for link in links:
        links_into.setdefault((link.to_account_id, link.security_id), []).append(link)
        links_out.setdefault((link.from_account_id, link.security_id), []).append(link)

    out: dict[tuple[int, int], QuantityPath] = {}
    for key, snaps in snaps_by_group.items():
        txs = txs_by_group.get(key, [])
        incoming = links_into.get(key, [])
        if not txs and not incoming:
            continue
        path, _status = _group_quantity_path(snaps, _ordered(txs), incoming)
        if path is not None:
            out[key] = path
    # スナップショットの無い口座×銘柄。移管で経由しただけの口座（記録の無い
    # 口座を手動で結び付けた場合）は、出入りを仮の入庫・出庫で補う
    for key in (set(txs_by_group) | set(links_into)) - set(snaps_by_group):
        txs = list(txs_by_group.get(key, []))
        txs += [_virtual_transfer(link, TxType.TRANSFER_IN) for link in links_into.get(key, [])
                if not _has_transfer(txs, link, TxType.TRANSFER_IN)]
        txs += [_virtual_transfer(link, TxType.TRANSFER_OUT) for link in links_out.get(key, [])
                if not _has_transfer(txs, link, TxType.TRANSFER_OUT)]
        path = _closed_group_path(_ordered(txs))
        if path is not None:
            out[key] = path
    return out


def _tx_key(t: Transaction) -> tuple[str, int]:
    """取引の同一性（transfers.tx_key と同じ。循環 import を避けて複製）。"""
    return ("id", t.id) if t.id is not None else ("obj", id(t))


def _closed_group_path(txs: list[Transaction]) -> QuantityPath | None:
    """スナップショットの無い口座×銘柄の全期間の保有数（今は 0 を錨に戻す）。"""
    by_day: dict[date, list[Transaction]] = {}
    for t in txs:
        by_day.setdefault(t.trade_date, []).append(t)
    q = ZERO
    changes: list[tuple[date, Decimal]] = []
    for day in sorted(by_day, reverse=True):
        changes.append((day, q))
        for t in reversed(by_day[day]):
            mult, delta = _quantity_step(t)
            q = (q - delta) / mult
        if q < -QTY_EPSILON:
            return None
        if abs(q) <= QTY_EPSILON:
            q = ZERO
    changes.reverse()
    return QuantityPath(before=date.max, changes=tuple(changes), opening=q)


def _virtual_transfer(link: "TransferLink", tx_type: TxType) -> Transaction:
    """移管を仮の入庫（移管先）・出庫（移管元）にする。台帳には書かない（推移の計算用）。"""
    incoming = tx_type is TxType.TRANSFER_IN
    return Transaction(
        dedup_key=f"transfer:{link.from_account_id}:{link.date.isoformat()}",
        account_id=link.to_account_id if incoming else link.from_account_id,
        security_id=link.security_id,
        trade_date=link.date,
        tx_type=tx_type,
        quantity=link.quantity if incoming else -link.quantity,
        origin="transfer",
    )


def _linked_path(
    held: Decimal, first: date, incoming: Sequence["TransferLink"]
) -> tuple[QuantityPath | None, str]:
    """取引履歴の無い移管先の、最初のスナップショットより前の保有数。

    最初のスナップショットの数量から移ってきた数量を引いた残りが、移管より前から
    その口座にあった分（期首ロット。遡った数量として破線で描く）。移管の日が
    最初のスナップショットの日と同じ（記録の上で消えた日と現れた日が同じ）なら、
    その日の記録に移ってきた分が含まれている。
    """
    arrived = sorted(
        (link.date, link.quantity) for link in incoming if link.date <= first
    )
    if not arrived:
        return (None, "no_earlier_trades")
    opening = held - sum((q for _d, q in arrived), ZERO)
    if opening < -QTY_EPSILON:
        return (None, "inconsistent")
    if abs(opening) <= QTY_EPSILON:
        opening = ZERO
    q = opening
    changes: list[tuple[date, Decimal]] = []
    for day, quantity in arrived:
        q += quantity
        if day >= first:
            continue
        if changes and changes[-1][0] == day:
            changes[-1] = (day, q)
        else:
            changes.append((day, q))
    return (QuantityPath(before=first, changes=tuple(changes), opening=opening), "linked")


def _has_transfer(txs: Sequence[Transaction], link: "TransferLink", tx_type: TxType) -> bool:
    """移管の日の前後（7 日前〜30 日後）にその向きの移動の記録が取引履歴にあるか。

    数量は比べない — 移管元は貸株の出し入れと移管の出庫が同じ日に何行にも分かれて
    いることがあり、1 行ずつでは移管の数量にならない。
    """
    lo, hi = link.date - timedelta(days=7), link.date + timedelta(days=30)
    return any(t.tx_type is tx_type and lo <= t.trade_date <= hi for t in txs)


def quantity_path_status(
    transactions: Sequence[Transaction],
    snapshots: Sequence[HoldingSnapshot],
    account_id: int,
    security_id: int,
) -> str:
    """口座×銘柄の取引が推移グラフに使われるか。使われないならその理由。

    traced（使う）/ closed（記録は無いが、今は持っていない保有として取引だけで
    描く — 買って売り切った・移管した）/ no_snapshot（その口座にその銘柄の記録が無い）/
    no_trades / no_earlier_trades（最初の取込より前の取引が無い）/
    lots_differ / inconsistent。手動登録の直後に、入れた取引がグラフに
    届いたかを利用者へ返すためのもの（判定は quantity_paths と同じ）。
    """
    key = (account_id, security_id)
    snaps = [s for s in snapshots if (s.account_id, s.security_id) == key]
    txs = [
        t for t in transactions
        if (t.account_id, t.security_id) == key and _quantity_step(t) is not None
    ]
    if not snaps:
        # 今は持っていない保有として取引履歴だけで描けるか（買って売り切った等）
        if txs and _closed_group_path(_ordered(txs)) is not None:
            return "closed"
        return "no_snapshot"
    if not txs:
        return "no_trades"
    return _group_quantity_path(snaps, _ordered(txs))[1]


def _group_quantity_path(
    snaps: list[HoldingSnapshot],
    txs: list[Transaction],
    incoming: Sequence["TransferLink"] = (),
) -> tuple[QuantityPath | None, str]:
    lots: dict[int, list[HoldingSnapshot]] = {}
    for s in snaps:
        lots.setdefault(s.lot_seq, []).append(s)
    starts = {min(s.as_of_date for s in members) for members in lots.values()}
    if len(starts) != 1:
        return (None, "lots_differ")
    first = starts.pop()
    if incoming:
        if not any(t.trade_date < first for t in txs):
            held = sum(
                (min(members, key=lambda s: s.as_of_date).quantity for members in lots.values()),
                ZERO,
            )
            return _linked_path(held, first, incoming)
        # 自前の取引履歴がある移管先。移管が届いた入庫は呼び出し側で外してあるので、
        # 移管の日に入庫が残っていない移管だけを仮の入庫として足す
        extra = [
            _virtual_transfer(link, TxType.TRANSFER_IN) for link in incoming
            if link.date < first and not _has_transfer(txs, link, TxType.TRANSFER_IN)
        ]
        if extra:
            txs = _ordered([*txs, *extra])
    if not any(t.trade_date < first for t in txs):
        return (None, "no_earlier_trades")

    # 錨は「その日に約定の無い」最初のスナップショット日。取込の基準日に約定が
    # あると、それがスナップショットに入っているか（夕方の取込）いないか（朝の
    # 自動取込）が判らない。約定の無い日の数量ならどちらでも同じになる。
    # そういう日が無ければ突合（reconcile_group）と同じく基準日の約定を含める
    trade_days = {t.trade_date for t in txs}
    anchor = next(
        (d for d in sorted({s.as_of_date for s in snaps}) if d not in trade_days), first
    )
    held = ZERO
    for members in lots.values():
        upto = [s for s in members if s.as_of_date <= anchor]
        held += max(upto, key=lambda s: s.as_of_date).quantity

    by_day: dict[date, list[Transaction]] = {}
    for t in txs:
        if t.trade_date <= anchor:
            by_day.setdefault(t.trade_date, []).append(t)
    q = held
    changes: list[tuple[date, Decimal]] = []
    for day in sorted(by_day, reverse=True):
        if day < first:
            changes.append((day, q))
        for t in reversed(by_day[day]):
            mult, delta = _quantity_step(t)
            q = (q - delta) / mult
        if q < -QTY_EPSILON:
            return (None, "inconsistent")
        if abs(q) <= QTY_EPSILON:
            q = ZERO
    changes.reverse()
    return (QuantityPath(before=first, changes=tuple(changes), opening=q), "traced")
