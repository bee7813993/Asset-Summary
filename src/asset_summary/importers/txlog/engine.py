"""判定エンジンの入口（純関数）。

    detect_format(grid, universe)  → DetectedFormat
    parse_grid(grid, universe)     → TxParseResult

どちらも DB に触れない。KnownUniverse は読むだけの入力で、テストは合成した
グリッドと架空の銘柄集合を直接渡す。
"""

from __future__ import annotations

import hashlib

from .classify import infer_blank_trade_types, row_to_tx
from .columns import (
    assign_categorical,
    build_score_matrix,
    check_identities,
    collect_cost_columns,
    detect_sign_convention,
    solve_numeric,
)
from .contracts import (
    ADDITIVE_FIELDS,
    CONFIDENCE_INCLUDE_THRESHOLD,
    IDENTITY_PASS_RATE,
    NUMERIC_QUARTET,
    sample_rows,
    CanonicalField as F,
    ColumnAssignment,
    DetectedFormat,
    KnownUniverse,
    SheetGrid,
    TxParseReport,
    TxParseResult,
)
from .header import data_row_indices, find_region
from .shapes import date_order_ambiguous, detect_date_order
from .vocab import header_scores, normalize_label


def fingerprint(headers: tuple[str, ...]) -> str | None:
    """見出し集合の指紋。並べ替えには強く、列の増減では変わる（安全側）。"""
    labels = sorted({normalize_label(h) for h in headers if normalize_label(h)})
    if not labels:
        return None
    return "hl:" + hashlib.sha1("\x1f".join(labels).encode("utf-8")).hexdigest()


def shape_fingerprint(stats, width: int) -> str:
    """ヘッダ無し書式用の指紋。列数と各列の形で表す。"""
    letters = []
    for c in range(width):
        st = stats.get(c)
        if st is None:
            letters.append("T")
        elif st.known_security_rate >= 0.3:
            letters.append("K")
        elif st.is_date:
            letters.append("D")
        elif st.code_rate >= 0.5:
            letters.append("C")
        elif st.is_numeric:
            letters.append("N")
        else:
            letters.append("T")
    return f"sh:{width}|" + "".join(letters)


def detect_format(
    grid: SheetGrid,
    universe: KnownUniverse,
    *,
    overrides: dict[int, str] | None = None,
) -> DetectedFormat:
    """グリッドから列の対応を決める。overrides は利用者が直した対応（列→フィールド名）。"""
    warnings: list[str] = list(grid.meta.warnings)
    region = find_region(grid, universe)
    rows = data_row_indices(region)

    if not rows:
        warnings.append("データ行が見つかりませんでした")
        return DetectedFormat(region=region, confidence=0.0, warnings=tuple(warnings))

    sample = sample_rows(rows)
    stats, scores, evidence = build_score_matrix(grid, region, universe, sample)
    width = max(grid.width, len(region.headers))

    if region.header_row is None:
        warnings.append("ヘッダ行を見つけられませんでした。列の内容から推定しています")
    if universe.is_empty:
        warnings.append(
            "登録済みの銘柄がまだ無いため、銘柄列の判定が弱くなっています。"
            "先にマネーフォワードME の PDF を取り込むと精度が上がります"
        )

    forced: dict[F, int] = {}
    if overrides:
        for col, name in overrides.items():
            try:
                field = F(name)
            except ValueError:
                continue
            if field is not F.IGNORE:
                forced[field] = col

    # 1) 分類系はスコア行列の最適割当
    free_cols = [c for c in range(width) if c not in set(forced.values())]
    categorical = assign_categorical(free_cols, scores)
    categorical.update({f: c for f, c in forced.items() if f not in NUMERIC_QUARTET
                        and f not in ADDITIVE_FIELDS})

    # 2) 手数料・税額は見出しから先に確定させる。総当たりの検算で
    #    「受渡金額 − 約定代金 = 手数料 + 税額」を使うので、これが先に要る。
    fee_cols, tax_cols = collect_cost_columns(region, stats)
    if F.FEE in forced:
        fee_cols = [forced[F.FEE]]
    if F.TAX in forced:
        tax_cols = [forced[F.TAX]]
    used = set(categorical.values()) | set(fee_cols) | set(tax_cols)
    quartet_pool = [c for c in range(width)
                    if c not in used and stats.get(c) is not None and stats[c].is_numeric]

    # 3) 数量・単価・約定代金・受渡金額は総当たり＋算術検算で確定
    if any(f in forced for f in NUMERIC_QUARTET):
        numeric = {f: c for f, c in forced.items() if f in NUMERIC_QUARTET}
        divisor = 1
        identities = check_identities(
            grid, sample,
            qty=numeric.get(F.QUANTITY), price=numeric.get(F.UNIT_PRICE),
            gross=numeric.get(F.GROSS_AMOUNT), net=numeric.get(F.NET_AMOUNT),
            fee=fee_cols, tax=tax_cols, divisor=divisor,
        )
        for cand in (10000,):
            alt = check_identities(
                grid, sample,
                qty=numeric.get(F.QUANTITY), price=numeric.get(F.UNIT_PRICE),
                gross=numeric.get(F.GROSS_AMOUNT), net=numeric.get(F.NET_AMOUNT),
                fee=fee_cols, tax=tax_cols, divisor=cand,
            )
            if _rate(alt) > _rate(identities):
                identities, divisor = alt, cand
    else:
        numeric, identities, divisor = solve_numeric(
            grid, sample, quartet_pool, scores,
            fee=fee_cols, tax=tax_cols,
            pinned=_pinned_by_header(region, quartet_pool),
        )
        # 検算で裏が取れなかったときは、見出しの裏づけがある列だけ残す。
        # 数値列にはどれも「数値である」以上の手がかりが無く、割り当てない
        # よりは割り当てたほうがスコアが上がるので、放っておくと無関係な列
        # （楽天証券の書式では全部 0 の『名義書換料』）が約定代金に化ける。
        if not any(
            c.conclusive and c.pass_rate >= IDENTITY_PASS_RATE for c in identities
        ):
            kept = {
                f: c
                for f, c in numeric.items()
                if header_scores(
                    region.headers[c] if c < len(region.headers) else ""
                ).get(f, 0.0) >= 0.5
            }
            if kept != numeric:
                # 外した列で計算した検算結果を残すと、画面に「0/57 行 一致」と
                # いう最終的な対応とは無関係の数字が出る。取り直す。
                numeric = kept
                identities = check_identities(
                    grid, sample,
                    qty=numeric.get(F.QUANTITY), price=numeric.get(F.UNIT_PRICE),
                    gross=numeric.get(F.GROSS_AMOUNT), net=numeric.get(F.NET_AMOUNT),
                    fee=fee_cols, tax=tax_cols, divisor=divisor,
                )

    mapping: dict[F, int] = dict(categorical)
    mapping.update(numeric)
    if fee_cols:
        mapping[F.FEE] = fee_cols[0]
    if tax_cols:
        mapping[F.TAX] = tax_cols[0]

    extra_fees, extra_taxes = fee_cols[1:], tax_cols[1:]

    # 日付の並び（03/04/2026 のような曖昧表記の解釈）
    date_order = "ymd"
    trade_col = mapping.get(F.TRADE_DATE)
    if trade_col is not None:
        values = [grid.cell(r, trade_col) for r in sample]
        date_order = detect_date_order(values)
        if date_order_ambiguous(values):
            warnings.append(
                "日付の月日の並びを判別できませんでした。年/月/日として読んでいます"
            )

    sign, sign_warnings = detect_sign_convention(
        stats,
        tx_type_col=mapping.get(F.TX_TYPE),
        quantity_col=mapping.get(F.QUANTITY),
        net_col=mapping.get(F.NET_AMOUNT),
    )
    warnings.extend(sign_warnings)

    columns = _build_assignments(region, stats, scores, evidence, mapping, width)
    confidence = _confidence(scores, mapping, identities, columns)
    if sign == "unsigned":
        # 売買の向きが分からないまま取り込むと保有数が黙って壊れる。
        # 行の信頼度は書式の信頼度から始まるので、ここで閾値未満に抑えて
        # すべての行を既定で取込対象から外す。
        confidence = min(confidence, 0.4)

    for check in identities:
        if check.conclusive and check.pass_rate < IDENTITY_PASS_RATE:
            label = "数量×単価と約定代金" if check.name == "qty*price=gross" else "受渡金額と約定代金"
            warnings.append(
                f"{label}が {check.tested} 行中 {check.tested - check.passed} 行で一致しません"
            )

    fp = fingerprint(region.headers) if region.headers else shape_fingerprint(stats, width)

    return DetectedFormat(
        region=region,
        columns=columns,
        identities=tuple(identities),
        divisor=divisor,
        sign_convention=sign,
        date_order=date_order,
        extra_fee_columns=tuple(extra_fees),
        extra_tax_columns=tuple(extra_taxes),
        confidence=confidence,
        fingerprint=fp,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _pinned_by_header(region, columns: list[int]) -> dict[F, int]:
    """見出しが完全一致し、かつその列が一意なフィールドを固定する。

    手数料がゼロの書式では 受渡金額 = 数量×単価 になり、検算では約定代金と
    受渡金額を区別できない。見出しがはっきり言っているならそちらを信じる。

    同じフィールドに完全一致する列が複数あるとき（『取得単価』と『単価』が
    並ぶ書式）は固定しない。そこは本当に曖昧なので検算に決めさせる。
    """
    hits: dict[F, list[int]] = {}
    for col in columns:
        label = region.headers[col] if col < len(region.headers) else ""
        if not label:
            continue
        for field, score in header_scores(label).items():
            if field in NUMERIC_QUARTET and score >= 0.9:
                hits.setdefault(field, []).append(col)

    pinned = {f: cols[0] for f, cols in hits.items() if len(cols) == 1}
    # 1 列が 2 つのフィールドに完全一致した場合は、どちらも固定しない
    seen: dict[int, int] = {}
    for col in pinned.values():
        seen[col] = seen.get(col, 0) + 1
    return {f: c for f, c in pinned.items() if seen[c] == 1}


def _rate(checks) -> float:
    usable = [c for c in checks if c.conclusive]
    if not usable:
        return 0.0
    return sum(c.pass_rate for c in usable) / len(usable)


def _build_assignments(region, stats, scores, evidence, mapping, width):
    by_col = {c: f for f, c in mapping.items()}
    out = []
    for col in range(width):
        label = region.headers[col] if col < len(region.headers) else ""
        field = by_col.get(col, F.IGNORE)
        score = scores.get((col, field), 0.0) if field is not F.IGNORE else 0.0
        alts = sorted(
            ((f.value, s) for (c, f), s in scores.items() if c == col and f is not field),
            key=lambda kv: -kv[1],
        )[:3]
        st = stats.get(col)
        split = bool(
            st is not None
            and field is F.SECURITY_NAME
            and st.leading_code_gain > st.known_security_rate
        )
        out.append(
            ColumnAssignment(
                index=col,
                header=label,
                field=field,
                score=score,
                evidence=tuple(evidence.get((col, field), ())),
                alternatives=tuple(alts),
                split_leading_code=split,
            )
        )
    return tuple(out)


def _confidence(scores, mapping, identities, columns) -> float:
    """割当の余裕・検算の合格率・列の埋まり具合から 0-1 を作る。"""
    if not mapping:
        return 0.0
    margins = []
    for field, col in mapping.items():
        mine = scores.get((col, field), 0.0)
        others = [s for (c, f), s in scores.items() if c == col and f is not field]
        second = max(others) if others else 0.0
        margins.append(min(max(mine - second, 0.0), 1.0))
    margin = sum(margins) / len(margins) if margins else 0.0

    usable = [c for c in identities if c.conclusive]
    identity = (sum(c.pass_rate for c in usable) / len(usable)) if usable else 0.5

    essential = (F.TRADE_DATE, F.QUANTITY)
    filled = sum(1 for f in essential if f in mapping) / len(essential)

    return round(0.35 * margin + 0.35 * identity + 0.30 * filled, 4)


def parse_grid(
    grid: SheetGrid,
    universe: KnownUniverse,
    *,
    overrides: dict[int, str] | None = None,
    fmt: DetectedFormat | None = None,
) -> TxParseResult:
    """グリッド全体を解析して取引の一覧にする。例外は投げない。"""
    detected = fmt or detect_format(grid, universe, overrides=overrides)
    region = detected.region
    rows = data_row_indices(region)

    report = TxParseReport(
        detection=detected.to_dict(),
        row_count=len(rows),
        warnings=list(detected.warnings),
    )
    for row, reason in region.dropped:
        report.skipped_rows.append(
            {"row": row, "reason": reason, "text": " | ".join(grid.rows[row])
             if row < grid.height else ""}
        )

    transactions = []
    if detected.columns:
        for row in rows:
            tx = row_to_tx(grid, row, detected)
            if tx.trade_date is None and tx.quantity is None and not tx.security_name_raw:
                report.skipped_rows.append(
                    {"row": row, "reason": "empty", "text": " | ".join(grid.rows[row])}
                )
                continue
            transactions.append(tx)
    inferred = infer_blank_trade_types(
        transactions, detected.divisor,
        has_type_column=detected.column_for(F.TX_TYPE) is not None,
    )
    if inferred:
        report.warnings.append(
            f"取引区分が空欄の行 {inferred} 件を買付と推定しました"
            "（数量の整合から。各行の警告に根拠を残しています）"
        )
    report.parsed_count = len(transactions)

    # 信用取引は「確認が必要」ではなく設計上の対象外。混ぜて数えると、
    # 対処の要らない行が対処を迫る警告に化ける。
    margin = sum(1 for t in transactions if t.raw.get("margin"))
    low = sum(
        1 for t in transactions
        if t.confidence < CONFIDENCE_INCLUDE_THRESHOLD and not t.raw.get("margin")
    )
    if margin:
        report.warnings.append(
            f"信用取引の行が {margin} 件あります（現物の保有と混ざると数が壊れるため、"
            "設計上取り込みません）"
        )
    if low:
        report.warnings.append(
            f"確認が必要な行が {low} 件あります（既定では取込対象から外しています）"
        )

    return TxParseResult(transactions=transactions, report=report)
