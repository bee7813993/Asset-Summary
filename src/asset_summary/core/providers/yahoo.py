"""Yahoo Finance v8 chart プロバイダ（株式・ETF・先物・source='yahoo'）。

- 【重要】adjclose ではなく indicators.quote[0].close の生値を使う
  （adjclose は配当調整済みで評価額を歪める）。null エントリは skip。
- timestamp[] は meta.exchangeTimezoneName で市場ローカル日付に変換
  （tzdata が無い環境では meta.gmtoffset でフォールバック）。
- 全範囲が上場前で "Data doesn't exist" が返る場合は range=max で再取得し、
  要求範囲全体を被覆済みとして返す（上場前区間を再取得しないため）。
- スポットは同APIの meta.regularMarketPrice（range=1d）。
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from . import base
from .base import HistoryResult, WarnFn

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_THROTTLE_KEY = "yahoo"

_tz_cache: dict[str, Any] = {}


def _noop(_msg: str) -> None:
    pass


def _epoch(d: date) -> int:
    return int(datetime.combine(d, dtime.min, tzinfo=timezone.utc).timestamp())


def _get_tz(name: str):
    if name not in _tz_cache:
        try:
            from zoneinfo import ZoneInfo

            _tz_cache[name] = ZoneInfo(name)
        except Exception:
            _tz_cache[name] = None  # tzdata なし等 → gmtoffset フォールバック
    return _tz_cache[name]


def _to_local_date(ts: int, tzname: str | None, gmtoffset: int | None) -> date:
    tz = _get_tz(tzname) if tzname else None
    if tz is not None:
        return datetime.fromtimestamp(ts, tz).date()
    return datetime.fromtimestamp(ts + (gmtoffset or 0), timezone.utc).date()


def _parse_payload(
    resp: httpx.Response,
) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = resp.json()
    except ValueError:
        return None, "JSONを解釈できません"
    chart = (payload or {}).get("chart") or {}
    results = chart.get("result") or []
    if results:
        return results[0], ""
    err = chart.get("error")
    if isinstance(err, dict):
        desc = err.get("description") or err.get("code") or "不明なエラー"
    else:
        desc = str(err) if err else "空のレスポンス"
    return None, desc


def _extract_prices(
    result: dict[str, Any], start: date, end: date
) -> tuple[dict[date, Decimal], str]:
    meta = result.get("meta") or {}
    currency = meta.get("currency") or "JPY"
    tzname = meta.get("exchangeTimezoneName")
    gmtoffset = meta.get("gmtoffset")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    prices: dict[date, Decimal] = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue  # 休場等の null エントリ
        try:
            value = Decimal(str(close))
        except InvalidOperation:
            continue
        d = _to_local_date(ts, tzname, gmtoffset)
        if start <= d <= end:
            prices[d] = value
    return prices, currency


def fetch_history(
    symbol: str,
    start: date,
    end: date,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> HistoryResult:
    """[start, end]（市場ローカル日付）の日次終値。建値通貨のまま返す。"""
    w = warn or _noop
    url = CHART_URL.format(symbol=symbol)
    params = {
        # 市場TZとUTCのずれを吸収するため前後に余白を取り、ローカル日付で絞り込む
        "period1": _epoch(start - timedelta(days=1)),
        "period2": _epoch(end + timedelta(days=2)),
        "interval": "1d",
    }
    # "Data doesn't exist"（上場前範囲）は HTTP 400 + JSONボディで返るため、
    # 4xx でもボディを解釈する（base.request に None にさせない）
    resp = base.request(
        "GET",
        url,
        key=_THROTTLE_KEY,
        params=params,
        warn=w,
        client=client,
        allow_statuses=(400, 404, 422),
    )
    if resp is None:
        return HistoryResult()  # 通信失敗: 被覆記録せず次回再試行
    result, err = _parse_payload(resp)
    if result is None:
        if "data doesn't exist" in err.lower():
            # 全範囲が上場前 → range=max で全履歴を取得し、要求範囲を被覆確定
            resp2 = base.request(
                "GET",
                url,
                key=_THROTTLE_KEY,
                params={"range": "max", "interval": "1d"},
                warn=w,
                client=client,
                allow_statuses=(400, 404, 422),
            )
            if resp2 is None:
                return HistoryResult()
            result2, err2 = _parse_payload(resp2)
            if result2 is None:
                w(f"yahoo {symbol}: range=max の再取得に失敗しました: {err2}")
                return HistoryResult()
            prices, currency = _extract_prices(result2, start, end)
            return HistoryResult(prices, currency, [(start, end)])
        w(f"yahoo {symbol}: {err}")
        return HistoryResult()
    prices, currency = _extract_prices(result, start, end)
    return HistoryResult(prices, currency, [(start, end)])


def fetch_spot(
    symbol: str,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> tuple[Decimal, str] | None:
    """meta.regularMarketPrice による現在値（建値通貨のまま）。"""
    w = warn or _noop
    resp = base.request(
        "GET",
        CHART_URL.format(symbol=symbol),
        key=_THROTTLE_KEY,
        params={"range": "1d", "interval": "1d"},
        warn=w,
        client=client,
        allow_statuses=(400, 404, 422),
    )
    if resp is None:
        return None
    result, err = _parse_payload(resp)
    if result is None:
        w(f"yahoo {symbol}: {err}")
        return None
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        w(f"yahoo {symbol}: 現在値(regularMarketPrice)がありません")
        return None
    try:
        return (Decimal(str(price)), meta.get("currency") or "JPY")
    except InvalidOperation:
        w(f"yahoo {symbol}: 現在値を数値として解釈できません: {price!r}")
        return None
