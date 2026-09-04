"""daily_prices / fetched_ranges / spot_cache の読み書きと範囲被覆ロジック。

設計方針（設計書 §価格データ層）:
- 過去日は恒久キャッシュ。当日は daily_prices に入れない（spot 層のみ）。
- 欠損判定は fetched_ranges の被覆で行う（価格行の有無ではない）。
  「被覆範囲内に価格行が無い日 = 非営業日」として再取得しない。
- 範囲は記録時にマージする（重複・隣接[end+1日=start]は結合）。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Mapping

from .store import Store

DateRange = tuple[date, date]

_ONE_DAY = timedelta(days=1)


# ----------------------------------------------------------------------
# 純関数（テスト先行対象）
# ----------------------------------------------------------------------

def merge_ranges(ranges: Iterable[DateRange]) -> list[DateRange]:
    """重複・隣接（end+1日 == start）の範囲を結合し、開始日順で返す。"""
    out: list[DateRange] = []
    for s, e in sorted(ranges):
        if s > e:
            continue
        if out and s <= out[-1][1] + _ONE_DAY:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def subtract_ranges(
    start: date, end: date, covered: Iterable[DateRange]
) -> list[DateRange]:
    """[start, end] から被覆範囲を除いた欠損範囲を返す。"""
    if start > end:
        return []
    gaps: list[DateRange] = []
    cursor = start
    for s, e in merge_ranges(covered):
        if e < cursor:
            continue
        if s > end:
            break
        if s > cursor:
            gaps.append((cursor, s - _ONE_DAY))
        cursor = e + _ONE_DAY
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def clamp_history_end(end: date, today: date | None = None) -> date:
    """履歴取得の終端を「昨日」以前へクランプ（当日は daily_prices に入れない）。"""
    t = today if today is not None else date.today()
    return min(end, t - _ONE_DAY)


# ----------------------------------------------------------------------
# fetched_ranges
# ----------------------------------------------------------------------

def get_ranges(store: Store, source: str, source_id: str) -> list[DateRange]:
    conn = store.connect()
    try:
        rows = conn.execute(
            """SELECT start_date, end_date FROM fetched_ranges
               WHERE source = ? AND source_id = ? ORDER BY start_date""",
            (source, source_id),
        ).fetchall()
    finally:
        conn.close()
    return [
        (date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
        for r in rows
    ]


def record_range(
    store: Store, source: str, source_id: str, start: date, end: date
) -> None:
    """取得済み範囲を記録し、既存範囲とマージして書き戻す（単一トランザクション）。

    read-modify-write のため BEGIN IMMEDIATE で書き込みロックを先に取る。
    既定の遅延トランザクションだと、同一銘柄を並行して記録したとき
    双方が古いスナップショットを読んでから上書きし、片方の被覆記録
    （最大で数年分）が消えて再ダウンロードが発生する。
    """
    if start > end:
        return
    conn = store.connect()
    try:
        conn.isolation_level = None  # 明示的にトランザクションを制御する
        conn.execute("BEGIN IMMEDIATE")  # busy_timeout=30s でロック待ち
        try:
            rows = conn.execute(
                "SELECT start_date, end_date FROM fetched_ranges WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchall()
            ranges: list[DateRange] = [
                (date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
                for r in rows
            ]
            ranges.append((start, end))
            merged = merge_ranges(ranges)
            conn.execute(
                "DELETE FROM fetched_ranges WHERE source = ? AND source_id = ?",
                (source, source_id),
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                """INSERT INTO fetched_ranges
                   (source, source_id, start_date, end_date, fetched_at)
                   VALUES (?,?,?,?,?)""",
                [
                    (source, source_id, s.isoformat(), e.isoformat(), now)
                    for s, e in merged
                ],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def missing_ranges(
    store: Store, source: str, source_id: str, start: date, end: date
) -> list[DateRange]:
    """[start, end] のうち fetched_ranges に被覆されていない欠損範囲。"""
    return subtract_ranges(start, end, get_ranges(store, source, source_id))


def fetched_today(
    store: Store, source: str, source_id: str, today: date | None = None
) -> bool:
    """この (source, source_id) の範囲記録が今日（ローカル日付）行われたか。

    投信協会CSVの「1銘柄1日1回まで」ガードに使う。
    """
    t = today if today is not None else date.today()
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT fetched_at FROM fetched_ranges WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["fetched_at"])
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.astimezone().date() == t:
            return True
    return False


def record_attempt(store: Store, source: str, source_id: str, ok: bool) -> None:
    """価格取得の試行を記録する（失敗も残す）。"""
    conn = store.connect()
    try:
        with conn:
            conn.execute(
                """INSERT INTO fetch_attempts (source, source_id, attempted_at, ok)
                   VALUES (?,?,?,?)
                   ON CONFLICT(source, source_id) DO UPDATE SET
                     attempted_at = excluded.attempted_at, ok = excluded.ok""",
                (source, source_id, datetime.now(timezone.utc).isoformat(), int(ok)),
            )
    finally:
        conn.close()


def attempted_today(
    store: Store, source: str, source_id: str, only_failed: bool = False,
    today: date | None = None,
) -> bool:
    """この (source, source_id) を今日（ローカル日付）試行済みか。

    only_failed=True なら「今日失敗した」場合のみ True。恒久的なエラー
    （上場廃止・シンボル誤り・空CSV）で毎リクエスト叩き続けるのを防ぐ。
    利用者は「価格キャッシュを更新」（POST /api/refresh-prices）で解除できる。
    """
    t = today if today is not None else date.today()
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT attempted_at, ok FROM fetch_attempts WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    if only_failed and row["ok"]:
        return False
    try:
        dt = datetime.fromisoformat(row["attempted_at"])
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date() == t


def clear_attempts(store: Store) -> None:
    """試行記録を消す（利用者による明示的な再取得のため）。"""
    conn = store.connect()
    try:
        with conn:
            conn.execute("DELETE FROM fetch_attempts")
    finally:
        conn.close()


# ----------------------------------------------------------------------
# daily_prices
# ----------------------------------------------------------------------

def save_daily_prices(
    store: Store,
    source: str,
    source_id: str,
    prices: Mapping[date, Decimal],
    currency: str = "JPY",
) -> None:
    """日次価格の一括upsert（単一トランザクション）。"""
    if not prices:
        return
    conn = store.connect()
    try:
        with conn:
            conn.executemany(
                """INSERT INTO daily_prices (source, source_id, date, price, currency)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source, source_id, date)
                   DO UPDATE SET price = excluded.price, currency = excluded.currency""",
                [
                    (source, source_id, d.isoformat(), str(p), currency)
                    for d, p in sorted(prices.items())
                ],
            )
    finally:
        conn.close()


def latest_price_date(store: Store, source: str, source_id: str) -> date | None:
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
    finally:
        conn.close()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


# ----------------------------------------------------------------------
# spot_cache（TTL 300秒）
# ----------------------------------------------------------------------

def put_spot(
    store: Store,
    source: str,
    source_id: str,
    price: Decimal,
    currency: str = "JPY",
    now: float | None = None,
) -> None:
    ts = time.time() if now is None else now
    conn = store.connect()
    try:
        with conn:
            conn.execute(
                """INSERT INTO spot_cache (source, source_id, price, currency, fetched_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source, source_id) DO UPDATE SET
                     price = excluded.price,
                     currency = excluded.currency,
                     fetched_at = excluded.fetched_at""",
                (source, source_id, str(price), currency, ts),
            )
    finally:
        conn.close()


def get_cached_spot(
    store: Store,
    source: str,
    source_id: str,
    ttl: float = 300.0,
    now: float | None = None,
) -> tuple[Decimal, str] | None:
    """TTL内のスポットキャッシュを返す。期限切れ・未登録は None。"""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT price, currency, fetched_at FROM spot_cache WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    ts = time.time() if now is None else now
    if ts - row["fetched_at"] > ttl:
        return None
    return (Decimal(row["price"]), row["currency"])
