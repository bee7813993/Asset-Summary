"""Asset Summary Web API（FastAPI・単一ユーザー・認証なし）。

封筒規約（Crypto-Summary踏襲）:
- Decimal は文字列で JSON 化する（str(d)）。% 系は小数2桁に quantize。
- 価格を触る応答は currency / warnings / generated_at(UTC ISO) を必ず含める。
- 価格取得等の失敗は例外にせず warnings 配列へ流す（warnings-as-data）。
  アプリは価格層が未実装・停止中でも常に起動・応答可能。

価格・取込関数（fetch_spot / fetch_fx_rates / fetch_prev_close /
fetch_prev_fx_rates / ensure_price_history /
ensure_fx_history / search_funds / build_preview / commit_batch）と
Crypto-Summary 連携（fetch_cs_summary / fetch_cs_history /
fetch_cs_asset_accounts / fetch_cs_coin_icons）は
モジュール名前空間経由で参照する — テストが
monkeypatch.setattr(web_app, "fetch_spot", fake) で差し替えられるようにするため。
"""

from __future__ import annotations

import base64
import importlib.metadata
import json
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.models import (
    ASSET_CLASS_META,
    SUPPORTED_CURRENCIES,
    AssetClass,
    HoldingSnapshot,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from ..core import (
    crypto_summary_client,
    fund_autolink,
    portfolio,
    price_store,
    re_index,
    tag_rules,
    tagging,
)
from ..core.providers import re_index as re_index_provider
from ..core.crypto_summary_client import (
    fetch_cs_asset_accounts,
    fetch_cs_coin_icons,
    fetch_cs_history,
    fetch_cs_summary,
    merge_cs_history,
    merge_cs_into_summary,
)
from ..core.portfolio import aggregate_by_security, daily_series, summarize
from ..core.price_history import (
    ensure_fx_history,
    ensure_price_history,
    ensure_re_index_history,
    search_coins,
    search_funds,
)
from ..core.prices import (
    fetch_fx_rates,
    fetch_prev_close,
    fetch_prev_fx_rates,
    fetch_spot,
)
from ..core.store import ConflictError, Store, StoreError
from ..importers.base import make_name_key, normalize_code
from ..importers.inbox import InboxWatcher
from ..importers.service import DuplicateImportError, build_preview, commit_batch
from ..importers.tx_service import (
    build_tx_preview,
    commit_tx_batch,
    cost_basis_events,
    recompute_cost_basis,
    remap_tx_preview,
)
from . import auth as as_auth

ZERO = Decimal("0")
_TWO_DP = Decimal("0.01")

log = logging.getLogger("asset_summary.web")

try:
    _VERSION = importlib.metadata.version("asset-summary")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    _VERSION = "0.1.0"

# PDL 1.0（CC BY 4.0 互換）の出典表記。加工物なので「加工して作成」まで含める。
RE_INDEX_ATTRIBUTION = "出典：「不動産価格指数」（国土交通省）を加工して作成"

_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}

# asset_class ごとの unit / divisor / 価格ソース既定値（POST /api/securities で補完）
_CLASS_DEFAULTS: dict[str, dict[str, Any]] = {
    "fund_jp": {"unit": "kuchi", "price_unit_divisor": 10000},
    "fund_foreign": {"unit": "kuchi", "price_unit_divisor": 10000},
    "cash": {"unit": "currency", "status": "not_required"},
    "point": {"unit": "point", "status": "not_required"},
    "pension": {"unit": "unit", "status": "not_required"},
    "metal": {"unit": "gram"},
    "real_estate": {"unit": "unit", "type": "manual", "status": "manual"},
    "crypto": {"unit": "unit"},
}


# ----------------------------------------------------------------------
# serialization helpers
# ----------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(value: Decimal | None) -> str | None:
    """Decimal → 文字列（None許容）。"""
    return None if value is None else str(value)


def _pct(value: Decimal | None) -> str | None:
    """%系は小数2桁に quantize して文字列化。"""
    if value is None:
        return None
    try:
        return str(value.quantize(_TWO_DP, rounding=ROUND_HALF_UP))
    except InvalidOperation:
        # 桁数が Decimal のコンテキスト精度を超える場合（極端な入力値）
        return None


# 保有数量・単価の絶対値上限。SQLite/Decimal は更に大きな値も扱えるが、
# 現実の資産では起こり得ず、系列計算の桁あふれや表示崩れの原因になるため弾く。
_MAX_ABS = Decimal("1E24")


def _to_decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"{field} が数値として解釈できません: {value!r}"
        )
    if not parsed.is_finite():
        raise HTTPException(
            status_code=400, detail=f"{field} に有限の数値を指定してください: {value!r}"
        )
    if parsed.copy_abs() > _MAX_ABS:
        raise HTTPException(
            status_code=400, detail=f"{field} の値が大きすぎます: {value!r}"
        )
    return parsed


def _to_int(value: Any, field: str, minimum: int | None = None) -> int:
    """整数変換（bool・float・巨大値を弾く）。"""
    if isinstance(value, bool) or isinstance(value, float):
        raise HTTPException(
            status_code=400, detail=f"{field} は整数で指定してください: {value!r}"
        )
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"{field} は整数で指定してください: {value!r}"
        )
    if abs(parsed) > 2**63 - 1:
        raise HTTPException(status_code=400, detail=f"{field} の値が大きすぎます")
    if minimum is not None and parsed < minimum:
        raise HTTPException(
            status_code=400, detail=f"{field} は {minimum} 以上で指定してください"
        )
    return parsed


def _to_decimal_opt(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _to_decimal(value, field)


def _to_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"{field} は YYYY-MM-DD 形式で指定してください"
        )


# ダッシュボードに置けるウィジェットと既定の並び順
DASHBOARD_WIDGETS = ("history", "classes", "tags", "portfolios", "accounts", "holdings")
DEFAULT_DASHBOARD_LAYOUT = [
    {"id": "history", "visible": True},
    {"id": "classes", "visible": True},
    {"id": "accounts", "visible": True},
    {"id": "holdings", "visible": True},
    # タグ別は banks/タグを設定してから意味を持つ。Myポートフォリオは
    # タグのドリルダウンで代替できるため、どちらも既定では出さない。
    {"id": "tags", "visible": False},
    {"id": "portfolios", "visible": False},
]


# 既定でダッシュボードに出す「総資産に含める」チップ。
# 投資資産だけを見たいときに外したくなるのは、たいていこの3つ。
DEFAULT_CHIP_CLASSES = ["cash", "pension", "point"]


def _dashboard_chip_classes(settings: dict[str, str]) -> list[str] | None:
    """ダッシュボードに出す「総資産に含める」チップの資産クラス。

    未設定は既定セット（現金・年金・ポイント）。空リストは「1つも出さない」。
    """
    raw = settings.get("dashboard_chip_classes")
    if not raw:  # 未設定・空文字とも既定セットに戻す
        return list(DEFAULT_CHIP_CLASSES)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return list(DEFAULT_CHIP_CLASSES)
    if not isinstance(parsed, list):
        return list(DEFAULT_CHIP_CLASSES)
    return [c for c in parsed if c in ASSET_CLASS_META]


def _dashboard_layout(settings: dict[str, str]) -> list[dict[str, Any]]:
    """保存済みのダッシュボード構成。未知/欠落ウィジェットは既定で補う。"""
    raw = settings.get("dashboard_layout")
    saved: list[dict[str, Any]] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                wid = str(item.get("id") or "")
                if wid in DASHBOARD_WIDGETS and wid not in {s["id"] for s in saved}:
                    saved.append({"id": wid, "visible": bool(item.get("visible", True))})
    known = {s["id"] for s in saved}
    for default in DEFAULT_DASHBOARD_LAYOUT:
        if default["id"] not in known:
            saved.append(dict(default))
    return saved


def _clean_source_ref(value: Any) -> str | None:
    """price_source_ref のサニタイズ。

    この値は外部APIのURLに埋め込まれるため、制御文字・空白・パス区切りを拒否する
    （改行が入ると httpx.InvalidURL、'..' が入るとURLのパスが変わる）。
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400, detail=f"price_source_ref は文字列で指定してください: {value!r}"
        )
    ref = value.strip()
    if not ref:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in ref) or "/" in ref or "\\" in ref:
        raise HTTPException(
            status_code=400, detail=f"price_source_ref に使用できない文字が含まれています: {value!r}"
        )
    return ref


def _enum_or_400(enum_cls: type, value: Any, field: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} の値が不正です: {value!r}")


def _resolve_currency(raw: str | None, settings: dict[str, str]) -> str:
    """currency クエリの解決。未指定は設定の既定通貨、未対応は JPY へフォールバック。"""
    c = (raw or settings.get("default_currency") or "JPY").upper()
    return c if c in SUPPORTED_CURRENCIES else "JPY"


# 投信協会への照会を直列化するロック。設定ページからの一括判定と、取込直後の
# 自動連携が同時に走らないようにする（相手は外部サービスなのでプロセス共有でよい）。
autolink_lock = threading.Lock()

# 取込直後に自動連携を試みる新規投信の上限。1件あたり検索1〜3回＋CSV最大3回の
# 照会が要るため、これを超えるときは取込を待たせず設定ページへ誘導する。
AUTOLINK_ON_IMPORT_MAX = 5


def _dedupe_linked_funds(store: Store, warnings: list[str]) -> list[dict[str, Any]]:
    """同じ投信協会ファンドに連携された重複銘柄を自動統合する。

    ref（ISIN:協会コード）が同じなら同一ファンドなので確認は要らない。
    取引が移動したら原価を作り直す（holding_cost_basis は派生値なので冪等）。
    """
    merged = fund_autolink.dedupe_same_fund(store, warn=warnings.append)
    if any(m["transactions"] for m in merged):
        try:
            warnings.extend(recompute_cost_basis(store).get("warnings", []))
        except Exception as e:  # noqa: BLE001 — 統合自体は完了している
            warnings.append(f"取得原価の再計算に失敗しました: {e}")
    return merged


def _autolink_new_funds(
    store: Store, security_ids: list[int], warnings: list[str]
) -> dict[str, Any]:
    """取込で新しくできた投信を、その場で投信協会へ照会して連携する。

    投信は MF の PDF に銘柄コードが無く名前しか手がかりが無いので、表記が
    揺れると別銘柄として登録されてしまう。ISIN:協会コードまで辿れば同一性は
    確定するため、新規銘柄が出たときだけ照会し、基準価額が一致した候補へ
    自動連携する。連携できれば dedupe_same_fund が既存銘柄へ統合する。

    対象は「この取込で新しくできた銘柄」だけに絞る。全未連携を見ると照会に
    数分かかり、取込がその間止まってしまう。件数が多いときも見送り、設定
    ページからまとめて実行してもらう。

    連携できなかったものは reason を添えて返す（協会へ届かなかったのか、
    該当が無かったのかを利用者が区別できるようにする）。
    """
    targets = [
        s.id
        for s in (store.get_security(i) for i in security_ids)
        if s is not None
        and s.price_source_status == PriceSourceStatus.UNLINKED
        and s.asset_class in (AssetClass.FUND_JP, AssetClass.FUND_FOREIGN)
    ]
    out: dict[str, Any] = {"attempted": len(targets), "linked": [], "unresolved": []}
    if not targets:
        return out
    if len(targets) > AUTOLINK_ON_IMPORT_MAX:
        out["skipped"] = True
        warnings.append(
            f"新しい投信が {len(targets)} 件あります。"
            "取込を待たせないため自動連携は行いませんでした（設定ページから実行できます）"
        )
        return out
    if not autolink_lock.acquire(blocking=False):
        out["skipped"] = True
        warnings.append("投信の自動判定が実行中のため、今回の自動連携は見送りました")
        return out
    try:
        suggestions = fund_autolink.suggest_links(
            store, warn=warnings.append, security_ids=targets
        )
    except Exception as e:  # noqa: BLE001 — 取込自体は成功させる
        warnings.append(f"投信の自動連携に失敗しました: {e}")
        return out
    finally:
        autolink_lock.release()

    applied: list[Security] = []
    for s in suggestions:
        if s["status"] == "auto" and s["best_ref"]:
            store.update_security(
                s["security_id"],
                price_source_type=PriceSourceType.TOUSHIN.value,
                price_source_ref=s["best_ref"],
                price_source_status=PriceSourceStatus.LINKED.value,
            )
            sec = store.get_security(s["security_id"])
            if sec is not None:
                applied.append(sec)
            out["linked"].append({"security_id": s["security_id"], "name": s["name"],
                                  "ref": s["best_ref"]})
        else:
            out["unresolved"].append(
                {
                    "security_id": s["security_id"],
                    "name": s["name"],
                    "status": s["status"],
                    "reason": s.get("reason"),
                }
            )
    if applied:
        # ref が同じ既存銘柄があればここで統合される（別名の重複が消える）
        out["merged"] = _dedupe_linked_funds(store, warnings)
        try:
            today = date.today()
            ensure_price_history(
                store, applied, today - timedelta(days=365 * 5), today, warnings.append
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"価格履歴の取得に失敗しました: {e}")
    return out


def _derive_pension_units_now(
    store: Store, warnings: list[str]
) -> list[dict[str, Any]]:
    """連携済み年金銘柄の口数を評価額から逆算する（fund_autolink 参照）。

    基準価額の履歴が要るため、ensure_price_history の後に呼ぶこと。
    対象が無ければ何もしない（冪等・ローカル読みだけ）。
    """
    try:
        return fund_autolink.derive_pension_quantities(store, warn=warnings.append)
    except Exception as e:  # noqa: BLE001 — 逆算に失敗しても連携自体は有効
        warnings.append(f"年金の口数逆算に失敗しました: {e}")
        return []


def _acct_display(acct: Any) -> str:
    if acct is None:
        return ""
    return acct.display_name or acct.name


def _ser_account_ref(a: dict[str, Any]) -> dict[str, Any]:
    """銘柄単位行に添える口座別内訳（参考情報）。"""
    return {
        "account_id": a.get("account_id"),
        "account": a.get("account") or "",
        "quantity": _s(a.get("quantity")),
        "avg_cost": _s(a.get("avg_cost")),
        "value": _s(a.get("value")),
        "pl": _s(a.get("pl")),
        "pl_pct": _pct(a.get("pl_pct")),
        "day_change": _s(a.get("day_change")),
        "day_change_pct": _pct(a.get("day_change_pct")),
    }


def _ser_holding(h: dict[str, Any]) -> dict[str, Any]:
    out = dict(h)
    out["quantity"] = _s(h["quantity"])
    out["costed_quantity"] = _s(h.get("costed_quantity"))
    out["avg_cost"] = _s(h["avg_cost"])
    out["price"] = _s(h["price"])
    out["value"] = _s(h["value"])
    out["cost"] = _s(h["cost"])
    out["costed_value"] = _s(h.get("costed_value"))
    out["pl"] = _s(h["pl"])
    out["pl_pct"] = _pct(h["pl_pct"])
    out["prev_value"] = _s(h.get("prev_value"))
    out["day_change"] = _s(h.get("day_change"))
    out["day_change_pct"] = _pct(h.get("day_change_pct"))
    if h.get("accounts") is not None:
        out["accounts"] = [_ser_account_ref(a) for a in h["accounts"]]
    return out


def _mf_acquired_on(lot: HoldingSnapshot) -> str | None:
    """MF PDF が取り込んだ取得日（holding_snapshots.raw の meta.acquired_on）。"""
    from ..core.cost_basis import parse_loose_date

    text = ((lot.raw or {}).get("meta") or {}).get("acquired_on")
    if not text:
        return None
    parsed = parse_loose_date(str(text))
    return parsed.isoformat() if parsed else None


def _ser_transaction(tx: Any, accounts: dict[int, Any]) -> dict[str, Any]:
    account = accounts.get(tx.account_id)
    return {
        "id": tx.id,
        "account_id": tx.account_id,
        "account": (account.display_name or account.name) if account else "",
        "security_id": tx.security_id,
        "trade_date": tx.trade_date.isoformat(),
        "settle_date": tx.settle_date.isoformat() if tx.settle_date else None,
        "tx_type": tx.tx_type.value,
        "quantity": _s(tx.quantity),
        "unit_price": _s(tx.unit_price),
        "gross_amount": _s(tx.gross_amount),
        "fee": _s(tx.fee),
        "tax": _s(tx.tax),
        "net_amount": _s(tx.net_amount),
        "split_ratio": _s(tx.split_ratio),
        "currency": tx.currency,
        "lot_label": tx.lot_label,
        "note": tx.note,
        "batch_id": tx.batch_id,
    }


def _totals_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """保有行の集計（詳細画面のヘッダ用・JSON化済み）。

    クラス別・口座別・保有一覧のどのドリルダウンでも同じ数字が出るように、
    集計規約は summarize() に揃える:
    - 損益は原価の判る評価額(costed_value)と cost だけを突き合わせる
    - 前日比は前日値が判る行だけを足し、欠けがあれば partial を立てる
    """
    value = sum((r["value"] for r in rows if r["value"] is not None), ZERO)
    pl_rows = [
        r for r in rows if r.get("cost") is not None and r.get("costed_value") is not None
    ]
    cost = sum((r["cost"] for r in pl_rows), ZERO) if pl_rows else None
    costed_value = sum((r["costed_value"] for r in pl_rows), ZERO) if pl_rows else None
    pl = (costed_value - cost) if cost is not None else None

    day_rows = [r for r in rows if r.get("day_change") is not None]
    # 「一部を除く」は数字を出しているときだけ意味がある（1件も無ければ partial ではない）
    partial = bool(day_rows) and any(
        r.get("day_change") is None and r.get("value") is not None for r in rows
    )
    day_change = sum((r["day_change"] for r in day_rows), ZERO) if day_rows else None
    prev_value = sum(
        (r["prev_value"] for r in day_rows if r.get("prev_value") is not None), ZERO
    )
    return {
        "total_value": _s(value),
        "total_cost": _s(cost),
        "total_pl": _s(pl),
        "total_pl_pct": _pct((pl / cost * 100) if (pl is not None and cost) else None),
        "total_day_change": _s(day_change),
        "total_day_change_pct": _pct(
            (day_change / prev_value * 100)
            if (day_change is not None and prev_value)
            else None
        ),
        "day_change_partial": partial,
    }


def _ser_class(c: dict[str, Any]) -> dict[str, Any]:
    meta = ASSET_CLASS_META.get(c["class"], {})
    return {
        "class": c["class"],
        "label": meta.get("ja", c["class"]),
        "color": meta.get("color", "#6e7681"),
        "value": _s(c["value"]),
        "cost": _s(c["cost"]),
        "pl": _s(c["pl"]),
        "pl_pct": _pct(c["pl_pct"]),
        "day_change": _s(c.get("day_change")),
        "day_change_pct": _pct(c.get("day_change_pct")),
        "day_change_partial": bool(c.get("day_change_partial")),
        "weight": _pct(c["weight"]),
        "holding_count": c["holding_count"],
        "in_total": c["in_total"],
    }


def _ser_security(sec: Security) -> dict[str, Any]:
    return {
        "id": sec.id,
        "code": sec.code,
        "name": sec.name,
        "asset_class": sec.asset_class.value,
        "currency": sec.currency,
        "unit": sec.unit.value,
        "price_unit_divisor": sec.price_unit_divisor,
        "price_source_type": sec.price_source_type.value,
        "price_source_ref": sec.price_source_ref,
        "price_source_status": sec.price_source_status.value,
        "inactive": sec.inactive,
    }


# ----------------------------------------------------------------------
# summary computation (cli.py の summary コマンドも compute_summary を使う)
# ----------------------------------------------------------------------


def _prev_snapshot_day(store: Store, today: date | None = None) -> date:
    """前日比の基準日（この日の保有数で前日の評価額を出す）。

    「今日の前日」に固定すると、取込がもたらした変化は**取込時刻から次の
    深夜0時まで**しか出ない。夜に取り込む運用なら数時間で消える。0時時点の
    保有額は前日に取り込んだデータの金額なのだから、暦ではなくデータ側の
    最新地点から1日戻すのが正しい。

    基準日 = max(最新スナップショット日, 今日 − 1日) − 1日
      - 今日取り込んだ  → 最新の1つ前と比べる（取込ぶんの増減が出る）
      - 昨日取り込んだ  → やはり最新の1つ前。次の取込まで出続ける
      - 2日以上取込なし → 実質「今日の前日」に戻り、最新と同じ状態を指すので
                          数量差は消えて相場ぶんだけになる（古い変化を出さない）
    未来日付のスナップショット（時計ずれ等）は今日に丸めて同じ規則を保つ。
    """
    today = today or date.today()
    latest = store.latest_snapshot_date() or today
    anchor = min(latest, today)
    return max(anchor, today - timedelta(days=1)) - timedelta(days=1)


def _summarize_now(
    store: Store, currency: str, warnings: list[str]
) -> tuple[dict[str, Any], dict[int, Decimal], dict[str, Decimal]]:
    """現在保有の評価（Decimalのまま）。(summarize結果, spot, fx) を返す。

    fetch_spot / fetch_fx_rates はモジュール名前空間経由で呼ぶ
    （テストのモンキーパッチ対象）。未実装・失敗は warnings へ。

    前日比の基準（fetch_prev_close / fetch_prev_fx_rates）は daily_prices を
    読むだけでネットワークを使わない。履歴が未取得の銘柄は前日比が出ない
    （画面では「—」）。ここで履歴を取りに行くと主要APIが軒並み遅くなるため、
    履歴の充填は推移グラフ側（ensure_price_history）に任せる。

    保有数も前日のスナップショットを渡す。前日比を「前日の資産評価との差」に
    揃えるため — 現金は数量がそのまま金額なので、当日の数量で評価すると
    前日比が定義上ゼロになってしまう。
    """
    lots = store.current_holdings()
    prev_lots = store.holdings_as_of(_prev_snapshot_day(store))
    secs = store.securities_by_id()
    accounts = {a.id: a for a in store.list_accounts()}
    settings = store.get_settings()

    spot: dict[int, Decimal] = {}
    try:
        spot = fetch_spot(store, list(secs.values()), warn=warnings.append)
    except Exception as e:  # noqa: BLE001 — 価格層未実装でも応答は返す
        warnings.append(f"価格取得エラー: {e}")

    ccys = {s.currency for s in secs.values() if s.currency != "JPY"}
    if currency != "JPY":
        ccys.add(currency)
    fx: dict[str, Decimal] = {}
    if ccys:
        try:
            fx = fetch_fx_rates(store, sorted(ccys), warn=warnings.append)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"価格取得エラー: {e}")

    jpy_per_display = Decimal("1")
    if currency != "JPY":
        rate = fx.get(currency)
        if rate:
            jpy_per_display = rate
        else:
            warnings.append(
                f"為替レートが取得できないため {currency} 表示は円ベースのままです"
            )

    prev_spot: dict[int, Decimal] = {}
    prev_as_of: dict[int, str] = {}
    prev_fx: dict[str, Decimal] = {}
    try:
        prev_spot, prev_as_of = fetch_prev_close(
            store, list(secs.values()), warn=warnings.append
        )
        if ccys:
            prev_fx = fetch_prev_fx_rates(store, sorted(ccys), warn=warnings.append)
    except Exception as e:  # noqa: BLE001 — 前日比が出ないだけでサマリーは返す
        warnings.append(f"前日比の算出に失敗しました: {e}")
    prev_jpy_per_display = (
        prev_fx.get(currency, jpy_per_display) if currency != "JPY" else Decimal("1")
    )

    result = summarize(
        lots,
        secs,
        accounts,
        spot,
        fx,
        settings,
        jpy_per_display=jpy_per_display,
        prev_spot=prev_spot,
        prev_fx=prev_fx,
        prev_as_of=prev_as_of,
        prev_jpy_per_display=prev_jpy_per_display,
        prev_lots=prev_lots,
        cost_overrides=store.cost_basis_overrides(),
    )
    _mark_estimated(store, secs, result)
    return result, spot, fx


def _mark_estimated(
    store: Store, secs: dict[int, Any], result: dict[str, Any]
) -> None:
    """公的指数で最終査定日より先へ延長した評価額に「目安」の印を付ける。

    has_price は使い回さない。あれは「MF記載値へのフォールバック」を意味しており、
    倒すと lot_value_jpy の意味が変わって損益計算まで波及する。指数延長は
    「価格はあるが模型である」という別の主張なので、直交するフラグにする。

    印を付けるのは web 層。summarize は spot(dict) しか見ないし、
    prices.fetch_spot の戻り値契約はテストがモンキーパッチする公開契約なので
    広げない。全ドリルダウンが _summarize_now を通るのでここ1箇所で足りる。
    """
    today = date.today().isoformat()
    estimated: set[int] = set()
    for sec_id, sec in secs.items():
        if re_index.parse_ref(sec.price_source_ref) is None:
            continue
        latest = store.get_latest_price("manual", str(sec_id))
        if latest is not None and latest[0] < today:
            estimated.add(sec_id)
    if not estimated:
        return
    for row in result.get("holdings", []):
        if row.get("id") in estimated:
            row["estimated"] = True


def _cs_user_sub(request: Request | None) -> str | None:
    """CS へ渡す利用者 sub。認証セッションがあればその sub、無ければ env フォールバック。

    AS の認証が無効（SessionMiddleware 不在）のときは request.session が
    使えないため、開発用の CS_USER_SUB を使う（CS がシングルユーザーなら不要）。
    """
    if request is not None:
        try:
            user = request.session.get("user")
        except (AssertionError, AttributeError):  # SessionMiddleware なし
            user = None
        if user and user.get("sub"):
            return str(user["sub"])
    return os.environ.get("CS_USER_SUB", "").strip() or None


def _validate_external_asset_key(raw: str) -> str:
    """外部資産キー（"cs:BTC"）の検証。想定外の文字列で行を増やさない。"""
    key = (raw or "").strip()
    prefix = crypto_summary_client.EXTERNAL_KEY_PREFIX
    if not key.startswith(prefix):
        raise HTTPException(
            status_code=400, detail=f"未対応の資産キーです: {raw!r}"
        )
    symbol = key[len(prefix):]
    if not symbol or len(symbol) > 32 or not symbol.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail=f"不正な資産キーです: {raw!r}")
    return prefix + symbol.upper()


def _cs_effective_currency(cur: str, fx: dict[str, Decimal]) -> str:
    """CS へ渡す通貨。AS 側が FX フォールバック中なら JPY に落とす。

    表示通貨のレートが無いとき _summarize_now は警告を出して円ベースの値を
    そのまま返す。その状態で CS だけ表示通貨建てで取ると、総資産が
    JPY + 外貨の混ざった無意味な値になるため、CS も JPY で揃える。
    """
    return cur if (cur == "JPY" or fx.get(cur)) else "JPY"


def _apply_cs_merge(
    res: dict[str, Any],
    settings: dict[str, str],
    currency: str,
    user_sub: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Crypto-Summary 分を summarize() 結果へ合算し、接続状態ブロックを返す。

    fetch_cs_summary はモジュール名前空間経由（テストの monkeypatch 対象）。
    """
    if not crypto_summary_client.is_enabled():
        return {"configured": False, "connected": None, "generated_at": None}
    cs = fetch_cs_summary(currency, user_sub, warn=warnings.append)
    cs_total = merge_cs_into_summary(res, cs, settings, currency, warn=warnings.append)
    if cs_total is not None:
        # CS の /api/summary は各資産の前日値を返すので、合算は行から作れる
        # （追加リクエスト0）。前日値を返さない旧 CS 相手のときだけ、
        # 全体の日次履歴1本から求めるフォールバックへ回る。
        cs_rows = [
            h for h in res["holdings"] if h.get("origin") == "crypto_summary"
        ]
        prev, diff, partial = crypto_summary_client.cs_day_change_from_rows(cs_rows)
        if diff is None:
            prev, diff, _pct = crypto_summary_client.cs_total_day_change(
                currency, user_sub, cs_total,
                fetch=fetch_cs_history, warn=warnings.append,
            )
            partial = False
        crypto_summary_client.merge_cs_day_change(res, settings, prev, diff, partial)
    return {
        "configured": True,
        "connected": cs is not None,
        "generated_at": (cs or {}).get("generated_at"),
    }


def _fill_cs_asset_day_changes(
    res: dict[str, Any],
    currency: str,
    user_sub: str | None,
    warnings: list[str],
) -> None:
    """CS 由来コインの行に前日比を埋める（前日値を返さない旧 CS 向けの保険）。

    いまの CS は /api/summary で各資産の前日値を返すので、ここは何もしない。
    それを返さない版が相手のときだけ、コイン別の履歴を引いて埋める。
    コイン数ぶんのリクエストになるため、呼ぶのはコイン一覧を出す画面
    （暗号資産クラス詳細・CS疑似口座の口座詳細）だけに絞ってある。
    """
    cs_rows = [h for h in res["holdings"] if h.get("origin") == "crypto_summary"]
    if any(h.get("prev_value") is not None for h in cs_rows):
        # CS が前日値を返している。残りは CS 側でも履歴が無い分なので、
        # コイン別に問い合わせても埋まらない（無駄な往復になるだけ）。
        return
    rows = [h for h in cs_rows if h.get("day_change") is None]
    if not rows:
        return
    crypto_summary_client.cs_asset_day_changes(
        rows, currency, user_sub, fetch=fetch_cs_history, warn=warnings.append
    )


def compute_summary(
    store: Store, currency: str = "JPY", user_sub: str | None = None
) -> dict[str, Any]:
    """/api/summary のレスポンス形状（JSON化済み・Decimalは文字列）を返す公開ヘルパー。"""
    warnings: list[str] = []
    settings = store.get_settings()
    cur = _resolve_currency(currency, settings)
    res, _spot, _fx = _summarize_now(store, cur, warnings)
    cs_block = _apply_cs_merge(
        res, settings, _cs_effective_currency(cur, _fx),
        user_sub or _cs_user_sub(None), warnings,
    )
    holdings = [_ser_holding(h) for h in res["holdings"]]
    # 保有一覧は銘柄単位（同じ銘柄を複数口座で持っていても1行）。口座別が主題の
    # 画面は holdings（銘柄×口座）のほうを使う。
    by_security = aggregate_by_security(
        res["holdings"], merge_cash=portfolio.merge_cash_enabled(settings)
    )
    return {
        "currency": cur,
        "total_value": _s(res["total_value"]),
        "total_cost": _s(res["total_cost"]),
        "total_pl": _s(res["total_pl"]),
        "total_pl_pct": _pct(res["total_pl_pct"]),
        "pl_excluded_count": res["pl_excluded_count"],
        "total_day_change": _s(res.get("total_day_change")),
        "total_day_change_pct": _pct(res.get("total_day_change_pct")),
        "day_change_partial": bool(res.get("day_change_partial")),
        "holding_count": len(by_security),
        "priced_count": sum(1 for h in by_security if h["has_price"]),
        "unpriced": res["unpriced"],
        "classes": [_ser_class(c) for c in res["classes"]],
        "holdings": holdings,
        "holdings_by_security": [_ser_holding(h) for h in by_security],
        "crypto_summary": cs_block,
        "warnings": warnings,
        "generated_at": _utcnow_iso(),
    }


# ----------------------------------------------------------------------
# portfolio-history helpers
# ----------------------------------------------------------------------


# 銘柄の集合でフィルタするスコープ（daily_series が自前で絞る）と、
# 銘柄ごとの計上率で重み付けするスコープ（ratio_by_security を組み立てる）
_RATIO_SCOPES = ("tag", "portfolio")


def _parse_scope(scope: str | None) -> tuple[str, str] | None:
    if not scope or scope == "total":
        return None
    for prefix in ("account:", "class:", "security:", "tag:", "portfolio:"):
        if scope.startswith(prefix):
            ref = scope[len(prefix):]
            if ref:
                return (prefix[:-1], ref)
    raise HTTPException(status_code=400, detail=f"不正な scope です: {scope}")


def _range_start(range_key: str, snapshots: list[HoldingSnapshot], end: date) -> date:
    if range_key == "all":
        five_years_ago = end - timedelta(days=365 * 5)
        oldest = min((s.as_of_date for s in snapshots), default=five_years_ago)
        return min(oldest, five_years_ago)
    days = _RANGE_DAYS.get(range_key, 90)
    return end - timedelta(days=days)


def _scope_match(
    snap: HoldingSnapshot,
    sec: Security,
    accounts: dict[int, Any],
    scope: tuple[str, str] | None,
) -> bool:
    if scope is None:
        return True
    kind, ref = scope
    if kind == "account":
        return _acct_display(accounts.get(snap.account_id)) == ref
    if kind == "class":
        return sec.asset_class.value == ref
    if kind == "security":
        return str(snap.security_id) == ref
    return True


# ----------------------------------------------------------------------
# app factory
# ----------------------------------------------------------------------


def _app_path(request: Request) -> str:
    """公開パスから接頭辞を除いた、アプリ内部から見たパス。

    サブパス配信（https://例.com/asset/）ではリクエストのパスに接頭辞が
    付いたまま届く。ルーティングは Starlette が root_path を見て解決するが、
    ミドルウェアが自前でパスを判定する箇所は自分で外す必要がある。
    """
    root = request.scope.get("root_path") or ""
    path = request.url.path
    # 接頭辞そのもの、またはその直下のときだけ剥がす（/assetx を巻き込まない）
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root):]
    return path or "/"


def create_app(db_path: str = "data/assets.db") -> FastAPI:
    store = Store(db_path)

    # MF PDF 受信フォルダの監視スレッド。lifespan で起動・停止する
    # （TestClient を with なしで使うテストでは起動しない）。
    inbox_watcher = InboxWatcher(store, db_path)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # 同じ投信協会ファンドに連携された重複銘柄を起動時に自動統合する。
        # 別名で二重登録された後に両方を同じファンドへ連携済みの既存DBの救済で、
        # 重複が無ければ何もしない（冪等）。以後は連携した瞬間に統合される。
        try:
            startup_warnings: list[str] = []
            for m in _dedupe_linked_funds(store, startup_warnings):
                log.info(
                    "同一ファンドの重複銘柄を自動統合しました: %s ← %s",
                    m["target_name"],
                    "、".join(m["merged_names"]),
                )
            # 連携済み年金の口数逆算（前回起動後に系列が伸びた分の未導出行を救済）
            for d in _derive_pension_units_now(store, startup_warnings):
                log.info(
                    "年金の口数を評価額から逆算しました: %s（%d件）",
                    d["name"], d["lots"],
                )
            for w in startup_warnings:
                log.warning(w)
        except Exception:  # noqa: BLE001 — 統合できなくても起動は止めない
            log.exception("重複銘柄の自動統合に失敗しました")
        inbox_watcher.start()
        try:
            yield
        finally:
            inbox_watcher.stop()

    app = FastAPI(
        title="Asset-Summary",
        docs_url="/api/docs",
        version=_VERSION,
        # サブパス配信の接頭辞（例: /asset）。空ならルート直下。
        root_path=as_auth.root_path(),
        lifespan=_lifespan,
    )
    app.state.store = store
    app.state.db_path = str(db_path)
    app.state.inbox_watcher = inbox_watcher

    # CORS 許可オリジン。既定はローカル開発用（Crypto-Summary の 8000 と自分の 8010）。
    # クラウド等では AS_ALLOWED_ORIGINS（カンマ区切り）で上書きできる。
    _default_origins = (
        "http://127.0.0.1:8000,http://localhost:8000,"
        "http://127.0.0.1:8010,http://localhost:8010"
    )
    allow_origins = [
        o.strip()
        for o in os.environ.get("AS_ALLOWED_ORIGINS", _default_origins).split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)  # 並行作成中でも起動可能に
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ------------------------------------------------------------------
    # 認証（任意有効化のログインゲート — AS_ALLOWED_EMAILS 設定時のみ）
    # ------------------------------------------------------------------
    from .auth import auth_enabled, https_only, router as auth_router, session_secret

    if auth_enabled():
        if not (
            os.environ.get("GOOGLE_CLIENT_ID", "").strip()
            and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        ):
            # 半端な設定でゲートなし公開になるのを防ぐ（fail-closed）
            raise RuntimeError(
                "AS_ALLOWED_EMAILS が設定されていますが GOOGLE_CLIENT_ID / "
                "GOOGLE_CLIENT_SECRET がありません。両方設定してください。"
            )

        # 認証不要で通すパス: 画面の骨組み・静的ファイル・認証フロー・死活監視
        _open_paths = ("/static", "/auth/")

        @app.middleware("http")
        async def _login_gate(request, call_next):  # type: ignore[no-untyped-def]
            p = _app_path(request)
            if (
                request.method == "OPTIONS"
                or p == "/"
                or p == "/api/health"
                or any(p.startswith(prefix) for prefix in _open_paths)
            ):
                return await call_next(request)
            if not request.session.get("user"):
                # ミドルウェアでは HTTPException を投げられないので直接応答
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return await call_next(request)

        # SessionMiddleware は「後から」追加する — Starlette は後に追加した
        # ミドルウェアほど外側で実行するため、これでゲートから request.session が見える。
        from starlette.middleware.sessions import SessionMiddleware

        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret(str(Path(db_path).resolve().parent)),
            https_only=https_only(),
            same_site="lax",
            # CS と同一ホストで併用しても Cookie が衝突しないよう名前を分ける
            # （Cookie はポートを区別しないため localhost 併用時に必要）
            session_cookie="as_session",
        )
        app.include_router(auth_router)
    else:

        @app.get("/auth/me", include_in_schema=False)
        def auth_me_stub() -> dict[str, Any]:
            """認証オフ時もフロントのプローブ先を一本化するためのスタブ。"""
            return {"authenticated": True, "enabled": False}

    @app.middleware("http")
    async def _no_store_static(request, call_next):  # type: ignore[no-untyped-def]
        # パスは call_next の前に読む。StaticFiles の Mount は一致した時点で
        # scope の root_path に自分のマウント先を足すので、後から読むと
        # _app_path が /static ぶんまで剥がしてしまい判定が外れる。
        path = _app_path(request)
        response = await call_next(request)
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------
    # root / meta
    # ------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Any:
        index_html = static_dir / "index.html"
        if not index_html.exists():
            return JSONResponse({"app": "asset-summary"})
        # 静的ファイル・API・認証リンクはすべて相対 URL で書いてあるので、
        # ここで基準を与える。パスだけの base（スキームとホストを書かない）に
        # するのが要点 — TLS 終端プロキシの内側ではリクエストが http に
        # 見えるため、絶対 URL を入れると https のページから http を読みに
        # いって mixed content で止まる。
        base = (request.scope.get("root_path") or "") + "/"
        html = index_html.read_text(encoding="utf-8").replace(
            "<head>", f'<head>\n  <base href="{base}">', 1
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def api_health() -> dict[str, str]:
        """死活監視用（認証不要）。プロセスが応答できることだけを示す。"""
        return {"status": "ok"}

    @app.get("/api/meta")
    def api_meta() -> dict[str, Any]:
        settings = store.get_settings()
        cs_enabled = crypto_summary_client.is_enabled()
        return {
            "app": "asset-summary",
            "version": _VERSION,
            "currencies": list(SUPPORTED_CURRENCIES),
            "crypto_summary": {
                "enabled": cs_enabled,
                "url": crypto_summary_client.public_url() if cs_enabled else None,
            },
            "asset_classes": [
                {
                    "id": cid,
                    "label_ja": meta["ja"],
                    "label_en": meta["en"],
                    "color": meta["color"],
                }
                for cid, meta in ASSET_CLASS_META.items()
            ],
            "settings": {
                # 資産クラスごとの「総資産に含める」（既定はすべて true）
                "include_classes": {
                    cid: settings.get(portfolio.include_setting_key(cid), "1") == "1"
                    for cid in ASSET_CLASS_META
                },
                # 後方互換（旧クライアント向け）
                "include_pension": settings.get("include_pension", "1") == "1",
                "include_points": settings.get("include_points", "1") == "1",
                "default_currency": settings.get("default_currency", "JPY"),
                "merge_cash": portfolio.merge_cash_enabled(settings),
                "dashboard_layout": _dashboard_layout(settings),
                "dashboard_chip_classes": _dashboard_chip_classes(settings),
            },
        }

    # ------------------------------------------------------------------
    # summary / classes / accounts
    # ------------------------------------------------------------------

    @app.get("/api/summary")
    def api_summary(
        request: Request, currency: str | None = Query(None)
    ) -> dict[str, Any]:
        return compute_summary(store, currency or "", user_sub=_cs_user_sub(request))

    @app.get("/api/classes")
    def api_classes(
        request: Request, currency: str | None = Query(None)
    ) -> dict[str, Any]:
        warnings: list[str] = []
        settings = store.get_settings()
        cur = _resolve_currency(currency, settings)
        res, _spot, _fx = _summarize_now(store, cur, warnings)
        cs_block = _apply_cs_merge(
            res, settings, _cs_effective_currency(cur, _fx),
            _cs_user_sub(request), warnings,
        )
        return {
            "currency": cur,
            "classes": [_ser_class(c) for c in res["classes"]],
            "crypto_summary": cs_block,
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    @app.get("/api/class-holdings")
    def api_class_holdings(
        request: Request,
        asset_class: str = Query(..., alias="class"),
        currency: str | None = Query(None),
    ) -> dict[str, Any]:
        meta = ASSET_CLASS_META.get(asset_class)
        if meta is None:
            raise HTTPException(status_code=400, detail=f"不明な資産クラスです: {asset_class}")
        warnings: list[str] = []
        settings = store.get_settings()
        cur = _resolve_currency(currency, settings)
        res, _spot, _fx = _summarize_now(store, cur, warnings)
        cs_block: dict[str, Any] | None = None
        if asset_class == "crypto":
            cs_cur = _cs_effective_currency(cur, _fx)
            cs_block = _apply_cs_merge(
                res, settings, cs_cur, _cs_user_sub(request), warnings,
            )
            # コイン一覧を出す画面なので、ここだけコイン別の前日比も取りに行く
            _fill_cs_asset_day_changes(res, cs_cur, _cs_user_sub(request), warnings)
        rows = [h for h in res["holdings"] if h["asset_class"] == asset_class]
        out = {
            "currency": cur,
            "class": asset_class,
            "label": meta["ja"],
            "holdings": [_ser_holding(h) for h in rows],
            **_totals_block(rows),
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }
        if cs_block is not None:
            out["crypto_summary"] = cs_block
        return out

    @app.get("/api/accounts")
    def api_accounts(currency: str | None = Query(None)) -> dict[str, Any]:
        warnings: list[str] = []
        cur = _resolve_currency(currency, store.get_settings())
        res, _spot, _fx = _summarize_now(store, cur, warnings)
        agg: dict[int, list[dict[str, Any]]] = {}
        for h in res["holdings"]:
            agg.setdefault(h["account_id"], []).append(h)
        total_value = sum(
            (h["value"] for h in res["holdings"]
             if h["value"] is not None and h["in_total"] is not False),
            ZERO,
        )
        rows = []
        for acct in store.list_accounts():
            held = agg.get(acct.id, [])
            block = _totals_block(held)
            value = sum((h["value"] for h in held if h["value"] is not None), ZERO)
            rows.append(
                {
                    "id": acct.id,
                    "name": acct.name,
                    "display_name": _acct_display(acct),
                    "kind": acct.kind,
                    "holding_count": len(held),
                    "value": block["total_value"],
                    "cost": block["total_cost"],
                    "pl": block["total_pl"],
                    "pl_pct": block["total_pl_pct"],
                    "day_change": block["total_day_change"],
                    "day_change_pct": block["total_day_change_pct"],
                    "day_change_partial": block["day_change_partial"],
                    "weight": _pct((value / total_value * 100) if total_value else None),
                }
            )
        rows.sort(key=lambda r: -Decimal(r["value"] or "0"))
        return {
            "currency": cur,
            "accounts": rows,
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    @app.put("/api/accounts/{account_id}")
    def api_account_update(
        account_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        if store.get_account(account_id) is None:
            raise HTTPException(status_code=404, detail="口座が見つかりません")
        fields: dict[str, Any] = {}
        for key in ("display_name", "kind"):
            if key in payload:
                # 型を強制する（非文字列を通すと後続の読み出しで壊れる）
                fields[key] = None if payload[key] is None else str(payload[key])
        if "sort_order" in payload:
            fields["sort_order"] = _to_int(payload["sort_order"], "sort_order")
        if fields:
            store.update_account(account_id, **fields)
        return {"ok": True}

    @app.get("/api/account-holdings")
    def api_account_holdings(
        request: Request,
        account: str = Query(...),
        currency: str | None = Query(None),
    ) -> dict[str, Any]:
        warnings: list[str] = []
        settings = store.get_settings()
        cur = _resolve_currency(currency, settings)
        res, _spot, _fx = _summarize_now(store, cur, warnings)
        # CS 疑似口座のドリルダウン（ダッシュボードの口座一覧から遷移できる）
        if account == crypto_summary_client.CS_ACCOUNT_NAME:
            cs_cur = _cs_effective_currency(cur, _fx)
            _apply_cs_merge(res, settings, cs_cur, _cs_user_sub(request), warnings)
            _fill_cs_asset_day_changes(res, cs_cur, _cs_user_sub(request), warnings)
        rows = [h for h in res["holdings"] if h["account"] == account]
        return {
            "currency": cur,
            "account": account,
            "holdings": [_ser_holding(h) for h in rows],
            **_totals_block(rows),
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    # ------------------------------------------------------------------
    # securities
    # ------------------------------------------------------------------

    @app.get("/api/securities")
    def api_securities(
        asset_class: str | None = Query(None, alias="class"),
        q: str | None = Query(None),
    ) -> dict[str, Any]:
        secs = store.list_securities(asset_class=asset_class, q=q)
        return {"securities": [_ser_security(s) for s in secs]}

    @app.post("/api/securities")
    def api_security_create(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name は必須です")
        asset_class: AssetClass = _enum_or_400(
            AssetClass, payload.get("asset_class"), "asset_class"
        )
        defaults = _CLASS_DEFAULTS.get(asset_class.value, {})
        unit: Unit = _enum_or_400(
            Unit, payload.get("unit") or defaults.get("unit", "share"), "unit"
        )
        divisor_raw = payload.get("price_unit_divisor") or defaults.get(
            "price_unit_divisor", 1
        )
        # 0 や負数を許すと評価・損益計算がゼロ除算で落ちる
        divisor = _to_int(divisor_raw, "price_unit_divisor", minimum=1)
        ps_type: PriceSourceType = _enum_or_400(
            PriceSourceType,
            payload.get("price_source_type") or defaults.get("type", "none"),
            "price_source_type",
        )
        ps_ref = _clean_source_ref(payload.get("price_source_ref"))
        if ps_type == PriceSourceType.MANUAL:
            status = PriceSourceStatus.MANUAL
        elif ps_type != PriceSourceType.NONE and ps_ref:
            status = PriceSourceStatus.LINKED
        else:
            status = PriceSourceStatus(defaults.get("status", "unlinked"))
        sec = Security(
            code=normalize_code(payload.get("code")),
            name=name,
            name_key=make_name_key(name),
            asset_class=asset_class,
            currency=str(payload.get("currency") or "JPY").upper(),
            unit=unit,
            price_unit_divisor=divisor,
            price_source_type=ps_type,
            price_source_ref=ps_ref,
            price_source_status=status,
        )
        try:
            sec_id = store.create_security(sec)
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409, detail=f"証券コード {sec.code} は既に登録されています"
            )
        return {"ok": True, "id": sec_id}

    @app.put("/api/securities/{security_id}")
    def api_security_update(
        security_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        sec = store.get_security(security_id)
        if sec is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        fields: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload["name"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="name を空にはできません")
            fields["name"] = name
            fields["name_key"] = make_name_key(name)
        if "code" in payload:
            fields["code"] = normalize_code(payload["code"])
        if "asset_class" in payload:
            fields["asset_class"] = _enum_or_400(
                AssetClass, payload["asset_class"], "asset_class"
            ).value
        if "currency" in payload:
            fields["currency"] = str(payload["currency"] or "JPY").upper()
        if "unit" in payload:
            fields["unit"] = _enum_or_400(Unit, payload["unit"], "unit").value
        if "price_unit_divisor" in payload:
            fields["price_unit_divisor"] = _to_int(
                payload["price_unit_divisor"], "price_unit_divisor", minimum=1
            )
        if "inactive" in payload:
            fields["inactive"] = bool(payload["inactive"])
        if "price_source_type" in payload:
            fields["price_source_type"] = _enum_or_400(
                PriceSourceType, payload["price_source_type"], "price_source_type"
            ).value
        clear: list[str] = []
        if "price_source_ref" in payload:
            cleaned = _clean_source_ref(payload["price_source_ref"])
            if cleaned is None:
                # 明示的な null は「連携を外す」。fields に None を入れても
                # update_security が「変更しない」と解釈して落としてしまう。
                clear.append("price_source_ref")
            else:
                fields["price_source_ref"] = cleaned
        # 価格ソースを設定したら status を追随（linked / manual / unlinked）
        if "price_source_type" in payload or "price_source_ref" in payload:
            new_type = fields.get("price_source_type", sec.price_source_type.value)
            if "price_source_ref" in fields:
                new_ref = fields["price_source_ref"]
            elif "price_source_ref" in clear:
                new_ref = None
            else:
                new_ref = sec.price_source_ref
            if new_type == "manual":
                fields["price_source_status"] = "manual"
            elif new_type != "none" and new_ref:
                fields["price_source_status"] = "linked"
            else:
                # 現金・ポイント・年金は元々価格ソース不要のクラス。連携の解除で
                # 「未連携」にすると警告バッジの対象になってしまうため既定へ戻す
                new_class = fields.get("asset_class", sec.asset_class.value)
                fields["price_source_status"] = (
                    "not_required"
                    if new_class in ("cash", "point", "pension")
                    else "unlinked"
                )
        store.update_security(security_id, clear=tuple(clear), **fields)

        warnings: list[str] = []
        # 投信協会のファンドへ連携したら、同じファンドに連携済みの別銘柄と自動で
        # 統合する（ref = ISIN:協会コード が同じなら同一ファンド。名前の表記揺れで
        # 二重登録されていても、ここで同一だと確定する）。この銘柄自身が統合で
        # 消えることもある — その場合の履歴取得は統合先に対して行う。
        merged: list[dict[str, Any]] = []
        if "price_source_type" in payload or "price_source_ref" in payload:
            now_sec = store.get_security(security_id)
            if (
                now_sec is not None
                and now_sec.price_source_type == PriceSourceType.TOUSHIN
                and now_sec.price_source_status == PriceSourceStatus.LINKED
            ):
                merged = _dedupe_linked_funds(store, warnings)

        # 価格ソースを新たに紐づけたら、その場で履歴を取得しておく。
        # 特に投信(toushin)は「日次系列の最新値＝現在値」のため、ここで取得して
        # おかないとサマリー画面では参考値のままになる（履歴APIを開くまで解決しない）。
        if fields.get("price_source_status") == "linked":
            updated = store.get_security(security_id)
            if updated is None:
                updated = next(
                    (
                        store.get_security(m["target_id"])
                        for m in merged
                        if security_id in m["merged_ids"]
                    ),
                    None,
                )
            if updated is not None:
                try:
                    today = date.today()
                    ensure_price_history(
                        store, [updated], today - timedelta(days=365 * 5), today,
                        warnings.append,
                    )
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"価格履歴の取得に失敗しました: {e}")
        # 年金銘柄を基準価額へ連携した場合は、評価額から口数を逆算して以後は
        # 口数×基準価額の自動評価に切り替える（対象が無ければ何もしない）。
        # 履歴取得の後に呼ぶ — 逆算には基準価額の系列が要る
        pension_units: list[dict[str, Any]] = []
        if (
            "price_source_type" in payload
            or "price_source_ref" in payload
            or "asset_class" in payload
        ):
            pension_units = _derive_pension_units_now(store, warnings)
        return {
            "ok": True,
            "warnings": warnings,
            "merged": merged,
            "pension_units": pension_units,
        }

    @app.delete("/api/securities/{security_id}")
    def api_security_delete(security_id: int) -> dict[str, Any]:
        if store.get_security(security_id) is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        try:
            store.delete_security(security_id)
        except ConflictError as e:
            raise HTTPException(
                status_code=409,
                detail=f"スナップショットが存在するため削除できません（{e}）",
            )
        return {"ok": True}

    @app.post("/api/securities/{security_id}/merge")
    def api_security_merge(
        security_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """payload の source_id を security_id（統合先）へ統合する。

        MF PDF が証券会社ごとの表記で同じファンドを別銘柄として作ってしまった
        ときの名寄せ。source は消え、その名前は alias として統合先に残る。
        """
        source_id = _to_int(payload.get("source_id"), "source_id", minimum=1)
        if source_id == security_id:
            raise HTTPException(status_code=400, detail="統合元と統合先が同じ銘柄です")
        if store.get_security(security_id) is None:
            raise HTTPException(status_code=404, detail="統合先の銘柄が見つかりません")
        if store.get_security(source_id) is None:
            raise HTTPException(status_code=404, detail="統合元の銘柄が見つかりません")
        try:
            result = store.merge_security(source_id, security_id)
        except ConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))

        warnings: list[str] = []
        # 取引が移ったら原価を作り直す（holding_cost_basis は派生値なので冪等）
        if result["transactions"]:
            try:
                warnings.extend(recompute_cost_basis(store).get("warnings", []))
            except Exception as e:  # noqa: BLE001 — 統合自体は完了している
                warnings.append(f"取得原価の再計算に失敗しました: {e}")
        # source から価格ソースを引き継いだ場合は履歴もその場で取得しておく
        # （PUT /api/securities と同じ理由: 投信は日次系列の最新値が現在値）
        if result.get("adopted_price_source"):
            merged_sec = store.get_security(security_id)
            if merged_sec is not None and (
                merged_sec.price_source_status == PriceSourceStatus.LINKED
            ):
                try:
                    today = date.today()
                    ensure_price_history(
                        store, [merged_sec], today - timedelta(days=365 * 5), today,
                        warnings.append,
                    )
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"価格履歴の取得に失敗しました: {e}")
        return {"ok": True, **result, "warnings": warnings}

    # ------------------------------------------------------------------
    # holdings (raw lots)
    # ------------------------------------------------------------------

    @app.get("/api/holdings")
    def api_holdings(
        account_id: int | None = Query(None), security_id: int | None = Query(None)
    ) -> dict[str, Any]:
        secs = store.securities_by_id()
        accounts = {a.id: a for a in store.list_accounts()}
        rows = []
        for lot in store.current_holdings():
            if account_id is not None and lot.account_id != account_id:
                continue
            if security_id is not None and lot.security_id != security_id:
                continue
            sec = secs.get(lot.security_id)
            rows.append(
                {
                    "id": lot.id,
                    "account_id": lot.account_id,
                    "account": _acct_display(accounts.get(lot.account_id)),
                    "security_id": lot.security_id,
                    "name": sec.name if sec else "",
                    "lot_seq": lot.lot_seq,
                    "as_of": lot.as_of_date.isoformat(),
                    "quantity": _s(lot.quantity),
                    "avg_cost": _s(lot.avg_cost),
                    "lot_label": lot.lot_label,
                    "origin": lot.origin,
                }
            )
        return {"holdings": rows}

    @app.post("/api/holdings")
    def api_holding_create(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if payload.get("security_id") is None:
            raise HTTPException(status_code=400, detail="security_id は必須です")
        sec_id = _to_int(payload["security_id"], "security_id", minimum=1)
        if store.get_security(sec_id) is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        if payload.get("account_id") is not None:
            acct = store.get_account(_to_int(payload["account_id"], "account_id", minimum=1))
            if acct is None:
                raise HTTPException(status_code=404, detail="口座が見つかりません")
        elif payload.get("account_name"):
            acct = store.get_or_create_account(
                str(payload["account_name"]).strip(), kind="manual", origin="manual"
            )
        else:
            raise HTTPException(
                status_code=400, detail="account_id か account_name のいずれかが必要です"
            )
        quantity = _to_decimal(payload.get("quantity"), "quantity")
        avg_cost = _to_decimal_opt(payload.get("avg_cost"), "avg_cost")
        as_of = (
            _to_date(payload["as_of"], "as_of")
            if payload.get("as_of")
            else date.today()
        )
        lot_seq = _to_int(payload.get("lot_seq") or 0, "lot_seq", minimum=0)
        snap = HoldingSnapshot(
            account_id=acct.id,
            security_id=sec_id,
            lot_seq=lot_seq,
            as_of_date=as_of,
            quantity=quantity,
            avg_cost=avg_cost,
            lot_label=payload.get("lot_label"),
            origin="manual",
        )
        store.upsert_snapshot(snap)
        return {"ok": True}

    @app.delete("/api/holdings/{snapshot_id}")
    def api_holding_delete(snapshot_id: int) -> dict[str, Any]:
        if store.get_snapshot(snapshot_id) is None:
            raise HTTPException(status_code=404, detail="スナップショットが見つかりません")
        store.delete_snapshot(snapshot_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # 不動産価格指数
    # ------------------------------------------------------------------

    @app.get("/api/re-index/options")
    def api_re_index_options() -> dict[str, Any]:
        """指数の選択肢と、手元に取り込めている最新月。

        語彙はパーサの隣（core.re_index）を単一の真実にする。画面側に地域名を
        べた書きすると、公表側の収録範囲が変わったときに二重管理になる。
        """
        as_of = price_store.latest_price_date(
            store, "re_index", re_index.index_source_id("zenkoku", "residential")
        )
        return {
            "regions": [
                {"code": code, "label": label}
                for code, label in re_index.REGIONS.items()
            ],
            "types": [
                {"code": code, "label": label}
                for code, label in re_index.INDEX_TYPES.items()
            ],
            "ref_prefix": re_index.REF_PREFIX,
            "as_of": as_of.isoformat() if as_of else None,
            "attribution": RE_INDEX_ATTRIBUTION,
            "source_url": re_index_provider.LANDING_URL,
        }

    # ------------------------------------------------------------------
    # manual prices（不動産評価・未リンク投信の手動基準価額など）
    # ------------------------------------------------------------------

    @app.post("/api/securities/{security_id}/manual-price")
    def api_manual_price_set(
        security_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        sec = store.get_security(security_id)
        if sec is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        day = _to_date(payload.get("date"), "date")
        value = _to_decimal(payload.get("value"), "value")
        store.upsert_daily_price(
            "manual", str(security_id), day.isoformat(), value, currency=sec.currency
        )
        # 価格ソース未設定の銘柄に手動評価額を入れたら、それを価格源として扱う。
        # そうしないと登録しても評価額に反映されない（不動産の評価履歴など）。
        if sec.price_source_status == PriceSourceStatus.UNLINKED:
            store.update_security(
                security_id,
                price_source_type=PriceSourceType.MANUAL.value,
                price_source_status=PriceSourceStatus.MANUAL.value,
            )
        return {"ok": True}

    @app.get("/api/securities/{security_id}/manual-prices")
    def api_manual_price_list(security_id: int) -> dict[str, Any]:
        if store.get_security(security_id) is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        series, _ccy = store.get_price_rows("manual", str(security_id))
        return {
            "prices": [
                {"date": d, "value": str(v)} for d, v in sorted(series.items())
            ]
        }

    @app.delete("/api/securities/{security_id}/manual-prices/{day}")
    def api_manual_price_delete(security_id: int, day: str) -> dict[str, Any]:
        parsed = _to_date(day, "date")
        store.delete_price_row("manual", str(security_id), parsed.isoformat())
        # 手動評価額が全て消えたら「未設定」に戻す（価格源として機能しないため）
        sec = store.get_security(security_id)
        if sec is not None and sec.price_source_status == PriceSourceStatus.MANUAL:
            remaining, _ = store.get_price_rows("manual", str(security_id))
            if not remaining:
                store.update_security(
                    security_id,
                    price_source_type=PriceSourceType.NONE.value,
                    price_source_status=PriceSourceStatus.UNLINKED.value,
                )
        return {"ok": True}

    # ------------------------------------------------------------------
    # portfolio history
    # ------------------------------------------------------------------

    def _virtual_tag_portfolio(tag: dict[str, Any]) -> dict[str, Any]:
        """タグ1つを「そのタグだけを対象にしたポートフォリオ」として扱う定義。

        タグ別サマリーの金額・ドリルダウンの合計・推移グラフが必ず一致するよう、
        3箇所ともこの1つの定義を使う。
        """
        return {
            "id": None,
            "name": tag["name"],
            "note": None,
            "tag_ids": [tag["id"]],
            "include_security_ids": [],
            "exclude_security_ids": [],
        }

    def _scope_portfolio(scope_t: tuple[str, str]) -> dict[str, Any]:
        """scope=tag:<id> / portfolio:<id> を計上率の判定に使える定義へ解決する。"""
        kind, ref = scope_t
        if kind == "tag":
            tag = store.get_tag(_to_int(ref, "tag"))
            if tag is None:
                raise HTTPException(status_code=404, detail="タグが見つかりません")
            return _virtual_tag_portfolio(tag)
        p = store.get_portfolio(_to_int(ref, "portfolio"))
        if p is None:
            raise HTTPException(status_code=404, detail="ポートフォリオが見つかりません")
        return p

    @app.get("/api/portfolio-history")
    def api_portfolio_history(
        request: Request,
        currency: str | None = Query(None),
        range_key: str = Query("90d", alias="range"),
        scope: str = Query("total"),
    ) -> dict[str, Any]:
        warnings: list[str] = []
        settings = store.get_settings()
        cur = _resolve_currency(currency, settings)
        scope_t = _parse_scope(scope)

        snapshots = store.all_snapshots()
        secs = store.securities_by_id()
        # タグ・Myポートフォリオは「銘柄の集合」ではなく「銘柄ごとの計上率」で
        # 決まるので、daily_series には scope ではなく重みを渡す
        ratio_by_security: dict[Any, Decimal] | None = None
        scope_portfolio: dict[str, Any] | None = None
        series_scope = scope_t
        if scope_t is not None and scope_t[0] in _RATIO_SCOPES:
            scope_portfolio = _scope_portfolio(scope_t)
            scope_tag_map = _tag_map_all()
            ratio_by_security = {}
            for sid in secs:
                ratio = tagging.portfolio_ratio(
                    sid, scope_tag_map.get(sid, {}), scope_portfolio
                )
                if ratio > ZERO:
                    ratio_by_security[sid] = ratio
            series_scope = None
        accounts = {a.id: a for a in store.list_accounts()}
        end = date.today()
        start = _range_start(range_key, snapshots, end)

        linked = [
            s
            for s in secs.values()
            if s.price_source_status == PriceSourceStatus.LINKED
        ]
        if linked:
            try:
                ensure_price_history(store, linked, start, end, warn=warnings.append)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"価格取得エラー: {e}")
        try:
            ensure_re_index_history(store, secs.values(), end, warn=warnings.append)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"価格取得エラー: {e}")
        ccys = {s.currency for s in secs.values() if s.currency != "JPY"}
        if cur != "JPY":
            ccys.add(cur)
        if ccys:
            try:
                ensure_fx_history(store, sorted(ccys), start, end, warn=warnings.append)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"価格取得エラー: {e}")

        # start は制限しない（初回登録日以前への遡及バックフィルに過去価格を使う）
        price_series: dict[int, tuple[dict[str, Decimal], str]] = {}
        for sid, sec in secs.items():
            price_series[sid] = store.price_series_for_security(
                sec, start=None, end=end.isoformat()
            )
        fx_series = {c: store.get_price_rows("fx", c)[0] for c in ccys}

        jpy_disp_series: dict[str, Decimal] | None = None
        if cur != "JPY":
            jpy_disp_series = fx_series.get(cur) or {}
            if not jpy_disp_series:
                warnings.append(
                    f"為替履歴が取得できないため {cur} 表示は円ベースのままです"
                )
                jpy_disp_series = None

        points = daily_series(
            snapshots,
            secs,
            accounts,
            price_series,
            fx_series,
            start,
            end,
            settings,
            jpy_per_display_series=jpy_disp_series,
            scope=series_scope,
            ratio_by_security=ratio_by_security,
        )

        # 価格が無い保有を2つに分ける。is_partial は「取得中＝待てば出る」という
        # 一時性の主張なので、ユーザーの入力待ち（手動評価）をそこへ混ぜない。
        # 混ぜると不動産を登録した時点で「⏳ 価格データ取得中」が恒久的に出続ける。
        unpriced_names: set[str] = set()
        needs_valuation: set[str] = set()
        for lot in store.current_holdings():
            sec = secs.get(lot.security_id)
            if sec is None or lot.quantity == ZERO:
                continue
            if sec.asset_class in (
                AssetClass.CASH,
                AssetClass.POINT,
                AssetClass.PENSION,
            ):
                continue
            if ratio_by_security is not None:
                if not ratio_by_security.get(lot.security_id):
                    continue
            elif not _scope_match(lot, sec, accounts, scope_t):
                continue
            if not price_series.get(lot.security_id, ({}, ""))[0]:
                if sec.price_source_status == PriceSourceStatus.MANUAL:
                    needs_valuation.add(sec.name)
                else:
                    unpriced_names.add(sec.name)

        # Crypto-Summary 分の合算。CS の全ポートフォリオ = AS の暗号資産クラス
        # なので、total / class:crypto / account:<CS疑似口座> の3スコープが
        # いずれも CS 側の scope=total に対応する。
        range_norm = range_key if range_key in (*_RANGE_DAYS, "all") else "90d"
        crypto_included = settings.get(portfolio.include_setting_key("crypto"), "1") == "1"
        cs_is_partial = False
        # FX フォールバック中（表示通貨レート無し→円ベースの点列）は
        # CS 履歴も JPY で取得して通貨を揃える
        cs_cur = "JPY" if (cur != "JPY" and jpy_disp_series is None) else cur
        if crypto_summary_client.is_enabled() and (
            (scope_t is None and crypto_included)
            or scope_t == ("class", "crypto")
            or scope_t == ("account", crypto_summary_client.CS_ACCOUNT_NAME)
        ):
            cs_hist = fetch_cs_history(
                cs_cur, range_norm, "total", _cs_user_sub(request), warn=warnings.append
            )
            if cs_hist:
                merge_cs_history(points, cs_hist.get("points"))
                # is_partial は「系列がまだ埋まりきっていない」合図なので残す。
                # 価格の付かないコインの名前は AS では扱わないため取り込まない。
                cs_is_partial = bool(cs_hist.get("is_partial"))
        elif (
            crypto_summary_client.is_enabled()
            and ratio_by_security is not None
            and crypto_included
        ):
            # タグ・Myポートフォリオは CS の total では代用できない（コインごとに
            # 配分が違う）。そのタグに入っているコインだけ履歴を引いて按分する。
            sub = _cs_user_sub(request)
            cs_ratios: dict[str, Decimal] = {}
            for asset_key, symbol in _cs_asset_keys(request):
                ratio = tagging.portfolio_ratio(
                    asset_key, scope_tag_map.get(asset_key, {}), scope_portfolio
                )
                if ratio > ZERO:
                    cs_ratios[symbol] = ratio
            if cs_ratios:
                hists = crypto_summary_client.fetch_cs_asset_histories(
                    cs_ratios, cs_cur, range_norm, sub,
                    fetch=fetch_cs_history, warn=warnings.append,
                )
                for symbol, ratio in cs_ratios.items():
                    hist = hists.get(symbol)
                    if hist is None:
                        cs_is_partial = True
                        continue
                    merge_cs_history(points, hist.get("points"), ratio)
                    cs_is_partial = cs_is_partial or bool(hist.get("is_partial"))

        return {
            "currency": cur,
            "range": range_norm,
            "scope": scope,
            "points": [
                {"t": p["t"], "value": _s(p["value"]), "cost": _s(p["cost"])}
                for p in points
            ],
            "unpriced": sorted(unpriced_names),
            "needs_valuation": sorted(needs_valuation),
            "is_partial": bool(unpriced_names) or cs_is_partial,
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    # ------------------------------------------------------------------
    # security detail
    # ------------------------------------------------------------------

    @app.get("/api/security/{security_id}")
    def api_security_detail(
        security_id: int,
        currency: str | None = Query(None),
        range_key: str = Query("90d", alias="range"),
    ) -> dict[str, Any]:
        sec = store.get_security(security_id)
        if sec is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        warnings: list[str] = []
        cur = _resolve_currency(currency, store.get_settings())
        res, spot, _fx = _summarize_now(store, cur, warnings)
        rows = [h for h in res["holdings"] if h["id"] == security_id]

        # 口座横断の合算は保有一覧と同じヘルパで行う（損益の突き合わせ規約を揃える）。
        # 銘柄詳細は常に全口座の合算を見せるページなので、預金・ポイントでも
        # 口座別に分けない（merge_all）。内訳は下の accounts 表に出る。
        merged = aggregate_by_security(rows, merge_all=True)
        agg = merged[0] if merged else None
        tiles = {
            "quantity": _s(agg["quantity"]) if agg else _s(ZERO),
            "avg_cost": _s(agg["avg_cost"]) if agg else None,
            "price": _s(spot.get(security_id)),
            "value": _s(agg["value"]) if agg else _s(ZERO),
            "pl": _s(agg["pl"]) if agg else None,
            "pl_pct": _pct(agg["pl_pct"]) if agg else None,
            "day_change": _s(agg["day_change"]) if agg else None,
            "day_change_pct": _pct(agg["day_change_pct"]) if agg else None,
            "day_change_as_of": agg.get("day_change_as_of") if agg else None,
            "estimated": bool(agg.get("estimated")) if agg else False,
        }
        account_rows = [_ser_account_ref(a) for a in (agg["accounts"] if agg else [])]

        accounts = {a.id: a for a in store.list_accounts()}
        basis_by_account = {
            b["account_id"]: b
            for b in store.list_cost_basis(security_id=security_id)
        }
        lot_rows = []
        for lot in store.current_holdings():
            if lot.security_id != security_id:
                continue
            basis = basis_by_account.get(lot.account_id)
            # MF PDF は取得日を取り込んで raw に入れているので、取引履歴が
            # 無い銘柄でもここから拾える（これまで誰も読んでいなかった）。
            acquired = (basis or {}).get("acquired_on") or _mf_acquired_on(lot)
            lot_rows.append(
                {
                    "account": _acct_display(accounts.get(lot.account_id)),
                    "lot_seq": lot.lot_seq,
                    "lot_label": lot.lot_label,
                    "quantity": _s(lot.quantity),
                    "avg_cost": _s(lot.avg_cost),
                    "as_of": lot.as_of_date.isoformat(),
                    "acquired_on": acquired,
                    "coverage": (basis or {}).get("coverage"),
                }
            )

        cost_basis = [
            {**b, "account": _acct_display(accounts.get(b["account_id"]))}
            for b in basis_by_account.values()
        ]
        tx_count = store.count_transactions(security_id=security_id)

        end = date.today()
        start = _range_start(range_key, store.all_snapshots(), end)
        if sec.price_source_status == PriceSourceStatus.LINKED:
            try:
                ensure_price_history(store, [sec], start, end, warn=warnings.append)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"価格取得エラー: {e}")
        try:
            ensure_re_index_history(store, [sec], end, warn=warnings.append)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"価格取得エラー: {e}")
        series, _ccy = store.price_series_for_security(
            sec, start=None, end=end.isoformat()
        )
        start_iso = start.isoformat()
        # 指数で延長した区間（最終査定日より後）に印を付け、チャートで破線にする
        estimated_after = None
        if re_index.parse_ref(sec.price_source_ref):
            latest_anchor = store.get_latest_price("manual", str(security_id))
            if latest_anchor is not None:
                estimated_after = latest_anchor[0]
        price_history = [
            {
                "t": d,
                "price": str(p),
                "estimated": estimated_after is not None and d > estimated_after,
            }
            for d, p in sorted(series.items())
            if d >= start_iso
        ]

        return {
            "currency": cur,
            "range": range_key if range_key in (*_RANGE_DAYS, "all") else "90d",
            "security": _ser_security(sec),
            "tiles": tiles,
            "accounts": account_rows,
            "lots": lot_rows,
            "cost_basis": cost_basis,
            "lot_events": cost_basis_events(store, security_id) if tx_count else [],
            "transaction_count": tx_count,
            "price_history": price_history,
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    # ------------------------------------------------------------------
    # import
    # ------------------------------------------------------------------

    @app.post("/api/import/pdf")
    def api_import_pdf(payload: dict[str, Any] = Body(...)) -> Any:
        filename = str(payload.get("filename") or "upload.pdf")
        raw_b64 = payload.get("content_b64")
        if not raw_b64:
            raise HTTPException(status_code=400, detail="content_b64 は必須です")
        try:
            pdf_bytes = base64.b64decode(raw_b64)
        except Exception:
            raise HTTPException(status_code=400, detail="content_b64 が base64 として不正です")
        if not pdf_bytes:
            raise HTTPException(status_code=400, detail="PDFデータが空です")
        try:
            preview = build_preview(store, pdf_bytes, filename)
        except DuplicateImportError as e:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "同じPDFは既に取込済みです",
                    "existing_batch_id": e.existing_batch_id,
                },
            )
        except Exception as e:  # noqa: BLE001  PDFでない・壊れている等は利用者側の入力誤り
            raise HTTPException(
                status_code=400,
                detail=f"PDFを解析できませんでした。マネーフォワードMEの資産内訳ページを"
                f"印刷保存したPDFか確認してください（{type(e).__name__}）",
            )
        return {**preview, "ok": True}

    @app.post("/api/import/{batch_id}/commit")
    def api_import_commit(
        batch_id: str, payload: dict[str, Any] = Body(...)
    ) -> Any:
        as_of = _to_date(payload.get("as_of"), "as_of")
        try:
            result = commit_batch(
                store,
                batch_id,
                as_of_date=as_of,
                include_sections=payload.get("include_sections"),
                exclude_keys=payload.get("exclude_keys"),
                zero_keys=payload.get("zero_keys"),
                include_crypto=bool(payload.get("include_crypto", False)),
            )
        except DuplicateImportError as e:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "同じPDFは既に取込済みです",
                    "existing_batch_id": e.existing_batch_id,
                },
            )
        except StoreError as e:
            # バッチ自体が無ければ 404、あるが確定済み等でコミットできないなら 409
            status = 404 if store.get_batch(batch_id) is None else 409
            raise HTTPException(status_code=status, detail=str(e))
        # 新しくできた投信は、その場で投信協会へ照会して連携を試みる。
        # 表記揺れで別銘柄になったものを ISIN で元の銘柄へ寄せるため。
        warnings: list[str] = []
        autolink = _autolink_new_funds(
            store, result.get("new_security_ids") or [], warnings
        )
        return {
            "ok": True,
            "created": result.get("created", 0),
            "updated": result.get("updated", 0),
            "zeroed": result.get("zeroed", 0),
            "snapshot_date": as_of.isoformat(),
            "autolink": autolink,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 取引履歴の取込（証券会社CSV / Excel / 貼り付け）
    #
    # 書式は自動判定するが、判定結果は必ずプレビューで見せて直せるようにする。
    # ヒューリスティックの当たり外れをそのまま確定させない、というのが要点。
    # ------------------------------------------------------------------

    @app.post("/api/import/table")
    def api_import_table(payload: dict[str, Any] = Body(...)) -> Any:
        filename = str(payload.get("filename") or "upload.csv")
        text = payload.get("text")
        raw_b64 = payload.get("content_b64")
        data: bytes | None = None
        if raw_b64:
            try:
                data = base64.b64decode(raw_b64)
            except Exception:
                raise HTTPException(status_code=400, detail="content_b64 が base64 として不正です")
        elif not text:
            raise HTTPException(
                status_code=400, detail="content_b64 か text のどちらかが必要です"
            )
        if data is not None and not data:
            raise HTTPException(status_code=400, detail="ファイルが空です")
        try:
            preview = build_tx_preview(
                store, data, filename,
                text=text if data is None else None,
                account_name=payload.get("account_name"),
                sheet=payload.get("sheet"),
            )
        except DuplicateImportError as e:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "同じファイルは既に取込済みです",
                    "existing_batch_id": e.existing_batch_id,
                },
            )
        except Exception as e:  # noqa: BLE001  壊れたファイルは利用者側の入力誤り
            raise HTTPException(
                status_code=400,
                detail=f"ファイルを解析できませんでした（{type(e).__name__}）",
            )
        return {**preview, "generated_at": _utcnow_iso()}

    @app.post("/api/import/table/{batch_id}/remap")
    def api_import_table_remap(
        batch_id: str, payload: dict[str, Any] = Body(...)
    ) -> Any:
        try:
            result = remap_tx_preview(
                store, batch_id,
                column_overrides=payload.get("column_overrides"),
                security_map=payload.get("security_map"),
                account_name=payload.get("account_name"),
            )
        except StoreError as e:
            status = 404 if store.get_batch(batch_id) is None else 409
            raise HTTPException(status_code=status, detail=str(e))
        return {**result, "generated_at": _utcnow_iso()}

    @app.post("/api/import/table/{batch_id}/commit")
    def api_import_table_commit(
        batch_id: str, payload: dict[str, Any] = Body(...)
    ) -> Any:
        try:
            result = commit_tx_batch(
                store, batch_id,
                account_name=payload.get("account_name"),
                include_keys=payload.get("include_keys"),
                exclude_keys=payload.get("exclude_keys"),
                security_map=payload.get("security_map"),
                new_securities=payload.get("new_securities"),
                type_overrides=payload.get("type_overrides"),
                apply_cost_basis=bool(payload.get("apply_cost_basis", True)),
            )
        except DuplicateImportError as e:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "同じファイルは既に取込済みです",
                    "existing_batch_id": e.existing_batch_id,
                },
            )
        except StoreError as e:
            status = 404 if store.get_batch(batch_id) is None else 409
            raise HTTPException(status_code=status, detail=str(e))
        return {**result, "generated_at": _utcnow_iso()}

    @app.get("/api/securities/{security_id}/transactions")
    def api_security_transactions(
        security_id: int,
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        if store.get_security(security_id) is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        accounts = {a.id: a for a in store.list_accounts()}
        txs = store.list_transactions(
            security_id=security_id, limit=limit, offset=offset
        )
        return {
            "transactions": [_ser_transaction(t, accounts) for t in txs],
            "total": store.count_transactions(security_id=security_id),
            "warnings": [],
            "generated_at": _utcnow_iso(),
        }

    @app.get("/api/cost-basis")
    def api_cost_basis(
        security_id: int | None = Query(None), account_id: int | None = Query(None)
    ) -> dict[str, Any]:
        return {
            "groups": store.list_cost_basis(
                security_id=security_id, account_id=account_id
            ),
            "warnings": [],
            "generated_at": _utcnow_iso(),
        }

    @app.post("/api/cost-basis/recompute")
    def api_cost_basis_recompute() -> dict[str, Any]:
        result = recompute_cost_basis(store)
        return {"ok": True, **result, "generated_at": _utcnow_iso()}

    @app.get("/api/import/history")
    def api_import_history() -> dict[str, Any]:
        return {"imports": store.list_batches()}

    @app.get("/api/import/inbox")
    def api_import_inbox_status() -> dict[str, Any]:
        return inbox_watcher.status()

    @app.post("/api/import/inbox/scan")
    def api_import_inbox_scan() -> dict[str, Any]:
        """受信フォルダを今すぐスキャンする（安定待ちも省略する）。"""
        if not inbox_watcher.enabled:
            raise HTTPException(status_code=409, detail="受信フォルダは無効です")
        events = inbox_watcher.scan_once(force=True)
        return {"ok": True, "new_events": events, **inbox_watcher.status()}

    @app.delete("/api/import/batches/{batch_id}")
    def api_import_batch_delete(batch_id: str) -> dict[str, Any]:
        if store.get_batch(batch_id) is None:
            raise HTTPException(status_code=404, detail="バッチが見つかりません")
        deleted = store.delete_batch(batch_id)
        return {"ok": True, "deleted": deleted}

    # ------------------------------------------------------------------
    # fund search / prices / settings
    # ------------------------------------------------------------------

    @app.get("/api/coin-search")
    def api_coin_search(q: str = Query("")) -> dict[str, Any]:
        """暗号資産の銘柄検索（CoinGecko）。ref はコインID。"""
        warnings: list[str] = []
        results: list[dict[str, Any]] = []
        if q.strip():
            try:
                results = search_coins(q.strip(), warn=warnings.append)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"暗号資産検索エラー: {e}")
        return {"results": results, "warnings": warnings}

    @app.get("/api/fund-search")
    def api_fund_search(q: str = Query("")) -> dict[str, Any]:
        warnings: list[str] = []
        results: list[dict[str, Any]] = []
        if q.strip():
            try:
                results = search_funds(q.strip(), warn=warnings.append)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"投信検索エラー: {e}")
        return {"results": results, "warnings": warnings}

    # ------------------------------------------------------------------
    # Crypto-Summary 連携プロキシ
    # （フロントは CS を直接呼ばない — CORS 不要・トークンをブラウザへ出さない）
    # ------------------------------------------------------------------

    @app.get("/api/crypto-summary/status")
    def api_cs_status(
        request: Request, currency: str | None = Query(None)
    ) -> dict[str, Any]:
        """接続状態（管理タブ・設定画面のカード用）。常に 200。"""
        cur = _resolve_currency(currency, store.get_settings())
        enabled = crypto_summary_client.is_enabled()
        out: dict[str, Any] = {
            "configured": enabled,
            "connected": None,
            "url": crypto_summary_client.public_url() if enabled else None,
            "currency": cur,
            "asset_count": None,
            "total_value": None,
            "cs_generated_at": None,
            "warnings": [],
            "generated_at": _utcnow_iso(),
        }
        if not enabled:
            return out
        warnings: list[str] = []
        cs = fetch_cs_summary(cur, _cs_user_sub(request), warn=warnings.append)
        out["warnings"] = warnings
        out["connected"] = cs is not None
        if cs:
            out["asset_count"] = cs.get("asset_count")
            out["total_value"] = cs.get("total_value")
            out["cs_generated_at"] = cs.get("generated_at")
        return out

    @app.get("/api/crypto-summary/asset/{sym}")
    def api_cs_asset(
        request: Request,
        sym: str,
        currency: str | None = Query(None),
        range_key: str = Query("90d", alias="range"),
    ) -> dict[str, Any]:
        """CS 由来1コインの詳細（口座別内訳 + 履歴）をひとまとめで返す。"""
        if not crypto_summary_client.is_enabled():
            raise HTTPException(
                status_code=404, detail="Crypto-Summary 連携が設定されていません"
            )
        warnings: list[str] = []
        cur = _resolve_currency(currency, store.get_settings())
        sub = _cs_user_sub(request)
        sym = sym.strip().upper()
        acc = fetch_cs_asset_accounts(sym, cur, sub, warn=warnings.append)
        range_norm = range_key if range_key in (*_RANGE_DAYS, "all") else "90d"
        hist = fetch_cs_history(
            cur, range_norm, f"asset:{sym}", sub, warn=warnings.append
        )
        # 前日比は /api/summary の前日値から作る（同じコインの行が保有テーブルにも
        # 出るので、そちらと同じ数字にする必要がある）。summary は TTL キャッシュ
        # 済みで追加の往復にならない。前日値を返さない旧 CS 相手のときだけ、
        # ここで既に取っている履歴の末尾から求めるフォールバックへ回る。
        day_change = day_change_pct = None
        cs = fetch_cs_summary(cur, sub, warn=warnings.append)
        row = crypto_summary_client.cs_asset_from_summary(cs, sym)
        if row is not None:
            day_change = row["day_change"]
            day_change_pct = row["day_change_pct"]
        if day_change is None:
            points = (hist or {}).get("points") or []
            _prev, day_change, day_change_pct = crypto_summary_client.day_change_from_points(
                points, (acc or {}).get("total_value")
            )
        return {
            "currency": cur,
            "asset": sym,
            "price": (acc or {}).get("price"),
            "balance": (acc or {}).get("total_balance"),
            "value": (acc or {}).get("total_value"),
            "day_change": _s(day_change),
            "day_change_pct": _pct(day_change_pct),
            "accounts": (acc or {}).get("accounts") or [],
            "history": {
                "points": (hist or {}).get("points") or [],
                "is_partial": bool((hist or {}).get("is_partial")),
            },
            "range": range_norm,
            "connected": acc is not None or hist is not None,
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    @app.get("/api/crypto-summary/coin-icons")
    def api_cs_coin_icons() -> dict[str, Any]:
        """CS のコインアイコン URL マップのパススルー（失敗時は空）。"""
        return fetch_cs_coin_icons() or {}

    # ------------------------------------------------------------------
    # タグ / Myポートフォリオ
    # ------------------------------------------------------------------

    def _tag_weights_payload(allocs: Any) -> dict[int, Decimal]:
        if not isinstance(allocs, list):
            raise HTTPException(status_code=400, detail="allocations を配列で指定してください")
        out: dict[int, Decimal] = {}
        total = Decimal("0")
        for a in allocs:
            if not isinstance(a, dict):
                raise HTTPException(status_code=400, detail="allocations の形式が不正です")
            tag_id = _to_int(a.get("tag_id"), "tag_id", minimum=1)
            weight = _to_decimal(a.get("weight", 100), "weight")
            if weight < 0 or weight > 100:
                raise HTTPException(
                    status_code=400, detail="weight は 0〜100 で指定してください"
                )
            if store.get_tag(tag_id) is None:
                raise HTTPException(status_code=404, detail=f"タグが見つかりません: {tag_id}")
            out[tag_id] = weight
            total += weight
        if total > Decimal("100.0001"):
            raise HTTPException(
                status_code=400,
                detail=f"配分の合計が100%を超えています（{total}%）",
            )
        return out

    @app.get("/api/tags")
    def api_tags_list() -> dict[str, Any]:
        tags = store.list_tags()
        for t in tags:
            t["security_count"] = store.tag_usage_count(t["id"])
        return {"tags": tags}

    @app.post("/api/tags")
    def api_tag_create(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name は必須です")
        color = payload.get("color")
        try:
            tag_id = store.create_tag(name, str(color) if color else None)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"同名のタグが既にあります: {name}")
        return {"ok": True, "id": tag_id}

    @app.put("/api/tags/{tag_id}")
    def api_tag_update(tag_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if store.get_tag(tag_id) is None:
            raise HTTPException(status_code=404, detail="タグが見つかりません")
        fields: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload["name"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="name を空にはできません")
            fields["name"] = name
        if "color" in payload:
            fields["color"] = str(payload["color"]) if payload["color"] else None
        if "sort_order" in payload:
            fields["sort_order"] = _to_int(payload["sort_order"], "sort_order")
        try:
            store.update_tag(tag_id, **fields)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="同名のタグが既にあります")
        return {"ok": True}

    @app.delete("/api/tags/{tag_id}")
    def api_tag_delete(tag_id: int) -> dict[str, Any]:
        if store.get_tag(tag_id) is None:
            raise HTTPException(status_code=404, detail="タグが見つかりません")
        store.delete_tag(tag_id)
        return {"ok": True}

    @app.get("/api/security-tags")
    def api_security_tags() -> dict[str, Any]:
        """全資産のタグ配分（キー → [{tag_id, weight}]）。

        AS の銘柄は security_id、Crypto-Summary 由来の資産は "cs:BTC" のような
        文字列キーで返る（フロントは元々キーを文字列として扱っている）。
        """
        combined: dict[str, dict[int, Decimal]] = {
            str(k): v for k, v in store.security_tag_map().items()
        }
        combined.update(store.external_tag_map())
        return {
            "allocations": {
                key: [{"tag_id": tid, "weight": _s(w)} for tid, w in weights.items()]
                for key, weights in combined.items()
            }
        }

    @app.put("/api/securities/{security_id}/tags")
    def api_security_tags_set(
        security_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        if store.get_security(security_id) is None:
            raise HTTPException(status_code=404, detail="銘柄が見つかりません")
        allocations = _tag_weights_payload(payload.get("allocations"))
        store.set_security_tags(security_id, allocations)
        return {"ok": True}

    @app.put("/api/asset-tags/{asset_key}")
    def api_external_tags_set(
        asset_key: str, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """外部アプリ由来の資産（Crypto-Summary のコイン）へのタグ配分。

        実体は向こうにあるので存在確認はしない（CS 停止中でも分類を編集できる）。
        受け付けるのは既知の接頭辞だけにして、任意の文字列で行が増えるのを防ぐ。
        """
        key = _validate_external_asset_key(asset_key)
        allocations = _tag_weights_payload(payload.get("allocations"))
        store.set_external_tags(key, allocations)
        return {"ok": True}

    def _portfolio_payload_composition(payload: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, field in (
            ("tag_ids", "tag_ids"),
            ("include_security_ids", "include_security_ids"),
            ("exclude_security_ids", "exclude_security_ids"),
        ):
            if key in payload:
                raw = payload[key]
                if not isinstance(raw, list):
                    raise HTTPException(status_code=400, detail=f"{key} は配列で指定してください")
                out[field] = [_to_int(v, key, minimum=1) for v in raw]
        return out

    def _tag_map_all() -> dict[Any, dict[int, Decimal]]:
        """AS 銘柄（int キー）と外部資産（"cs:BTC"）を混ぜたタグ配分マップ。

        tagging 層は id を辞書キーとしか見ないため、そのまま渡せる。
        """
        combined: dict[Any, dict[int, Decimal]] = dict(store.security_tag_map())
        combined.update(store.external_tag_map())
        return combined

    @app.get("/api/portfolios")
    def api_portfolios_list(
        request: Request, currency: str | None = Query(None)
    ) -> dict[str, Any]:
        warnings: list[str] = []
        settings = store.get_settings()
        cur = _resolve_currency(currency, settings)
        res, _spot, _fx = _summarize_now(store, cur, warnings)
        _apply_cs_merge(
            res, settings, _cs_effective_currency(cur, _fx),
            _cs_user_sub(request), warnings,
        )
        tag_map = _tag_map_all()
        tags = store.list_tags()
        rows = []
        for p in store.list_portfolios():
            agg = tagging.summarize_by_tag(res["holdings"], tag_map, tags, p)
            rows.append(
                {
                    **p,
                    "value": _s(agg["total_value"]),
                    "cost": _s(agg["total_cost"]),
                    "pl": _s(agg["total_pl"]),
                    "pl_pct": _pct(agg["total_pl_pct"]),
                    "day_change": _s(agg["total_day_change"]),
                    "day_change_pct": _pct(agg["total_day_change_pct"]),
                    "day_change_partial": agg["day_change_partial"],
                    "holding_count": len(agg["holdings"]),
                }
            )
        return {
            "currency": cur,
            "portfolios": rows,
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    @app.post("/api/portfolios")
    def api_portfolio_create(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name は必須です")
        comp = _portfolio_payload_composition(payload)
        try:
            pid = store.create_portfolio(name, payload.get("note"))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"同名のポートフォリオが既にあります: {name}")
        if comp:
            store.set_portfolio_composition(pid, **comp)
        return {"ok": True, "id": pid}

    @app.put("/api/portfolios/{portfolio_id}")
    def api_portfolio_update(
        portfolio_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        if store.get_portfolio(portfolio_id) is None:
            raise HTTPException(status_code=404, detail="ポートフォリオが見つかりません")
        fields: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload["name"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="name を空にはできません")
            fields["name"] = name
        if "note" in payload:
            fields["note"] = str(payload["note"]) if payload["note"] else None
        if "sort_order" in payload:
            fields["sort_order"] = _to_int(payload["sort_order"], "sort_order")
        try:
            store.update_portfolio(portfolio_id, **fields)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="同名のポートフォリオが既にあります")
        comp = _portfolio_payload_composition(payload)
        if comp:
            store.set_portfolio_composition(portfolio_id, **comp)
        return {"ok": True}

    @app.delete("/api/portfolios/{portfolio_id}")
    def api_portfolio_delete(portfolio_id: int) -> dict[str, Any]:
        if store.get_portfolio(portfolio_id) is None:
            raise HTTPException(status_code=404, detail="ポートフォリオが見つかりません")
        store.delete_portfolio(portfolio_id)
        return {"ok": True}

    def _tag_breakdown_response(
        portfolio: dict[str, Any] | None,
        currency: str | None,
        request: Request | None = None,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        settings = store.get_settings()
        cur = _resolve_currency(currency, settings)
        res, _spot, _fx = _summarize_now(store, cur, warnings)
        _apply_cs_merge(
            res, settings, _cs_effective_currency(cur, _fx),
            _cs_user_sub(request), warnings,
        )
        tag_map = _tag_map_all()
        tags = store.list_tags()
        agg = tagging.summarize_by_tag(res["holdings"], tag_map, tags, portfolio)
        members = agg["holdings"]
        class_label = lambda c: ASSET_CLASS_META.get(c, {}).get("ja", c)  # noqa: E731

        def ser_group(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "key": str(r["key"]),
                    "label": r["label"],
                    "value": _s(r["value"]),
                    "weight": _pct(r["weight"]),
                    "holding_count": r["holding_count"],
                    "day_change": _s(r["day_change"]),
                    "day_change_pct": _pct(r["day_change_pct"]),
                }
                for r in rows
            ]

        return {
            "currency": cur,
            "portfolio": portfolio,
            "total_value": _s(agg["total_value"]),
            "total_cost": _s(agg["total_cost"]),
            "total_pl": _s(agg["total_pl"]),
            "total_pl_pct": _pct(agg["total_pl_pct"]),
            "total_day_change": _s(agg["total_day_change"]),
            "total_day_change_pct": _pct(agg["total_day_change_pct"]),
            "day_change_partial": agg["day_change_partial"],
            "by_tag": [
                {
                    "tag_id": r["tag_id"],
                    "name": r["name"],
                    "color": r["color"],
                    "value": _s(r["value"]),
                    "weight": _pct(r["weight"]),
                    "holding_count": r["holding_count"],
                    "day_change": _s(r["day_change"]),
                    "day_change_pct": _pct(r["day_change_pct"]),
                    "day_change_partial": r["day_change_partial"],
                }
                for r in agg["tags"]
            ],
            "unallocated": [
                {
                    "security_id": u["security_id"],
                    "name": u["name"],
                    "asset_class": u["asset_class"],
                    "allocated_pct": _pct(u["allocated_pct"]),
                    "value": _s(u["value"]),
                    "unallocated_value": _s(u["unallocated_value"]),
                }
                for u in agg["unallocated"]
            ],
            "by_currency": ser_group(tagging.group_by(members, "currency")),
            "by_class": ser_group(
                tagging.group_by(members, "asset_class", label_of=class_label)
            ),
            "by_account": ser_group(tagging.group_by(members, "account")),
            "holdings": [
                {
                    **_ser_holding(h),
                    "portfolio_value": _s(h.get("portfolio_value")),
                    "portfolio_ratio": _pct(
                        (h.get("portfolio_ratio") or Decimal("0")) * Decimal("100")
                    ),
                    "tags": [
                        {"id": t["id"], "name": t["name"], "weight": _s(t["weight"])}
                        for t in h.get("tags", [])
                    ],
                }
                for h in members
            ],
            "warnings": warnings,
            "generated_at": _utcnow_iso(),
        }

    @app.get("/api/tag-summary")
    def api_tag_summary(
        request: Request, currency: str | None = Query(None)
    ) -> dict[str, Any]:
        """全保有のタグ別按分集計（ポートフォリオ指定なし）。"""
        return _tag_breakdown_response(None, currency, request)

    @app.get("/api/tags/{tag_id}/holdings")
    def api_tag_holdings(
        request: Request,
        tag_id: int,
        currency: str | None = Query(None),
    ) -> dict[str, Any]:
        """タグ1つ分の内訳。

        「そのタグだけを対象にしたポートフォリオ」と同じ計算をするので、
        タグ別サマリーの金額とドリルダウン先の合計が必ず一致する。
        """
        tag = store.get_tag(tag_id)
        if tag is None:
            raise HTTPException(status_code=404, detail="タグが見つかりません")
        res = _tag_breakdown_response(
            _virtual_tag_portfolio(tag), currency, request
        )
        res["tag"] = tag
        return res

    @app.get("/api/portfolios/{portfolio_id}")
    def api_portfolio_detail(
        request: Request,
        portfolio_id: int,
        currency: str | None = Query(None),
    ) -> dict[str, Any]:
        p = store.get_portfolio(portfolio_id)
        if p is None:
            raise HTTPException(status_code=404, detail="ポートフォリオが見つかりません")
        return _tag_breakdown_response(p, currency, request)

    # ------------------------------------------------------------------
    # タグの自動配分（ルールベース）
    # ------------------------------------------------------------------
    # 投信自動連携と違い外部照会が無くローカル計算だけで一瞬で終わるため、
    # ロックも進捗表示も置かない。

    def _cs_asset_keys(request: Request | None) -> list[tuple[str, str]]:
        """Crypto-Summary 由来のコイン [(asset_key, シンボル), ...]。

        銘柄行が無いので、提案・適用のたびに生きた一覧を取り直す。
        CS 停止中は空（連携分の提案が出ないだけで、AS の銘柄は通常どおり）。
        """
        if not crypto_summary_client.is_enabled():
            return []
        cur = _resolve_currency(None, store.get_settings())
        cs = fetch_cs_summary(cur, _cs_user_sub(request))
        rows = crypto_summary_client.cs_holding_rows(cs, True, cur) if cs else []
        return [(r["id"], r["code"]) for r in rows]

    @app.post("/api/tag-rules/suggest")
    def api_tag_rules_suggest(request: Request) -> dict[str, Any]:
        """銘柄名・コード・資産クラスからタグ配分を提案する（DBは変更しない）。"""
        suggestions = tag_rules.suggest_all(store, _cs_asset_keys(request))
        counts: dict[str, int] = {}
        missing: set[str] = set()
        for s in suggestions:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
            missing.update(s["missing_tags"])
        return {
            "suggestions": suggestions,
            "counts": counts,
            "missing_tags": sorted(missing),
            "generated_at": _utcnow_iso(),
        }

    @app.post("/api/tag-rules/apply")
    def api_tag_rules_apply(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """指定された銘柄にだけルールの配分を適用する。

        提案表示中にタグが改名された場合でも古い tag_id を書かないよう、
        クライアントの配分値は受け取らず、サーバ側でルールを再判定する。
        """
        raw = payload.get("security_ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=400, detail="security_ids を指定してください")
        # Crypto-Summary 由来のコインは "cs:BTC" 形式で混ざって届く
        prefix = crypto_summary_client.EXTERNAL_KEY_PREFIX
        external_keys = [
            _validate_external_asset_key(v)
            for v in raw
            if isinstance(v, str) and v.startswith(prefix)
        ]
        ids = [
            _to_int(v, "security_ids", minimum=1)
            for v in raw
            if not (isinstance(v, str) and v.startswith(prefix))
        ]
        tags = store.list_tags()
        applied = 0
        skipped: list[dict[str, Any]] = []
        warnings: list[str] = []
        for key in external_keys:
            symbol = key[len(prefix):]
            m = tag_rules.match_crypto_symbol(symbol)
            if m is None:
                skipped.append(
                    {"security_id": key, "name": symbol, "reason": "no-rule"}
                )
                continue
            alloc, missing = tag_rules.resolve_allocation(m.rule, tags)
            if missing:
                skipped.append({
                    "security_id": key, "name": symbol,
                    "reason": "missing-tag", "missing_tags": missing,
                })
                warnings.append(
                    f"タグが未作成のため適用できません: {symbol} → {'/'.join(missing)}"
                )
                continue
            weights = _tag_weights_payload(
                [{"tag_id": tid, "weight": _s(w)} for tid, w in alloc.items()]
            )
            store.set_external_tags(key, weights)
            applied += 1
        for sec_id in ids:
            sec = store.get_security(sec_id)
            if sec is None:
                raise HTTPException(
                    status_code=404, detail=f"銘柄が見つかりません: {sec_id}"
                )
            m = tag_rules.match_rule(sec.name, sec.code, sec.asset_class.value)
            if m is None:
                skipped.append(
                    {"security_id": sec_id, "name": sec.name, "reason": "no-rule"}
                )
                continue
            alloc, missing = tag_rules.resolve_allocation(m.rule, tags)
            if missing:
                skipped.append({
                    "security_id": sec_id,
                    "name": sec.name,
                    "reason": "missing-tag",
                    "missing_tags": missing,
                })
                warnings.append(
                    f"タグが未作成のため適用できません: {sec.name} → {'/'.join(missing)}"
                )
                continue
            # 100%上限・タグ実在の検証は既存の共通処理を再利用する
            weights = _tag_weights_payload(
                [{"tag_id": tid, "weight": _s(w)} for tid, w in alloc.items()]
            )
            store.set_security_tags(sec_id, weights)
            applied += 1
        return {"ok": True, "applied": applied, "skipped": skipped, "warnings": warnings}

    # ------------------------------------------------------------------
    # 投信の自動連携
    # ------------------------------------------------------------------

    @app.post("/api/fund-links/suggest")
    def api_fund_links_suggest() -> dict[str, Any]:
        """未連携投信の連携候補を自動判定する（投信協会へ照会・数分かかる）。"""
        if not autolink_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="自動判定を実行中です")
        try:
            warnings: list[str] = []
            suggestions = fund_autolink.suggest_links(store, warn=warnings.append)
            return {
                "suggestions": suggestions,
                "warnings": warnings,
                "generated_at": _utcnow_iso(),
            }
        finally:
            autolink_lock.release()

    @app.post("/api/fund-links/apply")
    def api_fund_links_apply(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """選択された連携を一括適用し、価格履歴も取得する。"""
        links = payload.get("links")
        if not isinstance(links, list) or not links:
            raise HTTPException(status_code=400, detail="links を指定してください")
        warnings: list[str] = []
        updated: list[Security] = []
        for link in links:
            if not isinstance(link, dict):
                raise HTTPException(status_code=400, detail="links の形式が不正です")
            sec_id = _to_int(link.get("security_id"), "security_id", minimum=1)
            ref = _clean_source_ref(link.get("ref"))
            if not ref:
                raise HTTPException(
                    status_code=400, detail=f"ref を指定してください (security_id={sec_id})"
                )
            sec = store.get_security(sec_id)
            if sec is None:
                raise HTTPException(
                    status_code=404, detail=f"銘柄が見つかりません (security_id={sec_id})"
                )
            store.update_security(
                sec_id,
                price_source_type=PriceSourceType.TOUSHIN.value,
                price_source_ref=ref,
                price_source_status=PriceSourceStatus.LINKED.value,
            )
            updated.append(store.get_security(sec_id))
        linked_count = len(updated)
        # 同じファンド（ref が同一）へ連携された銘柄が複数あれば、その場で統合する
        # — 別名の重複銘柄はここで初めて同一だと確定するため
        merged = _dedupe_linked_funds(store, warnings)
        if merged:
            updated = [
                s for s in (store.get_security(u.id) for u in updated) if s is not None
            ]
        if updated:
            try:
                today = date.today()
                ensure_price_history(
                    store, updated, today - timedelta(days=365 * 5), today,
                    warnings.append,
                )
            except Exception as e:  # noqa: BLE001
                warnings.append(f"価格履歴の取得に失敗しました: {e}")
        # 年金銘柄が含まれていれば口数を逆算（履歴取得の後）
        pension_units = _derive_pension_units_now(store, warnings)
        return {
            "ok": True,
            "linked": linked_count,
            "merged": merged,
            "pension_units": pension_units,
            "warnings": warnings,
        }

    @app.post("/api/refresh-prices")
    def api_refresh_prices() -> dict[str, Any]:
        with store.connect() as conn:
            conn.execute("DELETE FROM spot_cache")
        # 当日の失敗記録も解除し、利用者が明示的に再取得できるようにする
        price_store.clear_attempts(store)
        # Crypto-Summary 応答のキャッシュも破棄（明示リフレッシュに追随）
        crypto_summary_client.clear_cache()
        return {"ok": True, "warnings": []}

    @app.put("/api/settings")
    def api_settings_update(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if "include_classes" in payload:
            raw = payload["include_classes"]
            if not isinstance(raw, dict):
                raise HTTPException(
                    status_code=400, detail="include_classes はオブジェクトで指定してください"
                )
            for cid, flag in raw.items():
                if cid not in ASSET_CLASS_META:
                    raise HTTPException(
                        status_code=400, detail=f"資産クラスが不正です: {cid}"
                    )
                store.set_setting(
                    portfolio.include_setting_key(cid), "1" if flag else "0"
                )
        # 後方互換（個別キー指定も受け付ける）
        if "include_pension" in payload:
            store.set_setting(
                "include_pension", "1" if payload["include_pension"] else "0"
            )
        if "include_points" in payload:
            store.set_setting(
                "include_points", "1" if payload["include_points"] else "0"
            )
        if "merge_cash" in payload:
            store.set_setting("merge_cash", "1" if payload["merge_cash"] else "0")
        if "dashboard_layout" in payload:
            raw = payload["dashboard_layout"]
            if not isinstance(raw, list):
                raise HTTPException(
                    status_code=400, detail="dashboard_layout は配列で指定してください"
                )
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    raise HTTPException(
                        status_code=400, detail="dashboard_layout の形式が不正です"
                    )
                wid = str(item.get("id") or "")
                if wid not in DASHBOARD_WIDGETS:
                    raise HTTPException(
                        status_code=400, detail=f"未知のウィジェットです: {wid}"
                    )
                cleaned.append({"id": wid, "visible": bool(item.get("visible", True))})
            store.set_setting("dashboard_layout", json.dumps(cleaned, ensure_ascii=False))
        if "dashboard_chip_classes" in payload:
            raw = payload["dashboard_chip_classes"]
            if raw is None:
                store.set_setting("dashboard_chip_classes", "")  # 既定セットに戻す
            elif isinstance(raw, list):
                # 空リストは "[]" として保存され、「1つも出さない」を意味する
                # （未設定＝空文字とは区別される）
                for cid in raw:
                    if cid not in ASSET_CLASS_META:
                        raise HTTPException(
                            status_code=400, detail=f"資産クラスが不正です: {cid}"
                        )
                store.set_setting(
                    "dashboard_chip_classes",
                    json.dumps(list(dict.fromkeys(raw)), ensure_ascii=False),
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="dashboard_chip_classes は配列または null で指定してください",
                )
        if "default_currency" in payload:
            c = str(payload["default_currency"] or "").upper()
            if c not in SUPPORTED_CURRENCIES:
                raise HTTPException(status_code=400, detail=f"未対応の通貨です: {c}")
            store.set_setting("default_currency", c)
        return {"ok": True}

    return app


app = create_app(os.environ.get("AS_DB_PATH", "data/assets.db"))
