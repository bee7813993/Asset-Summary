"""CoinGecko プロバイダ（暗号資産・source='coingecko'）。

- スポット: GET /api/v3/simple/price?ids=...&vs_currencies=jpy
- 履歴:     GET /api/v3/coins/{id}/market_chart/range?vs_currency=jpy&from=&to=
            （intraday の系列が返るので UTC 日次の終値へ畳む）

price_source_ref は CoinGecko のコインID（'bitcoin' 等）。シンボル（BTC）は
重複するためIDを正とする。無料枠は分あたりのリクエスト数が絞られるので、
スロットルは長めにし、履歴は欠損範囲のみ取得する（他プロバイダと同じ）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from . import base
from .base import HistoryResult, WarnFn

BASE_URL = "https://api.coingecko.com/api/v3"

_THROTTLE_KEY = "coingecko"

# よく使う銘柄の シンボル → CoinGecko ID。UIの候補に出すだけで、
# ここに無いものは検索APIで引ける。
KNOWN_COINS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "XRP": "ripple",
    "SOL": "solana",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "LINK": "chainlink",
    "XLM": "stellar",
    "ATOM": "cosmos",
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI": "dai",
    "BNB": "binancecoin",
}


def _noop(_msg: str) -> None:
    pass


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def search(
    query: str,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """銘柄検索。[{"name","symbol","ref"}]（ref は CoinGecko ID）。"""
    w = warn or _noop
    resp = base.request(
        "GET", f"{BASE_URL}/search", key=_THROTTLE_KEY,
        params={"query": query}, warn=w, client=client,
    )
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError:
        w("coingecko: 検索レスポンス(JSON)を解釈できません")
        return []
    out: list[dict[str, Any]] = []
    for c in (data or {}).get("coins") or []:
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        out.append(
            {
                "name": str(c.get("name") or cid),
                "symbol": str(c.get("symbol") or "").upper(),
                "ref": cid,
                "rank": c.get("market_cap_rank"),
            }
        )
    return out


def fetch_spot(
    coin_id: str,
    currency: str = "jpy",
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> tuple[Decimal, str] | None:
    w = warn or _noop
    resp = base.request(
        "GET", f"{BASE_URL}/simple/price", key=_THROTTLE_KEY,
        params={"ids": coin_id, "vs_currencies": currency},
        warn=w, client=client,
    )
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        w(f"coingecko {coin_id}: レスポンス(JSON)を解釈できません")
        return None
    price = _to_decimal(((data or {}).get(coin_id) or {}).get(currency))
    if price is None:
        w(f"coingecko {coin_id}: 価格が取得できませんでした（コインIDを確認してください）")
        return None
    return (price, currency.upper())


def _bucket_daily(points: list[list[Any]]) -> dict[date, Decimal]:
    """[[epoch_ms, price], ...] を UTC日次の終値（その日の最後の値）へ畳む。"""
    out: dict[date, Decimal] = {}
    for item in points or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            ts = datetime.fromtimestamp(float(item[0]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        price = _to_decimal(item[1])
        if price is None:
            continue
        out[ts.date()] = price   # 同日は後の値で上書き＝その日の最後の値
    return out


def fetch_history(
    coin_id: str,
    start: date,
    end: date,
    currency: str = "jpy",
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> HistoryResult:
    w = warn or _noop
    # 端の日が落ちないよう前後に余裕を持たせる
    frm = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()) - 86400
    to = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    resp = base.request(
        "GET", f"{BASE_URL}/coins/{coin_id}/market_chart/range", key=_THROTTLE_KEY,
        params={"vs_currency": currency, "from": frm, "to": to},
        warn=w, client=client,
    )
    if resp is None:
        return HistoryResult()
    try:
        data = resp.json()
    except ValueError:
        w(f"coingecko {coin_id}: 履歴レスポンス(JSON)を解釈できません")
        return HistoryResult()
    prices = _bucket_daily((data or {}).get("prices") or [])
    if not prices:
        w(f"coingecko {coin_id}: 履歴に価格行がありませんでした")
        return HistoryResult()
    prices = {d: p for d, p in prices.items() if start <= d <= end}
    return HistoryResult(prices, currency.upper(), [(start, end)] if prices else [])
