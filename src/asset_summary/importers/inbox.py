"""マネーフォワードME PDF の受信フォルダ（inbox）自動取込。

受信フォルダ（既定 `<DBと同じディレクトリ>/inbox`、AS_MF_INBOX_DIR で変更、
"off" で無効）に置かれた PDF を web プロセス内のデーモンスレッドが定期
スキャンし、Web UI の既定操作と同じ内容で自動確定する。処理後のファイルは
processed/（確定・重複）または failed/（検算NG・解析不能）へ移動する。

設計メモ:
- ファイルイベント監視（watchdog / inotify）ではなくポーリング。
  Docker バインドマウント越し（特に Windows ホスト）ではファイル
  イベントが届かないため。既定 30 秒間隔（AS_MF_INBOX_POLL、秒）。
- 別プロセスにはしない — assets.db の書き手を web プロセス 1 つに保つ。
- 書き込み途中のファイルを読まないよう、2 回連続のスキャンで
  (size, mtime) が変わらなかったファイルだけを処理する（mtime は
  コピー元の値が保存されるため「古い mtime = 完了」とは判定できない）。
  読み取りが OSError になった場合も次回スキャンへ持ち越す。
- 自動確定の内容は Web UI でプレビューを開いてそのまま確定した場合と同じ:
  * セクションは default_include のもの（暗号資産は除外 — CS が本体）
  * included=False の行（低信頼マッチ）は除外し、件数をイベントに残す
  * missing 行（PDFに無い既存ロット）は、そのセクションが PDF に存在する
    場合のみゼロ化（UI が未選択セクションの行を送らないのと同じ）
- 検算 NG（総額不一致・セクション不一致）は自動確定しない。プレビューの
  バッチを消し、ファイルを failed/ へ移して手動取込に委ねる。
- 直近イベントはメモリ上のみ（再起動で消える）。確定済みは取込履歴に残る。
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..core.store import Store
from .service import DuplicateImportError, build_preview, commit_batch

log = logging.getLogger("asset_summary.inbox")

DEFAULT_POLL_SECONDS = 30
MIN_POLL_SECONDS = 5
MAX_EVENTS = 30
_DISABLE_VALUES = {"off", "none", "no", "false", "0"}

PROCESSED_DIR = "processed"
FAILED_DIR = "failed"


def resolve_inbox_dir(db_path: str | os.PathLike[str]) -> Path | None:
    """AS_MF_INBOX_DIR から受信フォルダを決める。None なら機能無効。"""
    raw = os.environ.get("AS_MF_INBOX_DIR", "").strip()
    if raw.lower() in _DISABLE_VALUES:
        return None
    if raw:
        return Path(raw)
    return Path(db_path).resolve().parent / "inbox"


def resolve_poll_seconds() -> int:
    raw = os.environ.get("AS_MF_INBOX_POLL", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_POLL_SECONDS
    except ValueError:
        return DEFAULT_POLL_SECONDS
    return max(MIN_POLL_SECONDS, value)


def evaluate_preview(
    preview: dict[str, Any],
) -> tuple[list[str], dict[str, Any], int]:
    """プレビューから自動確定の可否と commit_batch の引数を決める。

    返値: (reject_reasons, commit_kwargs, excluded_count)。
    reject_reasons が空のときだけ自動確定してよい。
    """
    reasons: list[str] = []
    report = preview.get("report") or {}
    sections = preview.get("sections") or []
    include_sections = [s["section"] for s in sections if s.get("default_include")]
    if not include_sections:
        reasons.append("取込対象のセクションがありません")
    if report.get("grand_total_ok") is False:
        reasons.append("資産総額の検算が一致しません")
    ng_sections = [
        str(s.get("name") or s.get("section") or "?")
        for s in report.get("sections") or []
        if not s.get("ok", True)
    ]
    if ng_sections:
        reasons.append("検算不一致のセクションがあります: " + "、".join(ng_sections))

    included_set = set(include_sections)
    exclude_keys: list[str] = []
    zero_keys: list[str] = []
    excluded = 0
    for row in preview.get("diff") or []:
        section = row.get("section")
        if row.get("status") == "missing":
            if section in included_set and row.get("included") is not False:
                zero_keys.append(row["key"])
            continue
        if section not in included_set:
            continue  # crypto 等は include_sections / include_crypto 側で落ちる
        if row.get("included") is False:
            exclude_keys.append(row["key"])
            excluded += 1
    kwargs = {
        "include_sections": include_sections,
        "exclude_keys": exclude_keys,
        "zero_keys": zero_keys,
        "include_crypto": False,
    }
    return reasons, kwargs, excluded


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _event(
    status: str,
    filename: str,
    *,
    detail: str = "",
    as_of: str | None = None,
    created: int = 0,
    updated: int = 0,
    zeroed: int = 0,
    excluded: int = 0,
) -> dict[str, Any]:
    return {
        "at": _now_iso(),
        "filename": filename,
        "status": status,  # committed | duplicate | rejected | error
        "detail": detail,
        "as_of": as_of,
        "created": created,
        "updated": updated,
        "zeroed": zeroed,
        "excluded": excluded,
    }


def process_pdf(store: Store, data: bytes, path: Path) -> dict[str, Any]:
    """PDF 1 件を解析し、可能なら自動確定する。結果をイベントで返す。"""
    filename = path.name
    try:
        preview = build_preview(store, data, filename)
    except DuplicateImportError as e:
        detail = "同じ内容のPDFは取込済みです"
        if e.as_of_date:
            detail += f"（基準日 {e.as_of_date}）"
        return _event("duplicate", filename, detail=detail, as_of=e.as_of_date)
    except Exception as e:  # noqa: BLE001  PDFでない・壊れている等
        return _event(
            "error",
            filename,
            detail=f"解析できませんでした（{type(e).__name__}）。"
            "マネーフォワードMEの資産内訳ページのPDFか確認してください",
        )

    reasons, kwargs, excluded = evaluate_preview(preview)
    if reasons:
        store.delete_batch(preview["batch_id"])
        return _event(
            "rejected",
            filename,
            detail="、".join(reasons) + " — 画面から手動で取り込んでください",
        )

    as_of_iso = preview.get("suggested_as_of")
    if not as_of_iso:
        try:
            as_of_iso = date.fromtimestamp(path.stat().st_mtime).isoformat()
        except OSError:
            as_of_iso = date.today().isoformat()
    try:
        result = commit_batch(
            store, preview["batch_id"], as_of_date=date.fromisoformat(as_of_iso), **kwargs
        )
    except Exception as e:  # noqa: BLE001  確定失敗はロールバック済み — 片づけて報告
        store.delete_batch(preview["batch_id"])
        return _event("error", filename, detail=f"取込を確定できませんでした: {e}")

    detail = ""
    if excluded:
        detail = f"低信頼マッチのため {excluded} 行を除外しました（必要なら手動取込で確認）"
    return _event(
        "committed",
        filename,
        detail=detail,
        as_of=as_of_iso,
        created=int(result.get("created", 0)),
        updated=int(result.get("updated", 0)),
        zeroed=int(result.get("zeroed", 0)),
        excluded=excluded,
    )


def _move_file(path: Path, dest_dir: Path) -> Path | None:
    """同一ボリューム内の rename で移動。名前衝突は -2, -3… を付ける。"""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / path.name
        n = 2
        while target.exists():
            target = dest_dir / f"{path.stem}-{n}{path.suffix}"
            n += 1
            if n > 1000:
                return None
        path.rename(target)
        return target
    except OSError:
        return None


class InboxWatcher:
    """受信フォルダのポーリングスレッドと直近イベントの保持。"""

    def __init__(self, store: Store, db_path: str | os.PathLike[str]):
        self.store = store
        self.dir = resolve_inbox_dir(db_path)
        self.poll_seconds = resolve_poll_seconds()
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.last_scan_at: str | None = None
        # name -> (size, mtime_ns)。pending は「前回スキャン時点の姿」、
        # done は「処理済みだが移動できず inbox に残った姿」（再処理ループ防止）。
        self._pending: dict[str, tuple[int, int]] = {}
        self._done: dict[str, tuple[int, int]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.dir is not None

    # ------------------------------------------------------------------
    # thread lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mf-inbox-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        self._thread = None

    def _run(self) -> None:
        while True:
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001  スキャン失敗で監視スレッドを死なせない
                log.exception("受信フォルダのスキャンに失敗しました")
            if self._stop.wait(self.poll_seconds):
                return

    # ------------------------------------------------------------------
    # scanning
    # ------------------------------------------------------------------

    def scan_once(self, force: bool = False) -> list[dict[str, Any]]:
        """1 回スキャンする。force=True は安定待ち（2回スキャン則）を省く。

        新しく発生したイベントのリストを返す（API の「今すぐスキャン」用）。
        """
        if self.dir is None:
            return []
        with self._scan_lock:
            return self._scan_locked(force)

    def _scan_locked(self, force: bool) -> list[dict[str, Any]]:
        inbox = self.dir
        assert inbox is not None
        try:
            inbox.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.last_scan_at = _now_iso()
            log.warning("受信フォルダを作成できません: %s (%s)", inbox, e)
            return []

        new_events: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            entries = sorted(inbox.iterdir())
        except OSError as e:
            self.last_scan_at = _now_iso()
            log.warning("受信フォルダを読めません: %s (%s)", inbox, e)
            return []
        for path in entries:
            if (
                path.name.startswith(".")
                or path.suffix.lower() != ".pdf"
                or not path.is_file()
            ):
                continue
            try:
                st = path.stat()
            except OSError:
                continue  # 直前に消えた等 — 次回に任せる
            name = path.name
            sig = (st.st_size, st.st_mtime_ns)
            seen.add(name)
            if self._done.get(name) == sig:
                continue
            if not force and self._pending.get(name) != sig:
                # 初見またはサイズ/mtime が変化中 — 次回のスキャンまで待つ
                self._pending[name] = sig
                continue
            event = self._process(path, sig)
            if event is not None:
                new_events.append(event)

        # inbox から消えたファイルの記録を掃除（メモリを増やし続けない）
        self._pending = {k: v for k, v in self._pending.items() if k in seen}
        self._done = {k: v for k, v in self._done.items() if k in seen}
        self.last_scan_at = _now_iso()
        return new_events

    def _process(self, path: Path, sig: tuple[int, int]) -> dict[str, Any] | None:
        try:
            data = path.read_bytes()
        except OSError:
            # コピー中でロックされている等 — 記録せず次回スキャンで再試行
            self._pending[path.name] = sig
            return None
        self._pending.pop(path.name, None)
        self._done[path.name] = sig

        event = process_pdf(self.store, data, path)
        dest = PROCESSED_DIR if event["status"] in ("committed", "duplicate") else FAILED_DIR
        assert self.dir is not None
        moved = _move_file(path, self.dir / dest)
        if moved is None:
            note = "ファイルを移動できませんでした（inbox に残っています）"
            event["detail"] = f"{event['detail']}、{note}" if event["detail"] else note
        else:
            event["moved_to"] = f"{dest}/{moved.name}"
            # 移動済みなら記録も消す。processed/ から同名・同 mtime のまま
            # inbox へ戻す「やり直し」を無視しないため（二重取込は sha で防ぐ）。
            self._done.pop(path.name, None)
        self.events.appendleft(event)
        log.info(
            "inbox: %s → %s %s", event["filename"], event["status"], event["detail"]
        )
        return event

    # ------------------------------------------------------------------
    # status for the API
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dir": str(self.dir) if self.dir is not None else None,
            "poll_seconds": self.poll_seconds,
            "running": self._thread is not None and self._thread.is_alive(),
            "last_scan_at": self.last_scan_at,
            "events": list(self.events),
        }
