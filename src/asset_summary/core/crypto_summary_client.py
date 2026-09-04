"""Crypto-Summary 連携クライアント（読み取り専用・warnings-as-data）。

兄弟アプリ Crypto-Summary (CS) の集計 API をサーバー間で読み、
暗号資産クラスの「仮想保有」として AS の応答に合算するための層。

- CS_BASE_URL 未設定なら連携は無効（全 fetch が None を返す）。
- HTTP は providers.base.request() を再利用（key="crypto_summary"）—
  共有 httpx クライアント・429 バックオフ・set_client() のテスト注入が使える。
- 応答はメモリ内 TTL キャッシュ + キー単位の single-flight。
  ダッシュボードは1画面で summary/classes/history を連打するため必須。
  失敗もネガティブキャッシュして、停止中の CS を毎リクエスト叩かない。
- 失敗は例外にせず warn(str) に流す（warnings-as-data）。キャッシュヒット時も
  取得時の warnings を再放出する（「なぜ値が無いか」が常に応答に残るように）。
- CS のデータは AS の DB に保存しない。キャッシュは再起動で消えてよい。
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from .portfolio import SeriesLookup, include_setting_key
from .providers import base as provider_base

WarnFn = Callable[[str], None]

ZERO = Decimal("0")

# CS が停止していてもダッシュボード全体を待たせない（base 既定 30s は長すぎる）
CS_TIMEOUT = 10.0
HEALTH_TIMEOUT = 3.0

SUMMARY_TTL = 60.0    # summary / sources / asset-accounts
HISTORY_TTL = 300.0   # portfolio-history（CS 側が CoinGecko を叩くため長め）
ICONS_TTL = 3600.0    # coin-icons（ほぼ不変）
FAILURE_TTL = 30.0    # 失敗のネガティブキャッシュ

# CS 由来の仮想保有が名乗る口座名（フロントの疑似口座表示にも使う）
CS_ACCOUNT_NAME = "Crypto-Summary"

# 仮想保有の id 接頭辞。AS のネイティブ銘柄 id は int なので衝突しない。
# タグ配分（external_asset_tags）のキーにもこの形をそのまま使う。
EXTERNAL_KEY_PREFIX = "cs:"

# テストがモンキーパッチする現在時刻関数
_now = time.monotonic

_cache: dict[tuple, tuple[float, dict | None, tuple[str, ...]]] = {}
_key_locks: dict[tuple, threading.Lock] = {}
_registry_lock = threading.Lock()


# ----------------------------------------------------------------------
# 設定（すべて呼び出し時に env を読む — テスト・再設定を容易に）
# ----------------------------------------------------------------------


def base_url() -> str:
    return os.environ.get("CS_BASE_URL", "").strip().rstrip("/")


def is_enabled() -> bool:
    return bool(base_url())


def public_url() -> str | None:
    """ブラウザ向けのリンクアウト先 URL（未設定なら CS_BASE_URL にフォールバック）。"""
    url = os.environ.get("CS_PUBLIC_URL", "").strip().rstrip("/")
    return url or (base_url() or None)


def _headers(user_sub: str | None) -> dict[str, str]:
    h: dict[str, str] = {}
    token = os.environ.get("CS_SERVICE_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    if user_sub:
        h["X-CS-User"] = user_sub
    return h


def clear_cache() -> None:
    """テストおよび /api/refresh-prices 用。"""
    with _registry_lock:
        _cache.clear()


# ----------------------------------------------------------------------
# HTTP 層（TTL キャッシュ + single-flight）
# ----------------------------------------------------------------------


def _key_lock(key: tuple) -> threading.Lock:
    with _registry_lock:
        return _key_locks.setdefault(key, threading.Lock())


def _cached(key: tuple) -> tuple[dict | None, tuple[str, ...]] | None:
    with _registry_lock:
        hit = _cache.get(key)
    if hit is None:
        return None
    expires_at, payload, warns = hit
    if _now() >= expires_at:
        return None
    return payload, warns


def _store_cache(key: tuple, payload: dict | None, warns: list[str], ttl: float) -> None:
    with _registry_lock:
        _cache[key] = (_now() + ttl, payload, tuple(warns))


def _fetch_json(
    path: str, params: dict[str, str], user_sub: str | None, warn: WarnFn
) -> dict | None:
    """1回の HTTP GET。失敗は warn して None（例外は出さない）。"""
    url = base_url() + path
    resp = provider_base.request(
        "GET",
        url,
        key="crypto_summary",
        warn=warn,
        headers=_headers(user_sub),
        params=params,
        timeout=CS_TIMEOUT,
        allow_statuses=(400, 401, 404),
    )
    if resp is None:
        # 接続不可 / 5xx / 429 諦め — base.request が warn 済み
        return None
    if resp.status_code == 401:
        warn("Crypto-Summary: 認証に失敗しました（CS_SERVICE_TOKEN を確認してください）")
        return None
    if resp.status_code == 404:
        warn("Crypto-Summary にこの利用者の台帳がありません")
        return None
    if resp.status_code == 400:
        warn(f"Crypto-Summary: リクエストが不正です ({resp.text[:200]})")
        return None
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        warn(f"Crypto-Summary: 応答を JSON として解釈できません: {exc}")
        return None
    if not isinstance(data, dict):
        warn("Crypto-Summary: 応答の形式が想定外です")
        return None
    return data


def _get(
    path: str,
    params: dict[str, str],
    user_sub: str | None,
    ttl: float,
    warn: WarnFn | None,
) -> dict | None:
    """TTL キャッシュ → single-flight → HTTP。無効時は即 None。"""
    w = warn or (lambda _msg: None)
    if not is_enabled():
        return None
    key = (path, tuple(sorted(params.items())), user_sub or "")
    hit = _cached(key)
    if hit is not None:
        payload, warns = hit
        for msg in warns:
            w(msg)
        return payload

    with _key_lock(key):
        hit = _cached(key)  # ロック待ちの間に他スレッドが取得済みかもしれない
        if hit is not None:
            payload, warns = hit
            for msg in warns:
                w(msg)
            return payload
        local_warns: list[str] = []
        payload = _fetch_json(path, params, user_sub, local_warns.append)
        _store_cache(key, payload, local_warns, ttl if payload is not None else FAILURE_TTL)
    for msg in local_warns:
        w(msg)
    return payload


# ----------------------------------------------------------------------
# 公開フェッチャー（CS の応答 dict をそのまま返す。値は文字列のまま）
# ----------------------------------------------------------------------


def fetch_cs_summary(
    currency: str, user_sub: str | None, warn: WarnFn | None = None
) -> dict | None:
    return _get("/api/summary", {"currency": currency}, user_sub, SUMMARY_TTL, warn)


def fetch_cs_sources(
    currency: str, user_sub: str | None, warn: WarnFn | None = None
) -> dict | None:
    return _get("/api/sources", {"currency": currency}, user_sub, SUMMARY_TTL, warn)


def fetch_cs_asset_accounts(
    asset: str, currency: str, user_sub: str | None, warn: WarnFn | None = None
) -> dict | None:
    return _get(
        "/api/asset-accounts",
        {"asset": asset, "currency": currency},
        user_sub,
        SUMMARY_TTL,
        warn,
    )


def fetch_cs_history(
    currency: str,
    range_key: str,
    scope: str,
    user_sub: str | None,
    warn: WarnFn | None = None,
) -> dict | None:
    return _get(
        "/api/portfolio-history",
        {"currency": currency, "range": range_key, "scope": scope},
        user_sub,
        HISTORY_TTL,
        warn,
    )


def fetch_cs_coin_icons(warn: WarnFn | None = None) -> dict | None:
    # 認証不要のエンドポイント（sub 不要）
    return _get("/api/coin-icons", {}, None, ICONS_TTL, warn)


def probe_health() -> bool:
    """CS の死活確認（/api/health）。ステータス表示用の軽量チェック。"""
    if not is_enabled():
        return False
    resp = provider_base.request(
        "GET",
        base_url() + "/api/health",
        key="crypto_summary",
        timeout=HEALTH_TIMEOUT,
    )
    return resp is not None


# ----------------------------------------------------------------------
# 純粋マージヘルパー（I/O なし）
# ----------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def cs_holding_rows(
    cs_summary: dict,
    in_total: bool,
    currency: str,
    warn: WarnFn | None = None,
) -> list[dict[str, Any]]:
    """CS の /api/summary 応答 → AS 内部形式の仮想保有 dict 群（Decimal のまま）。

    - id は "cs:<SYM>"（AS ネイティブは int なので衝突しない）
    - origin="crypto_summary" がフロント側の識別マーカー
    - AS の DB には存在しないため account_id は None
    """
    w = warn or (lambda _msg: None)
    generated = str(cs_summary.get("generated_at") or "")
    as_of = generated[:10] if len(generated) >= 10 else ""
    rows: list[dict[str, Any]] = []
    for a in cs_summary.get("assets") or []:
        if not isinstance(a, dict):
            continue
        sym = str(a.get("asset") or "").strip()
        qty = _dec(a.get("balance"))
        if not sym or qty is None:
            w(f"Crypto-Summary: 解釈できない資産データを無視しました: {a!r}")
            continue
        value = _dec(a.get("value"))
        if not a.get("has_price") or value is None:
            # 価格が付かないコイン（未対応銘柄・エアドロップ等）は AS では扱わない。
            # 評価額に寄与せず、タグ割当の対象に出てくるだけ雑音になるため。
            continue
        # CS の prev_value は「前日の残高 × 前営業日の終値」＝ AS 内の銘柄と同じ
        # 定義（前日の評価額との差）なので、そのまま引き算できる。prev_balance を
        # 返すのがその版の CS である印。返さない旧 CS の prev_value は「いまの残高
        # × 前日終値」＝相場変動のみで、暗号資産だけ定義が揃わない（前日比が
        # 出ないよりはましなので、そのまま使う）。
        prev_value = _dec(a.get("prev_value"))
        day_change = (value - prev_value) if prev_value is not None else None
        rows.append(
            {
                "id": f"{EXTERNAL_KEY_PREFIX}{sym}",
                "origin": "crypto_summary",
                "account_id": None,
                "account": CS_ACCOUNT_NAME,
                "name": sym,
                "code": sym,
                "asset_class": "crypto",
                "unit": "unit",
                "currency": currency,
                "quantity": qty,
                "avg_cost": None,
                "price": _dec(a.get("price")),
                "value": value,
                "cost": None,
                "costed_value": None,
                "pl": None,
                "pl_pct": None,
                "prev_value": prev_value,
                "day_change": day_change,
                "day_change_pct": (
                    (day_change / prev_value * Decimal("100"))
                    if (day_change is not None and prev_value)
                    else None
                ),
                "day_change_as_of": a.get("prev_date") if day_change is not None else None,
                "has_price": True,
                "price_source_status": "linked",
                "lot_count": 1,
                "in_total": in_total,
                "as_of": as_of,
            }
        )
    return rows


def merge_cs_into_summary(
    res: dict[str, Any],
    cs_summary: dict | None,
    settings: dict[str, str],
    currency: str,
    warn: WarnFn | None = None,
) -> Decimal | None:
    """summarize() の結果（Decimal のまま）に CS 分を合算する（破壊的更新）。

    - holdings に仮想行を追加し評価額降順を維持
    - classes の crypto 行に加算（無ければ追加）、全クラスの weight を再計算
    - include_crypto 設定が有効なときだけ total_value に加算
    - CS の unpriced 銘柄を unpriced に追記
    損益系（total_cost / total_pl）は触らない — CS に原価情報が無いため。
    前日比は行ごとに入っているので、合算は cs_day_change_from_rows() →
    merge_cs_day_change() で別途行う（クラス行・総資産への足し込みが要るため）。
    戻り値は合算した CS 分の評価額（何も合算しなければ None）。
    """
    if not cs_summary:
        return None
    in_total = settings.get(include_setting_key("crypto"), "1") == "1"
    rows = cs_holding_rows(cs_summary, in_total, currency, warn)
    if not rows:
        return None

    res["holdings"].extend(rows)
    res["holdings"].sort(key=lambda h: (h["value"] is None, -(h["value"] or ZERO)))

    cs_total = sum((r["value"] for r in rows if r["value"] is not None), ZERO)
    if in_total:
        res["total_value"] = (res["total_value"] or ZERO) + cs_total

    crypto_row = next((c for c in res["classes"] if c["class"] == "crypto"), None)
    if crypto_row is None:
        crypto_row = {
            "class": "crypto",
            "value": ZERO,
            "cost": None,
            "pl": None,
            "pl_pct": None,
            "holding_count": 0,
            "in_total": in_total,
            "weight": None,
        }
        res["classes"].append(crypto_row)
    crypto_row["value"] = (crypto_row["value"] or ZERO) + cs_total
    crypto_row["holding_count"] += len(rows)

    total = res["total_value"]
    for c in res["classes"]:
        c["weight"] = (
            (c["value"] / total * 100) if (c["in_total"] and total) else None
        )
    res["classes"].sort(key=lambda c: -(c["value"] or ZERO))
    # CS の unpriced（価格の付かないコイン）は AS では扱わないので警告にも出さない。
    # 該当コインは cs_holding_rows の時点で落としてある。
    return cs_total


def merge_cs_day_change(
    res: dict[str, Any],
    settings: dict[str, str],
    prev_value: Decimal | None,
    day_change: Decimal | None,
    partial: bool = False,
) -> None:
    """CS 全体の前日比を summarize() 結果へ合算する（破壊的更新）。

    CS の全ポートフォリオ = AS の暗号資産クラスの CS 分 なので、暗号資産クラス行と
    （include_crypto が有効なら）総資産の前日比に足す。前日比が取れなかった場合や
    一部のコインだけ欠けている場合は day_change_partial を立て、UI が
    「一部を除く」と断れるようにする。
    """
    in_total = settings.get(include_setting_key("crypto"), "1") == "1"
    crypto_row = next((c for c in res["classes"] if c["class"] == "crypto"), None)
    if day_change is None or prev_value is None:
        if crypto_row is not None:
            crypto_row["day_change_partial"] = True
        if in_total:
            res["day_change_partial"] = True
        return
    if partial:
        if crypto_row is not None:
            crypto_row["day_change_partial"] = True
        if in_total:
            res["day_change_partial"] = True

    def _add(target: dict[str, Any], prev_key: str, change_key: str, pct_key: str) -> None:
        prev_total = (target.get(prev_key) or ZERO) + prev_value
        change_total = (target.get(change_key) or ZERO) + day_change
        target[prev_key] = prev_total
        target[change_key] = change_total
        target[pct_key] = (change_total / prev_total * Decimal("100")) if prev_total else None

    if crypto_row is not None:
        _add(crypto_row, "prev_value", "day_change", "day_change_pct")
    if in_total:
        _add(res, "total_prev_value", "total_day_change", "total_day_change_pct")


def merge_cs_history(
    points: list[dict[str, Any]],
    cs_points: list[dict[str, Any]] | None,
    ratio: Decimal | None = None,
) -> bool:
    """daily_series() の日次点（Decimal のまま）に CS の評価額を加算する。

    - 日付は ISO 文字列で突き合わせ。CS 側の欠測日は直近値で前方フィル。
    - CS の最初のデータ日以前には加算しない（履歴を捏造しない）。
    - ratio を渡すとその計上率ぶんだけ加算する（タグ・Myポートフォリオの推移）。
    - cost には触れない。戻り値は「CS が1点でも寄与したか」。
    """
    weight = Decimal("1") if ratio is None else ratio
    if weight <= ZERO:
        return False
    if not cs_points:
        return False
    series: dict[str, Decimal] = {}
    for p in cs_points:
        if not isinstance(p, dict):
            continue
        t = str(p.get("t") or "")
        v = _dec(p.get("value"))
        if t and v is not None:
            series[t] = v
    if not series:
        return False
    lookup = SeriesLookup(series)
    contributed = False
    for p in points:
        v = lookup.strictly_at_or_before(p["t"])
        if v is not None:
            p["value"] = (p["value"] or ZERO) + v * weight
            contributed = True
    return contributed


# ----------------------------------------------------------------------
# 前日比（CS には「前日値」の口が無いので日次履歴の点から求める）
# ----------------------------------------------------------------------

# 前日比の基準として許す最大のズレ（日）。prices.PREV_MAX_GAP_DAYS と同じ考え方で、
# 週末・連休は吸収しつつ、何週間も前の点を「前日」と呼ばないための上限。
PREV_MAX_GAP_DAYS = 7

# 前日比のためだけに取る履歴の長さ。連休を跨いでも1点は入る最短のレンジ。
DAY_CHANGE_RANGE = "7d"

# コイン別履歴の並列取得数（price_history.MAX_WORKERS と同じ考え方）
ASSET_HISTORY_WORKERS = 5


def prev_value_from_points(
    points: list[dict[str, Any]] | None, today: date | None = None
) -> Decimal | None:
    """日次点列から「当日より前の直近の評価額」。判らなければ None。

    CS の履歴は当日の点も含む（ほぼ現在値）ため、当日を除いた最後の点を
    前日終値とみなす。点が古すぎる（PREV_MAX_GAP_DAYS 超）ときは、
    それを「前日」と呼ぶと増減が実態とずれるので None を返す。
    """
    if not points:
        return None
    day = today or date.today()
    today_iso = day.isoformat()
    best_t = ""
    best_v: Decimal | None = None
    for p in points:
        if not isinstance(p, dict):
            continue
        t = str(p.get("t") or "")
        if not t or t >= today_iso:
            continue
        v = _dec(p.get("value"))
        if v is None:
            continue
        if t > best_t:
            best_t, best_v = t, v
    if best_v is None:
        return None
    try:
        if (day - date.fromisoformat(best_t)).days > PREV_MAX_GAP_DAYS:
            return None
    except ValueError:
        return None
    return best_v


def day_change_from_points(
    points: list[dict[str, Any]] | None,
    current_value: Any,
    today: date | None = None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """(前日値, 前日差, 前日比%)。AS 側と同じく「現在値 − 前日終値」で揃える。

    current_value は CS 応答の生値（文字列）でもよい。解釈できなければ全て None。
    """
    prev = prev_value_from_points(points, today)
    current = _dec(current_value) if not isinstance(current_value, Decimal) else current_value
    if prev is None or current is None:
        return (None, None, None)
    diff = current - prev
    pct = (diff / prev * Decimal("100")) if prev else None
    return (prev, diff, pct)


def fetch_cs_asset_histories(
    symbols: Iterable[str],
    currency: str,
    range_key: str,
    user_sub: str | None,
    fetch: Callable[..., dict | None] | None = None,
    warn: WarnFn | None = None,
) -> dict[str, dict]:
    """シンボル → portfolio-history(scope=asset:SYM) 応答。取れなかった分は載せない。

    CS にはコイン別の前日値をまとめて返す口が無いため、コイン数ぶんの
    リクエストになる。並列化と TTL キャッシュで賄うが、呼ぶのはコイン一覧を
    出す画面（暗号資産クラス詳細・CS疑似口座の口座詳細・タグ推移）だけに絞る。

    fetch は web 層が自分の名前空間の fetch_cs_history を渡すための差し替え口
    （テストが monkeypatch する対象を跨いでも同じ関数が使われるようにするため）。
    """
    syms = [s for s in dict.fromkeys(symbols) if s]
    if not syms:
        return {}
    get = fetch or fetch_cs_history
    w = warn or (lambda _msg: None)
    lock = threading.Lock()

    def _warn(msg: str) -> None:
        with lock:
            w(msg)

    def _one(sym: str) -> tuple[str, dict | None]:
        return (sym, get(currency, range_key, f"asset:{sym}", user_sub, _warn))

    if len(syms) == 1:
        results = [_one(syms[0])]
    else:
        with ThreadPoolExecutor(
            max_workers=min(ASSET_HISTORY_WORKERS, len(syms))
        ) as pool:
            results = list(pool.map(_one, syms))
    return {sym: hist for sym, hist in results if hist is not None}


def cs_asset_from_summary(
    cs_summary: dict | None, symbol: str, currency: str = "", in_total: bool = True
) -> dict[str, Any] | None:
    """/api/summary から1コインぶんの行を取り出す。無ければ None。

    コイン詳細の前日比を保有テーブルの行と同じ数字にするために使う
    （どちらも cs_holding_rows が作る行なので、定義がずれようがない）。
    """
    for row in cs_holding_rows(cs_summary or {}, in_total, currency):
        if row["code"] == symbol:
            return row
    return None


def cs_day_change_from_rows(
    rows: list[dict[str, Any]],
) -> tuple[Decimal | None, Decimal | None, bool]:
    """CS 仮想保有行から (前日値の合計, 前日差の合計, 欠けがあるか)。

    /api/summary が各資産の prev_value を返すようになったので、履歴を引かずに
    ここで合計できる。合計は「AS が実際に表示している行」から作る — CS の
    total_prev_value をそのまま使うと、AS 側で落とした資産（価格なし等）が
    混ざって評価額と対象集合がずれる。

    前日値が1件も無ければ (None, None, False) — 古い CS か、CS 側でも
    履歴が取れていない状態。呼び出し側は履歴からのフォールバックへ回す。
    """
    prev_total = ZERO
    day_total = ZERO
    known = 0
    missing = 0
    for row in rows:
        if row.get("day_change") is not None and row.get("prev_value") is not None:
            prev_total += row["prev_value"]
            day_total += row["day_change"]
            known += 1
        elif row.get("value") is not None:
            missing += 1
    if not known:
        return (None, None, False)
    return (prev_total, day_total, bool(missing))


def cs_total_day_change(
    currency: str,
    user_sub: str | None,
    current_total: Decimal | None,
    fetch: Callable[..., dict | None] | None = None,
    warn: WarnFn | None = None,
    today: date | None = None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """CS 全体の (前日値, 前日差, 前日比%)。履歴1本（TTLキャッシュ有）で済む。

    CS の全ポートフォリオ = AS の暗号資産クラスの CS 分 = CS疑似口座なので、
    この1つの値が総資産・暗号資産クラス・CS疑似口座の3箇所を賄える。
    """
    get = fetch or fetch_cs_history
    hist = get(currency, DAY_CHANGE_RANGE, "total", user_sub, warn)
    return day_change_from_points((hist or {}).get("points"), current_total, today)


def cs_asset_day_changes(
    rows: list[dict[str, Any]],
    currency: str,
    user_sub: str | None,
    fetch: Callable[..., dict | None] | None = None,
    warn: WarnFn | None = None,
    today: date | None = None,
) -> None:
    """CS 仮想保有行に前日比を書き込む（破壊的更新）。

    rows は cs_holding_rows() が作った行。取れなかったコインは触らない
    （day_change が None のまま＝画面では「—」）。
    """
    hists = fetch_cs_asset_histories(
        (r.get("code") or "" for r in rows), currency,
        DAY_CHANGE_RANGE, user_sub, fetch=fetch, warn=warn,
    )
    for row in rows:
        hist = hists.get(row.get("code") or "")
        if hist is None:
            continue
        prev, diff, pct = day_change_from_points(
            hist.get("points"), row.get("value"), today
        )
        if diff is None:
            continue
        row["prev_value"] = prev
        row["day_change"] = diff
        row["day_change_pct"] = pct
        row["day_change_as_of"] = None  # CS は日次点のみ（基準日は暦の前営業日）
