"""取引履歴（証券会社CSV等）の書式判定エンジン。

証券会社ごとのアダプタは書かない。どんな書式でも判定できる 1 本のエンジンを
組むため、各段を純関数で繋ぐ:

    bytes ──grid.load_grid──▶ SheetGrid
                              │
       header.find_region ────┤
                              ▼
       columns.assign ──▶ DetectedFormat ──engine.parse_grid──▶ TxParseResult

DB に触れるのは KnownUniverse を組む所（tx_service）だけで、判定本体は
KnownUniverse を読むだけの純粋な計算。テストは合成グリッドを直接与える
（mf_pdf.parse_words が合成ワードリストを受けるのと同じ流儀）。
"""

from __future__ import annotations

from .contracts import (
    CanonicalField,
    ColumnAssignment,
    DetectedFormat,
    IdentityCheck,
    KnownSecurity,
    KnownUniverse,
    ParsedTx,
    SheetGrid,
    SourceMeta,
    TableRegion,
    TxParseReport,
    TxParseResult,
)

__all__ = [
    "CanonicalField",
    "ColumnAssignment",
    "DetectedFormat",
    "IdentityCheck",
    "KnownSecurity",
    "KnownUniverse",
    "ParsedTx",
    "SheetGrid",
    "SourceMeta",
    "TableRegion",
    "TxParseReport",
    "TxParseResult",
]
