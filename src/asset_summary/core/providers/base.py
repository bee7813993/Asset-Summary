"""価格プロバイダ共通基盤: Protocol・スロットル・バックオフ・共有HTTPクライアント。

- ネットワークはすべて sync httpx。テストは set_client() で
  httpx.Client(transport=httpx.MockTransport(handler)) を注入する。
- スロットルはプロバイダキー単位（yahoo 1.5s / toushin 1.0s / fx 0.5s）。
  429 は 3s/6s バックオフ後、warn して諦める（warnings-as-data、例外にしない）。
- テストは本モジュールの ``_sleep`` をモンキーパッチして待ち時間を無効化できる。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Protocol

import httpx

WarnFn = Callable[[str], None]

DEFAULT_HEADERS = {
    # Yahoo v8 chart はブラウザ風 User-Agent が必須
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}

# プロバイダキー → リクエスト間隔（秒）
#
# yahoo は1銘柄1リクエストのため、間隔がそのままダッシュボード初回表示の
# 待ち時間になる（20銘柄 × 1.5秒 ≒ 30秒）。実使用は「スポット20件を
# spot_cache TTL(300秒)ごとに1バースト」＋「履歴は恒久キャッシュで初回のみ」
# であり平均数リクエスト/分にすぎないため、0.3秒間隔（バースト時 約3req/秒）
# とする。レート制限の安全網は 429 バックオフが担う。
THROTTLE_SECONDS: dict[str, float] = {
    "yahoo": 0.3, "toushin": 1.0, "fx": 0.5,
    # CoinGecko 無料枠は分あたりの上限が厳しいため長めに空ける
    "coingecko": 2.0,
    # 不動産価格指数は月1更新の860KB。1日1回しか引かないので厚めに空ける
    "re_index": 5.0,
}
# 429 時のバックオフ（この回数を超えたら warn して諦める）
BACKOFF_SECONDS: tuple[float, ...] = (3.0, 6.0)

REQUEST_TIMEOUT = 30.0

# テストがモンキーパッチする待機関数（time.sleep の別名）
_sleep = time.sleep

_registry_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_last_request: dict[str, float] = {}

_client: httpx.Client | None = None
_client_lock = threading.Lock()


@dataclass
class HistoryResult:
    """履歴取得の結果。

    prices   : 日付 → 建値通貨のままの終値/基準価額
    currency : 建値通貨（レスポンスの meta 等から）
    covered  : fetched_ranges に記録してよい被覆範囲（取得失敗時は空 = 次回再試行）
    reachable: 提供元へ届いたか。False は「取得できなかった」で、届いたうえで
               データが無い（prices が空）状態と区別する
    """

    prices: dict[date, Decimal] = field(default_factory=dict)
    currency: str = "JPY"
    covered: list[tuple[date, date]] = field(default_factory=list)
    reachable: bool = True


class PriceProvider(Protocol):
    """プロバイダモジュールが満たす形（モジュール関数ベースで実装）。"""

    def fetch_spot(
        self, source_id: str, warn: WarnFn | None = None
    ) -> tuple[Decimal, str] | None: ...

    def fetch_history(
        self, source_id: str, start: date, end: date, warn: WarnFn | None = None
    ) -> HistoryResult: ...


def get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True
            )
        return _client


def set_client(client: httpx.Client | None) -> None:
    """テスト用: 共有クライアントを差し替える（None でリセット）。"""
    global _client
    with _client_lock:
        _client = client


def reset_throttle() -> None:
    """テスト用: スロットルの最終リクエスト時刻を初期化する。"""
    with _registry_lock:
        _last_request.clear()


def _key_lock(key: str) -> threading.Lock:
    with _registry_lock:
        return _key_locks.setdefault(key, threading.Lock())


def throttle(key: str) -> None:
    """同一プロバイダの連続リクエストを最小間隔に抑える（スレッド間で直列化）。"""
    interval = THROTTLE_SECONDS.get(key, 0.0)
    lock = _key_lock(key)
    with lock:
        now = time.monotonic()
        last = _last_request.get(key)
        if last is not None and interval > 0:
            wait = last + interval - now
            if wait > 0:
                _sleep(wait)
        _last_request[key] = time.monotonic()


def request(
    method: str,
    url: str,
    *,
    key: str,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
    allow_statuses: tuple[int, ...] = (),
    **kwargs: Any,
) -> httpx.Response | None:
    """スロットル + 429バックオフ付きリクエスト。失敗は warn して None を返す。

    allow_statuses に含めた 4xx/5xx はエラー扱いせずレスポンスを返す
    （例: Yahoo は上場前範囲の "Data doesn't exist" を HTTP 400 + JSONボディで
    返すため、呼び出し側がボディを解釈する必要がある）。
    """
    w = warn or (lambda _msg: None)
    cl = client or get_client()
    attempts = len(BACKOFF_SECONDS) + 1
    for i in range(attempts):
        throttle(key)
        try:
            resp = cl.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            # httpx.InvalidURL は HTTPError のサブクラスではないため、
            # 個別に列挙せず広く捕捉する（warnings-as-data: 例外を外に出さない）
            w(f"{key}: リクエストに失敗しました ({url}): {exc}")
            return None
        if resp.status_code == 429:
            if i < len(BACKOFF_SECONDS):
                _sleep(BACKOFF_SECONDS[i])
                continue
            w(f"{key}: レート制限(429)が解消しないため諦めました ({url})")
            return None
        if resp.status_code >= 400 and resp.status_code not in allow_statuses:
            w(f"{key}: HTTP {resp.status_code} ({url})")
            return None
        return resp
    return None
