"""タグ配分と Myポートフォリオの集計。

タグは「1銘柄あたり合計100%」の配分率を持つ。オルカンのように複数の資産を
含む投信を「世界株 95% / 国内株式 5%」のように按分できるため、タグ別の
集計が総額と一致する（重複計上・取りこぼしが起きない）。合計が100%に満た
ない残りは「未分類」として扱う。

Myポートフォリオの構成:
- タグ条件（OR）… 一致したタグの配分率ぶんだけ計上する（按分計上）
- 個別追加 … 全額計上（タグ条件より優先）
- 個別除外 … 常に除外（最優先）

この定義により、タグ [世界株, 国内株式] のポートフォリオではオルカンが
95%+5%=100% 計上され、タグ [国内株式] のみなら5%分だけが計上される。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

ZERO = Decimal("0")
HUNDRED = Decimal("100")

UNTAGGED_ID = 0
UNTAGGED_NAME = "未分類"
UNTAGGED_COLOR = "#6e7681"


def portfolio_ratio(
    security_id: int,
    tag_weights: dict[int, Decimal],
    portfolio: dict[str, Any] | None,
) -> Decimal:
    """銘柄がポートフォリオに計上される割合（0〜1）。"""
    if portfolio is None:
        return Decimal("1")
    if security_id in set(portfolio.get("exclude_security_ids") or []):
        return ZERO
    if security_id in set(portfolio.get("include_security_ids") or []):
        return Decimal("1")
    tag_ids = set(portfolio.get("tag_ids") or [])
    if not tag_ids:
        return ZERO
    matched = sum(
        (w for tid, w in tag_weights.items() if tid in tag_ids), ZERO
    )
    if matched <= ZERO:
        return ZERO
    return min(matched / HUNDRED, Decimal("1"))


def _tag_contributions(
    security_id: int,
    value: Decimal,
    tag_weights: dict[int, Decimal],
    portfolio: dict[str, Any] | None,
) -> list[tuple[int, Decimal]]:
    """(tag_id, 金額) のリスト。合計は value × portfolio_ratio に一致する。

    未分類ぶんは tag_id=UNTAGGED_ID で返す。金額の代わりに前日差を渡せば
    そのまま「タグ別の前日比」の按分にも使える（配分率は同じため）。
    """
    if portfolio is not None:
        if security_id in set(portfolio.get("exclude_security_ids") or []):
            return []
        explicit = security_id in set(portfolio.get("include_security_ids") or [])
        tag_ids = set(portfolio.get("tag_ids") or [])
    else:
        explicit = True
        tag_ids = None  # 全タグ対象

    out: list[tuple[int, Decimal]] = []
    used = ZERO
    for tid, weight in tag_weights.items():
        if not explicit and tag_ids is not None and tid not in tag_ids:
            continue
        w = min(weight, HUNDRED)
        if w <= ZERO:
            continue
        out.append((tid, value * w / HUNDRED))
        used += w
    if explicit and used < HUNDRED:
        # 配分されていない残り（タグ未設定を含む）
        out.append((UNTAGGED_ID, value * (HUNDRED - used) / HUNDRED))
    return out


def summarize_by_tag(
    holdings: Iterable[dict[str, Any]],
    tag_map: dict[int, dict[int, Decimal]],
    tags: list[dict[str, Any]],
    portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """タグ別の按分集計。

    holdings: portfolio.summarize() が返す保有行（value/cost/costed_value を持つ）
    tag_map:  security_id → {tag_id: 配分率%}
    返値: {"tags": [{tag_id,name,color,value,cost,pl,pl_pct,weight,holding_count}],
           "total_value": Decimal, "total_cost", "total_pl", "holdings": [...]}
    """
    tag_info = {t["id"]: t for t in tags}
    buckets: dict[int, dict[str, Any]] = {}
    total_value = ZERO
    total_cost = ZERO
    total_costed_value = ZERO
    has_cost = False
    # 前日比も計上率ぶんだけ按分する。前日値が判らない銘柄（CS連携のコイン等）が
    # 混ざったら partial を立て、UI 側で「一部を除く」と断れるようにする。
    total_day_change = ZERO
    total_prev_value = ZERO
    has_day_change = False
    day_change_partial = False
    member_rows: list[dict[str, Any]] = []
    # 配分が100%に満たない銘柄（銘柄単位で集約: 同一銘柄が複数口座にある場合を合算）
    unalloc: dict[int, dict[str, Any]] = {}

    for h in holdings:
        if not h.get("in_total", True):
            continue
        sec_id = h["id"]
        weights = tag_map.get(sec_id, {})
        ratio = portfolio_ratio(sec_id, weights, portfolio)
        if ratio <= ZERO:
            continue
        value = h.get("value") or ZERO
        parts = _tag_contributions(sec_id, value, weights, portfolio)
        if not parts:
            continue
        for tid, amount in parts:
            b = buckets.setdefault(
                tid,
                {"value": ZERO, "cost": ZERO, "costed_value": ZERO,
                 "has_cost": False, "holding_count": 0,
                 "prev_value": ZERO, "day_change": ZERO,
                 "has_day_change": False, "day_change_partial": False},
            )
            b["value"] += amount
            b["holding_count"] += 1
        # 前日比も同じ配分率で割り振る（評価額と同じ按分なので合計が一致する）
        if h.get("day_change") is not None and h.get("prev_value") is not None:
            prev_parts = dict(
                _tag_contributions(sec_id, h["prev_value"], weights, portfolio)
            )
            for tid, amount in _tag_contributions(
                sec_id, h["day_change"], weights, portfolio
            ):
                b = buckets[tid]
                b["day_change"] += amount
                b["prev_value"] += prev_parts.get(tid, ZERO)
                b["has_day_change"] = True
        elif h.get("value") is not None:
            for tid, _amount in parts:
                buckets[tid]["day_change_partial"] = True
        # 原価・損益はポートフォリオ全体でのみ按分（タグ単位の原価は
        # 配分率で割り振っても意味が薄いため、タグ別は評価額のみ扱う）
        total_value += value * ratio
        if h.get("day_change") is not None and h.get("prev_value") is not None:
            total_day_change += h["day_change"] * ratio
            total_prev_value += h["prev_value"] * ratio
            has_day_change = True
        elif h.get("value") is not None:
            day_change_partial = True
        if h.get("cost") is not None and h.get("costed_value") is not None:
            total_cost += h["cost"] * ratio
            total_costed_value += h["costed_value"] * ratio
            has_cost = True
        allocated = sum((min(w, HUNDRED) for w in weights.values()), ZERO)
        if allocated < HUNDRED:
            u = unalloc.setdefault(
                sec_id,
                {
                    "security_id": sec_id,
                    "name": h.get("name", ""),
                    "asset_class": h.get("asset_class"),
                    "allocated_pct": allocated,
                    "value": ZERO,
                    "unallocated_value": ZERO,
                },
            )
            u["value"] += value
            u["unallocated_value"] += value * (HUNDRED - allocated) / HUNDRED

        row = dict(h)
        row["portfolio_ratio"] = ratio
        row["portfolio_value"] = value * ratio
        row["tags"] = [
            {
                "id": tid,
                "name": tag_info.get(tid, {}).get("name", UNTAGGED_NAME),
                "weight": weights.get(tid, ZERO),
            }
            for tid in weights
        ]
        member_rows.append(row)

    rows = []
    for tid, b in buckets.items():
        info = tag_info.get(tid)
        rows.append(
            {
                "tag_id": tid,
                "name": info["name"] if info else UNTAGGED_NAME,
                "color": (info.get("color") if info else None) or UNTAGGED_COLOR,
                "value": b["value"],
                "holding_count": b["holding_count"],
                "day_change": b["day_change"] if b["has_day_change"] else None,
                "day_change_pct": (
                    (b["day_change"] / b["prev_value"] * HUNDRED)
                    if (b["has_day_change"] and b["prev_value"])
                    else None
                ),
                "day_change_partial": b["day_change_partial"] and b["has_day_change"],
                "weight": (b["value"] / total_value * HUNDRED) if total_value else None,
            }
        )
    rows.sort(key=lambda r: (r["tag_id"] == UNTAGGED_ID, -(r["value"] or ZERO)))

    total_pl = (total_costed_value - total_cost) if has_cost else None
    day_change = total_day_change if has_day_change else None
    return {
        "tags": rows,
        "total_day_change": day_change,
        "total_day_change_pct": (
            (day_change / total_prev_value * HUNDRED)
            if (day_change is not None and total_prev_value)
            else None
        ),
        "day_change_partial": day_change_partial and has_day_change,
        "total_value": total_value,
        "total_cost": total_cost if has_cost else None,
        "total_pl": total_pl,
        "total_pl_pct": (
            (total_pl / total_cost * HUNDRED) if (total_pl is not None and total_cost) else None
        ),
        "holdings": sorted(
            member_rows, key=lambda r: -(r["portfolio_value"] or ZERO)
        ),
        "unallocated": sorted(
            unalloc.values(), key=lambda u: -(u["unallocated_value"] or ZERO)
        ),
    }


def group_by(
    member_rows: Iterable[dict[str, Any]], key: str, label_of=None
) -> list[dict[str, Any]]:
    """ポートフォリオ計上額を任意のキーで集計（通貨別・クラス別・口座別）。"""
    buckets: dict[Any, dict[str, Any]] = {}
    total = ZERO
    for h in member_rows:
        k = h.get(key)
        amount = h.get("portfolio_value") or ZERO
        ratio = h.get("portfolio_ratio") or ZERO
        b = buckets.setdefault(
            k,
            {"value": ZERO, "holding_count": 0, "prev_value": ZERO,
             "day_change": ZERO, "has_day_change": False},
        )
        b["value"] += amount
        b["holding_count"] += 1
        total += amount
        # 前日比も計上率ぶんだけ（評価額と同じ按分なので内訳の合計が一致する）
        if h.get("day_change") is not None and h.get("prev_value") is not None:
            b["day_change"] += h["day_change"] * ratio
            b["prev_value"] += h["prev_value"] * ratio
            b["has_day_change"] = True
    rows = []
    for k, b in buckets.items():
        rows.append(
            {
                "key": k,
                "label": label_of(k) if label_of else str(k),
                "value": b["value"],
                "holding_count": b["holding_count"],
                "day_change": b["day_change"] if b["has_day_change"] else None,
                "day_change_pct": (
                    (b["day_change"] / b["prev_value"] * HUNDRED)
                    if (b["has_day_change"] and b["prev_value"])
                    else None
                ),
                "weight": (b["value"] / total * HUNDRED) if total else None,
            }
        )
    rows.sort(key=lambda r: -(r["value"] or ZERO))
    return rows
