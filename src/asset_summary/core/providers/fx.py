"""Frankfurter（ECB参照レート）プロバイダ（FX・source='fx'）。

- 履歴: GET /v1/{start}..{end}?base={CCY}&symbols=JPY（キー不要・10年分を1コールで可）。
  週末・祝日の欠損はそのまま保存する（forward-fill はクエリ層= portfolio.SeriesLookup）。
  レンジ開始が非営業日だと直前営業日の行が先頭に付くことがある → そのまま保存してよい。
- スポット: GET /v1/latest?base={CCY}&symbols=JPY。
- 1通貨あたりのJPYを返す（source_id=通貨コード, currency='JPY'）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from . import base
from .base import WarnFn

BASE_URL = "https://api.frankfurter.dev/v1"

_THROTTLE_KEY = "fx"


def _noop(_msg: str) -> None:
    pass


def _parse_rates(data: dict, warn: WarnFn) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    for day_str, symbols in ((data or {}).get("rates") or {}).items():
        value = (symbols or {}).get("JPY")
        if value is None:
            continue
        try:
            d = date.fromisoformat(day_str)
            out[d] = Decimal(str(value))
        except (ValueError, InvalidOperation):
            warn(f"fx: レート行を解釈できません: {day_str!r}={value!r}")
    return out


def fetch_history(
    currency: str,
    start: date,
    end: date,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> dict[date, Decimal] | None:
    """[start, end] の日次 {通貨}→JPY レート。失敗時は None（空dictは「営業日なし」）。"""
    w = warn or _noop
    url = f"{BASE_URL}/{start.isoformat()}..{end.isoformat()}"
    resp = base.request(
        "GET",
        url,
        key=_THROTTLE_KEY,
        params={"base": currency, "symbols": "JPY"},
        warn=w,
        client=client,
    )
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        w(f"fx {currency}: レスポンス(JSON)を解釈できません")
        return None
    if not isinstance(data, dict) or "rates" not in data:
        # 200 でも rates が無いレスポンス（エラーメッセージ等）は失敗として扱う。
        # 空 dict を返すと「取得済み・営業日なし」として恒久的に確定してしまう。
        w(f"fx {currency}: レート情報を含まない応答でした")
        return None
    return _parse_rates(data, w)


def fetch_spot(
    currency: str,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> Decimal | None:
    """現在の {通貨}→JPY レート（ECB参照レートの最新値）。"""
    w = warn or _noop
    resp = base.request(
        "GET",
        f"{BASE_URL}/latest",
        key=_THROTTLE_KEY,
        params={"base": currency, "symbols": "JPY"},
        warn=w,
        client=client,
    )
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        w(f"fx {currency}: レスポンス(JSON)を解釈できません")
        return None
    value = ((data or {}).get("rates") or {}).get("JPY")
    if value is None:
        w(f"fx {currency}: JPYレートがレスポンスにありません")
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        w(f"fx {currency}: レートを数値として解釈できません: {value!r}")
        return None
