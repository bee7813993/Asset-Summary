"""Import orchestration: parse → diff preview → commit.

このモジュールの公開契約（web/app.py と cli.py が呼ぶ）:

- build_preview(store, pdf_bytes, filename) -> dict
    返値: {
      "batch_id": str,
      "filename": str,
      "suggested_as_of": "YYYY-MM-DD" | None,
      "sections": [{"section","label","count","total","default_include"}],
      "diff": [{"key","status","section","name","code","account","lot_label",
                "old_quantity","new_quantity","old_avg_cost","new_avg_cost",
                "value","confidence","included"}],
      "report": {...ParseReport要約（grand_total_ok, sections, warnings含む）},
      "warnings": [str],
    }
    同一sha256のPDFがcommitted済みなら DuplicateImportError(existing_batch_id) を送出。
    プレビューは import_batches(status='previewed') に parse_report として永続化し、
    commit はそこから再構築する（PDFバイトは保持しない）。

- commit_batch(store, batch_id, as_of_date, include_sections=None,
               exclude_keys=None, zero_keys=None, include_crypto=False) -> dict
    返値: {"created": int, "updated": int, "zeroed": int}
    単一トランザクションで accounts/securities 作成・holding_snapshots INSERT・
    reported_price を daily_prices(source='mf_reported') へ書き込み・batch を committed に。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Any

from ..core.models import ImportBatch
from ..core.store import (
    Store,
    StoreError,
    like_prefix,
    looks_truncated_name_key,
    resolve_truncated_sql,
)
from . import matching, mf_pdf


class DuplicateImportError(Exception):
    def __init__(self, existing_batch_id: str, as_of_date: str | None = None):
        self.existing_batch_id = existing_batch_id
        self.as_of_date = as_of_date
        super().__init__(f"already imported (batch={existing_batch_id})")


def build_preview(store: Store, pdf_bytes: bytes, filename: str) -> dict[str, Any]:
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    existing = store.find_committed_batch_by_sha(sha256)
    if existing is not None:
        raise DuplicateImportError(
            existing.id,
            existing.as_of_date.isoformat() if existing.as_of_date else None,
        )

    result, suggested = mf_pdf.parse_pdf(pdf_bytes)
    rows, diff, sections = matching.build_matches(store, result)
    report = result.report.model_dump(mode="json")
    suggested_iso = suggested.isoformat() if suggested else None

    batch_id = str(uuid.uuid4())
    store.create_batch(
        ImportBatch(
            id=batch_id,
            source_kind=mf_pdf.SOURCE_KIND,
            filename=filename,
            file_sha256=sha256,
            status="previewed",
            parse_report={
                "filename": filename,
                "suggested_as_of": suggested_iso,
                "rows": rows,
                "diff": diff,
                "sections": sections,
                "report": report,
            },
        )
    )
    return {
        "batch_id": batch_id,
        "filename": filename,
        "suggested_as_of": suggested_iso,
        "sections": sections,
        "diff": diff,
        "report": report,
        "warnings": list(report.get("warnings", [])),
    }


def commit_batch(
    store: Store,
    batch_id: str,
    as_of_date: date,
    include_sections: list[str] | None = None,
    exclude_keys: list[str] | None = None,
    zero_keys: list[str] | None = None,
    include_crypto: bool = False,
) -> dict[str, Any]:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise StoreError(f"バッチが見つかりません: {batch_id}")
    if batch.status != "previewed":
        raise StoreError(f"バッチはコミットできない状態です（status={batch.status}）")

    rows: list[dict[str, Any]] = batch.parse_report.get("rows", [])
    excluded = set(exclude_keys or [])
    zeros = set(zero_keys or [])
    rows_by_key = {row["key"]: row for row in rows}
    as_of = as_of_date.isoformat()
    now = datetime.now(timezone.utc).isoformat()

    created = updated = zeroed = 0
    account_cache: dict[str, int] = {}
    security_cache: dict[str, int] = {}
    # この取込で新しく作った銘柄。取込後の自動連携をこの分だけに絞るために返す
    # （全未連携銘柄を対象にすると投信協会への照会が数分かかる）
    new_security_ids: list[int] = []

    conn = store.connect()
    try:
        with conn:  # 単一トランザクション（例外時は全ロールバック）
            # 同一基準日への再取込は「置き換え」セマンティクス。
            # 先に旧バッチの MF 由来行を消しておかないと、UPSERT で行の所有
            # batch_id だけが新バッチへ移り、その新バッチを巻き戻したときに
            # 旧バッチのデータまで消えてしまう。
            conn.execute(
                """DELETE FROM holding_snapshots
                   WHERE as_of_date = ? AND origin = 'mf'
                     AND (batch_id IS NULL OR batch_id <> ?)""",
                (as_of, batch_id),
            )
            conn.execute(
                "DELETE FROM daily_prices WHERE source = 'mf_reported' AND date = ?",
                (as_of,),
            )
            for row in rows:
                if row["status"] == "missing":
                    continue  # missing は zero_keys 経由でのみ書く
                if row["section"] == "crypto" and not include_crypto:
                    continue
                if include_sections is not None and row["section"] not in include_sections:
                    continue
                if row["key"] in excluded:
                    continue
                account_id = _ensure_account(
                    conn, account_cache, row["account"], row["account_kind"], now
                )
                security_id = _ensure_security(
                    conn, security_cache, row, now, new_security_ids
                )
                _upsert_snapshot(
                    conn,
                    account_id=account_id,
                    security_id=security_id,
                    lot_seq=row["lot_seq"],
                    as_of=as_of,
                    quantity=row["new_quantity"] or "0",
                    avg_cost=row["new_avg_cost"],
                    reported_price=row.get("reported_price"),
                    reported_value_jpy=row.get("reported_value_jpy"),
                    reported_pl_jpy=row.get("reported_pl_jpy"),
                    lot_label=row.get("lot_label"),
                    batch_id=batch_id,
                    raw=row.get("raw") or {},
                    now=now,
                )
                if row["section"] in ("stock", "fund") and row.get("reported_price"):
                    conn.execute(
                        """INSERT INTO daily_prices (source, source_id, date, price, currency)
                           VALUES ('mf_reported', ?, ?, ?, 'JPY')
                           ON CONFLICT(source, source_id, date)
                           DO UPDATE SET price = excluded.price""",
                        (str(security_id), as_of, row["reported_price"]),
                    )
                if row["status"] == "new":
                    created += 1
                else:
                    updated += 1

            for key in sorted(zeros):
                row = rows_by_key.get(key)
                if row is None or row["status"] != "missing":
                    continue
                _upsert_snapshot(
                    conn,
                    account_id=row["account_id"],
                    security_id=row["security_id"],
                    lot_seq=row["lot_seq"],
                    as_of=as_of,
                    quantity="0",
                    avg_cost=None,
                    reported_price=None,
                    reported_value_jpy=None,
                    reported_pl_jpy=None,
                    lot_label=row.get("lot_label"),
                    batch_id=batch_id,
                    raw={"zeroed": True, "old_quantity": row.get("old_quantity")},
                    now=now,
                )
                zeroed += 1

            conn.execute(
                """UPDATE import_batches
                   SET status = 'committed', as_of_date = ?, committed_at = ?
                   WHERE id = ?""",
                (as_of, now, batch_id),
            )
    finally:
        conn.close()
    return {
        "created": created,
        "updated": updated,
        "zeroed": zeroed,
        "new_security_ids": new_security_ids,
    }


# ----------------------------------------------------------------------
# 単一トランザクション用の内部ヘルパ（store のスキーマ規約に従う）
# ----------------------------------------------------------------------


def _ensure_account(
    conn: sqlite3.Connection, cache: dict[str, int], name: str, kind: str, now: str
) -> int:
    if not (name or "").strip():
        # 保有金融機関の読取り失敗。名前のない口座を作ると既存ロットと永久に
        # 突き合わなくなるため、確定させずトランザクションごと巻き戻す。
        raise StoreError(
            "口座名が空の行があるため取込を中止しました"
            "（PDFの保有金融機関を読み取れていません）"
        )
    if name in cache:
        return cache[name]
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row is not None:
        cache[name] = row["id"]
        return row["id"]
    cur = conn.execute(
        "INSERT INTO accounts (name, kind, origin, created_at) VALUES (?,?, 'mf', ?)",
        (name, kind, now),
    )
    cache[name] = cur.lastrowid
    return cur.lastrowid


def _resolve_security(
    conn: sqlite3.Connection, code: str | None, name_key: str | None
) -> int | None:
    """store.resolve_security と同じ照合順: code → alias → name_key → 欠けた名称。"""
    if code:
        row = conn.execute("SELECT id FROM securities WHERE code = ?", (code,)).fetchone()
        if row:
            return row["id"]
    if name_key:
        row = conn.execute(
            "SELECT security_id FROM security_aliases WHERE alias_key = ? AND source_kind = 'mf_pdf'",
            (name_key,),
        ).fetchone()
        if row:
            return row["security_id"]
        row = conn.execute(
            "SELECT id FROM securities WHERE name_key = ? ORDER BY id LIMIT 1",
            (name_key,),
        ).fetchone()
        if row:
            return row["id"]
        if looks_truncated_name_key(name_key):
            rows = conn.execute(
                resolve_truncated_sql(), (name_key, like_prefix(name_key))
            ).fetchall()
            if len(rows) == 1:
                return rows[0]["id"]
    return None


def _ensure_security(
    conn: sqlite3.Connection,
    cache: dict[str, int],
    row: dict[str, Any],
    now: str,
    new_ids: list[int] | None = None,
) -> int:
    identity = row["code"] if row["code"] else row["name_key"]
    if identity in cache:
        return cache[identity]
    security_id = row.get("security_id")
    if security_id is not None:
        found = conn.execute(
            "SELECT id FROM securities WHERE id = ?", (security_id,)
        ).fetchone()
        if found is None:
            security_id = None
    if security_id is None:
        security_id = _resolve_security(conn, row["code"], row["name_key"])
    if security_id is None:
        cur = conn.execute(
            """INSERT INTO securities
               (code, name, name_key, asset_class, currency, unit,
                price_unit_divisor, price_source_type, price_source_ref,
                price_source_status, inactive, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
            (
                row["code"],
                row["name"],
                row["name_key"],
                row["asset_class"],
                row["currency"],
                row["unit"],
                row["price_unit_divisor"],
                row["price_source_type"],
                row["price_source_ref"],
                row["price_source_status"],
                now,
            ),
        )
        security_id = cur.lastrowid
        if new_ids is not None:
            new_ids.append(security_id)
        # 新規銘柄は name_key を alias にも登録（名称変更時の名寄せ用）
        conn.execute(
            """INSERT INTO security_aliases (alias_key, source_kind, security_id)
               VALUES (?, 'mf_pdf', ?)
               ON CONFLICT(alias_key, source_kind)
               DO UPDATE SET security_id = excluded.security_id""",
            (row["name_key"], security_id),
        )
    cache[identity] = security_id
    return security_id


def _upsert_snapshot(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    security_id: int,
    lot_seq: int,
    as_of: str,
    quantity: str,
    avg_cost: str | None,
    reported_price: str | None,
    reported_value_jpy: str | None,
    reported_pl_jpy: str | None,
    lot_label: str | None,
    batch_id: str,
    raw: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """INSERT INTO holding_snapshots
           (account_id, security_id, lot_seq, as_of_date, quantity, avg_cost,
            reported_price, reported_value_jpy, reported_pl_jpy, lot_label,
            origin, batch_id, raw, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'mf', ?, ?, ?)
           ON CONFLICT(account_id, security_id, lot_seq, as_of_date) DO UPDATE SET
             quantity = excluded.quantity,
             avg_cost = excluded.avg_cost,
             reported_price = excluded.reported_price,
             reported_value_jpy = excluded.reported_value_jpy,
             reported_pl_jpy = excluded.reported_pl_jpy,
             lot_label = excluded.lot_label,
             origin = excluded.origin,
             batch_id = excluded.batch_id,
             raw = excluded.raw""",
        (
            account_id,
            security_id,
            lot_seq,
            as_of,
            quantity,
            avg_cost,
            reported_price,
            reported_value_jpy,
            reported_pl_jpy,
            lot_label,
            batch_id,
            json.dumps(raw, ensure_ascii=False),
            now,
        ),
    )
