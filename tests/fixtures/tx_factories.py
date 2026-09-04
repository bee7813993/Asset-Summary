"""cost_basis テスト用の Transaction ファクトリ（架空データ）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from asset_summary.core.models import Transaction, TxType

_SEQ = {"n": 0}


def _d(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _next_id() -> int:
    _SEQ["n"] += 1
    return _SEQ["n"]


def _tx(day: str, kind: TxType, **kw) -> Transaction:
    return Transaction(
        id=_next_id(),
        account_id=kw.pop("account_id", 1),
        security_id=kw.pop("security_id", 10),
        trade_date=date.fromisoformat(day),
        tx_type=kind,
        **kw,
    )


def buy(day: str, qty, price, fee=0, **kw) -> Transaction:
    """買付。約定代金は数量×単価（divisor は呼び出し側の錨に合わせる）。"""
    gross = kw.pop("gross", None)
    if gross is None:
        gross = Decimal(str(qty)) * Decimal(str(price)) / Decimal(kw.pop("divisor", 1))
    return _tx(
        day, TxType.BUY,
        quantity=_d(qty), unit_price=_d(price),
        gross_amount=_d(gross), fee=_d(fee),
        net_amount=-(Decimal(str(gross)) + Decimal(str(fee))),
        **kw,
    )


def sell(day: str, qty, price, fee=0, tax=0, **kw) -> Transaction:
    gross = kw.pop("gross", None)
    if gross is None:
        gross = Decimal(str(qty)) * Decimal(str(price)) / Decimal(kw.pop("divisor", 1))
    return _tx(
        day, TxType.SELL,
        quantity=-_d(qty), unit_price=_d(price),
        gross_amount=_d(gross), fee=_d(fee), tax=_d(tax),
        net_amount=Decimal(str(gross)) - Decimal(str(fee)) - Decimal(str(tax)),
        **kw,
    )


def reinvest(day: str, qty, price, **kw) -> Transaction:
    gross = Decimal(str(qty)) * Decimal(str(price)) / Decimal(kw.pop("divisor", 1))
    return _tx(
        day, TxType.REINVEST,
        quantity=_d(qty), unit_price=_d(price),
        gross_amount=gross, net_amount=-gross, **kw,
    )


def dividend(day: str, amount, tax=0, **kw) -> Transaction:
    return _tx(day, TxType.DIVIDEND, net_amount=_d(amount), tax=_d(tax), **kw)


def roc(day: str, amount, **kw) -> Transaction:
    """特別分配金（元本払戻金）。数量は動かさず取得原価だけ減る。"""
    return _tx(day, TxType.RETURN_OF_CAPITAL, net_amount=_d(amount), **kw)


def split(day: str, ratio=None, delta=None, **kw) -> Transaction:
    return _tx(day, TxType.SPLIT, split_ratio=_d(ratio), quantity=_d(delta), **kw)


def transfer_in(day: str, qty, price=None, **kw) -> Transaction:
    gross = None if price is None else Decimal(str(qty)) * Decimal(str(price))
    return _tx(
        day, TxType.TRANSFER_IN,
        quantity=_d(qty), unit_price=_d(price), gross_amount=gross, **kw,
    )
