"""受信フォルダ自動取込（importers/inbox.py）のテスト。

PDF解析そのものは test_mf_parser / test_matching で検証済みのため、ここでは
build_preview / commit_batch をモジュール名前空間経由で差し替え、スキャンの
編成（安定待ち・移動・重複・却下）と自動確定の判定ロジックを検証する。
"""

from __future__ import annotations

import os
import tempfile

# test_web.py と同じ措置: モジュールレベルの app = create_app(...) が
# リポジトリの data/assets.db を作らないよう、import 前に退避する。
os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import asset_summary.importers.inbox as inbox_mod
from asset_summary.core.models import ImportBatch
from asset_summary.core.store import Store
from asset_summary.importers.inbox import (
    InboxWatcher,
    _move_file,
    evaluate_preview,
    resolve_inbox_dir,
    resolve_poll_seconds,
)
from asset_summary.importers.service import DuplicateImportError


def _preview(batch_id: str = "b1", **over: Any) -> dict[str, Any]:
    """UI と同じ形のプレビュー。株1新規・1低信頼・missing2（株/投信）・crypto1。"""
    base: dict[str, Any] = {
        "batch_id": batch_id,
        "filename": "a.pdf",
        "suggested_as_of": "2026-08-20",
        "sections": [
            {"section": "stock", "label": "株式（現物）", "default_include": True},
            {"section": "crypto", "label": "暗号資産", "default_include": False},
        ],
        "diff": [
            {"key": "k1", "status": "new", "section": "stock", "included": True},
            {"key": "k2", "status": "unchanged", "section": "stock", "included": False},
            {"key": "k3", "status": "missing", "section": "stock", "included": True},
            {"key": "k4", "status": "missing", "section": "fund", "included": True},
            {"key": "k5", "status": "new", "section": "crypto", "included": False},
        ],
        "report": {
            "grand_total_ok": True,
            "sections": [{"section": "stock", "name": "株式（現物）", "ok": True}],
            "warnings": [],
        },
        "warnings": [],
    }
    base.update(over)
    return base


# ----------------------------------------------------------------------
# evaluate_preview: 自動確定の判定
# ----------------------------------------------------------------------


def test_evaluate_preview_mirrors_ui_defaults():
    reasons, kwargs, excluded = evaluate_preview(_preview())
    assert reasons == []
    assert kwargs["include_sections"] == ["stock"]
    assert kwargs["include_crypto"] is False
    # 低信頼行は除外し、crypto の included=False は数えない
    assert kwargs["exclude_keys"] == ["k2"]
    assert excluded == 1
    # missing のゼロ化は PDF に存在するセクションだけ（fund はPDFに無い）
    assert kwargs["zero_keys"] == ["k3"]


def test_evaluate_preview_rejects_grand_total_ng():
    p = _preview()
    p["report"]["grand_total_ok"] = False
    reasons, _, _ = evaluate_preview(p)
    assert any("資産総額" in r for r in reasons)


def test_evaluate_preview_rejects_section_ng():
    p = _preview()
    p["report"]["sections"][0]["ok"] = False
    reasons, _, _ = evaluate_preview(p)
    assert any("株式（現物）" in r for r in reasons)


def test_evaluate_preview_rejects_empty_sections():
    reasons, _, _ = evaluate_preview(_preview(sections=[]))
    assert reasons


# ----------------------------------------------------------------------
# 設定の解決
# ----------------------------------------------------------------------


def test_resolve_inbox_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AS_MF_INBOX_DIR", raising=False)
    assert resolve_inbox_dir(tmp_path / "db" / "t.db") == (
        tmp_path / "db"
    ).resolve() / "inbox"
    monkeypatch.setenv("AS_MF_INBOX_DIR", "OFF")
    assert resolve_inbox_dir(tmp_path / "t.db") is None
    monkeypatch.setenv("AS_MF_INBOX_DIR", str(tmp_path / "drop"))
    assert resolve_inbox_dir(tmp_path / "t.db") == tmp_path / "drop"


def test_resolve_poll_seconds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AS_MF_INBOX_POLL", raising=False)
    assert resolve_poll_seconds() == inbox_mod.DEFAULT_POLL_SECONDS
    monkeypatch.setenv("AS_MF_INBOX_POLL", "120")
    assert resolve_poll_seconds() == 120
    monkeypatch.setenv("AS_MF_INBOX_POLL", "1")  # 下限にクランプ
    assert resolve_poll_seconds() == inbox_mod.MIN_POLL_SECONDS
    monkeypatch.setenv("AS_MF_INBOX_POLL", "abc")
    assert resolve_poll_seconds() == inbox_mod.DEFAULT_POLL_SECONDS


# ----------------------------------------------------------------------
# スキャンの編成
# ----------------------------------------------------------------------


@pytest.fixture()
def watcher(tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch) -> InboxWatcher:
    monkeypatch.setenv("AS_MF_INBOX_DIR", str(tmp_path / "inbox"))
    w = InboxWatcher(store, tmp_path / "test.db")
    assert w.dir is not None
    w.dir.mkdir(parents=True, exist_ok=True)
    return w


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch, store: Store):
    """build_preview / commit_batch を記録付きの偽実装に差し替える。"""
    calls: dict[str, Any] = {"previews": [], "commits": []}

    def fake_build_preview(_store, data: bytes, filename: str):
        calls["previews"].append(filename)
        batch_id = f"batch-{len(calls['previews'])}"
        _store.create_batch(ImportBatch(id=batch_id, filename=filename))
        return _preview(batch_id=batch_id, filename=filename)

    def fake_commit_batch(_store, batch_id, as_of_date, **kwargs):
        calls["commits"].append({"batch_id": batch_id, "as_of": as_of_date, **kwargs})
        _store.update_batch(batch_id, status="committed", as_of_date=as_of_date)
        return {"created": 1, "updated": 2, "zeroed": 1}

    monkeypatch.setattr(inbox_mod, "build_preview", fake_build_preview)
    monkeypatch.setattr(inbox_mod, "commit_batch", fake_commit_batch)
    return calls


def test_scan_waits_for_stable_file_then_commits(watcher: InboxWatcher, fake_service):
    pdf = watcher.dir / "資産内訳.pdf"
    pdf.write_bytes(b"%PDF-fake")

    # 1回目: 初見なので登録のみ（書き込み途中かもしれない）
    assert watcher.scan_once() == []
    assert fake_service["previews"] == []
    assert pdf.exists()

    # 2回目: サイズ・mtime が変わっていないので処理される
    events = watcher.scan_once()
    assert [e["status"] for e in events] == ["committed"]
    ev = events[0]
    assert ev["as_of"] == "2026-08-20"  # suggested_as_of を使う
    assert (ev["created"], ev["updated"], ev["zeroed"]) == (1, 2, 1)
    assert "低信頼" in ev["detail"]  # k2 の除外が通知される
    assert not pdf.exists()
    assert (watcher.dir / "processed" / "資産内訳.pdf").exists()

    # UI の既定と同じ引数で確定している
    commit = fake_service["commits"][0]
    assert commit["include_sections"] == ["stock"]
    assert commit["exclude_keys"] == ["k2"]
    assert commit["zero_keys"] == ["k3"]
    assert commit["include_crypto"] is False


def test_scan_force_skips_stability_wait(watcher: InboxWatcher, fake_service):
    (watcher.dir / "a.pdf").write_bytes(b"%PDF-fake")
    events = watcher.scan_once(force=True)
    assert [e["status"] for e in events] == ["committed"]


def test_scan_ignores_non_pdf_and_hidden(watcher: InboxWatcher, fake_service):
    (watcher.dir / "note.txt").write_text("x", encoding="utf-8")
    (watcher.dir / ".syncthing.a.pdf").write_bytes(b"x")
    (watcher.dir / "sub").mkdir()
    assert watcher.scan_once(force=True) == []
    assert fake_service["previews"] == []


def test_scan_duplicate_moves_to_processed(watcher: InboxWatcher, monkeypatch):
    def dup(_store, _data, _filename):
        raise DuplicateImportError("b0", "2026-08-01")

    monkeypatch.setattr(inbox_mod, "build_preview", dup)
    (watcher.dir / "same.pdf").write_bytes(b"%PDF-fake")
    events = watcher.scan_once(force=True)
    assert [e["status"] for e in events] == ["duplicate"]
    assert "2026-08-01" in events[0]["detail"]
    assert (watcher.dir / "processed" / "same.pdf").exists()


def test_scan_rejected_moves_to_failed_and_drops_preview(
    watcher: InboxWatcher, store: Store, monkeypatch
):
    def bad_preview(_store, _data, filename: str):
        _store.create_batch(ImportBatch(id="rej1", filename=filename))
        p = _preview(batch_id="rej1", filename=filename)
        p["report"]["grand_total_ok"] = False
        return p

    monkeypatch.setattr(inbox_mod, "build_preview", bad_preview)
    (watcher.dir / "broken.pdf").write_bytes(b"%PDF-fake")
    events = watcher.scan_once(force=True)
    assert [e["status"] for e in events] == ["rejected"]
    assert "手動で" in events[0]["detail"]
    assert (watcher.dir / "failed" / "broken.pdf").exists()
    # プレビューのバッチは残さない
    assert store.get_batch("rej1") is None


def test_scan_parse_error_moves_to_failed(watcher: InboxWatcher, monkeypatch):
    def boom(_store, _data, _filename):
        raise ValueError("not a pdf")

    monkeypatch.setattr(inbox_mod, "build_preview", boom)
    (watcher.dir / "junk.pdf").write_bytes(b"junk")
    events = watcher.scan_once(force=True)
    assert [e["status"] for e in events] == ["error"]
    assert (watcher.dir / "failed" / "junk.pdf").exists()
    assert watcher.scan_once(force=True) == []
    # 同名を置き直したら改めて処理され、failed/ では -2 が付く
    (watcher.dir / "junk.pdf").write_bytes(b"junk again")
    events = watcher.scan_once(force=True)
    assert [e["status"] for e in events] == ["error"]
    assert (watcher.dir / "failed" / "junk-2.pdf").exists()


def test_move_file_collision_suffix(tmp_path: Path):
    dest = tmp_path / "processed"
    a = tmp_path / "a.pdf"
    a.write_bytes(b"1")
    assert _move_file(a, dest) == dest / "a.pdf"
    a.write_bytes(b"2")
    assert _move_file(a, dest) == dest / "a-2.pdf"


def test_status_shape(watcher: InboxWatcher):
    st = watcher.status()
    assert st["enabled"] is True
    assert st["dir"] == str(watcher.dir)
    assert st["running"] is False
    assert st["events"] == []


# ----------------------------------------------------------------------
# Web API
# ----------------------------------------------------------------------


def _make_client(tmp_path: Path) -> tuple[TestClient, Any]:
    import asset_summary.web.app as web_app

    app = web_app.create_app(str(tmp_path / "t.db"))
    return TestClient(app), app


def test_inbox_api_status_and_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AS_MF_INBOX_DIR", str(tmp_path / "drop"))
    client, app = _make_client(tmp_path)

    r = client.get("/api/import/inbox")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["dir"] == str(tmp_path / "drop")

    # 空フォルダのスキャン（フォルダはこの時点で作られる）
    r = client.post("/api/import/inbox/scan")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert (tmp_path / "drop").is_dir()

    # ファイルを置いて強制スキャン → committed イベントが返る
    def fake_build_preview(_store, data, filename):
        app.state.store.create_batch(ImportBatch(id="api1", filename=filename))
        return _preview(batch_id="api1", filename=filename)

    monkeypatch.setattr(inbox_mod, "build_preview", fake_build_preview)
    monkeypatch.setattr(
        inbox_mod,
        "commit_batch",
        lambda _s, _b, as_of_date, **kw: {"created": 1, "updated": 0, "zeroed": 0},
    )
    (tmp_path / "drop" / "x.pdf").write_bytes(b"%PDF-fake")
    r = client.post("/api/import/inbox/scan")
    assert r.status_code == 200
    body = r.json()
    assert [e["status"] for e in body["new_events"]] == ["committed"]
    assert body["events"][0]["filename"] == "x.pdf"


def test_inbox_api_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AS_MF_INBOX_DIR", "off")
    client, _ = _make_client(tmp_path)
    assert client.get("/api/import/inbox").json()["enabled"] is False
    assert client.post("/api/import/inbox/scan").status_code == 409
