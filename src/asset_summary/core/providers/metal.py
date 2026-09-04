"""貴金属の合成プロバイダ（source='metal'、JPY/グラム）。

合成式（日次・スポット共通）:
    先物終値(USD/トロイオンス) × USDJPY ÷ 31.1034768 = JPY/グラム

- source_id: 'XAU'(金=GC=F) / 'XAG'(銀=SI=F) / 'XPT'(プラチナ=PL=F)
- 日次合成では FX を金属（先物）側の日付へ forward-fill する。
- 先物系列・FX系列の確保（yahoo/fx 経由）と保存はファサード
  （core.price_history / core.prices）が行い、本モジュールは純粋な合成のみ担当。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from decimal import Decimal

FUTURES_SYMBOL: dict[str, str] = {"XAU": "GC=F", "XAG": "SI=F", "XPT": "PL=F"}

GRAMS_PER_TROY_OUNCE = Decimal("31.1034768")


def compose(
    futures: dict[date, Decimal], fx: dict[date, Decimal]
) -> dict[date, Decimal]:
    """日次合成。FXは先物日付へ forward-fill（FX系列開始前の先物日はskip）。"""
    if not futures or not fx:
        return {}
    fx_dates = sorted(fx)
    out: dict[date, Decimal] = {}
    for d in sorted(futures):
        i = bisect_right(fx_dates, d)
        if i == 0:
            continue  # FX開始前は forward-fill 不能（backfill はしない）
        rate = fx[fx_dates[i - 1]]
        out[d] = futures[d] * rate / GRAMS_PER_TROY_OUNCE
    return out


def spot_from_components(futures_usd_per_ozt: Decimal, usdjpy: Decimal) -> Decimal:
    """先物スポット(USD/ozt) × USDJPYスポット → JPY/グラム。"""
    return futures_usd_per_ozt * usdjpy / GRAMS_PER_TROY_OUNCE
