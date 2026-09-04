"""Historical price facade.

公開契約（web/app.py が呼ぶ。テストはモンキーパッチする）:

- ensure_price_history(store, securities, start, end, warn=None) -> None
    リンク済み銘柄の日次履歴の欠損範囲のみ取得し daily_prices へ保存。
    fetched_ranges の被覆で欠損判定（価格行の有無ではない）。当日は保存しない。

- ensure_fx_history(store, currencies, start, end, warn=None) -> None
    FX履歴を daily_prices(source='fx', source_id=通貨) へ保存。

- ensure_re_index_history(store, securities, end, warn=None) -> None
    不動産価格指数を daily_prices(source='re_index', source_id='<地域>:<種別>') へ保存。

- search_funds(query, warn=None) -> list[dict]
    投信協会の銘柄検索。[{"name","isin","assoc_cd","ref","category"}]
    ref は price_source_ref にそのまま入る 'ISIN:協会コード' 形式。

実装メモ:
- end は常に「昨日」以前へクランプ（当日は spot 層のみ）。
- (type, ref) 単位で重複排除し、ThreadPoolExecutor(max_workers=5) で銘柄間並列。
  プロバイダ毎のスロットルは providers.base が保証する。
- toushin は全履歴CSVのため「1銘柄1日1回まで」再取得し、取得後は設定来〜昨日を被覆記録。
- metal は yahoo 先物(GC=F/SI=F/PL=F, USD/ozt) と fx USD を先に確保して日次合成。
- 取得失敗時は被覆を記録しない（次回に再試行）。warn はスレッドセーフに直列化。
"""

from __future__ import annotations

import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from . import price_store, re_index
from .models import PriceSourceStatus, Security
from .providers import coingecko
from .providers import fx as fx_provider
from .providers import metal, toushin, yahoo
from .providers import re_index as re_index_provider
from .store import Store

WarnFn = Callable[[str], None]

MAX_WORKERS = 5
# 金属合成でFXを先物日付へforward-fillするための取得マージン（日）
FX_FFILL_MARGIN_DAYS = 7
# Frankfurter は約10年/1コールが上限 → 長期欠損は分割して取得する
FX_MAX_DAYS_PER_CALL = 3650

_ONE_DAY = timedelta(days=1)


def _locked_warn(warn: WarnFn | None) -> WarnFn:
    """スレッドから安全に呼べる warn ラッパ。"""
    if warn is None:
        return lambda _msg: None
    lock = threading.Lock()

    def w(msg: str) -> None:
        with lock:
            warn(msg)

    return w


def _safe(task: Callable[[], None], w: WarnFn) -> None:
    try:
        task()
    except Exception as exc:  # warnings-as-data: 例外は集約して警告に
        w(f"価格履歴の取得中にエラーが発生しました: {exc!r}")


def _run_parallel(tasks: list[Callable[[], None]], w: WarnFn) -> None:
    if not tasks:
        return
    if len(tasks) == 1:
        _safe(tasks[0], w)
        return
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        list(executor.map(lambda t: _safe(t, w), tasks))


# ----------------------------------------------------------------------
# per-source ensure
# ----------------------------------------------------------------------

def _ensure_yahoo(
    store: Store, symbol: str, start: date, end: date, w: WarnFn
) -> None:
    gaps = price_store.missing_ranges(store, "yahoo", symbol, start, end)
    if not gaps:
        return
    # 上場廃止・シンボル誤り・障害で今日すでに失敗しているなら再試行しない
    # （被覆が記録できないため、放置すると毎リクエスト叩き続ける）
    if price_store.attempted_today(store, "yahoo", symbol, only_failed=True):
        return
    progressed = False
    for gap_start, gap_end in gaps:
        result = yahoo.fetch_history(symbol, gap_start, gap_end, warn=w)
        if result.prices:
            price_store.save_daily_prices(
                store, "yahoo", symbol, result.prices, result.currency
            )
        for cov_start, cov_end in result.covered:
            price_store.record_range(store, "yahoo", symbol, cov_start, cov_end)
            progressed = True
    price_store.record_attempt(store, "yahoo", symbol, ok=progressed)


def _ensure_coingecko(
    store: Store, coin_id: str, start: date, end: date, w: WarnFn
) -> None:
    gaps = price_store.missing_ranges(store, "coingecko", coin_id, start, end)
    if not gaps:
        return
    if price_store.attempted_today(store, "coingecko", coin_id, only_failed=True):
        return
    progressed = False
    for gap_start, gap_end in gaps:
        result = coingecko.fetch_history(coin_id, gap_start, gap_end, warn=w)
        if result.prices:
            price_store.save_daily_prices(
                store, "coingecko", coin_id, result.prices, result.currency
            )
        for cov_start, cov_end in result.covered:
            price_store.record_range(store, "coingecko", coin_id, cov_start, cov_end)
            progressed = True
    price_store.record_attempt(store, "coingecko", coin_id, ok=progressed)


def _ensure_toushin(
    store: Store, ref: str, start: date, end: date, w: WarnFn
) -> None:
    if not price_store.missing_ranges(store, "toushin", ref, start, end):
        return
    # 全履歴CSVは1銘柄1日1回まで（T+1公表: 未公表分は明日の再取得で自己修復）。
    # 成功・失敗どちらの試行も数えないと、失敗時に毎リクエストCSVを叩いてしまう。
    if price_store.attempted_today(store, "toushin", ref):
        return
    result = toushin.fetch_history(ref, warn=w)
    price_store.record_attempt(store, "toushin", ref, ok=bool(result.prices))
    if not result.prices:
        return  # 失敗時は被覆を記録しない（翌日に再試行）
    yesterday = date.today() - _ONE_DAY
    rows = {d: p for d, p in result.prices.items() if d <= yesterday}
    if rows:
        price_store.save_daily_prices(store, "toushin", ref, rows, "JPY")
    inception = min(result.prices)
    price_store.record_range(
        store, "toushin", ref, min(start, inception), yesterday
    )


def _ensure_fx_one(
    store: Store, currency: str, start: date, end: date, w: WarnFn
) -> None:
    for gap_start, gap_end in price_store.missing_ranges(
        store, "fx", currency, start, end
    ):
        # Frankfurter の 10年/1コール上限に合わせて欠損範囲を分割
        chunk_start = gap_start
        while chunk_start <= gap_end:
            chunk_end = min(
                gap_end, chunk_start + timedelta(days=FX_MAX_DAYS_PER_CALL - 1)
            )
            rows = fx_provider.fetch_history(currency, chunk_start, chunk_end, warn=w)
            if rows is not None:
                # レンジ開始が非営業日だと直前営業日の行が先頭に付くことがある → そのまま保存
                kept = {d: p for d, p in rows.items() if d <= chunk_end}
                if kept:
                    price_store.save_daily_prices(store, "fx", currency, kept, "JPY")
                price_store.record_range(store, "fx", currency, chunk_start, chunk_end)
            # 失敗チャンクは被覆を記録しない（次回に再試行）
            chunk_start = chunk_end + _ONE_DAY


def _ensure_metal(
    store: Store, source_id: str, start: date, end: date, w: WarnFn
) -> None:
    symbol = metal.FUTURES_SYMBOL.get(source_id)
    if symbol is None:
        w(f"metal: 未知の貴金属IDです: {source_id}（XAU/XAG/XPT のいずれか）")
        return
    gaps = price_store.missing_ranges(store, "metal", source_id, start, end)
    if not gaps:
        return
    span_start = min(g[0] for g in gaps)
    span_end = max(g[1] for g in gaps)
    fx_start = span_start - timedelta(days=FX_FFILL_MARGIN_DAYS)

    # 構成要素（先物USD/ozt・USDJPY）を先に確保
    _ensure_yahoo(store, symbol, span_start, span_end, w)
    _ensure_fx_one(store, "USD", fx_start, span_end, w)

    futures_rows, futures_ccy = store.get_price_rows(
        "yahoo", symbol, span_start.isoformat(), span_end.isoformat()
    )
    fx_rows, _ = store.get_price_rows(
        "fx", "USD", fx_start.isoformat(), span_end.isoformat()
    )
    if futures_ccy and futures_ccy != "USD":
        w(f"metal: {symbol} の建値通貨が {futures_ccy} です（USD想定）")
    futures = {date.fromisoformat(k): v for k, v in futures_rows.items()}
    fx_series = {date.fromisoformat(k): v for k, v in fx_rows.items()}
    composed = metal.compose(futures, fx_series)

    for gap_start, gap_end in gaps:
        rows = {d: p for d, p in composed.items() if gap_start <= d <= gap_end}
        if rows:
            price_store.save_daily_prices(store, "metal", source_id, rows, "JPY")
        # 元データ（先物・FX）の被覆が揃い、範囲内の全先物日を合成できた場合のみ確定
        deps_covered = not price_store.missing_ranges(
            store, "yahoo", symbol, gap_start, gap_end
        ) and not price_store.missing_ranges(store, "fx", "USD", gap_start, gap_end)
        uncomposed = [
            d for d in futures if gap_start <= d <= gap_end and d not in composed
        ]
        if deps_covered and not uncomposed:
            price_store.record_range(store, "metal", source_id, gap_start, gap_end)
        else:
            w(
                f"metal: {source_id} の {gap_start}〜{gap_end} は元データ不足のため"
                f"未確定です（次回に再取得します）"
            )


# ----------------------------------------------------------------------
# public facade
# ----------------------------------------------------------------------

def ensure_price_history(
    store: Store,
    securities: Iterable[Security],
    start: date,
    end: date,
    warn: WarnFn | None = None,
) -> None:
    w = _locked_warn(warn)
    end_clamped = price_store.clamp_history_end(end)
    if end_clamped < start:
        return
    seen: set[tuple[str, str]] = set()
    tasks: list[Callable[[], None]] = []
    for sec in securities:
        if sec.inactive:
            continue  # 上場廃止・償還済みは取得しない（キャッシュ済み履歴は使う）
        if sec.price_source_status != PriceSourceStatus.LINKED:
            continue
        stype = sec.price_source_type.value
        ref = sec.price_source_ref
        if not ref:
            continue
        key = (stype, ref)
        if key in seen:
            continue
        seen.add(key)
        if stype == "yahoo":
            tasks.append(
                functools.partial(_ensure_yahoo, store, ref, start, end_clamped, w)
            )
        elif stype == "toushin":
            tasks.append(
                functools.partial(_ensure_toushin, store, ref, start, end_clamped, w)
            )
        elif stype == "metal":
            tasks.append(
                functools.partial(_ensure_metal, store, ref, start, end_clamped, w)
            )
        elif stype == "fx":
            tasks.append(
                functools.partial(_ensure_fx_one, store, ref, start, end_clamped, w)
            )
        elif stype == "coingecko":
            tasks.append(
                functools.partial(_ensure_coingecko, store, ref, start, end_clamped, w)
            )
        # none / manual はプロバイダ対象外（manual は他モジュール管理）
    _run_parallel(tasks, w)


# 指数が「古すぎる」と見なす日数。公表は3ヶ月遅れが通常なので、月次の1周期分だけ
# 空けて判定する。遅れている間は1日1回だけ取りに行く（下の attempted_today）。
_RE_INDEX_STALE_DAYS = 35

# 取得の間引きに使う番兵キー。1ファイルのダウンロードで全系列が入るため、
# 試行の記録は系列ごとではなくワークブック単位で行う。
_RE_INDEX_WORKBOOK = "_workbook"


def ensure_re_index_history(
    store: Store,
    securities: Iterable[Security],
    end: date,
    warn: WarnFn | None = None,
) -> None:
    """不動産価格指数を確保する。指数を紐付けた銘柄が1つも無ければ何もしない。

    ensure_price_history の LINKED-only ループには入れない。あちらは「銘柄ごとに
    1リクエスト」の構造だが、指数は1ファイルに全16地域×4種別が入っており、
    銘柄別の取得ではないため。

    再取得は **1日1回まで**、かつ手元の最新月が古い時だけ。公表が止まっている間に
    毎リクエスト MLIT を叩かないための番人（成功・失敗どちらの試行も数える）。
    取得できたら全期間を upsert し直す —— 再開時に過去の値が改訂されうるため
    （公表停止の理由が「算出プログラムの不具合」なので、差分追記では追随できない）。
    """
    w = _locked_warn(warn)
    refs = {
        ref
        for sec in securities
        if (ref := re_index.parse_ref(sec.price_source_ref)) is not None
    }
    if not refs:
        return

    end_clamped = price_store.clamp_history_end(end)
    if all(_re_index_fresh(store, ref, end_clamped) for ref in refs):
        return
    if price_store.attempted_today(store, "re_index", _RE_INDEX_WORKBOOK):
        return

    series = re_index_provider.fetch_all(warn=w)
    price_store.record_attempt(store, "re_index", _RE_INDEX_WORKBOOK, ok=bool(series))
    if not series:
        return  # 失敗時は被覆を記録しない（翌日に再試行）

    for source_id, rows in series.items():
        if not rows:
            continue
        price_store.save_daily_prices(store, "re_index", source_id, rows, "JPY")
        # 行が無くても end まで被覆を記録する。月次かつ公表が数ヶ月遅れるため、
        # 最終行までしか記録しないと末尾が恒久的に「欠損」に見えてしまう。
        price_store.record_range(store, "re_index", source_id, min(rows), end_clamped)


def _re_index_fresh(store: Store, source_id: str, end: date) -> bool:
    latest = price_store.latest_price_date(store, "re_index", source_id)
    return latest is not None and (end - latest).days <= _RE_INDEX_STALE_DAYS


def ensure_fx_history(
    store: Store,
    currencies: Iterable[str],
    start: date,
    end: date,
    warn: WarnFn | None = None,
) -> None:
    w = _locked_warn(warn)
    end_clamped = price_store.clamp_history_end(end)
    if end_clamped < start:
        return
    tasks = [
        functools.partial(_ensure_fx_one, store, ccy, start, end_clamped, w)
        for ccy in dict.fromkeys(currencies)
        if ccy and ccy != "JPY"
    ]
    _run_parallel(tasks, w)


def search_coins(query: str, warn: WarnFn | None = None) -> list[dict[str, Any]]:
    """暗号資産の銘柄検索（CoinGecko）。[{"name","symbol","ref"}]。"""
    return coingecko.search(query, warn=warn)


def search_funds(query: str, warn: WarnFn | None = None) -> list[dict[str, Any]]:
    return toushin.search(query, warn=warn)
