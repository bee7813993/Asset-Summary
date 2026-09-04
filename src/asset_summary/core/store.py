"""SQLite persistence for Asset Summary.

Connection conventions follow Crypto-Summary's ledger.py: WAL journal mode,
busy_timeout 30s, synchronous NORMAL, Decimal stored as TEXT. A new
connection is opened per operation (cheap locally, avoids cross-thread
sharing under uvicorn).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import re_index
from .models import (
    Account,
    HoldingSnapshot,
    ImportBatch,
    Security,
    Transaction,
    TxType,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    display_name TEXT,
    kind         TEXT NOT NULL DEFAULT 'other',
    origin       TEXT NOT NULL DEFAULT 'manual',
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS securities (
    id                  INTEGER PRIMARY KEY,
    code                TEXT,
    name                TEXT NOT NULL,
    name_key            TEXT NOT NULL,
    asset_class         TEXT NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'JPY',
    unit                TEXT NOT NULL DEFAULT 'share',
    price_unit_divisor  INTEGER NOT NULL DEFAULT 1,
    price_source_type   TEXT NOT NULL DEFAULT 'none',
    price_source_ref    TEXT,
    price_source_status TEXT NOT NULL DEFAULT 'unlinked',
    inactive            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sec_code
    ON securities(code) WHERE code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sec_namekey ON securities(name_key);

CREATE TABLE IF NOT EXISTS security_aliases (
    alias_key   TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'mf_pdf',
    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
    PRIMARY KEY (alias_key, source_kind)
);

CREATE TABLE IF NOT EXISTS import_batches (
    id           TEXT PRIMARY KEY,
    source_kind  TEXT NOT NULL,
    filename     TEXT,
    file_sha256  TEXT,
    as_of_date   TEXT,
    status       TEXT NOT NULL DEFAULT 'previewed',
    parse_report TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    committed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_sha
    ON import_batches(file_sha256)
    WHERE status = 'committed' AND file_sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS holding_snapshots (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER NOT NULL REFERENCES accounts(id),
    security_id        INTEGER NOT NULL REFERENCES securities(id),
    lot_seq            INTEGER NOT NULL DEFAULT 0,
    as_of_date         TEXT NOT NULL,
    quantity           TEXT NOT NULL,
    avg_cost           TEXT,
    reported_price     TEXT,
    reported_value_jpy TEXT,
    reported_pl_jpy    TEXT,
    lot_label          TEXT,
    origin             TEXT NOT NULL DEFAULT 'manual',
    batch_id           TEXT REFERENCES import_batches(id),
    raw                TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    UNIQUE (account_id, security_id, lot_seq, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_snap_asof ON holding_snapshots(as_of_date);
CREATE INDEX IF NOT EXISTS idx_snap_sec  ON holding_snapshots(security_id, as_of_date);
CREATE INDEX IF NOT EXISTS idx_snap_batch ON holding_snapshots(batch_id);

CREATE VIEW IF NOT EXISTS current_holdings AS
SELECT h.*
FROM holding_snapshots h
JOIN (
    SELECT account_id, security_id, lot_seq, MAX(as_of_date) AS max_d
    FROM holding_snapshots GROUP BY account_id, security_id, lot_seq
) latest ON  h.account_id = latest.account_id
         AND h.security_id = latest.security_id
         AND h.lot_seq     = latest.lot_seq
         AND h.as_of_date  = latest.max_d;

CREATE TABLE IF NOT EXISTS daily_prices (
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    date       TEXT NOT NULL,
    price      TEXT NOT NULL,
    currency   TEXT NOT NULL DEFAULT 'JPY',
    PRIMARY KEY (source, source_id, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS fetched_ranges (
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id, start_date)
);

-- ============================================================
-- タグと Myポートフォリオ
-- ============================================================
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    color      TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 銘柄→タグの配分。weight は%で、1銘柄の合計が100になるようにする
-- （オルカンのように複数の資産を含む投信を按分するため）。
-- 合計が100未満の残りは集計時に「未分類」として扱う。
CREATE TABLE IF NOT EXISTS security_tags (
    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight      TEXT NOT NULL DEFAULT '100',
    PRIMARY KEY (security_id, tag_id)
);

CREATE TABLE IF NOT EXISTS portfolios (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    note       TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- ポートフォリオの構成条件: タグ（OR条件・按分計上）
CREATE TABLE IF NOT EXISTS portfolio_tags (
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    tag_id       INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (portfolio_id, tag_id)
);

-- 個別の追加（全額計上）/ 除外（タグ条件より優先）
-- 外部アプリ（Crypto-Summary）由来の資産へのタグ配分。
-- 実体は向こうにあり securities には行が無いため、キーは "cs:BTC" のような
-- 文字列。保存するのは「利用者による分類」だけで、残高は保存しない。
CREATE TABLE IF NOT EXISTS external_asset_tags (
    asset_key TEXT NOT NULL,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight    TEXT NOT NULL DEFAULT '100',
    PRIMARY KEY (asset_key, tag_id)
);

CREATE TABLE IF NOT EXISTS portfolio_members (
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    security_id  INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
    mode         TEXT NOT NULL DEFAULT 'include',
    PRIMARY KEY (portfolio_id, security_id)
);

-- 価格取得の試行記録。失敗も記録することで「毎リクエスト再取得」を防ぐ
-- （fetched_ranges は成功時しか書かないためガードにならない）。
CREATE TABLE IF NOT EXISTS fetch_attempts (
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    ok           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS spot_cache (
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    price      TEXT NOT NULL,
    currency   TEXT NOT NULL DEFAULT 'JPY',
    fetched_at REAL NOT NULL,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ============================================================
-- 取引履歴（証券会社CSV等）
--
-- アプリの中核はスナップショット方式のまま。取引台帳は「スナップショットを
-- どう説明するか」を持つ補助であって、正ではない（DESIGN.md の方針を維持）。
-- ============================================================

CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY,
    dedup_key    TEXT NOT NULL UNIQUE,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    security_id  INTEGER REFERENCES securities(id),  -- NULL = 未照合
    trade_date   TEXT NOT NULL,
    settle_date  TEXT,
    tx_type      TEXT NOT NULL,
    quantity     TEXT,          -- 符号つき増減（買+ / 売−）。配当行は NULL
    unit_price   TEXT,          -- price_unit_divisor 適用前
    gross_amount TEXT,
    fee          TEXT,
    tax          TEXT,          -- 源泉徴収税額。実現損益からは引かない
    net_amount   TEXT,          -- 符号つき（買− / 売+ / 配当+）
    split_ratio  TEXT,
    currency     TEXT NOT NULL DEFAULT 'JPY',
    lot_label    TEXT,          -- 特定 / 一般 / NISA
    note         TEXT,
    origin       TEXT NOT NULL DEFAULT 'broker_csv',
    broker_ref   TEXT,
    batch_id     TEXT REFERENCES import_batches(id),
    raw          TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_group ON transactions(account_id, security_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_tx_sec   ON transactions(security_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_tx_batch ON transactions(batch_id);

-- バッチが「見た」行の記録。重複でスキップした行も残す。
-- これが無いと、期間が重なる2つのバッチのうち古い方を巻き戻したときに、
-- 新しい方が依拠している行まで消えて無言のデータ欠損になる。
CREATE TABLE IF NOT EXISTS transaction_batches (
    batch_id  TEXT NOT NULL REFERENCES import_batches(id),
    dedup_key TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, dedup_key)
) WITHOUT ROWID;

-- 取引台帳から再計算した取得原価。transactions から常に再生成できる派生値。
CREATE TABLE IF NOT EXISTS holding_cost_basis (
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    security_id       INTEGER NOT NULL REFERENCES securities(id),
    as_of_date        TEXT NOT NULL,   -- 突き合わせたスナップショットの基準日
    coverage          TEXT NOT NULL,   -- full | partial | partial_uncosted | unreconciled
    applies_to_pl     INTEGER NOT NULL DEFAULT 0,
    avg_cost          TEXT,
    acquired_on       TEXT,
    acquired_on_src   TEXT,            -- csv | mf_raw
    covered_quantity  TEXT,
    residual_quantity TEXT,
    residual_avg_cost TEXT,
    realized_pl       TEXT,
    income_total      TEXT,
    withheld_tax      TEXT,
    lot_scope         TEXT NOT NULL DEFAULT 'lot',
    tx_count          INTEGER NOT NULL DEFAULT 0,
    first_tx_date     TEXT,
    last_tx_date      TEXT,
    batch_id          TEXT REFERENCES import_batches(id),
    warnings          TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    PRIMARY KEY (account_id, security_id)
);
CREATE INDEX IF NOT EXISTS idx_cb_batch ON holding_cost_basis(batch_id);

-- 確定した列対応を見出しの指紋で覚え、次回同じ書式なら自動適用する
CREATE TABLE IF NOT EXISTS tx_format_profiles (
    id            INTEGER PRIMARY KEY,
    fingerprint   TEXT NOT NULL UNIQUE,
    label         TEXT,
    header_labels TEXT NOT NULL DEFAULT '[]',
    mapping       TEXT NOT NULL DEFAULT '{}',
    options       TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    use_count     INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    "include_pension": "1",
    "include_points": "1",
    "default_currency": "JPY",
    # 保有一覧で預金・現金を「A銀行 他N件」の1行に合算する（portfolio.merge_cash_enabled）
    "merge_cash": "1",
}


class StoreError(Exception):
    pass


class ConflictError(StoreError):
    """Raised when a delete/update would violate integrity (HTTP 409)."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d2s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _s2d(value: str | None) -> Decimal | None:
    return None if value in (None, "") else Decimal(value)


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        name=row["name"],
        display_name=row["display_name"],
        kind=row["kind"],
        origin=row["origin"],
        sort_order=row["sort_order"],
    )


def _row_to_security(row: sqlite3.Row) -> Security:
    return Security(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        name_key=row["name_key"],
        asset_class=row["asset_class"],
        currency=row["currency"],
        unit=row["unit"],
        price_unit_divisor=row["price_unit_divisor"],
        price_source_type=row["price_source_type"],
        price_source_ref=row["price_source_ref"],
        price_source_status=row["price_source_status"],
        inactive=bool(row["inactive"]),
    )


def _row_to_snapshot(row: sqlite3.Row) -> HoldingSnapshot:
    return HoldingSnapshot(
        id=row["id"],
        account_id=row["account_id"],
        security_id=row["security_id"],
        lot_seq=row["lot_seq"],
        as_of_date=date.fromisoformat(row["as_of_date"]),
        quantity=Decimal(row["quantity"]),
        avg_cost=_s2d(row["avg_cost"]),
        reported_price=_s2d(row["reported_price"]),
        reported_value_jpy=_s2d(row["reported_value_jpy"]),
        reported_pl_jpy=_s2d(row["reported_pl_jpy"]),
        lot_label=row["lot_label"],
        origin=row["origin"],
        batch_id=row["batch_id"],
        raw=json.loads(row["raw"] or "{}"),
    )


def _row_to_transaction(row: sqlite3.Row) -> Transaction:
    return Transaction(
        id=row["id"],
        dedup_key=row["dedup_key"],
        account_id=row["account_id"],
        security_id=row["security_id"],
        trade_date=date.fromisoformat(row["trade_date"]),
        settle_date=date.fromisoformat(row["settle_date"]) if row["settle_date"] else None,
        tx_type=TxType(row["tx_type"]),
        quantity=_s2d(row["quantity"]),
        unit_price=_s2d(row["unit_price"]),
        gross_amount=_s2d(row["gross_amount"]),
        fee=_s2d(row["fee"]),
        tax=_s2d(row["tax"]),
        net_amount=_s2d(row["net_amount"]),
        split_ratio=_s2d(row["split_ratio"]),
        currency=row["currency"],
        lot_label=row["lot_label"],
        note=row["note"],
        origin=row["origin"],
        broker_ref=row["broker_ref"],
        batch_id=row["batch_id"],
        raw=json.loads(row["raw"] or "{}"),
    )


def _row_to_batch(row: sqlite3.Row) -> ImportBatch:
    return ImportBatch(
        id=row["id"],
        source_kind=row["source_kind"],
        filename=row["filename"],
        file_sha256=row["file_sha256"],
        as_of_date=date.fromisoformat(row["as_of_date"]) if row["as_of_date"] else None,
        status=row["status"],
        parse_report=json.loads(row["parse_report"] or "{}"),
    )


def looks_truncated_name_key(name_key: str) -> bool:
    """改ページで末尾が欠けた名称か。開き括弧が閉じていなければ途中で切れている。

    MF の PDF は改ページ位置でセルの続きをページ下端の装飾に重ねて描くことがあり、
    その行の末尾数文字が失われた name_key で届く
    （例: 「…(架空・プラチナ(為替ヘッジな」= 末尾の「し))」が欠落）。
    「ソフトバンク」と「ソフトバンクグループ」のように正当に短い別銘柄は括弧が
    釣り合うので、この判定には掛からない。
    """
    return name_key.count("(") > name_key.count(")")


def resolve_truncated_sql() -> str:
    """欠けた name_key を前方一致で1件に絞る SQL。呼び出し側で件数を確認する。"""
    return (
        "SELECT id FROM securities "
        r"WHERE name_key <> ? AND name_key LIKE ? ESCAPE '\' LIMIT 2"
    )


def like_prefix(name_key: str) -> str:
    """LIKE の前方一致パターン。名称中の % _ \\ はエスケープする。"""
    escaped = (
        name_key.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return escaped + "%"


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if self.db_path.parent and str(self.db_path.parent) not in (".", ""):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------

    def get_or_create_account(
        self, name: str, kind: str = "other", origin: str = "manual"
    ) -> Account:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
            if row:
                return _row_to_account(row)
            cur = conn.execute(
                "INSERT INTO accounts (name, kind, origin, created_at) VALUES (?,?,?,?)",
                (name, kind, origin, _utcnow()),
            )
            return Account(id=cur.lastrowid, name=name, kind=kind, origin=origin)

    def list_accounts(self) -> list[Account]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY sort_order, name"
            ).fetchall()
        return [_row_to_account(r) for r in rows]

    def get_account(self, account_id: int) -> Account | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return _row_to_account(row) if row else None

    def get_account_by_name(self, name: str) -> Account | None:
        if not (name or "").strip():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
        return _row_to_account(row) if row else None

    def update_account(self, account_id: int, **fields: Any) -> None:
        allowed = {"display_name", "kind", "sort_order", "name"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE accounts SET {cols} WHERE id = ?",
                (*sets.values(), account_id),
            )

    # ------------------------------------------------------------------
    # securities
    # ------------------------------------------------------------------

    def create_security(self, sec: Security) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO securities
                   (code, name, name_key, asset_class, currency, unit,
                    price_unit_divisor, price_source_type, price_source_ref,
                    price_source_status, inactive, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sec.code,
                    sec.name,
                    sec.name_key,
                    sec.asset_class.value,
                    sec.currency,
                    sec.unit.value,
                    sec.price_unit_divisor,
                    sec.price_source_type.value,
                    sec.price_source_ref,
                    sec.price_source_status.value,
                    int(sec.inactive),
                    _utcnow(),
                ),
            )
            return cur.lastrowid

    def update_security(
        self, security_id: int, clear: tuple[str, ...] = (), **fields: Any
    ) -> None:
        """銘柄を部分更新する。

        None の項目は「変更しない」を意味する（呼び出し側は変えたい列だけ渡す）。
        **NULL を明示的に入れたい列は clear で名指しする** —— そうしないと
        price_source_ref のような nullable 列を一度設定したら二度と外せない。
        """
        allowed = {
            "code",
            "name",
            "name_key",
            "asset_class",
            "currency",
            "unit",
            "price_unit_divisor",
            "price_source_type",
            "price_source_ref",
            "price_source_status",
            "inactive",
        }
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "inactive" in fields and fields["inactive"] is not None:
            sets["inactive"] = int(bool(fields["inactive"]))
        for col in clear:
            if col in allowed:
                sets[col] = None
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE securities SET {cols} WHERE id = ?",
                (*sets.values(), security_id),
            )

    def get_security(self, security_id: int) -> Security | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM securities WHERE id = ?", (security_id,)
            ).fetchone()
        return _row_to_security(row) if row else None

    def list_securities(
        self, asset_class: str | None = None, q: str | None = None
    ) -> list[Security]:
        sql = "SELECT * FROM securities WHERE 1=1"
        params: list[Any] = []
        if asset_class:
            sql += " AND asset_class = ?"
            params.append(asset_class)
        if q:
            sql += " AND (name LIKE ? OR code LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        sql += " ORDER BY name"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_security(r) for r in rows]

    def securities_by_id(self) -> dict[int, Security]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM securities").fetchall()
        return {r["id"]: _row_to_security(r) for r in rows}

    def resolve_security(
        self,
        code: str | None = None,
        name_key: str | None = None,
        source_kind: str = "mf_pdf",
    ) -> int | None:
        """照合順: code → alias → name_key → 欠けた名称の前方一致。"""
        with self.connect() as conn:
            if code:
                row = conn.execute(
                    "SELECT id FROM securities WHERE code = ?", (code,)
                ).fetchone()
                if row:
                    return row["id"]
            if name_key:
                row = conn.execute(
                    "SELECT security_id FROM security_aliases WHERE alias_key = ? AND source_kind = ?",
                    (name_key, source_kind),
                ).fetchone()
                if row:
                    return row["security_id"]
                row = conn.execute(
                    "SELECT id FROM securities WHERE name_key = ? ORDER BY id LIMIT 1",
                    (name_key,),
                ).fetchone()
                if row:
                    return row["id"]
                # 末尾が欠けた名称は、前方一致する銘柄が1件だけならそれと同じ銘柄
                # とみなす（「し」1文字の欠落で別銘柄を作らないため）。複数該当は
                # 判断できないので新規扱いに落とす。
                if looks_truncated_name_key(name_key):
                    rows = conn.execute(
                        resolve_truncated_sql(), (name_key, like_prefix(name_key))
                    ).fetchall()
                    if len(rows) == 1:
                        return rows[0]["id"]
        return None

    def add_alias(
        self, alias_key: str, security_id: int, source_kind: str = "mf_pdf"
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO security_aliases (alias_key, source_kind, security_id)
                   VALUES (?,?,?)
                   ON CONFLICT(alias_key, source_kind)
                   DO UPDATE SET security_id = excluded.security_id""",
                (alias_key, source_kind, security_id),
            )

    def delete_security(self, security_id: int) -> None:
        with self.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM holding_snapshots WHERE security_id = ?",
                (security_id,),
            ).fetchone()["c"]
            if n:
                raise ConflictError(f"security {security_id} has {n} holding snapshots")
            conn.execute("DELETE FROM security_aliases WHERE security_id = ?", (security_id,))
            conn.execute(
                "DELETE FROM daily_prices WHERE source IN ('manual','mf_reported') AND source_id = ?",
                (str(security_id),),
            )
            conn.execute("DELETE FROM securities WHERE id = ?", (security_id,))

    # 統合を許す条件。これが違う2銘柄は quantity / avg_cost の単位が異なり、
    # ロットを混ぜると評価・損益が壊れる（年金は quantity=1 で avg_cost=総額）。
    _MERGE_COMPAT_FIELDS = ("asset_class", "currency", "unit", "price_unit_divisor")

    def merge_security(self, source_id: int, target_id: int) -> dict[str, Any]:
        """source を target へ統合する（名寄せ）。

        MF PDF は同じファンドを証券会社ごとの表記で書くため、別名で二重に
        登録された銘柄がここで1つになる。保有・取引・タグ・手動価格を target へ
        移し、source の名前は alias として記憶する（次回取込から自動で当たる）。
        全変更は単一トランザクション — 途中で失敗したら何も変わらない。

        ロットの付番: target が同じ口座にスナップショットを持つ場合のみ、
        source 側の (口座, lot_seq) 系列へ空き番号を振り直す。UNIQUE
        (account_id, security_id, lot_seq, as_of_date) の衝突を避けつつ、
        系列としての同一性（同じ lot_seq が日をまたいで同じロットを指す）を保つ。

        返値: 移動件数の要約（web層のメッセージと後続の原価再計算の判断に使う）。
        """
        if source_id == target_id:
            raise StoreError("統合元と統合先が同じ銘柄です")
        src = self.get_security(source_id)
        tgt = self.get_security(target_id)
        if src is None or tgt is None:
            raise StoreError("銘柄が見つかりません")
        for field in self._MERGE_COMPAT_FIELDS:
            a, b = getattr(src, field), getattr(tgt, field)
            if a != b:
                a = getattr(a, "value", a)
                b = getattr(b, "value", b)
                raise ConflictError(
                    f"{field} が一致しないため統合できません（{a} ≠ {b}）。"
                    "同じ銘柄なら先に銘柄管理で属性を揃えてください"
                )

        with self.connect() as conn:
            # --- 保有スナップショット（必要な口座だけ lot_seq を振り直す） ---
            rows = conn.execute(
                """SELECT DISTINCT account_id, lot_seq FROM holding_snapshots
                   WHERE security_id = ? ORDER BY account_id, lot_seq""",
                (source_id,),
            ).fetchall()
            src_lots: dict[int, list[int]] = {}
            for r in rows:
                src_lots.setdefault(r["account_id"], []).append(r["lot_seq"])
            for acct_id, seqs in src_lots.items():
                t = conn.execute(
                    """SELECT COUNT(*) AS c, MAX(lot_seq) AS m FROM holding_snapshots
                       WHERE security_id = ? AND account_id = ?""",
                    (target_id, acct_id),
                ).fetchone()
                if not t["c"]:
                    continue  # 口座が重ならなければ衝突しない（番号を保つ）
                next_seq = max(t["m"], max(seqs)) + 1
                for old_seq in seqs:
                    conn.execute(
                        """UPDATE holding_snapshots SET lot_seq = ?
                           WHERE security_id = ? AND account_id = ? AND lot_seq = ?""",
                        (next_seq, source_id, acct_id, old_seq),
                    )
                    next_seq += 1
            snapshots = conn.execute(
                "UPDATE holding_snapshots SET security_id = ? WHERE security_id = ?",
                (target_id, source_id),
            ).rowcount

            # --- 取引台帳（dedup_key は口座・日付・原文由来なので変わらない） ---
            transactions = conn.execute(
                "UPDATE transactions SET security_id = ? WHERE security_id = ?",
                (target_id, source_id),
            ).rowcount
            # 原価は派生値。ロット構成が変わったので残しても正しくない。
            # target 側と重複しない行だけ移し、取引があれば呼び出し側で再計算する。
            conn.execute(
                "UPDATE OR IGNORE holding_cost_basis SET security_id = ? WHERE security_id = ?",
                (target_id, source_id),
            )
            conn.execute(
                "DELETE FROM holding_cost_basis WHERE security_id = ?", (source_id,)
            )

            # --- タグ配分・Myポートフォリオ（target 側の既存設定を優先） ---
            conn.execute(
                "UPDATE OR IGNORE security_tags SET security_id = ? WHERE security_id = ?",
                (target_id, source_id),
            )
            conn.execute("DELETE FROM security_tags WHERE security_id = ?", (source_id,))
            conn.execute(
                "UPDATE OR IGNORE portfolio_members SET security_id = ? WHERE security_id = ?",
                (target_id, source_id),
            )
            conn.execute(
                "DELETE FROM portfolio_members WHERE security_id = ?", (source_id,)
            )

            # --- 名寄せの記憶: source の別名と名前を target の alias に ---
            conn.execute(
                "UPDATE security_aliases SET security_id = ? WHERE security_id = ?",
                (target_id, source_id),
            )
            for name_key in (src.name_key, tgt.name_key):
                if name_key:
                    conn.execute(
                        """INSERT INTO security_aliases (alias_key, source_kind, security_id)
                           VALUES (?, 'mf_pdf', ?)
                           ON CONFLICT(alias_key, source_kind)
                           DO UPDATE SET security_id = excluded.security_id""",
                        (name_key, target_id),
                    )

            # --- 手動評価・MF記載値の価格系列（source_id は銘柄idの文字列） ---
            conn.execute(
                """UPDATE OR IGNORE daily_prices SET source_id = ?
                   WHERE source IN ('manual','mf_reported') AND source_id = ?""",
                (str(target_id), str(source_id)),
            )
            conn.execute(
                """DELETE FROM daily_prices
                   WHERE source IN ('manual','mf_reported') AND source_id = ?""",
                (str(source_id),),
            )
            for table in ("spot_cache", "fetch_attempts"):
                conn.execute(
                    f"DELETE FROM {table} WHERE source = 'manual' AND source_id = ?",
                    (str(source_id),),
                )

            # --- target に無い情報を source から引き継ぐ（コード・価格ソース） ---
            adopted_source = False
            if src.code and not tgt.code:
                conn.execute("UPDATE securities SET code = NULL WHERE id = ?", (source_id,))
                conn.execute(
                    "UPDATE securities SET code = ? WHERE id = ?", (src.code, target_id)
                )
            if (
                tgt.price_source_status.value == "unlinked"
                and src.price_source_status.value in ("linked", "manual")
            ):
                conn.execute(
                    """UPDATE securities SET price_source_type = ?,
                       price_source_ref = ?, price_source_status = ? WHERE id = ?""",
                    (
                        src.price_source_type.value,
                        src.price_source_ref,
                        src.price_source_status.value,
                        target_id,
                    ),
                )
                adopted_source = True

            conn.execute("DELETE FROM securities WHERE id = ?", (source_id,))

        return {
            "snapshots": snapshots,
            "transactions": transactions,
            "source_name": src.name,
            "target_name": tgt.name,
            "adopted_price_source": adopted_source,
        }

    # ------------------------------------------------------------------
    # holding snapshots
    # ------------------------------------------------------------------

    def upsert_snapshot(self, snap: HoldingSnapshot) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO holding_snapshots
                   (account_id, security_id, lot_seq, as_of_date, quantity, avg_cost,
                    reported_price, reported_value_jpy, reported_pl_jpy, lot_label,
                    origin, batch_id, raw, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    snap.account_id,
                    snap.security_id,
                    snap.lot_seq,
                    snap.as_of_date.isoformat(),
                    _d2s(snap.quantity),
                    _d2s(snap.avg_cost),
                    _d2s(snap.reported_price),
                    _d2s(snap.reported_value_jpy),
                    _d2s(snap.reported_pl_jpy),
                    snap.lot_label,
                    snap.origin,
                    snap.batch_id,
                    json.dumps(snap.raw, ensure_ascii=False),
                    _utcnow(),
                ),
            )
            return cur.lastrowid

    def current_holdings(self) -> list[HoldingSnapshot]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM current_holdings ORDER BY security_id, account_id, lot_seq"
            ).fetchall()
        return [_row_to_snapshot(r) for r in rows]

    def holdings_as_of(self, day: date) -> list[HoldingSnapshot]:
        """その日までに記録された最新のスナップショット（ロット単位）。

        current_holdings ビューと同じ「ロットごとに最新の as_of_date」を、
        day 以前に限って引く。前日比の基準（前日の保有数）に使う。
        day 以前に1件も無いロットは載らない — 呼び出し側は推移グラフと
        同じ遡及ルール（初回スナップショットの数量で埋める）で扱うこと。
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT h.* FROM holding_snapshots h
                   JOIN (
                       SELECT account_id, security_id, lot_seq, MAX(as_of_date) AS max_d
                       FROM holding_snapshots
                       WHERE as_of_date <= ?
                       GROUP BY account_id, security_id, lot_seq
                   ) latest ON  h.account_id  = latest.account_id
                            AND h.security_id = latest.security_id
                            AND h.lot_seq     = latest.lot_seq
                            AND h.as_of_date  = latest.max_d
                   ORDER BY h.security_id, h.account_id, h.lot_seq""",
                (day.isoformat(),),
            ).fetchall()
        return [_row_to_snapshot(r) for r in rows]

    def latest_snapshot_date(self) -> date | None:
        """記録されているスナップショットの最終日。1件も無ければ None。

        前日比の基準日を決めるのに使う。取込の時刻はまちまちで、暦の切り替わりと
        取込の間隔は一致しない（夜に取り込めば数時間で日付が変わる）ので、
        「今日の前日」ではなくデータ側の最新地点から数える。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(as_of_date) AS d FROM holding_snapshots"
            ).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def all_snapshots(self) -> list[HoldingSnapshot]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM holding_snapshots ORDER BY as_of_date, id"
            ).fetchall()
        return [_row_to_snapshot(r) for r in rows]

    def current_quantities_in_account(self, account_id: int) -> dict[int, Decimal]:
        """その口座の現在保有（銘柄 → 数量合計）。ロットは合算する。

        取引履歴の取込で「スナップショットのうち CSV がまだ説明できていない保有」
        を出すために使う。DESIGN の方針どおりスナップショットを錨にするなら、
        残った保有こそが「CSV のどれかがこれを指しているはず」という手がかりになる。
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT security_id, quantity FROM current_holdings
                   WHERE account_id = ?""",
                (account_id,),
            ).fetchall()
        totals: dict[int, Decimal] = {}
        for row in rows:
            sid = row["security_id"]
            totals[sid] = totals.get(sid, Decimal(0)) + Decimal(row["quantity"])
        return {sid: q for sid, q in totals.items() if q != 0}

    def security_ids_in_account(self, account_id: int) -> set[int]:
        """その口座で保有が記録されている銘柄。

        同じファンドが証券会社ごとに別名で登録されることがあるため
        （MF PDF は証券会社ごとの表記をそのまま書く）、取引履歴の取込で
        候補が絞れないときに「その口座が実際に持っているほう」を選ぶのに使う。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT security_id FROM holding_snapshots WHERE account_id = ?",
                (account_id,),
            ).fetchall()
        return {r["security_id"] for r in rows}

    def get_snapshot(self, snapshot_id: int) -> HoldingSnapshot | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM holding_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        return _row_to_snapshot(row) if row else None

    def delete_snapshot(self, snapshot_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM holding_snapshots WHERE id = ?", (snapshot_id,))

    def latest_lots(
        self, account_id: int, security_id: int
    ) -> list[HoldingSnapshot]:
        """(口座,銘柄)の現在ロット一覧（lot_seq ごとの最新スナップショット）。"""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM current_holdings
                   WHERE account_id = ? AND security_id = ?
                   ORDER BY lot_seq""",
                (account_id, security_id),
            ).fetchall()
        return [_row_to_snapshot(r) for r in rows]

    # ------------------------------------------------------------------
    # tags
    # ------------------------------------------------------------------

    def list_tags(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tags ORDER BY sort_order, name"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "color": r["color"],
                "sort_order": r["sort_order"],
            }
            for r in rows
        ]

    def get_tag(self, tag_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if r is None:
            return None
        return {
            "id": r["id"],
            "name": r["name"],
            "color": r["color"],
            "sort_order": r["sort_order"],
        }

    def create_tag(self, name: str, color: str | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO tags (name, color, created_at) VALUES (?,?,?)",
                (name, color, _utcnow()),
            )
            return cur.lastrowid

    def update_tag(self, tag_id: int, **fields: Any) -> None:
        allowed = {"name", "color", "sort_order"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE tags SET {cols} WHERE id = ?", (*sets.values(), tag_id)
            )

    def delete_tag(self, tag_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM security_tags WHERE tag_id = ?", (tag_id,))
            conn.execute("DELETE FROM portfolio_tags WHERE tag_id = ?", (tag_id,))
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    def security_tag_map(self) -> dict[int, dict[int, Decimal]]:
        """security_id → {tag_id: 配分率(%)}。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT security_id, tag_id, weight FROM security_tags"
            ).fetchall()
        out: dict[int, dict[int, Decimal]] = {}
        for r in rows:
            out.setdefault(r["security_id"], {})[r["tag_id"]] = Decimal(r["weight"])
        return out

    def get_security_tags(self, security_id: int) -> dict[int, Decimal]:
        return self.security_tag_map().get(security_id, {})

    def set_security_tags(
        self, security_id: int, allocations: dict[int, Decimal]
    ) -> None:
        """銘柄のタグ配分を置き換える（weight<=0 は削除）。"""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM security_tags WHERE security_id = ?", (security_id,)
            )
            for tag_id, weight in allocations.items():
                if weight <= 0:
                    continue
                conn.execute(
                    """INSERT INTO security_tags (security_id, tag_id, weight)
                       VALUES (?,?,?)""",
                    (security_id, tag_id, str(weight)),
                )

    def tag_usage_count(self, tag_id: int) -> int:
        """このタグを使っている資産の件数（外部アプリ由来の資産も数える）。"""
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM security_tags WHERE tag_id = ?",
                    (tag_id,),
                ).fetchone()["c"]
                + conn.execute(
                    "SELECT COUNT(*) AS c FROM external_asset_tags WHERE tag_id = ?",
                    (tag_id,),
                ).fetchone()["c"]
            )

    # ---- 外部アプリ由来の資産（Crypto-Summary のコイン等）へのタグ配分 ----
    #
    # security_tags と同じ形だが、キーが文字列（"cs:BTC"）である点だけが違う。
    # tagging 層は id を辞書キーとしか見ないため、両者を混ぜた 1 つのマップを
    # 渡せばそのまま按分集計できる。

    def external_tag_map(self) -> dict[str, dict[int, Decimal]]:
        """asset_key → {tag_id: 配分率(%)}。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT asset_key, tag_id, weight FROM external_asset_tags"
            ).fetchall()
        out: dict[str, dict[int, Decimal]] = {}
        for r in rows:
            out.setdefault(r["asset_key"], {})[r["tag_id"]] = Decimal(r["weight"])
        return out

    def get_external_tags(self, asset_key: str) -> dict[int, Decimal]:
        return self.external_tag_map().get(asset_key, {})

    def set_external_tags(
        self, asset_key: str, allocations: dict[int, Decimal]
    ) -> None:
        """外部資産のタグ配分を置き換える（weight<=0 は削除）。"""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM external_asset_tags WHERE asset_key = ?", (asset_key,)
            )
            for tag_id, weight in allocations.items():
                if weight <= 0:
                    continue
                conn.execute(
                    """INSERT INTO external_asset_tags (asset_key, tag_id, weight)
                       VALUES (?,?,?)""",
                    (asset_key, tag_id, str(weight)),
                )

    # ------------------------------------------------------------------
    # portfolios
    # ------------------------------------------------------------------

    def list_portfolios(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM portfolios ORDER BY sort_order, name"
            ).fetchall()
            tags = conn.execute("SELECT * FROM portfolio_tags").fetchall()
            members = conn.execute("SELECT * FROM portfolio_members").fetchall()
        tag_map: dict[int, list[int]] = {}
        for t in tags:
            tag_map.setdefault(t["portfolio_id"], []).append(t["tag_id"])
        inc: dict[int, list[int]] = {}
        exc: dict[int, list[int]] = {}
        for m in members:
            target = inc if m["mode"] == "include" else exc
            target.setdefault(m["portfolio_id"], []).append(m["security_id"])
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "note": r["note"],
                "sort_order": r["sort_order"],
                "tag_ids": sorted(tag_map.get(r["id"], [])),
                "include_security_ids": sorted(inc.get(r["id"], [])),
                "exclude_security_ids": sorted(exc.get(r["id"], [])),
            }
            for r in rows
        ]

    def get_portfolio(self, portfolio_id: int) -> dict[str, Any] | None:
        for p in self.list_portfolios():
            if p["id"] == portfolio_id:
                return p
        return None

    def create_portfolio(self, name: str, note: str | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO portfolios (name, note, created_at) VALUES (?,?,?)",
                (name, note, _utcnow()),
            )
            return cur.lastrowid

    def update_portfolio(self, portfolio_id: int, **fields: Any) -> None:
        allowed = {"name", "note", "sort_order"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            with self.connect() as conn:
                conn.execute(
                    f"UPDATE portfolios SET {cols} WHERE id = ?",
                    (*sets.values(), portfolio_id),
                )

    def set_portfolio_composition(
        self,
        portfolio_id: int,
        tag_ids: list[int] | None = None,
        include_security_ids: list[int] | None = None,
        exclude_security_ids: list[int] | None = None,
    ) -> None:
        with self.connect() as conn:
            if tag_ids is not None:
                conn.execute(
                    "DELETE FROM portfolio_tags WHERE portfolio_id = ?", (portfolio_id,)
                )
                for tag_id in dict.fromkeys(tag_ids):
                    conn.execute(
                        "INSERT INTO portfolio_tags (portfolio_id, tag_id) VALUES (?,?)",
                        (portfolio_id, tag_id),
                    )
            if include_security_ids is not None or exclude_security_ids is not None:
                if include_security_ids is not None:
                    conn.execute(
                        "DELETE FROM portfolio_members WHERE portfolio_id = ? AND mode = 'include'",
                        (portfolio_id,),
                    )
                    for sec_id in dict.fromkeys(include_security_ids):
                        conn.execute(
                            """INSERT INTO portfolio_members (portfolio_id, security_id, mode)
                               VALUES (?,?, 'include')
                               ON CONFLICT(portfolio_id, security_id)
                               DO UPDATE SET mode = 'include'""",
                            (portfolio_id, sec_id),
                        )
                if exclude_security_ids is not None:
                    conn.execute(
                        "DELETE FROM portfolio_members WHERE portfolio_id = ? AND mode = 'exclude'",
                        (portfolio_id,),
                    )
                    for sec_id in dict.fromkeys(exclude_security_ids):
                        conn.execute(
                            """INSERT INTO portfolio_members (portfolio_id, security_id, mode)
                               VALUES (?,?, 'exclude')
                               ON CONFLICT(portfolio_id, security_id)
                               DO UPDATE SET mode = 'exclude'""",
                            (portfolio_id, sec_id),
                        )

    def delete_portfolio(self, portfolio_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM portfolio_tags WHERE portfolio_id = ?", (portfolio_id,)
            )
            conn.execute(
                "DELETE FROM portfolio_members WHERE portfolio_id = ?", (portfolio_id,)
            )
            conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))

    def latest_reported_price(
        self, security_id: int
    ) -> tuple[Decimal, date] | None:
        """MF取込時に記録された直近の基準価額/現在値と、その基準日。

        投信の自動連携で「候補の基準価額と一致するか」の照合に使う。
        """
        with self.connect() as conn:
            row = conn.execute(
                """SELECT reported_price, as_of_date FROM holding_snapshots
                   WHERE security_id = ? AND reported_price IS NOT NULL
                   ORDER BY as_of_date DESC LIMIT 1""",
                (security_id,),
            ).fetchone()
        if row is None:
            return None
        return (Decimal(row["reported_price"]), date.fromisoformat(row["as_of_date"]))

    # ------------------------------------------------------------------
    # import batches
    # ------------------------------------------------------------------

    def create_batch(self, batch: ImportBatch) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO import_batches
                   (id, source_kind, filename, file_sha256, as_of_date, status,
                    parse_report, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    batch.id,
                    batch.source_kind,
                    batch.filename,
                    batch.file_sha256,
                    batch.as_of_date.isoformat() if batch.as_of_date else None,
                    batch.status,
                    json.dumps(batch.parse_report, ensure_ascii=False),
                    _utcnow(),
                ),
            )

    def get_batch(self, batch_id: str) -> ImportBatch | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return _row_to_batch(row) if row else None

    def update_batch(self, batch_id: str, **fields: Any) -> None:
        allowed = {"status", "as_of_date", "parse_report", "committed_at"}
        sets: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "parse_report":
                v = json.dumps(v, ensure_ascii=False)
            if k == "as_of_date" and isinstance(v, date):
                v = v.isoformat()
            sets[k] = v
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE import_batches SET {cols} WHERE id = ?",
                (*sets.values(), batch_id),
            )

    def find_committed_batch_by_sha(self, sha256: str) -> ImportBatch | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM import_batches WHERE file_sha256 = ? AND status = 'committed'",
                (sha256,),
            ).fetchone()
        return _row_to_batch(row) if row else None

    def list_batches(self, status: str = "committed") -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT b.*,
                          CASE WHEN b.source_kind = 'broker_csv'
                               THEN (SELECT COUNT(*) FROM transactions t
                                     WHERE t.batch_id = b.id)
                               ELSE (SELECT COUNT(*) FROM holding_snapshots h
                                     WHERE h.batch_id = b.id)
                          END AS row_count
                   FROM import_batches b
                   WHERE b.status = ?
                   ORDER BY b.created_at DESC""",
                (status,),
            ).fetchall()
        out = []
        for r in rows:
            b = _row_to_batch(r)
            out.append(
                {
                    "id": b.id,
                    "source_kind": b.source_kind,
                    "filename": b.filename,
                    "as_of_date": b.as_of_date.isoformat() if b.as_of_date else None,
                    "status": b.status,
                    "created_at": r["created_at"],
                    "committed_at": r["committed_at"],
                    "row_count": r["row_count"],
                }
            )
        return out

    def delete_batch(self, batch_id: str) -> int:
        """バッチとその書き込みを削除（巻き戻し）。削除行数を返す。

        取込時に書いた mf_reported 価格行も同じ基準日ぶん削除する
        （残すと巻き戻し後も取込時の価格が評価に使われ続けるため）。

        取引台帳は「他のバッチも見た行」を消さない。期間が重なる 2 つの CSV を
        取り込んだあと古い方を巻き戻すと、重複としてスキップされただけの行
        （所有者は古いバッチのまま）が消えて、新しいバッチが依拠するデータが
        無言で欠ける。transaction_batches に残した「見た記録」で判定し、
        生き残る行は残っているバッチへ付け替える。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT as_of_date FROM import_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            as_of = row["as_of_date"] if row else None

            _affected = [  # noqa: F841  巻き戻し後の再計算は service 側で全体に対して行う
                (r["account_id"], r["security_id"])
                for r in conn.execute(
                    """SELECT DISTINCT account_id, security_id FROM transactions
                       WHERE batch_id = ? AND security_id IS NOT NULL""",
                    (batch_id,),
                ).fetchall()
            ]

            conn.execute("DELETE FROM transaction_batches WHERE batch_id = ?", (batch_id,))
            conn.execute(
                """DELETE FROM transactions
                   WHERE batch_id = ?
                     AND NOT EXISTS (SELECT 1 FROM transaction_batches tb
                                     WHERE tb.dedup_key = transactions.dedup_key)""",
                (batch_id,),
            )
            conn.execute(
                """UPDATE transactions
                   SET batch_id = (SELECT tb.batch_id FROM transaction_batches tb
                                   WHERE tb.dedup_key = transactions.dedup_key
                                   ORDER BY tb.linked_at, tb.batch_id LIMIT 1)
                   WHERE batch_id = ?""",
                (batch_id,),
            )
            conn.execute("DELETE FROM holding_cost_basis WHERE batch_id = ?", (batch_id,))

            cur = conn.execute(
                "DELETE FROM holding_snapshots WHERE batch_id = ?", (batch_id,)
            )
            n = cur.rowcount
            if as_of:
                conn.execute(
                    "DELETE FROM daily_prices WHERE source = 'mf_reported' AND date = ?",
                    (as_of,),
                )
            conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
        return n

    # ------------------------------------------------------------------
    # 取引台帳
    # ------------------------------------------------------------------

    def insert_transactions(
        self, txs: list[Transaction], batch_id: str
    ) -> tuple[int, int]:
        """取引を投入する。返値は (新規, 重複でスキップ)。

        dedup_key で冪等。期間が重なる再取込でも二重計上しない。
        スキップした行も transaction_batches に記録する（巻き戻しの安全のため）。
        """
        now = _utcnow()
        inserted = skipped = 0
        with self.connect() as conn:
            for tx in txs:
                if not tx.dedup_key:
                    raise StoreError("dedup_key の無い取引は保存できません")
                cur = conn.execute(
                    """INSERT INTO transactions
                       (dedup_key, account_id, security_id, trade_date, settle_date,
                        tx_type, quantity, unit_price, gross_amount, fee, tax,
                        net_amount, split_ratio, currency, lot_label, note, origin,
                        broker_ref, batch_id, raw, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(dedup_key) DO NOTHING""",
                    (
                        tx.dedup_key, tx.account_id, tx.security_id,
                        tx.trade_date.isoformat(),
                        tx.settle_date.isoformat() if tx.settle_date else None,
                        tx.tx_type.value if hasattr(tx.tx_type, "value") else tx.tx_type,
                        _d2s(tx.quantity), _d2s(tx.unit_price), _d2s(tx.gross_amount),
                        _d2s(tx.fee), _d2s(tx.tax), _d2s(tx.net_amount),
                        _d2s(tx.split_ratio), tx.currency, tx.lot_label, tx.note,
                        tx.origin, tx.broker_ref, batch_id,
                        json.dumps(tx.raw, ensure_ascii=False, default=str), now,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
                conn.execute(
                    """INSERT INTO transaction_batches (batch_id, dedup_key, linked_at)
                       VALUES (?,?,?) ON CONFLICT(batch_id, dedup_key) DO NOTHING""",
                    (batch_id, tx.dedup_key, now),
                )
        return (inserted, skipped)

    def list_transactions(
        self,
        *,
        security_id: int | None = None,
        account_id: int | None = None,
        batch_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Transaction]:
        clauses, params = [], []
        if security_id is not None:
            clauses.append("security_id = ?")
            params.append(security_id)
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM transactions {where} ORDER BY trade_date, id"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_transaction(r) for r in rows]

    def count_transactions(self, **kw: Any) -> int:
        clauses, params = [], []
        for field in ("security_id", "account_id", "batch_id"):
            value = kw.get(field)
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM transactions {where}", params
            ).fetchone()
        return row["c"]

    def existing_dedup_keys(self, keys: list[str]) -> set[str]:
        """すでに台帳にある dedup_key（プレビューで「取込済み」を出すため）。"""
        found: set[str] = set()
        if not keys:
            return found
        with self.connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT dedup_key FROM transactions WHERE dedup_key IN ({marks})",
                    chunk,
                ).fetchall()
                found.update(r["dedup_key"] for r in rows)
        return found

    # ------------------------------------------------------------------
    # 再計算した取得原価
    # ------------------------------------------------------------------

    def replace_cost_basis(
        self, rows: list[dict[str, Any]], batch_id: str | None = None
    ) -> int:
        now = _utcnow()
        with self.connect() as conn:
            for row in rows:
                conn.execute(
                    """INSERT INTO holding_cost_basis
                       (account_id, security_id, as_of_date, coverage, applies_to_pl,
                        avg_cost, acquired_on, acquired_on_src, covered_quantity,
                        residual_quantity, residual_avg_cost, realized_pl, income_total,
                        withheld_tax, lot_scope, tx_count, first_tx_date, last_tx_date,
                        batch_id, warnings, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_id, security_id) DO UPDATE SET
                         as_of_date = excluded.as_of_date,
                         coverage = excluded.coverage,
                         applies_to_pl = excluded.applies_to_pl,
                         avg_cost = excluded.avg_cost,
                         acquired_on = excluded.acquired_on,
                         acquired_on_src = excluded.acquired_on_src,
                         covered_quantity = excluded.covered_quantity,
                         residual_quantity = excluded.residual_quantity,
                         residual_avg_cost = excluded.residual_avg_cost,
                         realized_pl = excluded.realized_pl,
                         income_total = excluded.income_total,
                         withheld_tax = excluded.withheld_tax,
                         lot_scope = excluded.lot_scope,
                         tx_count = excluded.tx_count,
                         first_tx_date = excluded.first_tx_date,
                         last_tx_date = excluded.last_tx_date,
                         -- バッチ指定なしの再計算では出所を消さない。消すと
                         -- そのバッチを巻き戻したときに派生行が残ってしまう。
                         batch_id = COALESCE(excluded.batch_id, holding_cost_basis.batch_id),
                         warnings = excluded.warnings""",
                    (
                        row["account_id"], row["security_id"], row["as_of_date"],
                        row["coverage"], 1 if row.get("applies_to_pl") else 0,
                        row.get("avg_cost"), row.get("acquired_on"),
                        row.get("acquired_on_src"), row.get("covered_quantity"),
                        row.get("residual_quantity"), row.get("residual_avg_cost"),
                        row.get("realized_pl"), row.get("income_total"),
                        row.get("withheld_tax"), row.get("lot_scope", "lot"),
                        row.get("tx_count", 0), row.get("first_tx_date"),
                        row.get("last_tx_date"), batch_id,
                        json.dumps(row.get("warnings", []), ensure_ascii=False), now,
                    ),
                )
        return len(rows)

    def list_cost_basis(
        self, *, security_id: int | None = None, account_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if security_id is not None:
            clauses.append("security_id = ?")
            params.append(security_id)
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM holding_cost_basis {where} ORDER BY security_id, account_id",
                params,
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["applies_to_pl"] = bool(item["applies_to_pl"])
            item["warnings"] = json.loads(item.get("warnings") or "[]")
            out.append(item)
        return out

    def cost_basis_overrides(self) -> dict[tuple[int, int], Decimal]:
        """損益計算に反映してよい取得単価だけを (口座, 銘柄) → 単価 で返す。

        完全被覆かつ単一ロットのものに限る（部分被覆の再計算値は MF と同じ値に
        なるため上書きする意味がなく、複数ロットは内訳を保証できない）。
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT account_id, security_id, avg_cost, as_of_date
                   FROM holding_cost_basis
                   WHERE applies_to_pl = 1 AND avg_cost IS NOT NULL"""
            ).fetchall()
        return {(r["account_id"], r["security_id"]): Decimal(r["avg_cost"]) for r in rows}

    # ------------------------------------------------------------------
    # 書式プロファイル
    # ------------------------------------------------------------------

    def get_format_profile(self, fingerprint: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tx_format_profiles WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["mapping"] = json.loads(item.get("mapping") or "{}")
        item["options"] = json.loads(item.get("options") or "{}")
        item["header_labels"] = json.loads(item.get("header_labels") or "[]")
        return item

    def save_format_profile(
        self,
        fingerprint: str,
        *,
        mapping: dict[str, Any],
        header_labels: list[str],
        options: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> None:
        now = _utcnow()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO tx_format_profiles
                   (fingerprint, label, header_labels, mapping, options,
                    created_at, last_used_at, use_count)
                   VALUES (?,?,?,?,?,?,?,1)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     label = COALESCE(excluded.label, tx_format_profiles.label),
                     header_labels = excluded.header_labels,
                     mapping = excluded.mapping,
                     options = excluded.options,
                     last_used_at = excluded.last_used_at,
                     use_count = tx_format_profiles.use_count + 1""",
                (
                    fingerprint, label,
                    json.dumps(header_labels, ensure_ascii=False),
                    json.dumps(mapping, ensure_ascii=False),
                    json.dumps(options or {}, ensure_ascii=False),
                    now, now,
                ),
            )

    def list_format_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tx_format_profiles ORDER BY last_used_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_stale_previews(self, max_age_hours: int = 24) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at FROM import_batches WHERE status = 'previewed'"
            ).fetchall()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["created_at"]).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    conn.execute(
                        "DELETE FROM import_batches WHERE id = ?", (r["id"],)
                    )

    # ------------------------------------------------------------------
    # prices（price_store.py が拡張利用する基本アクセサ）
    # ------------------------------------------------------------------

    def upsert_daily_price(
        self,
        source: str,
        source_id: str,
        day: str,
        price: Decimal,
        currency: str = "JPY",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO daily_prices (source, source_id, date, price, currency)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source, source_id, date)
                   DO UPDATE SET price = excluded.price, currency = excluded.currency""",
                (source, source_id, day, str(price), currency),
            )

    def get_price_rows(
        self,
        source: str,
        source_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[dict[str, Decimal], str | None]:
        """(date→price, currency) を返す。currency は系列内で一定の前提。"""
        sql = "SELECT date, price, currency FROM daily_prices WHERE source = ? AND source_id = ?"
        params: list[Any] = [source, source_id]
        if start:
            sql += " AND date >= ?"
            params.append(start)
        if end:
            sql += " AND date <= ?"
            params.append(end)
        sql += " ORDER BY date"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        series = {r["date"]: Decimal(r["price"]) for r in rows}
        currency = rows[-1]["currency"] if rows else None
        return series, currency

    def get_latest_price(
        self, source: str, source_id: str
    ) -> tuple[str, Decimal, str | None] | None:
        """系列の最新1件 (date, price, currency)。無ければ None。

        現在値の解決は最新の1点しか要らない。get_price_rows で全期間を読むと
        投信1銘柄で数千行になり、銘柄数ぶん積み上がってサマリー全体を律速する。
        主キー (source, source_id, date) をそのまま逆順に辿るので索引探索で済む。
        """
        with self.connect() as conn:
            row = conn.execute(
                """SELECT date, price, currency FROM daily_prices
                   WHERE source = ? AND source_id = ?
                   ORDER BY date DESC LIMIT 1""",
                (source, source_id),
            ).fetchone()
        if row is None:
            return None
        return (row["date"], Decimal(row["price"]), row["currency"])

    def get_price_before(
        self, source: str, source_id: str, day: str
    ) -> tuple[str, Decimal, str | None] | None:
        """day より前で最新の1件 (date, price, currency)。無ければ None。

        前日比の基準（「いま採用している値の1つ前の終値」）を引くために使う。
        get_latest_price と同じく主キーを逆順に辿るだけなので索引探索で済む。
        """
        with self.connect() as conn:
            row = conn.execute(
                """SELECT date, price, currency FROM daily_prices
                   WHERE source = ? AND source_id = ? AND date < ?
                   ORDER BY date DESC LIMIT 1""",
                (source, source_id, day),
            ).fetchone()
        if row is None:
            return None
        return (row["date"], Decimal(row["price"]), row["currency"])

    def delete_price_row(self, source: str, source_id: str, day: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM daily_prices WHERE source = ? AND source_id = ? AND date = ?",
                (source, source_id, day),
            )

    def price_series_for_security(
        self, sec: Security, start: str | None = None, end: str | None = None
    ) -> tuple[dict[str, Decimal], str]:
        """銘柄の価格系列（建値通貨・divisor適用前）。

        優先順: リンク済みプロバイダ系列 → 手動評価(manual)を上書き →
        どちらも無ければ MF 取込時の記載値(mf_reported)。

        **手動評価が唯一の情報源のとき**（不動産など）は、疎な査定額をそのまま
        返さず re_index.derive_series で日次へ導出する。返さないと SeriesLookup の
        forward-fill / backfill が階段や水平一直線を描くため。指数が紐付いていれば
        その形で補間し、最終査定日より先も延長する。

        start は **表示のための窓であって導出の入力ではない**。チェーンリンクには
        start より前のアンカーが要るので、導出経路ではアンカーを全期間読む。

        返す通貨は実際に採用した系列の通貨。特に mf_reported は常に円建て
        （取込時に 'JPY' 固定で保存）なので、外貨建て銘柄でも 'JPY' を返す
        ——ここで sec.currency を返すと為替換算が二重に掛かる。
        """
        series: dict[str, Decimal] = {}
        currency = sec.currency
        if (
            sec.price_source_type.value not in ("none", "manual")
            and sec.price_source_ref
        ):
            series, ccy = self.get_price_rows(
                sec.price_source_type.value, sec.price_source_ref, start, end
            )
            if ccy:
                currency = ccy
        if not series:
            # 手動評価が唯一の情報源なら、疎な査定額を日次へ導出する（不動産など）。
            # アンカーは start で絞らず全期間読む: チェーンリンクには start より前の
            # アンカーが要るため。窓の切り出しは derive_series が行う。
            anchors, anchors_ccy = self.get_price_rows("manual", str(sec.id))
            if anchors:
                derived = re_index.derive_series(
                    anchors, self.re_index_monthly(sec), start, end
                )
                return derived, (anchors_ccy or sec.currency)
        else:
            manual, _ = self.get_price_rows("manual", str(sec.id), start, end)
            if manual:
                series = {**series, **manual}
        if not series:
            series, _ = self.get_price_rows("mf_reported", str(sec.id), start, end)
            if series:
                currency = "JPY"
        return series, currency

    def re_index_monthly(self, sec: Security) -> dict[str, Decimal]:
        """銘柄に紐付いた不動産価格指数の月次系列。未連携なら空。"""
        source_id = re_index.parse_ref(sec.price_source_ref)
        if not source_id:
            return {}
        rows, _ = self.get_price_rows("re_index", source_id)
        return rows

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        merged = dict(DEFAULT_SETTINGS)
        merged.update({r["key"]: r["value"] for r in rows})
        return merged

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO app_settings (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
