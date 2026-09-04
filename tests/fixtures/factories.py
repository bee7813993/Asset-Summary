"""matching / service テスト用の ParsedHolding / ParseResult ファクトリ（架空データ）。"""

from __future__ import annotations

from decimal import Decimal

from asset_summary.importers.base import (
    ParsedHolding,
    ParseReport,
    ParseResult,
    SectionReport,
    SectionType,
)
from asset_summary.importers.mf_pdf import SECTION_LABELS


def _d(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def deposit(label: str, balance, inst: str = "架空銀行", **kw) -> ParsedHolding:
    return ParsedHolding(
        section=SectionType.DEPOSIT,
        institution=inst,
        name_raw=label,
        value_jpy=_d(balance),
        **kw,
    )


def stock(
    name: str, code: str | None, qty, avg, price, value, pl=None,
    inst: str = "架空証券", **kw,
) -> ParsedHolding:
    return ParsedHolding(
        section=SectionType.STOCK,
        institution=inst,
        name_raw=name,
        code_raw=code,
        quantity=_d(qty),
        avg_cost=_d(avg),
        price=_d(price),
        value_jpy=_d(value),
        pl_jpy=_d(pl),
        **kw,
    )


def fund(name: str, qty, avg, price, value, pl=None, inst: str = "架空証券", **kw) -> ParsedHolding:
    return ParsedHolding(
        section=SectionType.FUND,
        institution=inst,
        name_raw=name,
        quantity=_d(qty),
        avg_cost=_d(avg),
        price=_d(price),
        value_jpy=_d(value),
        pl_jpy=_d(pl),
        **kw,
    )


def crypto_row(label: str, balance, inst: str = "架空コイン", **kw) -> ParsedHolding:
    return ParsedHolding(
        section=SectionType.CRYPTO,
        institution=inst,
        name_raw=label,
        value_jpy=_d(balance),
        **kw,
    )


def pension(name: str, acq, value, pl=None, **kw) -> ParsedHolding:
    return ParsedHolding(
        section=SectionType.PENSION,
        institution="年金",
        name_raw=name,
        avg_cost=_d(acq),
        value_jpy=_d(value),
        pl_jpy=_d(pl),
        **kw,
    )


def point(
    name: str, qty, value, ptype: str = "ポイント", rate: str = "1.00",
    expiry: str | None = None, inst: str = "架空カード", **kw,
) -> ParsedHolding:
    meta = {"point_type": ptype, "rate": rate}
    if expiry:
        meta["expiry"] = expiry
    return ParsedHolding(
        section=SectionType.POINT,
        institution=inst,
        name_raw=name,
        quantity=_d(qty),
        value_jpy=_d(value),
        meta=meta,
        **kw,
    )


def make_result(*holdings: ParsedHolding) -> ParseResult:
    """holdings からセクション集計つきの ParseResult を合成する。"""
    reports: dict[SectionType, SectionReport] = {}
    for h in holdings:
        rep = reports.setdefault(
            h.section,
            SectionReport(name=SECTION_LABELS.get(h.section, h.section.value), section=h.section),
        )
        rep.row_count += 1
        if h.value_jpy is not None:
            rep.computed_total = (rep.computed_total or Decimal("0")) + h.value_jpy
    return ParseResult(
        source_kind="mf_pdf",
        holdings=list(holdings),
        report=ParseReport(sections=list(reports.values())),
    )
