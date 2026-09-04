"""Spot price facade.

公開契約（web/app.py・cli.py が呼ぶ。テストはこの関数をモンキーパッチする）:

- fetch_spot(store, securities, warn=None) -> dict[int, Decimal]
    security_id → 建値通貨・price_unit_divisor適用前の現在単価。
    spot_cache（TTL 300秒）を利用し、price_source_status='linked' の銘柄のみ取得。
    失敗は warn(str) に流して例外にしない（warnings-as-data）。

- fetch_fx_rates(store, currencies, warn=None) -> dict[str, Decimal]
    通貨コード → JPYレート（現在値）。'JPY' は常に 1。

- fetch_prev_close(store, securities, warn=None) -> (dict[int, Decimal], dict[int, str])
    前日比の基準となる「1つ前の終値」と、その基準日。daily_prices を読むだけで
    ネットワークは使わない（履歴が無ければその銘柄は結果に載らない＝前日比なし）。

- fetch_prev_fx_rates(store, currencies, warn=None) -> dict[str, Decimal]
    上と同じ基準の FX レート。外貨建て資産の前日比に為替変動を含めるために使う。

実装メモ:
- (price_source_type, price_source_ref) 単位で重複排除し、spot_cache を共有。
- toushin のスポットは日次系列（daily_prices）の最新値。ネットワークは叩かない
  （最新1件だけを引く。全期間を読むと投信1銘柄で数千行になり律速する）。
- metal のスポットは yahoo 先物スポット × fx USDスポット ÷ 31.1034768 の合成
  （構成要素は 'yahoo'/'fx' のキャッシュキーで共有される）。
- プロバイダ失敗時はキャッシュ済み日次系列の最新値へフォールバック（warn付き）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import Callable, Iterable

from . import price_store, re_index
from .models import PriceSourceStatus, PriceSourceType, Security
from .providers import coingecko
from .providers import fx as fx_provider
from .providers import metal, yahoo
from .store import Store

WarnFn = Callable[[str], None]

SPOT_TTL_SECONDS = 300.0
SPOT_MAX_WORKERS = 5

# ネットワークを使わず daily_prices を引くだけのソース（並列化の対象外）
_LOCAL_SOURCES = frozenset({"toushin", "manual"})

# 前日比の基準日として許す最大のズレ（日）。週末・連休は吸収しつつ、
# 手動評価（不動産・年金）の何ヶ月も前の登録値を「前日」と呼ばないための上限。
PREV_MAX_GAP_DAYS = 7


def _noop(_msg: str) -> None:
    pass


def _locked_warn(warn: WarnFn | None) -> WarnFn:
    """スレッドから安全に呼べる warn ラッパ（並列スポット取得用）。"""
    if warn is None:
        return _noop
    lock = threading.Lock()

    def w(msg: str) -> None:
        with lock:
            warn(msg)

    return w


def _latest_daily(
    store: Store, source: str, source_id: str
) -> tuple[Decimal, str] | None:
    got = store.get_latest_price(source, source_id)
    if got is None:
        return None
    _date, price, currency = got
    return (price, currency or "JPY")


def _metal_spot(store: Store, ref: str, w: WarnFn) -> tuple[Decimal, str] | None:
    symbol = metal.FUTURES_SYMBOL.get(ref)
    if symbol is None:
        w(f"metal: 未知の貴金属IDです: {ref}（XAU/XAG/XPT のいずれか）")
        return None
    futures = _spot_for(store, "yahoo", symbol, w)
    usdjpy = _spot_for(store, "fx", "USD", w)
    if futures is None or usdjpy is None:
        return None
    price_usd, futures_ccy = futures
    if futures_ccy != "USD":
        w(f"metal: {symbol} の建値通貨が {futures_ccy} です（USD想定）")
    return (metal.spot_from_components(price_usd, usdjpy[0]), "JPY")


def _spot_for(
    store: Store, stype: str, ref: str, w: WarnFn
) -> tuple[Decimal, str] | None:
    """(source_type, source_id) の現在値を (価格, 通貨) で返す。失敗は None。"""
    if stype == "toushin":
        # 基準価額はT+1公表のため日次系列の最新値がスポット（ネットワーク不要）
        got = _latest_daily(store, "toushin", ref)
        if got is None:
            w(f"toushin {ref}: 日次基準価額が未取得のため現在値を返せません")
        return got

    if stype == "manual":
        # 手動評価（不動産など）は登録済み評価額の最新値がそのまま現在値
        got = _latest_daily(store, "manual", ref)
        if got is None:
            w("手動評価額が未登録のため現在値を算出できません")
        return got

    cached = price_store.get_cached_spot(store, stype, ref, SPOT_TTL_SECONDS)
    if cached is not None:
        return cached

    got: tuple[Decimal, str] | None
    if stype == "yahoo":
        got = yahoo.fetch_spot(ref, warn=w)
    elif stype == "fx":
        rate = fx_provider.fetch_spot(ref, warn=w)
        got = (rate, "JPY") if rate is not None else None
    elif stype == "metal":
        got = _metal_spot(store, ref, w)
    elif stype == "coingecko":
        got = coingecko.fetch_spot(ref, warn=w)
    else:
        return None

    if got is None:
        fallback = _latest_daily(store, stype, ref)
        if fallback is not None:
            w(f"{stype} {ref}: 現在値の取得に失敗したため、キャッシュ済みの日次値で代用します")
        return fallback

    price_store.put_spot(store, stype, ref, got[0], got[1])
    return got


def _manual_spot(store: Store, sec: Security, fallback: Decimal) -> Decimal:
    """手動評価の現在値。指数を紐付けた不動産は当日まで延長した値を返す。

    _spot_for は (source_type, source_id) しか受け取らず Security を見ないため、
    price_source_ref に入っている指数の参照をここで解決する。manual は
    _LOCAL_SOURCES に入っていてネットワークを使わないので、追加コストは
    daily_prices の読み出しだけ。

    spot_cache には入れない。純粋に DB を引くだけで速く、キャッシュすると
    新しく登録した査定額の反映が最大300秒遅れてしまう。
    """
    source_id = re_index.parse_ref(sec.price_source_ref)
    if not source_id:
        return fallback
    anchors, _ = store.get_price_rows("manual", str(sec.id))
    if not anchors:
        return fallback
    monthly, _ = store.get_price_rows("re_index", source_id)
    if not monthly:
        return fallback
    value = re_index.spot_from_anchor(anchors, monthly, date.today().isoformat())
    return value if value is not None else fallback


def _group_by_source(
    securities: Iterable[Security],
) -> dict[tuple[str, str], list[Security]]:
    """銘柄を (source_type, source_id) でまとめる。同じ参照は1回だけ引くため。"""
    groups: dict[tuple[str, str], list[Security]] = {}
    for sec in securities:
        if sec.id is None:
            continue
        if sec.inactive:
            # 上場廃止・償還済み: 取得は止めるが、キャッシュ済み系列での評価は続ける
            continue
        # 手動評価（不動産など）は daily_prices(source='manual') の最新値が現在値。
        # ネットワークは不要だが、サマリーに載せるにはここで解決する必要がある。
        if (
            sec.price_source_status == PriceSourceStatus.MANUAL
            or sec.price_source_type == PriceSourceType.MANUAL
        ):
            groups.setdefault(("manual", str(sec.id)), []).append(sec)
            continue
        if sec.price_source_status != PriceSourceStatus.LINKED:
            continue
        if sec.price_source_type == PriceSourceType.NONE:
            continue
        if not sec.price_source_ref:
            continue
        key = (sec.price_source_type.value, sec.price_source_ref)
        groups.setdefault(key, []).append(sec)
    return groups


def fetch_spot(
    store: Store, securities: Iterable[Security], warn: WarnFn | None = None
) -> dict[int, Decimal]:
    w = _locked_warn(warn)
    groups = _group_by_source(securities)

    def _safe_spot(key: tuple[str, str]) -> tuple[Decimal, str] | None:
        # 1銘柄の失敗が他銘柄の現在値まで巻き添えにしないよう個別に捕捉する
        try:
            return _spot_for(store, key[0], key[1], w)
        except Exception as exc:  # noqa: BLE001
            w(f"{key[0]} {key[1]}: 現在値の取得でエラーが発生しました: {exc!r}")
            return None

    # 銘柄ごとに1リクエストになるネットワーク系だけを並列化する
    # （プロバイダ毎の最小間隔は providers.base.throttle が保証する）。
    # toushin / manual は daily_prices を引くだけでネットワークを使わない。
    # スレッドに載せてもGILと接続競合で遅くなるだけなので、そのまま逐次で引く。
    keys = list(groups)
    local = [k for k in keys if k[0] in _LOCAL_SOURCES]
    remote = [k for k in keys if k[0] not in _LOCAL_SOURCES]

    got_by_key: dict[tuple[str, str], tuple[Decimal, str] | None] = {}
    for key in local:
        got_by_key[key] = _safe_spot(key)
    if len(remote) > 1:
        with ThreadPoolExecutor(max_workers=min(SPOT_MAX_WORKERS, len(remote))) as pool:
            got_by_key.update(zip(remote, pool.map(_safe_spot, remote)))
    else:
        for key in remote:
            got_by_key[key] = _safe_spot(key)
    fetched = [got_by_key[k] for k in keys]

    out: dict[int, Decimal] = {}
    for (stype, ref), got in zip(keys, fetched):
        secs = groups[(stype, ref)]
        if got is None:
            names = "、".join(s.name for s in secs)
            w(f"現在値を取得できませんでした: {names} ({stype}:{ref})")
            continue
        price, currency = got
        for sec in secs:
            if currency and sec.currency != currency:
                w(
                    f"{sec.name}: 価格の通貨({currency})が銘柄設定の通貨"
                    f"({sec.currency})と一致しません"
                )
            out[sec.id] = (
                _manual_spot(store, sec, price) if stype == "manual" else price
            )
    return out


def fetch_fx_rates(
    store: Store, currencies: Iterable[str], warn: WarnFn | None = None
) -> dict[str, Decimal]:
    w = warn or _noop
    out: dict[str, Decimal] = {}
    for ccy in dict.fromkeys(currencies):  # 順序維持で重複排除
        if not ccy:
            continue
        if ccy == "JPY":
            out["JPY"] = Decimal("1")
            continue
        got = _spot_for(store, "fx", ccy, w)
        if got is None:
            w(f"FXレートを取得できませんでした: {ccy}/JPY")
            continue
        out[ccy] = got[0]
    return out


# ----------------------------------------------------------------------
# 前日終値（前日比の基準）
# ----------------------------------------------------------------------


def _prev_close_for(
    store: Store, stype: str, ref: str, today: date
) -> tuple[Decimal, str] | None:
    """(source_type, source_id) の1つ前の終値を (価格, 基準日ISO) で返す。

    基準は「いまスポットとして採用している値の日付より前の、最後の終値」。
    ソースによってスポットの基準日が違うので一律「昨日」では誤る:

    - toushin / manual: スポット自体が日次系列の最新行 → その1つ前の行。
      基準価額はT+1公表なので、当日より前で引くと現在値と同じ行を掴んでしまう。
    - yahoo / coingecko / fx: スポットは当日のライブ値 → 当日より前の最新行。
    - metal: スポットと同じ組み立て（先物 × USDJPY）を前日値で合成する。

    基準日が PREV_MAX_GAP_DAYS 以上離れている系列は「前日」と呼べないので None。
    """
    if stype == "metal":
        futures_symbol = metal.FUTURES_SYMBOL.get(ref)
        if futures_symbol is None:
            return None
        futures = _prev_close_for(store, "yahoo", futures_symbol, today)
        usdjpy = _prev_close_for(store, "fx", "USD", today)
        if futures is None or usdjpy is None:
            return None
        # 先物とFXで基準日がずれることがある。古いほうを合成値の基準日とする。
        as_of = min(futures[1], usdjpy[1])
        return (metal.spot_from_components(futures[0], usdjpy[0]), as_of)

    if stype in _LOCAL_SOURCES:
        latest = store.get_latest_price(stype, ref)
        if latest is None:
            return None
        spot_day = latest[0]
    else:
        spot_day = today.isoformat()
    got = store.get_price_before(stype, ref, spot_day)
    if got is None:
        return None
    prev_day, price, _ccy = got
    if _iso_gap_days(prev_day, spot_day) > PREV_MAX_GAP_DAYS:
        return None
    return (price, prev_day)


def _iso_gap_days(earlier_iso: str, later_iso: str) -> int:
    try:
        return (date.fromisoformat(later_iso) - date.fromisoformat(earlier_iso)).days
    except ValueError:
        # 想定外の日付表記は「離れすぎ」扱いにして前日比を出さない（安全側）
        return PREV_MAX_GAP_DAYS + 1


def fetch_prev_close(
    store: Store,
    securities: Iterable[Security],
    warn: WarnFn | None = None,
    today: date | None = None,
) -> tuple[dict[int, Decimal], dict[int, str]]:
    """(security_id → 前日終値, security_id → その基準日ISO)。

    daily_prices を読むだけでネットワークは使わない。履歴が未取得の銘柄は
    どちらの辞書にも載らない（呼び出し側は前日比を「—」にする）。
    """
    w = warn or _noop
    day = today or date.today()
    prices: dict[int, Decimal] = {}
    as_of: dict[int, str] = {}
    for (stype, ref), secs in _group_by_source(securities).items():
        try:
            got = _prev_close_for(store, stype, ref, day)
        except Exception as exc:  # noqa: BLE001 — 1銘柄の失敗で前日比全体を落とさない
            w(f"{stype} {ref}: 前日終値の取得でエラーが発生しました: {exc!r}")
            continue
        if got is None:
            continue  # 履歴が無いだけ。警告は出さない（前日比が「—」になるだけ）
        for sec in secs:
            prices[sec.id] = got[0]
            as_of[sec.id] = got[1]
    return prices, as_of


def fetch_prev_fx_rates(
    store: Store,
    currencies: Iterable[str],
    warn: WarnFn | None = None,
    today: date | None = None,
) -> dict[str, Decimal]:
    """通貨コード → 前日のJPYレート。'JPY' は常に 1。取れない通貨は載せない。"""
    w = warn or _noop
    day = today or date.today()
    out: dict[str, Decimal] = {}
    for ccy in dict.fromkeys(currencies):  # 順序維持で重複排除
        if not ccy:
            continue
        if ccy == "JPY":
            out["JPY"] = Decimal("1")
            continue
        try:
            got = _prev_close_for(store, "fx", ccy, day)
        except Exception as exc:  # noqa: BLE001
            w(f"fx {ccy}: 前日レートの取得でエラーが発生しました: {exc!r}")
            continue
        if got is not None:
            out[ccy] = got[0]
    return out
