"""Valuation and P&L logic (pure computation, no I/O).

すべて Decimal で計算し、価格・FXは呼び出し側（web層）が取得して渡す。
- 現在値サマリー: summarize()
- 日次推移系列: daily_series()

要件（確定済み）:
- 銘柄の初回登録日より前は「最初のスナップショットの保有数」で遡及表示。
- 平均取得単価が無い保有（現金・ポイント）は損益集計から除外し件数を返す。
- 価格が引けない保有は reported_value_jpy をフォールバック（参考値）。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

from .models import Account, AssetClass, HoldingSnapshot, PriceSourceType, Security

ZERO = Decimal("0")


def _sum_present(values: Iterable[Decimal | None]) -> Decimal | None:
    """Noneを除いた合計。全てNoneならNone（0円と「不明」を区別する）。"""
    present = [v for v in values if v is not None]
    return sum(present, ZERO) if present else None


def _account_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": row.get("account_id"),
        "account": row.get("account") or "",
        "quantity": row.get("quantity"),
        "avg_cost": row.get("avg_cost"),
        "value": row.get("value"),
        "pl": row.get("pl"),
        "pl_pct": row.get("pl_pct"),
        "day_change": row.get("day_change"),
        "day_change_pct": row.get("day_change_pct"),
    }


# 銘柄単位に合算せず口座ごとに分けて見せるクラス。預金は全行が単一の
# 「現金・預金」銘柄へ集約され（ポイントも同名のものが各社に散る）、銘柄名では
# なく口座名こそが識別子になるため。合算すると銀行別の残高が見えなくなる。
# 預金だけは設定（merge_cash）で「A銀行 他N件」の1行へ合算できる — 内訳は
# accounts・銘柄詳細・クラス別/口座別ページに残るため、情報は失われない。
_PER_ACCOUNT_CLASSES = {"cash", "point"}


def merge_cash_enabled(settings: dict[str, str]) -> bool:
    """保有一覧で預金・現金を1行に合算するか（既定: 合算する）。"""
    return settings.get("merge_cash", "1") == "1"


def _security_group_key(
    h: dict[str, Any], merge_cash: bool = False, merge_all: bool = False
) -> tuple:
    cls = h.get("asset_class")
    per_account = cls in _PER_ACCOUNT_CLASSES and not (merge_cash and cls == "cash")
    if per_account and not merge_all:
        return (h["id"], h.get("account_id"), h.get("account"))
    return (h["id"],)


def aggregate_by_security(
    holdings: list[dict[str, Any]],
    merge_cash: bool = False,
    merge_all: bool = False,
) -> list[dict[str, Any]]:
    """summarize() の (銘柄,口座) 行を銘柄単位へ合算する。

    同じ銘柄を複数口座で持っていても保有一覧では1行に見せるための集約。
    どの口座が持っているかは accounts に内訳として残す（参考情報）。
    預金・ポイントは口座が識別子なので合算しない（_PER_ACCOUNT_CLASSES）。
    ただし merge_cash=True なら預金も1行へ合算し、merge_all=True は
    クラスによらず常に銘柄単位へ合算する（銘柄詳細の口座横断タイル用）。

    - 損益は summarize() と同じ規約に従い、原価の判るロットの評価額
      （costed_value）とだけ突き合わせる。value を使うと原価なしロットの
      評価額が利益に混ざる。
    - avg_cost は costed_quantity による加重平均（表示用の合成単価）。
    - 行の形は summarize() の holdings と同じで、accounts が増えるだけ。
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for h in holdings:
        groups.setdefault(_security_group_key(h, merge_cash, merge_all), []).append(h)

    out: list[dict[str, Any]] = []
    for rows in groups.values():
        first = rows[0]
        accounts = [_account_ref(r) for r in rows]
        if len(rows) == 1:
            out.append({**first, "accounts": accounts})
            continue

        # 損益が計算できるのは costed_value を持つ行だけ（summarize と同じ判定）
        pl_rows = [r for r in rows if r.get("costed_value") is not None]
        pl_cost = _sum_present(r["cost"] for r in pl_rows)
        costed_value = _sum_present(r["costed_value"] for r in pl_rows)
        pl = (costed_value - pl_cost) if pl_rows and pl_cost is not None else None

        weights = [
            (r["avg_cost"], r.get("costed_quantity") or ZERO)
            for r in rows
            if r.get("avg_cost") is not None
        ]
        total_weight = sum((q for _, q in weights), ZERO)
        avg_cost = (
            sum((c * q for c, q in weights), ZERO) / total_weight
            if total_weight
            else None
        )

        # 前日比は全口座ぶん揃ったときだけ出す。欠けた口座があると、その分だけ
        # 増減が薄まった数字になり「小さく動いた」という誤読を生む。
        value_rows = [r for r in rows if r.get("value") is not None]
        day_rows = [r for r in value_rows if r.get("day_change") is not None]
        day_complete = bool(day_rows) and len(day_rows) == len(value_rows)
        day_change = sum((r["day_change"] for r in day_rows), ZERO) if day_complete else None
        prev_value = sum((r["prev_value"] for r in day_rows), ZERO) if day_complete else None

        names = [a["account"] for a in accounts if a["account"]]
        out.append(
            {
                **first,
                "account_id": None,
                "account": (
                    f"{names[0]} 他{len(names) - 1}件" if len(names) > 1 else
                    (names[0] if names else "")
                ),
                "accounts": accounts,
                "quantity": sum((r["quantity"] for r in rows), ZERO),
                "costed_quantity": total_weight,
                "avg_cost": avg_cost,
                "value": _sum_present(r["value"] for r in rows),
                "cost": _sum_present(r["cost"] for r in rows),
                "costed_value": costed_value,
                "pl": pl,
                "pl_pct": (pl / pl_cost * 100) if (pl is not None and pl_cost) else None,
                "prev_value": prev_value,
                "day_change": day_change,
                "day_change_pct": (
                    (day_change / prev_value * 100)
                    if (day_change is not None and prev_value)
                    else None
                ),
                "day_change_as_of": next(
                    (r["day_change_as_of"] for r in day_rows if r.get("day_change_as_of")),
                    None,
                ) if day_complete else None,
                "has_price": all(r.get("has_price") for r in rows),
                # 銘柄単位に合算しても「目安」の印を落とさない
                "estimated": any(r.get("estimated") for r in rows),
                "lot_count": sum(r.get("lot_count") or 0 for r in rows),
                "as_of": max(r.get("as_of") or "" for r in rows),
            }
        )

    out.sort(key=lambda h: (h["value"] is None, -(h["value"] or ZERO)))
    return out


class SeriesLookup:
    """date(ISO文字列) → Decimal の疎な系列に対する「d以前の直近値」検索。

    d が系列の開始より前の場合は最初の値を返す（backfill）。
    価格系列・FX系列の forward-fill をクエリ時に行うための道具で、
    DBには埋めた値を保存しない（設計方針）。
    """

    def __init__(self, series: dict[str, Decimal]):
        self._dates = sorted(series.keys())
        self._values = [series[d] for d in self._dates]

    def __bool__(self) -> bool:
        return bool(self._dates)

    def at(self, day: str) -> Decimal | None:
        if not self._dates:
            return None
        i = bisect_right(self._dates, day)
        if i == 0:
            return self._values[0]  # backfill: 系列開始前は最初の値
        return self._values[i - 1]

    def strictly_at_or_before(self, day: str) -> Decimal | None:
        """backfillしない版（当日以前の値のみ。無ければNone）。"""
        if not self._dates:
            return None
        i = bisect_right(self._dates, day)
        if i == 0:
            return None
        return self._values[i - 1]


class FxLookup:
    """通貨→JPYレート系列。JPYは常に1。"""

    def __init__(self, series_by_ccy: dict[str, dict[str, Decimal]]):
        self._lookups = {c: SeriesLookup(s) for c, s in series_by_ccy.items()}

    def rate(self, ccy: str, day: str) -> Decimal | None:
        if ccy == "JPY":
            return Decimal("1")
        lk = self._lookups.get(ccy)
        if lk is None:
            return None
        return lk.at(day)


def _lot_key(s: HoldingSnapshot) -> tuple[int, int, int]:
    return (s.account_id, s.security_id, s.lot_seq)


def lot_value_jpy(
    sec: Security,
    lot: HoldingSnapshot,
    price: Decimal | None,
    fx_rate: Decimal | None,
) -> tuple[Decimal | None, bool]:
    """(評価額JPY, has_price)。has_price=False は reported フォールバック。"""
    if sec.asset_class == AssetClass.CASH:
        rate = fx_rate if sec.currency != "JPY" else Decimal("1")
        if rate is None:
            return (lot.reported_value_jpy, False)
        return (lot.quantity * rate, True)
    if (
        sec.asset_class == AssetClass.PENSION
        and sec.price_source_type == PriceSourceType.TOUSHIN
        and lot.quantity <= 1
    ):
        # 基準価額に連携済みだが口数が未導出の年金スナップショット（旧形式の
        # quantity=1）。1口ぶんの価格を掛けると桁が壊れるため、導出されるまで
        # 記載評価額で評価する（fund_autolink.derive_pension_quantities が導出）
        price = None
    if price is not None:
        rate = fx_rate if sec.currency != "JPY" else Decimal("1")
        if rate is not None:
            value = lot.quantity * price / Decimal(sec.price_unit_divisor) * rate
            return (value, True)
    if lot.reported_value_jpy is not None:
        return (lot.reported_value_jpy, False)
    if sec.asset_class == AssetClass.POINT:
        # 換算情報が無いポイントは 1pt=1円 とみなす
        return (lot.quantity, False)
    return (None, False)


def _prev_lot_value_jpy(
    sec: Security,
    prev_lot: HoldingSnapshot,
    prev_price: Decimal | None,
    prev_fx_rate: Decimal | None,
    has_price: bool,
) -> Decimal | None:
    """前日のロットを前日終値で評価した評価額JPY。判らなければ None。

    数量も価格も前日のものを使う（推移グラフの前日の点と同じ評価）。現金は
    数量そのものが金額なので、価格差だけを見る定義では前日比が定義上ゼロに
    なってしまう — 残高の増減を出すには前日の数量で評価するしかない。株や
    投信も同じ規則で評価するので、現金で株を買った日は現金の減少と株の増加が
    相殺され、振替が見かけの損失にならない。

    前日値は「当日と同じ経路で値が付いたとき」だけ採用する。記載値
    フォールバック同士を突き合わせると、実際には判らない銘柄に「前日比 0」
    という嘘が出てしまう。ポイントは価格を持たないので前日の残高で評価する。
    """
    prev_value, prev_has_price = lot_value_jpy(sec, prev_lot, prev_price, prev_fx_rate)
    if prev_has_price and has_price:
        return prev_value
    if sec.asset_class == AssetClass.POINT and not has_price:
        return prev_value
    return None


def lot_cost_jpy(
    sec: Security,
    lot: HoldingSnapshot,
    fx_rate: Decimal | None,
    avg_cost_override: Decimal | None = None,
) -> Decimal | None:
    """取得コストJPY。avg_cost 無しは None（損益対象外）。

    avg_cost_override は取引履歴から再計算した取得単価。スナップショットと
    同じ単位（建値通貨・divisor 適用前）で渡すこと。渡されるのは完全被覆の
    ものだけ — 部分被覆の再計算値は定義上スナップショットと一致するため
    （core/cost_basis.py の説明を参照）、上書きする意味が無い。
    """
    avg_cost = avg_cost_override if avg_cost_override is not None else lot.avg_cost
    if avg_cost is None:
        return None
    if sec.asset_class == AssetClass.PENSION:
        # 年金は avg_cost = 取得価額の総額（円）
        return avg_cost
    rate = fx_rate if sec.currency != "JPY" else Decimal("1")
    if rate is None:
        return None
    return lot.quantity * avg_cost / Decimal(sec.price_unit_divisor) * rate


def _cost_override(
    overrides: dict[tuple[int, int], Decimal] | None, lot: HoldingSnapshot
) -> Decimal | None:
    if not overrides:
        return None
    return overrides.get((lot.account_id, lot.security_id))


def _display(value: Decimal | None, jpy_per_display: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value / jpy_per_display


def include_setting_key(asset_class: AssetClass | str) -> str:
    """資産クラスの「総資産に含める」設定キー。

    'point' だけは旧キー 'include_points'（複数形）で保存されていたため、
    そちらを正とする（既存DBの設定を壊さない）。
    """
    value = asset_class.value if isinstance(asset_class, AssetClass) else asset_class
    return "include_points" if value == "point" else f"include_{value}"


def _excluded_classes(settings: dict[str, str]) -> set[AssetClass]:
    """総資産の集計から外す資産クラス（既定はすべて含める）。"""
    return {
        cls
        for cls in AssetClass
        if settings.get(include_setting_key(cls), "1") != "1"
    }


def summarize(
    lots: Iterable[HoldingSnapshot],
    securities: dict[int, Security],
    accounts: dict[int, Account],
    spot: dict[int, Decimal],
    fx: dict[str, Decimal],
    settings: dict[str, str],
    jpy_per_display: Decimal = Decimal("1"),
    prev_spot: dict[int, Decimal] | None = None,
    prev_fx: dict[str, Decimal] | None = None,
    prev_as_of: dict[int, str] | None = None,
    prev_jpy_per_display: Decimal | None = None,
    prev_lots: Iterable[HoldingSnapshot] | None = None,
    cost_overrides: dict[tuple[int, int], Decimal] | None = None,
) -> dict[str, Any]:
    """現在保有の評価サマリー。

    spot: security_id → 建値通貨・divisor適用前の現在単価
    fx:   通貨 → JPYレート（現在値）
    jpy_per_display: 表示通貨1単位あたりのJPY（JPY表示なら1）
    cost_overrides: (口座, 銘柄) → 取引履歴から再計算した取得単価
    金額はすべて Decimal のまま返す（JSON化は web 層で文字列に）。

    prev_spot / prev_fx / prev_as_of を渡すと前日比（day_change）も算出する。
    省略時は day_change 系がすべて None になり、従来どおりの結果になる。
    表示通貨換算は当日と前日それぞれのレートで行うので、外貨表示のときも
    「表示通貨で見た評価額の増減」になる。

    prev_lots: 前日時点のスナップショット（store.holdings_as_of(前日)）。
    渡すと前日値を「前日の数量 × 前日の価格」で評価するので、前日比が
    推移グラフの前日との差と同じ意味になる — 現金の残高増減や買付・売却も
    入る。渡さなければ当日の数量で評価する（価格が動いたぶんだけ）。
    """
    excluded = _excluded_classes(settings)
    merge_cash = merge_cash_enabled(settings)
    prev_spot = prev_spot or {}
    prev_fx = prev_fx or {}
    prev_as_of = prev_as_of or {}
    prev_display = prev_jpy_per_display or jpy_per_display
    # 前日にまだ記録の無いロットは当日のロットで代用する（推移グラフの
    # 遡及ルールと同じ — 初めて取り込んだ保有が「その日に増えた」ことに
    # ならないよう、過去にも同じ数量あったものとして扱う）
    prev_by_key = {_lot_key(s): s for s in (prev_lots or [])}

    # (security_id, account_id) 単位にロットを集約
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    unpriced: list[str] = []
    for lot in lots:
        sec = securities.get(lot.security_id)
        if sec is None:
            continue
        prev_lot = prev_by_key.get(_lot_key(lot), lot)
        if lot.quantity == ZERO and prev_lot.quantity == ZERO:
            continue
        fx_rate = fx.get(sec.currency) if sec.currency != "JPY" else Decimal("1")
        price = spot.get(lot.security_id)
        value, has_price = lot_value_jpy(sec, lot, price, fx_rate)
        cost = lot_cost_jpy(sec, lot, fx_rate, _cost_override(cost_overrides, lot))
        prev_value = _prev_lot_value_jpy(
            sec, prev_lot, prev_spot.get(lot.security_id),
            prev_fx.get(sec.currency) if sec.currency != "JPY" else Decimal("1"),
            has_price,
        )
        key = (lot.security_id, lot.account_id)
        entry = merged.setdefault(
            key,
            {
                "security": sec,
                "account": accounts.get(lot.account_id),
                "quantity": ZERO,
                "value_jpy": ZERO,
                "has_value": False,
                "prev_value_jpy": ZERO,
                "has_prev": False,
                "prev_partial": False,
                "cost_jpy": ZERO,
                "has_cost": False,
                "costed_value_jpy": ZERO,
                "costed_value_unknown": False,
                "costed_quantity": ZERO,
                "uncosted_lots": 0,
                "has_price": has_price,
                "lots": [],
                "as_of": lot.as_of_date,
            },
        )
        entry["quantity"] += lot.quantity
        if value is not None:
            entry["value_jpy"] += value
            entry["has_value"] = True
        if prev_value is not None:
            entry["prev_value_jpy"] += prev_value
            entry["has_prev"] = True
        elif value is not None:
            # 評価額はあるのに前日値が無いロット。1本でも欠けたら
            # この保有の前日比は出せない（欠けた分だけ増えたように見えるため）
            entry["prev_partial"] = True
        if cost is not None:
            entry["cost_jpy"] += cost
            entry["has_cost"] = True
            # 損益は「原価が判っているロット」の評価額とだけ突き合わせる
            # （原価なしロットの評価額を利益に混ぜないため）
            entry["costed_quantity"] += lot.quantity
            if value is not None:
                entry["costed_value_jpy"] += value
            else:
                # 原価は判るが評価額が判らない（価格ソース未設定の手動登録など）。
                # 0円扱いにすると「全損」という嘘の損益になるため損益は出さない
                entry["costed_value_unknown"] = True
        else:
            entry["uncosted_lots"] += 1
        entry["has_price"] = entry["has_price"] and has_price if entry["lots"] else has_price
        entry["as_of"] = max(entry["as_of"], lot.as_of_date)
        entry["lots"].append(lot)

    holdings: list[dict[str, Any]] = []
    total_value = ZERO          # 表示用の総評価額（原価の有無によらず全額）
    total_costed_value = ZERO   # 損益計算の対象となる評価額（原価ありロットのみ）
    total_cost = ZERO
    has_any_cost = False
    pl_excluded_count = 0
    # 前日比は「前日値が判る保有だけ」を足す。欠けた保有があれば partial を立て、
    # UI 側で「一部の銘柄を除く」と断れるようにする（pl_excluded_count と同じ流儀）
    total_prev_value = ZERO
    total_prev_known = ZERO     # 前日値が判る保有の当日評価額（差分の分母合わせ用）
    has_any_prev = False
    day_change_partial = False

    for (sec_id, acct_id), e in merged.items():
        sec: Security = e["security"]
        acct: Account | None = e["account"]
        value = e["value_jpy"] if e["has_value"] else None
        prev_value = (
            e["prev_value_jpy"] if (e["has_prev"] and not e["prev_partial"]) else None
        )
        qty = e["quantity"]
        pl_computable = e["has_cost"] and not e["costed_value_unknown"]
        cost = e["cost_jpy"] if e["has_cost"] else None
        costed_value = e["costed_value_jpy"]
        pl = (costed_value - cost) if pl_computable else None
        pl_pct = (pl / cost * 100) if (pl is not None and cost) else None
        avg_cost = None
        if e["has_cost"] and e["costed_quantity"]:
            # 表示用の合成平均取得単価（建値通貨・divisor適用前の単価に戻す）。
            # 原価ありロットの数量で割る（原価なしロットを混ぜると過少になる）
            fx_rate = fx.get(sec.currency) if sec.currency != "JPY" else Decimal("1")
            if fx_rate and sec.asset_class != AssetClass.PENSION:
                avg_cost = (
                    e["cost_jpy"] / fx_rate * Decimal(sec.price_unit_divisor)
                    / e["costed_quantity"]
                )
        price = spot.get(sec_id)
        # 数量ゼロの行は前日比のためだけに残している（当日ゼロになった保有）。
        # 価格ソースの有無を問うても意味がないので「価格なし」には数えない
        if not e["has_price"] and qty != ZERO and sec.asset_class not in (
            AssetClass.CASH,
            AssetClass.POINT,
            AssetClass.PENSION,
        ):
            unpriced.append(sec.name)
        # 前日比は表示通貨換算を当日・前日それぞれのレートで行う
        # （外貨表示のとき「表示通貨で見た増減」になるように）
        disp_value = _display(value, jpy_per_display)
        disp_prev_value = _display(prev_value, prev_display)
        day_change = (
            (disp_value - disp_prev_value)
            if (disp_value is not None and disp_prev_value is not None)
            else None
        )
        day_change_pct = (
            (day_change / disp_prev_value * 100)
            if (day_change is not None and disp_prev_value)
            else None
        )

        in_total = sec.asset_class not in excluded
        if in_total:
            if value is not None:
                total_value += value
            if disp_prev_value is not None and disp_value is not None:
                total_prev_value += disp_prev_value
                total_prev_known += disp_value
                has_any_prev = True
            elif value is not None:
                day_change_partial = True
            if pl_computable:
                total_cost += cost
                total_costed_value += costed_value
                has_any_cost = True
            # 損益を出せないロットを含む保有は、その旨を件数で示す
            # （現金・ポイントは本来原価を持たないため対象外）
            if (e["uncosted_lots"] or e["costed_value_unknown"]) and (
                sec.asset_class not in (AssetClass.CASH, AssetClass.POINT)
            ):
                pl_excluded_count += 1
        holdings.append(
            {
                "id": sec_id,
                "account_id": acct_id,
                "account": (acct.display_name or acct.name) if acct else "",
                "name": sec.name,
                "code": sec.code,
                "asset_class": sec.asset_class.value,
                "unit": sec.unit.value,
                "currency": sec.currency,
                "quantity": qty,
                # avg_cost の分母（原価ありロットの数量）。銘柄単位へ合算するとき
                # 加重平均の重みとして使う（quantity で重み付けすると原価なし
                # ロットの分だけ単価が薄まる）
                "costed_quantity": e["costed_quantity"] if e["has_cost"] else ZERO,
                "avg_cost": avg_cost,
                "price": price,
                "value": _display(value, jpy_per_display),
                "cost": _display(cost, jpy_per_display),
                # 損益対象の評価額（原価ありロット分のみ）。クラス別・口座別の
                # 損益集計はこの値と cost を突き合わせる（value を使うと
                # 原価なしロットの評価額が利益に混ざる）
                "costed_value": _display(costed_value, jpy_per_display)
                if pl_computable
                else None,
                "pl": _display(pl, jpy_per_display),
                "pl_pct": pl_pct,
                # 前日終値で評価した額と、そこからの増減（表示通貨）
                "prev_value": disp_prev_value,
                "day_change": day_change,
                "day_change_pct": day_change_pct,
                "day_change_as_of": prev_as_of.get(sec_id) if prev_value is not None else None,
                "has_price": e["has_price"],
                "price_source_status": sec.price_source_status.value,
                "lot_count": len(e["lots"]),
                "in_total": in_total,
                "as_of": e["as_of"].isoformat(),
            }
        )

    holdings.sort(
        key=lambda h: (h["value"] is None, -(h["value"] or ZERO))
    )

    total_pl = (total_costed_value - total_cost) if has_any_cost else None
    total_pl_pct = (
        (total_pl / total_cost * 100) if (total_pl is not None and total_cost) else None
    )

    # クラス別集計（トグル除外前のクラスも weight 計算のため保持）
    classes: dict[str, dict[str, Any]] = {}
    for h in holdings:
        c = classes.setdefault(
            h["asset_class"],
            {"class": h["asset_class"], "value": ZERO, "cost": ZERO,
             "costed_value": ZERO, "has_cost": False,
             "prev_value": ZERO, "day_change": ZERO,
             "has_prev": False, "prev_partial": False,
             "ids": set(), "in_total": h["in_total"]},
        )
        if h["value"] is not None:
            c["value"] += h["value"]
        if h["prev_value"] is not None and h["day_change"] is not None:
            c["prev_value"] += h["prev_value"]
            c["day_change"] += h["day_change"]
            c["has_prev"] = True
        elif h["value"] is not None:
            c["prev_partial"] = True
        if h["cost"] is not None and h["costed_value"] is not None:
            c["cost"] += h["cost"]
            c["costed_value"] += h["costed_value"]
            c["has_cost"] = True
        # 保有一覧の行数と一致させる（同一銘柄の複数口座は1件、預金は設定に従う）
        c["ids"].add(_security_group_key(h, merge_cash))
    class_rows = []
    for c in classes.values():
        pl = (c["costed_value"] - c["cost"]) if c["has_cost"] else None
        day_change = c["day_change"] if c["has_prev"] else None
        class_rows.append(
            {
                "class": c["class"],
                "value": c["value"],
                "cost": c["cost"] if c["has_cost"] else None,
                "pl": pl,
                "pl_pct": (pl / c["cost"] * 100) if (pl is not None and c["cost"]) else None,
                "day_change": day_change,
                "day_change_pct": (
                    (day_change / c["prev_value"] * 100)
                    if (day_change is not None and c["prev_value"])
                    else None
                ),
                # 「一部を除く」は数字を出しているときだけ意味がある。
                # 前日値が1件も無い（＝前日比を出していない）なら partial ではない
                "day_change_partial": c["prev_partial"] and c["has_prev"],
                "prev_value": c["prev_value"] if c["has_prev"] else None,
                "holding_count": len(c["ids"]),
                "in_total": c["in_total"],
                "weight": (
                    (c["value"] / _display(total_value, jpy_per_display) * 100)
                    if c["in_total"] and total_value
                    else None
                ),
            }
        )
    class_rows.sort(key=lambda c: -(c["value"] or ZERO))

    total_day_change = (total_prev_known - total_prev_value) if has_any_prev else None

    return {
        "total_value": _display(total_value, jpy_per_display),
        "total_cost": _display(total_cost, jpy_per_display) if total_cost else None,
        "total_pl": _display(total_pl, jpy_per_display) if total_pl is not None else None,
        "total_pl_pct": total_pl_pct,
        "pl_excluded_count": pl_excluded_count,
        "total_day_change": total_day_change,
        "total_day_change_pct": (
            (total_day_change / total_prev_value * 100)
            if (total_day_change is not None and total_prev_value)
            else None
        ),
        "day_change_partial": day_change_partial and has_any_prev,
        "total_prev_value": total_prev_value if has_any_prev else None,
        "holdings": holdings,
        "classes": class_rows,
        "unpriced": sorted(set(unpriced)),
    }


def daily_series(
    snapshots: Iterable[HoldingSnapshot],
    securities: dict[int, Security],
    accounts: dict[int, Account],
    price_series: dict[int, tuple[dict[str, Decimal], str]],
    fx_series: dict[str, dict[str, Decimal]],
    start: date,
    end: date,
    settings: dict[str, str],
    jpy_per_display_series: dict[str, Decimal] | None = None,
    scope: tuple[str, str] | None = None,
    ratio_by_security: dict[int, Decimal] | None = None,
    cost_overrides: dict[tuple[int, int], Decimal] | None = None,
) -> list[dict[str, Any]]:
    """日次評価額系列。

    price_series: security_id → (date→単価, 通貨)
    fx_series:    通貨 → (date→JPYレート)
    scope: ("account", 口座表示名) | ("class", asset_class) | ("security", id文字列) | None
    ratio_by_security: security_id → 計上率(0〜1)。タグ・Myポートフォリオの推移で
        「配分率ぶんだけ計上する」ために各銘柄の寄与へ掛ける。ここに載らない
        銘柄は集計から外れる（scope と違い銘柄ごとに重みを変えられる）。
    返値: [{"t": ISO日付, "value": Decimal, "cost": Decimal|None}, ...]
    """
    excluded = _excluded_classes(settings)
    fx = FxLookup(fx_series)
    display_fx = SeriesLookup(jpy_per_display_series or {})

    # ロットごとにスナップショット時系列を構築
    lots: dict[tuple[int, int, int], list[HoldingSnapshot]] = {}
    for s in snapshots:
        sec = securities.get(s.security_id)
        if sec is None:
            continue
        if sec.asset_class in excluded:
            continue
        if scope is not None:
            kind, ref = scope
            if kind == "account":
                acct = accounts.get(s.account_id)
                disp = (acct.display_name or acct.name) if acct else ""
                if disp != ref:
                    continue
            elif kind == "class" and sec.asset_class.value != ref:
                continue
            elif kind == "security" and str(s.security_id) != ref:
                continue
        if ratio_by_security is not None and not ratio_by_security.get(s.security_id):
            continue
        lots.setdefault(_lot_key(s), []).append(s)

    prepared = []
    for key, snaps in lots.items():
        snaps.sort(key=lambda s: s.as_of_date)
        sec = securities[key[1]]
        series, ccy = price_series.get(key[1], ({}, sec.currency))
        prepared.append(
            {
                "sec": sec,
                "snaps": snaps,
                "snap_dates": [s.as_of_date.isoformat() for s in snaps],
                "price": SeriesLookup(series),
                "price_ccy": ccy,
                "ratio": (
                    ratio_by_security.get(key[1], ZERO)
                    if ratio_by_security is not None
                    else Decimal("1")
                ),
            }
        )

    out: list[dict[str, Any]] = []
    day = start
    while day <= end:
        d = day.isoformat()
        total = ZERO
        total_cost = ZERO
        any_cost = False
        for p in prepared:
            snaps: list[HoldingSnapshot] = p["snaps"]
            i = bisect_right(p["snap_dates"], d)
            # 初回登録日以前は最初のスナップショットの保有数で遡及（確定要件）
            snap = snaps[i - 1] if i > 0 else snaps[0]
            if snap.quantity == ZERO:
                continue
            sec: Security = p["sec"]
            ratio: Decimal = p["ratio"]
            if sec.asset_class == AssetClass.CASH:
                rate = fx.rate(sec.currency, d)
                if rate is not None:
                    total += snap.quantity * rate * ratio
                elif snap.reported_value_jpy is not None:
                    total += snap.reported_value_jpy * ratio
                continue
            price = p["price"].at(d)
            if (
                sec.asset_class == AssetClass.PENSION
                and sec.price_source_type == PriceSourceType.TOUSHIN
                and snap.quantity <= 1
            ):
                # 口数未導出の年金は価格で評価しない（lot_value_jpy と同じガード）
                price = None
            rate = fx.rate(p["price_ccy"], d) if price is not None else None
            if price is not None and rate is not None:
                total += (
                    snap.quantity * price / Decimal(sec.price_unit_divisor) * rate * ratio
                )
            elif snap.reported_value_jpy is not None:
                # 価格はあるがFXレートが無い場合も含む。1:1で円換算すると
                # 外貨建て資産が桁違いに過少評価されるため、記載値へフォールバック
                total += snap.reported_value_jpy * ratio
            elif sec.asset_class == AssetClass.POINT:
                total += snap.quantity * ratio
            cost = lot_cost_jpy(
                sec, snap, fx.rate(sec.currency, d), _cost_override(cost_overrides, snap)
            )
            if cost is not None:
                total_cost += cost * ratio
                any_cost = True
        disp = display_fx.at(d) if display_fx else None
        divisor = disp if disp else Decimal("1")
        out.append(
            {
                "t": d,
                "value": total / divisor,
                "cost": (total_cost / divisor) if any_cost else None,
            }
        )
        day += timedelta(days=1)
    return out
