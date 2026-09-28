"""口座間の移管（証券会社を移した株・投信）を見つけて結び付ける（純粋な計算）。

## 何が困るのか

証券会社を移した銘柄は、移管元の取引履歴では「出庫」で消え、移管先では最初の
記録（マネーフォワード ME の取込や取引履歴）から突然現れる。1銘柄の推移を口座
ごとに描くと、移管元の履歴は「今は持っていない口座のもの」として使われず、
移管先は最初の保有数のまま過去へ遡る（破線）。移管元がスナップショットを持って
いれば、移管の前の期間を 2 回数えることさえある。

## 何を移管とみなすか

移管元（出ていった記録）:
- 取引履歴の出庫。同じ口座・同じ銘柄の入庫と ±10 日以内で相殺してから数える。
  NISA → 特定の払出や貸株の出し入れは口座の中の移動で、外へは出ていない
- スナップショットで保有が 0 になった日。前の記録からの間に取引履歴の売却・出庫が
  あれば、売却か既知の出庫なので数えない

移管先（入ってきた記録）: 別の口座に、その銘柄が現れた記録。移管元の出来事ごとに
見る（_arrival）。
- 移管の前後（7 日前〜30 日後。受渡日で見てもよい）の入庫は、その移管が届いたもの
- 移管より前の記録が入庫だけ（スナップショットも無い）なら、移管先の証券会社が
  移管で受け入れたロットを元の取得日の日付で記録している（楽天証券の取引履歴が
  こう出す）。これも移管が届いたものとみなす（backdated）。推移ではこの入庫を
  移管の日へ移して数える（absorbed）。取得原価の計算は元の日付のまま使う
- 移管より前に入庫以外の記録（買付・配当・スナップショット）がある口座は、移管より
  前から持っていた（early）。手動の候補にだけ出す
- どれでもなければ、移管の後の最初の記録（スナップショットか取引）

## 自動で結び付ける条件

移管先の候補のうち、次を満たすものが 1 つに決まるとき。
- 出ていった日の 7 日前以降、400 日以内にはじめて現れた
- 数量が出ていった数量と一致する
- 取得単価が食い違わない。出庫行の単価は移管先へ引き継がれる取得単価なので、
  両方が分かっていて 2% を超えて違えば、移管ではなく買い直したとみなす
- 最初の記録が買付ではない（買付で始まる口座は買ったもの）

候補が複数あるときは取得単価まで一致するものを、それでも複数ならいちばん早く
現れた口座を選ぶ。移管を重ねた銘柄（A → B → C）では、C も A の移管先の候補に
なるが、B から C への移管があとで別に結び付くので、最初に現れた B が正しい。
現れた日が同じで決められないもの・数量が合わないものは、候補を添えて利用者に
選んでもらう（Resolution.unresolved）。

株式併合（出庫 100 と入庫 10 の組）のように移管元に保有が残る出庫は、数量の
一致する候補が無い限り候補に出さない（出すと併合のたびに問い合わせになる）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .cost_basis import QTY_EPSILON, ZERO, _ordered, _quantity_step
from .models import HoldingSnapshot, Transaction, TxType

# 同じ口座の入庫と相殺する範囲（NISA → 特定の払出が別の日に入庫される）
CANCEL_DAYS = 10
# 移管先の記録が移管元の出庫より早くてよい日数（受渡日・計上日のずれ）
EARLY_DAYS = 7
# 移管先の入庫の計上が移管元の出庫より遅れてよい日数
ARRIVE_DAYS = 30
# 自動で結び付ける、移管から移管先の最初の記録までの日数。取込を始めた日が
# 移管から何か月も後になることはあるが、何年も後に同じ株数を買い直したものを
# 移管とみなさないための上限
WINDOW_DAYS = 400
# 引き継いだ取得単価の一致とみなす差（MF の丸め・端数の扱いの違いを吸収する）
COST_TOLERANCE = Decimal("0.02")


@dataclass(frozen=True)
class TransferOut:
    """移管元: ある口座から銘柄が出ていった記録。"""

    security_id: int
    account_id: int
    date: date
    quantity: Decimal
    source: str                      # "ledger"（取引履歴の出庫）| "snapshot"（保有が 0 に）
    carried_cost: Decimal | None     # 引き継いだ取得単価（分からなければ None）
    closed: bool                     # 移管元にその銘柄が残らない

    @property
    def key(self) -> tuple[int, int, date]:
        return (self.security_id, self.account_id, self.date)


@dataclass(frozen=True)
class Arrival:
    """移管元の出来事ひとつに対して、ある口座にその銘柄がどう現れたか（移管先の候補）。"""

    security_id: int
    account_id: int
    date: date                       # 記録の上で現れた日（表示用）
    effective: date                  # 移管として届いた日（候補の比べ合い・期限の判定用）
    quantity: Decimal
    avg_cost: Decimal | None
    kind: str                        # "snapshot" | "transfer_in" | "buy" | "other"
    early: bool = False              # 移管より前からその口座に保有があった
    backdated: bool = False          # 移管の入庫を元の取得日の日付で記録している
    absorbed: tuple[Any, ...] = ()   # 移管として数える移管先の入庫（tx_key）


@dataclass(frozen=True)
class Candidate:
    arrival: Arrival
    quantity_match: bool
    cost_match: bool | None          # None = 比べられない

    @property
    def early(self) -> bool:
        return self.arrival.early


@dataclass(frozen=True)
class TransferLink:
    """結び付けた移管。推移では移管先の最初の記録より前をこれでつなぐ。"""

    security_id: int
    from_account_id: int
    to_account_id: int
    date: date
    quantity: Decimal
    origin: str                      # "auto" | "manual"
    source: str                      # 移管元の記録: "ledger" | "snapshot" | "manual"
    cost_match: bool | None = None
    manual_id: int | None = None
    # 移管先の取引履歴のうち、この移管が届いたものとして数える入庫（tx_key）。
    # 推移ではこれを外し、代わりに移管の日の入庫として数える
    absorbed: tuple[Any, ...] = ()


def tx_key(t: Transaction) -> tuple[str, int]:
    """取引の同一性（DB の id。まだ保存していない取引はオブジェクトそのもの）。"""
    return ("id", t.id) if t.id is not None else ("obj", id(t))


@dataclass(frozen=True)
class ManualDecision:
    """利用者の判断（store.transfer_links の1行）。to_account_id=None は「移管ではない」。"""

    id: int
    security_id: int
    from_account_id: int
    to_account_id: int | None
    date: date
    quantity: Decimal

    @property
    def key(self) -> tuple[int, int, date]:
        return (self.security_id, self.from_account_id, self.date)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ManualDecision":
        return cls(
            id=row["id"],
            security_id=row["security_id"],
            from_account_id=row["from_account_id"],
            to_account_id=row["to_account_id"],
            date=row["transfer_date"],
            quantity=row["quantity"],
        )


@dataclass
class Resolution:
    links: list[TransferLink] = field(default_factory=list)
    # 自動で決められなかった移管元と、その候補（もっともらしい順）
    unresolved: list[tuple[TransferOut, list[Candidate]]] = field(default_factory=list)
    # 利用者が「移管ではない」とした移管元（記録が見つからなくなった判断は out=None）
    dismissed: list[tuple[TransferOut | None, ManualDecision]] = field(default_factory=list)
    # 移管元ごとの移管先の候補（結び付けたもの・移管ではないとしたものの付け替え用）
    candidates: dict[tuple[int, int, date], list[Candidate]] = field(default_factory=dict)


# ----------------------------------------------------------------------
# 移管元
# ----------------------------------------------------------------------


def _abs(q: Decimal | None) -> Decimal:
    return abs(q) if q is not None else ZERO


def _weighted_cost(rows: Iterable[tuple[Decimal, Decimal | None]]) -> Decimal | None:
    """(数量, 単価) の加重平均。1 行でも単価が無い・0 なら None（分からない）。"""
    total_q = total = ZERO
    for q, price in rows:
        if price is None or price <= ZERO:
            return None
        total_q += q
        total += q * price
    return total / total_q if total_q > ZERO else None


def ledger_transfer_outs(transactions: Sequence[Transaction]) -> list[TransferOut]:
    """取引履歴の出庫から、口座の外へ出ていった記録を拾う。"""
    groups: dict[tuple[int, int], list[Transaction]] = {}
    for t in transactions:
        if t.security_id is not None:
            groups.setdefault((t.account_id, t.security_id), []).append(t)

    out: list[TransferOut] = []
    for (account_id, security_id), txs in groups.items():
        txs = _ordered(txs)
        net: dict[date, Decimal] = {}
        costs: dict[date, list[tuple[Decimal, Decimal | None]]] = {}
        for t in txs:
            if t.tx_type is TxType.TRANSFER_IN:
                net[t.trade_date] = net.get(t.trade_date, ZERO) + _abs(t.quantity)
            elif t.tx_type is TxType.TRANSFER_OUT:
                net[t.trade_date] = net.get(t.trade_date, ZERO) - _abs(t.quantity)
                costs.setdefault(t.trade_date, []).append((_abs(t.quantity), t.unit_price))
        if not any(n < -QTY_EPSILON for n in net.values()):
            continue

        # 口座の中の移動（近い日の入庫）と相殺する。近い日から順に使う
        spare = {d: n for d, n in net.items() if n > QTY_EPSILON}
        balance = _running_balance(txs)
        for d in sorted(net):
            remaining = -net[d]
            if remaining <= QTY_EPSILON:
                continue
            for d2 in sorted(spare, key=lambda x: (abs((x - d).days), x)):
                if abs((d2 - d).days) > CANCEL_DAYS or remaining <= QTY_EPSILON:
                    continue
                used = min(spare[d2], remaining)
                spare[d2] -= used
                remaining -= used
            if remaining <= QTY_EPSILON:
                continue
            after = balance.get(d)
            out.append(
                TransferOut(
                    security_id=security_id,
                    account_id=account_id,
                    date=d,
                    quantity=remaining,
                    source="ledger",
                    carried_cost=_weighted_cost(costs.get(d, [])),
                    closed=after is not None and abs(after) <= QTY_EPSILON,
                )
            )
    return out


def _running_balance(txs: list[Transaction]) -> dict[date, Decimal]:
    """日ごとの、その日の約定をすべて終えたあとの保有数（取引履歴だけで数える）。"""
    q = ZERO
    after: dict[date, Decimal] = {}
    for t in txs:
        step = _quantity_step(t)
        if step is None:
            continue
        mult, delta = step
        q = q * mult + delta
        after[t.trade_date] = q
    return after


def _totals_by_date(
    snaps: Sequence[HoldingSnapshot],
) -> list[tuple[date, Decimal, Decimal | None]]:
    """口座×銘柄の各記録日の (日, 保有数の合計, 平均取得単価)。ロットごとに
    その日以前で最新の記録を足す（ロットごとに記録の日がずれていてもよい）。"""
    lots: dict[int, list[HoldingSnapshot]] = {}
    for s in snaps:
        lots.setdefault(s.lot_seq, []).append(s)
    for members in lots.values():
        members.sort(key=lambda s: s.as_of_date)
    out: list[tuple[date, Decimal, Decimal | None]] = []
    for d in sorted({s.as_of_date for s in snaps}):
        current = []
        for members in lots.values():
            upto = [s for s in members if s.as_of_date <= d]
            if upto:
                current.append(upto[-1])
        total = sum((s.quantity for s in current), ZERO)
        cost = _weighted_cost(
            (s.quantity, s.avg_cost) for s in current if s.quantity > QTY_EPSILON
        )
        out.append((d, total, cost))
    return out


def snapshot_transfer_outs(
    snapshots: Sequence[HoldingSnapshot], transactions: Sequence[Transaction]
) -> list[TransferOut]:
    """スナップショットで保有が 0 になった記録を、移管元の候補として拾う。

    売却でも 0 になるので、これだけでは移管とは言えない。移管先の候補が無ければ
    何もしない（resolve は候補の無いスナップショット由来の記録を問い合わせない）。
    """
    groups: dict[tuple[int, int], list[HoldingSnapshot]] = {}
    for s in snapshots:
        groups.setdefault((s.account_id, s.security_id), []).append(s)
    explained: dict[tuple[int, int], list[date]] = {}
    for t in transactions:
        if t.security_id is not None and t.tx_type in (TxType.SELL, TxType.TRANSFER_OUT):
            explained.setdefault((t.account_id, t.security_id), []).append(t.trade_date)

    out: list[TransferOut] = []
    for (account_id, security_id), snaps in groups.items():
        prev: tuple[date, Decimal, Decimal | None] | None = None
        for d, total, cost in _totals_by_date(snaps):
            if prev is not None and prev[1] > QTY_EPSILON and total <= QTY_EPSILON:
                known = any(
                    prev[0] < day <= d
                    for day in explained.get((account_id, security_id), [])
                )
                if not known:
                    out.append(
                        TransferOut(
                            security_id=security_id,
                            account_id=account_id,
                            date=d,
                            quantity=prev[1],
                            source="snapshot",
                            carried_cost=prev[2],
                            closed=True,
                        )
                    )
            prev = (d, total, cost)
    return out


# ----------------------------------------------------------------------
# 移管先の候補
# ----------------------------------------------------------------------


def _arrival(
    out: TransferOut,
    account_id: int,
    txs: Sequence[Transaction],
    snaps: Sequence[HoldingSnapshot],
) -> Arrival | None:
    """移管元の記録 out に対して、その口座にその銘柄がどう現れたか（無ければ None）。

    txs は口座×銘柄の取引すべて（配当も含む。移管より前から持っていたかの判定に使う）。
    """
    lo = out.date - timedelta(days=EARLY_DAYS)
    hi = out.date + timedelta(days=ARRIVE_DAYS)

    def near(d: date | None) -> bool:
        return d is not None and lo <= d <= hi

    moved = [
        t for t in txs
        if t.tx_type is TxType.TRANSFER_IN and (near(t.trade_date) or near(t.settle_date))
    ]
    moved_keys = {tx_key(t) for t in moved}
    prior = [t for t in txs if t.trade_date < lo and tx_key(t) not in moved_keys]
    prior_snaps = [s for s in snaps if s.as_of_date < lo and s.quantity > QTY_EPSILON]
    if not moved and not any(t.trade_date >= lo for t in txs) and not any(
        s.as_of_date >= lo for s in snaps
    ):
        return None     # 移管より後の記録が無い口座は移管先になりえない

    backdated: list[Transaction] = []
    if prior or prior_snaps:
        if prior_snaps or any(t.tx_type is not TxType.TRANSFER_IN for t in prior):
            first = min([t.trade_date for t in txs] + [s.as_of_date for s in snaps])
            return Arrival(
                out.security_id, account_id, first, first,
                quantity=_quantity_on(first, txs, snaps), avg_cost=None,
                kind="other", early=True,
            )
        backdated = prior

    rows = _ordered([*backdated, *moved])
    if rows:
        return Arrival(
            out.security_id, account_id,
            date=rows[0].trade_date,
            effective=out.date,
            quantity=sum((_abs(t.quantity) for t in rows), ZERO),
            avg_cost=_weighted_cost((_abs(t.quantity), t.unit_price) for t in rows),
            kind="transfer_in",
            backdated=bool(backdated),
            absorbed=tuple(tx_key(t) for t in rows),
        )

    # 入庫の記録は無い: 移管の後の最初の記録
    first_snap = min((s.as_of_date for s in snaps if s.as_of_date >= lo), default=None)
    steps = [t for t in txs if t.trade_date >= lo and _quantity_step(t) is not None]
    first_tx = min((t.trade_date for t in steps), default=None)
    if first_tx is not None and (first_snap is None or first_tx < first_snap):
        day = _ordered([t for t in steps if t.trade_date == first_tx])
        return Arrival(
            out.security_id, account_id, first_tx, first_tx,
            quantity=_running_balance(day).get(first_tx, ZERO), avg_cost=None,
            kind="buy" if any(t.tx_type is TxType.BUY for t in day) else "other",
        )
    if first_snap is None:
        return None
    totals = {d: (q, c) for d, q, c in _totals_by_date(snaps)}
    total, cost = totals[first_snap]
    return Arrival(out.security_id, account_id, first_snap, first_snap, total, cost, "snapshot")


def _quantity_on(day: date, txs: Sequence[Transaction], snaps: Sequence[HoldingSnapshot]) -> Decimal:
    """その日の記録の保有数（移管より前から持っていた口座の表示用）。"""
    on_day = [s for s in snaps if s.as_of_date == day]
    if on_day:
        return {d: q for d, q, _c in _totals_by_date(snaps)}[day]
    return _running_balance(_ordered(t for t in txs if t.trade_date <= day)).get(day, ZERO)


def _cost_match(carried: Decimal | None, avg_cost: Decimal | None) -> bool | None:
    if carried is None or avg_cost is None or carried <= ZERO or avg_cost <= ZERO:
        return None
    return abs(avg_cost - carried) / carried <= COST_TOLERANCE


def candidates(
    out: TransferOut,
    by_account: dict[int, tuple[list[Transaction], list[HoldingSnapshot]]],
    consumed: set[tuple[int, int]] | frozenset = frozenset(),
) -> list[Candidate]:
    """移管元の記録に対する移管先の候補（もっともらしい順）。

    by_account はその銘柄の口座ごとの (取引, スナップショット)。移管より前から持って
    いた口座（early）も、移管で買い増した場合のために手動用として残す。
    """
    res: list[Candidate] = []
    for account_id, (txs, snaps) in by_account.items():
        if account_id == out.account_id or (account_id, out.security_id) in consumed:
            continue
        a = _arrival(out, account_id, txs, snaps)
        if a is None:
            continue
        res.append(
            Candidate(
                arrival=a,
                quantity_match=abs(a.quantity - out.quantity) <= QTY_EPSILON,
                cost_match=_cost_match(out.carried_cost, a.avg_cost),
            )
        )
    res.sort(key=lambda c: (
        not c.quantity_match, c.early, c.cost_match is False, not c.cost_match,
        abs((c.arrival.effective - out.date).days), c.arrival.account_id,
    ))
    return res


def pick_automatically(out: TransferOut, cands: Sequence[Candidate]) -> Candidate | None:
    """自動で結び付けてよい候補（1 つに決まらなければ None）。"""
    pool = [
        c for c in cands
        if c.quantity_match and not c.early and c.cost_match is not False
        and c.arrival.kind in ("snapshot", "transfer_in")
        and (c.arrival.effective - out.date).days <= WINDOW_DAYS
    ]
    if not pool:
        return None
    confirmed = [c for c in pool if c.cost_match]
    pool = confirmed or pool
    first = min(c.arrival.effective for c in pool)
    firsts = [c for c in pool if c.arrival.effective == first]
    return firsts[0] if len(firsts) == 1 else None


# ----------------------------------------------------------------------
# 全体
# ----------------------------------------------------------------------


def resolve(
    transactions: Sequence[Transaction],
    snapshots: Sequence[HoldingSnapshot],
    manual: Iterable[dict[str, Any] | ManualDecision] = (),
) -> Resolution:
    """移管元を日付順に見て、移管先を決める。

    利用者の判断（manual）はその出来事について自動の判定より優先する。結び付けた
    移管先（口座×銘柄）は、以降の移管元の候補から外す — 1 つの現れた記録を 2 つの
    移管で説明しない。
    """
    decisions = [m if isinstance(m, ManualDecision) else ManualDecision.from_row(m)
                 for m in manual]
    by_key = {m.key: m for m in decisions}
    outs = sorted(
        ledger_transfer_outs(transactions) + snapshot_transfer_outs(snapshots, transactions),
        key=lambda o: (o.date, o.security_id, o.account_id),
    )
    groups: dict[int, dict[int, tuple[list[Transaction], list[HoldingSnapshot]]]] = {}
    for t in transactions:
        if t.security_id is not None:
            groups.setdefault(t.security_id, {}).setdefault(t.account_id, ([], []))[0].append(t)
    for sn in snapshots:
        groups.setdefault(sn.security_id, {}).setdefault(sn.account_id, ([], []))[1].append(sn)
    res = Resolution()
    consumed: set[tuple[int, int]] = set()
    seen: set[tuple[int, int, date]] = set()

    for m in decisions:
        if m.to_account_id is not None:
            consumed.add((m.to_account_id, m.security_id))
            # 選んだ口座の入庫のうち、この移管が届いたものとして数えられるもの
            # （数量がちょうど合うときだけ。合わなければどれか決められない）
            txs, snaps = groups.get(m.security_id, {}).get(m.to_account_id, ([], []))
            probe = TransferOut(m.security_id, m.from_account_id, m.date, m.quantity,
                                "manual", None, True)
            arrived = _arrival(probe, m.to_account_id, txs, snaps)
            absorbed = (
                arrived.absorbed
                if arrived is not None and arrived.kind == "transfer_in"
                and abs(arrived.quantity - m.quantity) <= QTY_EPSILON
                else ()
            )
            res.links.append(
                TransferLink(
                    security_id=m.security_id,
                    from_account_id=m.from_account_id,
                    to_account_id=m.to_account_id,
                    date=m.date,
                    quantity=m.quantity,
                    origin="manual",
                    source="manual",
                    manual_id=m.id,
                    absorbed=absorbed,
                )
            )

    for out in outs:
        seen.add(out.key)
        m = by_key.get(out.key)
        by_account = groups.get(out.security_id, {})
        if m is not None:
            # 付け替えの候補には、この判断で結び付けた口座も残す
            taken = consumed - {(m.to_account_id, out.security_id)}
            res.candidates[out.key] = candidates(out, by_account, taken)
            if m.to_account_id is None:
                res.dismissed.append((out, m))
            continue
        cands = candidates(out, by_account, consumed)
        res.candidates[out.key] = cands
        chosen = pick_automatically(out, cands)
        if chosen is not None:
            consumed.add((chosen.arrival.account_id, out.security_id))
            res.links.append(
                TransferLink(
                    security_id=out.security_id,
                    from_account_id=out.account_id,
                    to_account_id=chosen.arrival.account_id,
                    date=out.date,
                    quantity=out.quantity,
                    origin="auto",
                    source=out.source,
                    cost_match=chosen.cost_match,
                    absorbed=chosen.arrival.absorbed,
                )
            )
        elif out.source == "ledger" and (out.closed or any(c.quantity_match for c in cands)):
            res.unresolved.append((out, cands))
        elif out.source == "snapshot" and any(c.quantity_match for c in cands):
            # 0 になっただけなら売却と区別できない。数量の合う現れた記録が
            # 別の口座にあるときだけ問い合わせる
            res.unresolved.append((out, cands))

    for m in decisions:
        if m.to_account_id is None and m.key not in seen:
            res.dismissed.append((None, m))
    return res
