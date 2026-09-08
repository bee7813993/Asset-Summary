"""取込直後の投信自動連携（Web の確定エンドポイントと受信フォルダの自動取込で共用）。

投信は MF の PDF に銘柄コードが無く名前しか手がかりが無いので、表記が揺れると
別銘柄として登録される。ISIN:協会コードまで辿れば同一性は確定するため、取込で
新しくできた投信だけを投信協会へ照会し、基準価額が一致した候補へ連携する。
連携できれば dedupe_same_fund が既存銘柄へ統合し、重複は消える。

連携できなかったときは reason を添えて返す（協会へ届かなかったのか該当が
無かったのかで、利用者の対処が変わる）。
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Any

from ..core import fund_autolink
from ..core.models import AssetClass, PriceSourceStatus, PriceSourceType, Security
from ..core.price_history import ensure_price_history
from ..core.store import Store
from .tx_service import recompute_cost_basis

# 連携できなかった理由の説明（受信フォルダのイベントに書く。画面の i18n と同じ内容）
REASON_TEXT = {
    "search_unreachable": "投信協会へ照会できませんでした（通信エラー）",
    "not_found": "投信協会に該当するファンドが見つかりませんでした",
    "nav_unavailable": "候補は見つかりましたが、基準価額を取得できず確定できませんでした",
    "nav_mismatch": "候補は見つかりましたが、基準価額が一致しませんでした",
    "nav_matched_multiple": "基準価額が一致する候補が複数あり、確定できませんでした",
}

# 投信協会への照会を直列化するロック。設定ページからの一括判定と、取込直後の
# 自動連携が同時に走らないようにする（相手は外部サービスなのでプロセス共有でよい）。
autolink_lock = threading.Lock()

# 取込直後に自動連携を試みる新規投信の上限。1件あたり検索1〜3回＋CSV最大3回の
# 照会が要るため、これを超えるときは取込を待たせず設定ページへ誘導する。
AUTOLINK_ON_IMPORT_MAX = 5


def dedupe_linked_funds(store: Store, warnings: list[str]) -> list[dict[str, Any]]:
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


def autolink_new_funds(
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
        out["merged"] = dedupe_linked_funds(store, warnings)
        try:
            today = date.today()
            ensure_price_history(
                store, applied, today - timedelta(days=365 * 5), today, warnings.append
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"価格履歴の取得に失敗しました: {e}")
    return out


def describe(autolink: dict[str, Any]) -> str:
    """自動連携の結果を1行の日本語にする（受信フォルダのイベント用）。"""
    parts: list[str] = []
    linked = len(autolink.get("linked") or [])
    if linked:
        s = f"投信 {linked} 件を投信協会へ自動連携"
        merged = len(autolink.get("merged") or [])
        if merged:
            s += f"、{merged} 件を既存銘柄へ統合"
        parts.append(s)
    for u in autolink.get("unresolved") or []:
        why = REASON_TEXT.get(u.get("reason") or "", u.get("status") or "")
        parts.append(f"自動連携できず: {u.get('name')}（{why}）")
    return "、".join(parts)
