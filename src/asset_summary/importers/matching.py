"""照合・diff構築: ParseResult と既存DBを突き合わせてプレビュー行を作る。

- 銘柄照合は store.resolve_security（code → alias → name_key）。
- コードは base.normalize_code（既存codes集合を渡し、大和5桁末尾0の誤切詰めを回避）。
- 同一(口座,銘柄)の複数行は前回スナップショットとロット割当:
  預金・ポイント・暗号資産は lot_label 完全一致、その他は avg_cost 近傍の貪欲マッチ
  （同率は数量近さ）。余りは次の空き lot_seq。
- 消失(missing)検出は origin=mf の口座 × origin=mf の現在ロットに限定。

返す rows / diff / sections はすべて JSON 直列化可能（Decimalは文字列）。
rows は commit_batch がそのまま使える完全な行情報（diff はそのUI向け部分集合）。

build_matches は result.report.warnings へプレビュー用の警告を追記する
（service.build_preview はこの後に report を dump するため、そのままUIに出る）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..core import fund_autolink
from ..core.models import PriceSourceType
from ..core.store import Store
from .base import ParsedHolding, ParseResult, SectionType, make_name_key, normalize_code
from .mf_pdf import SECTION_LABELS

# 集約先の現金銘柄（預金・現金セクションは全行これに集約）
CASH_SECURITY_NAME = "現金・預金"
# 暗号資産セクションの参考集約先（既定では取込対象外）
CRYPTO_SECURITY_NAME = "暗号資産"

CONFIDENCE_INCLUDE_THRESHOLD = 0.7

# 口座名(保有金融機関)を読み取れなかった行の confidence 上限。
# 閾値を下回らせて既定で取込対象から外す（空の口座名で口座を作らせないため）。
CONFIDENCE_NO_ACCOUNT = 0.4

# 保有金融機関の列を持つセクション（年金は '年金' 固定なので対象外）
_INSTITUTION_REQUIRED = {
    SectionType.DEPOSIT,
    SectionType.STOCK,
    SectionType.FUND,
    SectionType.CRYPTO,
    SectionType.POINT,
}

# lot_label 完全一致で照合するセクション
_LABEL_MATCH_SECTIONS = {SectionType.DEPOSIT, SectionType.POINT, SectionType.CRYPTO}

_ACCOUNT_KINDS = {
    SectionType.DEPOSIT: "bank",
    SectionType.STOCK: "broker",
    SectionType.FUND: "broker",
    SectionType.CRYPTO: "broker",
    SectionType.PENSION: "pension",
    SectionType.POINT: "point",
}

# missing行のセクション表示用（asset_class → セクション値）
_CLASS_TO_SECTION = {
    "cash": "deposit",
    "stock_jp": "stock",
    "stock_foreign": "stock",
    "fund_jp": "fund",
    "fund_foreign": "fund",
    "pension": "pension",
    "point": "point",
    "metal": "stock",
    "other": "crypto",
}

_DIFF_FIELDS = (
    "key",
    "status",
    "section",
    "name",
    "code",
    "account",
    "lot_label",
    "old_quantity",
    "new_quantity",
    "old_avg_cost",
    "new_avg_cost",
    "value",
    "confidence",
    "included",
)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _d(value: str | None) -> Decimal | None:
    return None if value in (None, "") else Decimal(value)


def _dec_eq(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a == b


def _absdiff(a: Decimal | None, b: Decimal | None) -> Decimal:
    if a is None and b is None:
        return Decimal("0")
    if a is None or b is None:
        return Decimal("Infinity")
    return abs(a - b)


def target_from_holding(
    holding: ParsedHolding, known_codes: set[str]
) -> dict[str, Any] | None:
    """ParsedHolding → セクション別の取込ターゲット（口座・銘柄属性・ロット素材）。"""
    section = holding.section
    if section not in _ACCOUNT_KINDS:
        return None
    meta = dict(holding.meta)
    warnings = list(holding.warnings)
    confidence = holding.confidence
    # 口座名が空だと既存ロットと突き合わせられず全行が「新規」になり、確定すると
    # 名前のない口座が作られる。既定で取込対象から外して人の目を通させる。
    if section in _INSTITUTION_REQUIRED and not holding.institution.strip():
        warnings.append("保有金融機関(口座名)を読み取れませんでした")
        confidence = min(confidence, CONFIDENCE_NO_ACCOUNT)
    raw: dict[str, Any] = {
        "section": section.value,
        "name_raw": holding.name_raw,
        "code_raw": holding.code_raw,
        "institution": holding.institution,
        "meta": meta,
        "warnings": warnings,
    }
    base = {
        "section": section.value,
        "account": holding.institution,
        "account_kind": _ACCOUNT_KINDS[section],
        "confidence": confidence,
        "raw": raw,
        "lot_label": None,
        "reported_price": None,
        "reported_value_jpy": _s(holding.value_jpy),
        "reported_pl_jpy": _s(holding.pl_jpy),
    }
    if section == SectionType.DEPOSIT:
        return {
            **base,
            "name": CASH_SECURITY_NAME,
            "code": None,
            "name_key": make_name_key(CASH_SECURITY_NAME),
            "asset_class": "cash",
            "currency": "JPY",
            "unit": "currency",
            "price_unit_divisor": 1,
            "price_source_type": "none",
            "price_source_ref": None,
            "price_source_status": "not_required",
            "lot_label": holding.name_raw,
            "new_quantity": _s(holding.value_jpy),
            "new_avg_cost": None,
        }
    if section == SectionType.CRYPTO:
        return {
            **base,
            "name": CRYPTO_SECURITY_NAME,
            "code": None,
            "name_key": make_name_key(CRYPTO_SECURITY_NAME),
            "asset_class": "other",
            "currency": "JPY",
            "unit": "currency",
            "price_unit_divisor": 1,
            "price_source_type": "none",
            "price_source_ref": None,
            "price_source_status": "not_required",
            "lot_label": holding.name_raw,
            "new_quantity": _s(holding.value_jpy),
            "new_avg_cost": None,
        }
    if section == SectionType.STOCK:
        code = normalize_code(holding.code_raw, known_codes)
        linked = code is not None
        return {
            **base,
            "name": holding.name_raw,
            "code": code,
            "name_key": make_name_key(holding.name_raw),
            "asset_class": "stock_jp",
            "currency": "JPY",
            "unit": "share",
            "price_unit_divisor": 1,
            "price_source_type": "yahoo" if linked else "none",
            "price_source_ref": f"{code}.T" if linked else None,
            "price_source_status": "linked" if linked else "unlinked",
            "new_quantity": _s(holding.quantity),
            "new_avg_cost": _s(holding.avg_cost),
            "reported_price": _s(holding.price),
        }
    if section == SectionType.FUND:
        return {
            **base,
            "name": holding.name_raw,
            "code": None,
            "name_key": make_name_key(holding.name_raw),
            "asset_class": "fund_jp",
            "currency": "JPY",
            "unit": "kuchi",
            "price_unit_divisor": 10000,
            "price_source_type": "none",
            "price_source_ref": None,
            "price_source_status": "unlinked",
            "new_quantity": _s(holding.quantity),
            "new_avg_cost": _s(holding.avg_cost),
            "reported_price": _s(holding.price),
        }
    if section == SectionType.PENSION:
        return {
            **base,
            "account": "年金",
            "name": holding.name_raw,
            "code": None,
            "name_key": make_name_key(holding.name_raw),
            "asset_class": "pension",
            "currency": "JPY",
            "unit": "unit",
            "price_unit_divisor": 1,
            "price_source_type": "none",
            "price_source_ref": None,
            "price_source_status": "not_required",
            "new_quantity": "1",
            "new_avg_cost": _s(holding.avg_cost),  # 取得価額の総額
        }
    if section == SectionType.POINT:
        label_parts = [meta.get("point_type") or holding.name_raw]
        if meta.get("expiry"):
            label_parts.append(meta["expiry"])
        return {
            **base,
            "name": holding.name_raw,
            "code": None,
            "name_key": make_name_key(holding.name_raw),
            "asset_class": "point",
            "currency": "JPY",
            "unit": "point",
            "price_unit_divisor": 1,
            "price_source_type": "none",
            "price_source_ref": None,
            "price_source_status": "not_required",
            "lot_label": "|".join(label_parts),
            "new_quantity": _s(holding.quantity),
            "new_avg_cost": None,
        }
    return None


def _derive_pension_targets(
    store: Store, targets: list[dict[str, Any]]
) -> list[str]:
    """基準価額に連携済みの年金銘柄は、取込前に評価額から口数を逆算する。

    年金セクションの既定は quantity=1（件）だが、投信協会へ連携済みなら
    実口数で持つ（core.fund_autolink 参照）。ここ（照合の前）で行うことで、
    プレビューの diff に実口数の増減が出て、既存ロットとの数量比較も正しくなる。
    系列が手元に無ければ何もしない — quantity=1 のまま取り込まれても、評価は
    記載値へフォールバックし（portfolio 側のガード）、次回の逆算で直る。

    返値はプレビューに出す警告。整数口で評価額を再現できないのは連携先の
    ファンドが誤っている兆候なので、黙って取り込まず人の目を促す。
    """
    warnings: list[str] = []
    series_cache: dict[str, dict[str, Decimal]] = {}
    for target in targets:
        if target["section"] != SectionType.PENSION.value:
            continue
        value = _d(target.get("reported_value_jpy"))
        if value is None or value <= 0:
            continue
        sec_id = store.resolve_security(code=None, name_key=target["name_key"])
        if sec_id is None:
            continue
        sec = store.get_security(sec_id)
        if (
            sec is None
            or sec.price_source_type != PriceSourceType.TOUSHIN
            or not sec.price_source_ref
        ):
            continue
        ref = sec.price_source_ref
        if ref not in series_cache:
            series_cache[ref], _ = store.get_price_rows("toushin", ref)
        derived = fund_autolink.derive_pension_units(value, series_cache[ref])
        if derived is None:
            warnings.append(
                f"年金「{sec.name}」の口数を逆算できませんでした"
                "（基準価額の履歴が不足）。今回は記載評価額のまま取り込みます"
            )
            continue
        units, confident = derived
        if not confident:
            warnings.append(
                f"年金「{sec.name}」の評価額を連携先の基準価額で再現できません"
                "でした（比例口数で取り込みます）。連携先のファンドが正しいか"
                "確認してください"
            )
        target["new_quantity"] = _s(units)
    return warnings


def _identity(target: dict[str, Any]) -> str:
    return target["code"] if target["code"] else target["name_key"]


def _make_key(account: str, identity: str, lot_seq: int) -> str:
    return f"acct:{account}|sec:{identity}|lot:{lot_seq}"


def build_matches(
    store: Store, result: ParseResult
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """返値: (rows, diff, sections)。すべてJSON直列化可能。"""
    known_codes = {s.code for s in store.list_securities() if s.code}
    accounts_by_name = {a.name: a for a in store.list_accounts()}

    rows: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for holding in result.holdings:
        target = target_from_holding(holding, known_codes)
        if target is not None:
            targets.append(target)

    # 照合・status判定の前に行う（逆算後の口数が新旧比較に使われるように）
    result.report.warnings.extend(_derive_pension_targets(store, targets))

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for target in targets:
        groups.setdefault((target["account"], _identity(target)), []).append(target)

    matched_old: set[tuple[int, int, int]] = set()

    for (account_name, identity), group in groups.items():
        first = group[0]
        acct = accounts_by_name.get(account_name)
        security_id = store.resolve_security(
            code=first["code"], name_key=first["name_key"]
        )
        old_lots = (
            store.latest_lots(acct.id, security_id)
            if acct is not None and security_id is not None
            else []
        )
        section = SectionType(first["section"])
        assignment = _assign_lots(section, group, old_lots)
        used_seqs = {lot.lot_seq for lot in old_lots}
        next_seq = max(used_seqs) + 1 if used_seqs else 0
        for target, old in zip(group, assignment):
            target["account_id"] = acct.id if acct is not None else None
            target["security_id"] = security_id
            if old is not None:
                lot_seq = old.lot_seq
                matched_old.add((old.account_id, old.security_id, old.lot_seq))
                new_qty = _d(target["new_quantity"])
                new_cost = _d(target["new_avg_cost"])
                if not _dec_eq(new_qty, old.quantity):
                    status = "qty_changed"
                elif not _dec_eq(new_cost, old.avg_cost):
                    status = "cost_changed"
                else:
                    status = "unchanged"
                old_qty, old_cost = _s(old.quantity), _s(old.avg_cost)
            else:
                while next_seq in used_seqs:
                    next_seq += 1
                lot_seq = next_seq
                used_seqs.add(lot_seq)
                status, old_qty, old_cost = "new", None, None
            target["lot_seq"] = lot_seq
            target["key"] = _make_key(account_name, identity, lot_seq)
            target["status"] = status
            target["old_quantity"] = old_qty
            target["old_avg_cost"] = old_cost
            target["value"] = target["reported_value_jpy"]
            target["included"] = (
                target["confidence"] >= CONFIDENCE_INCLUDE_THRESHOLD
                and target["section"] != SectionType.CRYPTO.value
            )

    rows.extend(targets)  # 解析順を保持（targetsはholdings順）

    rows.extend(_missing_rows(store, matched_old))
    result.report.warnings.extend(_consistency_warnings(rows))

    diff = [{k: row.get(k) for k in _DIFF_FIELDS} for row in rows]
    sections = [
        {
            "section": rep.section.value,
            "label": rep.name,
            "count": rep.row_count,
            "total": _s(rep.computed_total),
            "default_include": rep.section != SectionType.CRYPTO,
        }
        for rep in result.report.sections
    ]
    return rows, diff, sections


def _consistency_warnings(rows: list[dict[str, Any]]) -> list[str]:
    """diff全体を見て、明らかに読取りを誤っている兆候を警告に変える。

    同じ銘柄コードが「新規」と「PDFに無し」の両方に現れるのは、銘柄ではなく
    口座の読取りに失敗したときの症状（口座が変われば突き合わせが外れ、
    同じ銘柄が新規＋消失の対になる）。銘柄コードは照合キーではなく、この
    異常を検知するシグナルとして使う。
    """
    warnings: list[str] = []
    no_account = sum(
        1 for r in rows if r["status"] != "missing" and not (r.get("account") or "").strip()
    )
    if no_account:
        warnings.append(
            f"保有金融機関(口座名)を読み取れなかった行が {no_account} 件あります。"
            "既定では取込対象から外しています"
        )
    new_codes = {r["code"] for r in rows if r["status"] == "new" and r.get("code")}
    paired = sorted(
        new_codes & {r["code"] for r in rows if r["status"] == "missing" and r.get("code")}
    )
    if paired:
        warnings.append(
            f"同じ銘柄コード（{', '.join(paired)}）が「新規」と「PDFに無し」の"
            "両方にあります。口座名を読み取れていない可能性があるため、"
            "確定前に口座を確認してください"
        )
    return warnings


def _lot_label_parts(label: str | None) -> tuple[str, str]:
    """lot_label を (種類, 有効期限) に分解する。期限が無ければ空文字。"""
    head, _, expiry = (label or "").partition("|")
    return head, expiry


def _expiry_sort_key(expiry: str) -> tuple[bool, str]:
    return (expiry == "", expiry)  # 期限なしは末尾へ


def _assign_by_label(group, old_lots):
    """lot_label 完全一致 → 期限だけがずれた余り同士の対応づけ、の2段。

    ポイントの有効期限はローリングする（JREポイントは毎月1ヶ月先へ動く）ため、
    完全一致だけだと同じポイントが毎回「新規」＋「PDFに無し」の対になる。
    かといって期限をラベルから外すこともできない — 楽天の期間限定ポイントは
    同一口座・同一種類で期限だけが違うロットが同時に並ぶため、潰れてしまう。
    そこで完全一致で拾えなかった分だけ、種類が同じもの同士を期限順に繋ぐ。
    """
    assignment: list[Any] = [None] * len(group)
    remaining = list(old_lots)
    for i, target in enumerate(group):
        for lot in remaining:
            if (lot.lot_label or "") == (target["lot_label"] or ""):
                assignment[i] = lot
                remaining.remove(lot)
                break

    leftovers: dict[str, list[int]] = {}
    for i, target in enumerate(group):
        if assignment[i] is None:
            kind = _lot_label_parts(target["lot_label"])[0]
            leftovers.setdefault(kind, []).append(i)
    if not leftovers or not remaining:
        return assignment

    pools: dict[str, list[Any]] = {}
    for lot in remaining:
        pools.setdefault(_lot_label_parts(lot.lot_label)[0], []).append(lot)
    for kind, idxs in leftovers.items():
        pool = pools.get(kind)
        if not pool:
            continue
        idxs.sort(key=lambda i: _expiry_sort_key(
            _lot_label_parts(group[i]["lot_label"])[1]))
        pool.sort(key=lambda lot: _expiry_sort_key(
            _lot_label_parts(lot.lot_label)[1]))
        for i, lot in zip(idxs, pool):
            assignment[i] = lot
    return assignment


def _assign_lots(section, group, old_lots):
    """グループ内の新行に既存ロットを割当。返値は group と同順の (lot|None) リスト。"""
    assignment: list[Any] = [None] * len(group)
    if not old_lots:
        return assignment
    if section in _LABEL_MATCH_SECTIONS:
        return _assign_by_label(group, old_lots)
    # avg_cost 近傍の貪欲マッチ（同率は数量の近さ、さらに同率は行順・lot_seq順）
    pairs = []
    for i, target in enumerate(group):
        new_cost = _d(target["new_avg_cost"])
        new_qty = _d(target["new_quantity"])
        for lot in old_lots:
            pairs.append(
                (
                    _absdiff(new_cost, lot.avg_cost),
                    _absdiff(new_qty, lot.quantity),
                    i,
                    lot.lot_seq,
                    lot,
                )
            )
    pairs.sort(key=lambda p: (p[0], p[1], p[2], p[3]))
    used_targets: set[int] = set()
    used_lots: set[int] = set()
    for _cost_diff, _qty_diff, i, lot_seq, lot in pairs:
        if i in used_targets or lot_seq in used_lots:
            continue
        assignment[i] = lot
        used_targets.add(i)
        used_lots.add(lot_seq)
    return assignment


def _missing_rows(
    store: Store, matched_old: set[tuple[int, int, int]]
) -> list[dict[str, Any]]:
    """PDFに無い既存ロット（origin=mf の現在ロットに限定）。

    判定は「ロットが MF 由来か」だけで行う。口座の origin は見ない —
    ユーザーが同名の口座を先に手動作成していると（POST /api/holdings は
    origin='manual' で口座を作る）、その口座のMF由来ロットが売却されても
    永久に検出されず残ってしまうため。手動登録のロットは origin='manual'
    なのでここで除外され、誤ってゼロ化されることはない。
    """
    accounts_by_id = {a.id: a for a in store.list_accounts()}
    securities = store.securities_by_id()
    out: list[dict[str, Any]] = []
    for lot in store.current_holdings():
        if lot.origin != "mf" or lot.quantity == 0:
            continue
        acct = accounts_by_id.get(lot.account_id)
        if acct is None:
            continue
        if (lot.account_id, lot.security_id, lot.lot_seq) in matched_old:
            continue
        sec = securities.get(lot.security_id)
        if sec is None:
            continue
        identity = sec.code if sec.code else sec.name_key
        out.append(
            {
                "key": _make_key(acct.name, identity, lot.lot_seq),
                "status": "missing",
                "section": _CLASS_TO_SECTION.get(sec.asset_class.value, "unknown"),
                "name": sec.name,
                "code": sec.code,
                "name_key": sec.name_key,
                "account": acct.name,
                "account_id": acct.id,
                "security_id": sec.id,
                "lot_seq": lot.lot_seq,
                "lot_label": lot.lot_label,
                "old_quantity": _s(lot.quantity),
                "new_quantity": None,
                "old_avg_cost": _s(lot.avg_cost),
                "new_avg_cost": None,
                "value": None,
                "confidence": 1.0,
                "included": True,  # 既定提案は「売却(数量0)」。保持する場合はUIで外す
            }
        )
    return out
