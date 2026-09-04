"""1 行 → ParsedTx への変換（純関数）。

ここでは「ファイルに何と書いてあったか」だけを組み立てる。現在保有との差分や
原価の再計算はしない（core/cost_basis.py の仕事）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ...core.models import TxType
from ..base import nfkc
from .contracts import (
    PENALTY_CHECK_FAILED,
    PENALTY_MISSING_FIELD,
    PENALTY_MINOR,
    MIN_IDENTITY_ROWS,
    PENALTY_UNKNOWN_TYPE,
    CanonicalField as F,
    DetectedFormat,
    ParsedTx,
    SheetGrid,
)
from .shapes import parse_amount, parse_date, parse_ratio, split_leading_code
from .vocab import classify_tx_type, is_margin_trade, normalize_currency, sell_kind

# 行内検算の許容差（円）。mf_pdf._check_row と同じ考え方。
ROW_TOLERANCE = Decimal("1")


def _text(grid: SheetGrid, row: int, col: int | None) -> str:
    if col is None:
        return ""
    return nfkc(grid.cell(row, col)).strip()


def _num(grid: SheetGrid, row: int, col: int | None) -> Decimal | None:
    if col is None:
        return None
    return parse_amount(grid.cell(row, col))


def _sum_cols(grid: SheetGrid, row: int, cols: tuple[int, ...]) -> Decimal | None:
    total: Decimal | None = None
    for col in cols:
        value = parse_amount(grid.cell(row, col))
        if value is None:
            continue
        total = value if total is None else total + value
    return total


def _add(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def row_to_tx(grid: SheetGrid, row: int, fmt: DetectedFormat) -> ParsedTx:
    """1 データ行を ParsedTx にする。読めない部分は警告と減点で表す。"""
    col = fmt.column_for
    tx = ParsedTx(row_index=row, confidence=fmt.confidence)

    tx.trade_date = parse_date(_text(grid, row, col(F.TRADE_DATE)), order=fmt.date_order)
    tx.settle_date = parse_date(_text(grid, row, col(F.SETTLE_DATE)), order=fmt.date_order)
    if tx.trade_date is None and tx.settle_date is not None:
        # 約定日が無い行は受渡日で代用する（順序が保てればよい）。
        # 配当金・振替にはそもそも約定日が無く、これは異常ではないので減点しない。
        # 減点すると、そういう行が丸ごと「要確認」に落ちて既定で取込対象から外れる。
        tx.trade_date = tx.settle_date
        tx.warnings.append("約定日が無いため受渡日を使いました")
    if tx.trade_date is None:
        tx.warnings.append("日付を読み取れませんでした")
        tx.confidence -= PENALTY_MISSING_FIELD

    name_raw = _text(grid, row, col(F.SECURITY_NAME))
    code_raw = _text(grid, row, col(F.SECURITY_CODE)) or None
    name_col = col(F.SECURITY_NAME)
    if name_col is not None:
        assignment = next((c for c in fmt.columns if c.index == name_col), None)
        if assignment is not None and assignment.split_leading_code:
            lead, rest = split_leading_code(name_raw)
            if lead:
                code_raw = code_raw or lead
                name_raw = rest
    tx.security_name_raw = name_raw
    tx.security_code_raw = code_raw
    # 銘柄が無いことを咎めるのは種別が決まってから。入出金の行に銘柄が無いのは
    # 当然で、ここで減点すると入出金がまとめて「要確認」に落ちる。
    missing_security = not name_raw and not code_raw

    tx.account_raw = _text(grid, row, col(F.ACCOUNT))
    tx.account_type_raw = _text(grid, row, col(F.ACCOUNT_TYPE)) or None
    tx.note = _text(grid, row, col(F.NOTE)) or None
    tx.exchange_rate = _num(grid, row, col(F.EXCHANGE_RATE))

    currency = normalize_currency(_text(grid, row, col(F.CURRENCY)))
    tx.currency = currency or "JPY"

    quantity = _num(grid, row, col(F.QUANTITY))
    tx.unit_price = _num(grid, row, col(F.UNIT_PRICE))
    gross = _num(grid, row, col(F.GROSS_AMOUNT))
    net = _num(grid, row, col(F.NET_AMOUNT))
    tx.fee = _add(_num(grid, row, col(F.FEE)), _sum_cols(grid, row, fmt.extra_fee_columns))
    tx.tax = _add(_num(grid, row, col(F.TAX)), _sum_cols(grid, row, fmt.extra_tax_columns))

    type_raw = _text(grid, row, col(F.TX_TYPE))
    kind, kind_conf = classify_tx_type(type_raw) if type_raw else (TxType.OTHER, 0.0)

    if kind is TxType.OTHER and fmt.sign_convention != "by_type":
        kind = _type_from_sign(fmt.sign_convention, quantity, net)

    # 向きの語が無い「振替」は金額の符号で入出金を決める。
    # 手数料の無い書式では受渡金額と約定代金が一致し、検算の結果しだいで
    # どちらの欄に入るか変わるので、両方を見て符号のあるほうを使う。
    signed_amount = net if net is not None else gross
    if (kind in (TxType.CASH_IN, TxType.CASH_OUT) and kind_conf < 0.9
            and signed_amount is not None):
        kind = TxType.CASH_OUT if signed_amount < 0 else TxType.CASH_IN

    # MRF は実質現金。どの形で来ても入出金として扱う（取込対象外になる）。
    if kind not in (TxType.CASH_IN, TxType.CASH_OUT) and _is_mrf(name_raw, type_raw or ""):
        kind = (TxType.CASH_OUT if signed_amount is not None and signed_amount < 0
                else TxType.CASH_IN)

    if kind is TxType.OTHER:
        if type_raw and is_margin_trade(type_raw):
            # 現物と同じ売買にすると保有数が壊れるので対象外にしている。
            # 「判別できません」ではなく、外している理由をそのまま言う。
            tx.warnings.append(f"信用取引（{type_raw}）のため取込対象から外しました")
            tx.raw["margin"] = True
        else:
            tx.warnings.append(
                f"取引区分を判別できませんでした（{type_raw or '空欄'}）" if type_raw
                else "取引区分を判別できませんでした"
            )
        tx.confidence -= PENALTY_UNKNOWN_TYPE
    tx.tx_type = kind.value

    if type_raw:
        tx.raw["tx_type_raw"] = type_raw
        sk = sell_kind(type_raw)
        if sk:
            tx.raw["sell_kind"] = sk
        # 現渡・現引の単価は建単価（建てたときの価格）で、その日の時価ではない。
        # 価格照合の証拠に混ぜると正しい候補まで落ちる（入出庫と同じ理屈）。
        if any(w in type_raw for w in ("現渡", "現引", "品受", "品渡")):
            tx.raw["off_market_price"] = True
        if kind is TxType.RETURN_OF_CAPITAL:
            tx.raw["dividend_kind"] = "特別分配金"
            tx.warnings.append("特別分配金（元本払戻金）として取得原価から差し引きます")

    tx.quantity = _signed_quantity(kind, quantity, fmt.sign_convention)
    tx.gross_amount = abs(gross) if gross is not None else None
    tx.net_amount = _signed_net(kind, net)
    if kind is TxType.SPLIT:
        tx.split_ratio = parse_ratio(_text(grid, row, col(F.UNIT_PRICE))) if tx.unit_price else None

    # 銘柄も数量も無い売買は売買ではない。証券会社は現金の動きに売買らしい
    # 名前を付けることがある（マネックスの『ご入金（カード積立）』は銘柄なし・
    # 数量 0・受渡金額のみで、語彙だけ見ると「積立」＝買付になる）。
    # 語彙の優先順ではなく行の形で決める — どの書式でも同じ判断ができる。
    if (kind in (TxType.BUY, TxType.SELL, TxType.REINVEST)
            and missing_security and not quantity and tx.net_amount is not None):
        kind = TxType.CASH_OUT if tx.net_amount < 0 else TxType.CASH_IN
        tx.tx_type = kind.value
        tx.quantity = None
        tx.confidence += PENALTY_MISSING_FIELD   # 上で引いた分は理由が消えるので戻す
        tx.warnings.append("銘柄も数量も無いため現金の移動として扱いました")

    if missing_security and kind not in (TxType.CASH_IN, TxType.CASH_OUT):
        tx.warnings.append("銘柄を読み取れませんでした")
        tx.confidence -= PENALTY_MISSING_FIELD

    if kind in (TxType.BUY, TxType.SELL, TxType.REINVEST) and tx.quantity is None:
        tx.warnings.append("数量を読み取れませんでした")
        tx.confidence -= PENALTY_MISSING_FIELD

    _check_row(tx, fmt.divisor)
    tx.confidence = max(0.0, min(1.0, tx.confidence))
    return tx


_BLANK_TYPE_WARNING = "取引区分を判別できませんでした"


def _is_mrf(name_raw: str, type_raw: str) -> bool:
    """MRF（マネー・リザーブ・ファンド）の行か。

    MRF は証券口座の待機資金の置き場で、投資信託の形をしているが実質は
    普通預金（入金すると自動で買われ、1口=1円、利息は毎月再投資される）。
    MF 側でも現金・預金として集計されるので、買付・売却・再投資の形で
    来ても現金の移動として扱う。MMF は含めない — あちらは基準価額を持つ
    投資商品で、保有として数える。
    """
    return "mrf" in nfkc(f"{name_raw} {type_raw}").lower()


def has_no_movement(tx: ParsedTx) -> bool:
    """数量も金額もすべて 0 の行。

    マネックスの『ＭＲＦ再投資』は利息が付かない月も 0 円の行を出す
    （長い履歴では数百行になる）。何も増減しない行は取り込む意味も見せる意味も
    無いので、プレビューにも出さない。
    """
    return all(not v for v in (tx.quantity, tx.gross_amount, tx.net_amount,
                               tx.fee, tx.tax))


def _is_blank_candidate(tx: ParsedTx, divisor: int) -> bool:
    """向きを推定してよい空欄行か。

    現金の約定の形（数量×単価≒受渡金額）をした行だけ。コーポレートアクション
    などは金額が合わないので対象にならない。ラベルのある行（信用取引を含む）は
    推定の対象外 — 読めなかったのではなく、読んだうえで対象外にした行だから。
    """
    if tx.tx_type != TxType.OTHER.value or "tx_type_raw" in tx.raw:
        return False
    if tx.raw.get("margin") or not tx.quantity or not tx.unit_price:
        return False
    settled = tx.gross_amount if tx.gross_amount is not None else (
        abs(tx.net_amount) if tx.net_amount is not None else None
    )
    if settled is None:
        return False
    # divisor はファイル単位で決まるが、株と投信が混ざったファイルでは行ごとに
    # 違う（株は 1、投信の基準価額は 1万口あたり）。どちらかで約定の形に
    # なっていれば現金の売買と見る。
    # 許容差は両方向の丸めを見る。金額指定のつみたては口数を 1 口未満で
    # 切り捨てるので「1 口 × 単価」ぶんの誤差が出る（実データでは、口数 ×
    # 基準価額 /1万 が受渡金額より数円多くなる行があった）。単価の丸めぶん
    # 「0.5 円 × 口数」も残す。これは約定の形かどうかの判定にだけ使い、
    # 売買の向きはこの後の残高の整合で決めるので、緩めても向きは誤らない。
    # 100 は外貨建てMMF・債券の慣習（100 口/額面100円あたりの単価）。
    for div in (divisor, 1, 100, 10000):
        d = Decimal(div)
        expected = abs(tx.quantity) * abs(tx.unit_price) / d
        tol = (ROW_TOLERANCE
               + (abs(tx.quantity) * Decimal("0.5") + abs(tx.unit_price)) / d)
        if abs(expected - settled) <= tol:
            return True
    return False


def infer_blank_trade_types(
    txs: list[ParsedTx], divisor: int, *, has_type_column: bool
) -> int:
    """取引区分が空欄の行の向きを推定する。返り値は推定した行数。

    証券会社は特定の取引で種別の欄を空にする（実データでは投信のつみたて
    買付がそうで、長期の履歴では数百行あった）。空欄のままだと保有も原価も
    動かせないが、向きは
    ファイル自身から決められることが多い:

    1. 銘柄ごとに、空欄行を売却と読むと保有がマイナスに落ちる（無いものは
       売れない）なら、その銘柄の空欄行は買付と確定する。実データでは
       空欄行の 6 割強がこれで確定した。
    2. 空欄は書式の性質 — 出力プログラムが特定の取引種別で空を書く — なので、
       同じファイルの中では同じ意味を持つ。1. で確定した行が十分あれば、
       残りの空欄行（入庫が先にあって 1. では証明できない銘柄）にも同じ
       読みを適用する。

    どちらも推定であることを行の警告に残す。間違っていれば利用者が
    プレビューで外せる。

    **種別の列そのものが無いファイルでは推定しない。** 列があって空欄なのは
    「この取引は種別を書かない」という出力側の選択で、他の行のラベルが
    裏づけになる。列が無いのは情報が最初から無いだけで、売却だけを出力した
    ファイル（保有はファイルの外で始まっている）と区別できない。
    """
    if not has_type_column:
        return 0
    groups: dict[str, list[ParsedTx]] = {}
    for tx in txs:
        if tx.security_name_raw:
            groups.setdefault(tx.security_name_raw, []).append(tx)

    proven: list[ParsedTx] = []
    unproven: list[ParsedTx] = []
    far_future = date.max
    for ts in groups.values():
        blanks = [t for t in ts if _is_blank_candidate(t, divisor)]
        if not blanks:
            continue
        balance = Decimal(0)
        breaks = False
        for t in sorted(ts, key=lambda t: (t.trade_date or far_future, t.row_index)):
            q = abs(t.quantity) if t.quantity is not None else Decimal(0)
            if _is_blank_candidate(t, divisor):
                balance -= q          # 売却と仮定してみる
            elif t.tx_type in (TxType.BUY.value, TxType.REINVEST.value,
                               TxType.TRANSFER_IN.value):
                balance += q
            elif t.tx_type in (TxType.SELL.value, TxType.TRANSFER_OUT.value):
                balance -= q
            if balance < 0:
                breaks = True
                break
        (proven if breaks else unproven).extend(blanks)

    if len(proven) < MIN_IDENTITY_ROWS:
        return 0      # 証明できた行が少なすぎる。書式の性質とまでは言えない

    for tx in proven:
        _apply_inferred_buy(tx, "売却と読むと保有がマイナスになるため")
    for tx in unproven:
        _apply_inferred_buy(tx, "同じファイルの空欄行が買付と確定しているため")
    return len(proven) + len(unproven)


def _apply_inferred_buy(tx: ParsedTx, reason: str) -> None:
    tx.tx_type = TxType.BUY.value
    tx.quantity = abs(tx.quantity) if tx.quantity is not None else None
    if tx.net_amount is not None:
        tx.net_amount = -abs(tx.net_amount)   # 買いは現金が出ていく
    tx.raw["inferred_type"] = "buy"
    tx.warnings = [w for w in tx.warnings if w != _BLANK_TYPE_WARNING]
    tx.warnings.append(f"取引区分が空欄のため買付と推定しました（{reason}）")
    tx.confidence = min(1.0, tx.confidence + PENALTY_UNKNOWN_TYPE)


def _type_from_sign(
    convention: str, quantity: Decimal | None, net: Decimal | None
) -> TxType:
    """取引区分の列が無い書式で、符号から売買を決める。"""
    if convention == "signed_quantity" and quantity is not None and quantity != 0:
        return TxType.BUY if quantity > 0 else TxType.SELL
    if convention == "signed_net" and net is not None and net != 0:
        # 買いは現金が出ていく＝受渡金額が負
        return TxType.BUY if net < 0 else TxType.SELL
    return TxType.OTHER


def _signed_quantity(
    kind: TxType, quantity: Decimal | None, convention: str
) -> Decimal | None:
    """数量を符号つき増減にする。売却は負。"""
    if quantity is None:
        return None
    if kind in (TxType.SELL, TxType.TRANSFER_OUT):
        return -abs(quantity)
    if kind in (TxType.BUY, TxType.REINVEST, TxType.TRANSFER_IN):
        return abs(quantity)
    if kind is TxType.SPLIT:
        return quantity        # 分割は差分をそのまま（比率形式なら別途 split_ratio）
    if kind in (TxType.DIVIDEND, TxType.RETURN_OF_CAPITAL):
        return None            # 配当は保有数を動かさない
    if kind in (TxType.CASH_IN, TxType.CASH_OUT):
        return None            # 現金の移動。数量欄に何か入っていても保有には無関係
    return quantity


def _signed_net(kind: TxType, net: Decimal | None) -> Decimal | None:
    """受渡金額の符号をそろえる（買い負・売り正・配当正）。"""
    if net is None:
        return None
    if kind is TxType.BUY or kind is TxType.REINVEST:
        return -abs(net)
    if kind in (TxType.DIVIDEND, TxType.RETURN_OF_CAPITAL):
        # 負の配当は実在する（信用配当金 = 空売り中の配当落調整金の支払い）。
        # abs でそろえると支払いが受け取りに化けるので、符号は原文を信じる。
        return net
    if kind in (TxType.SELL, TxType.CASH_IN):
        return abs(net)
    if kind is TxType.CASH_OUT:
        return -abs(net)
    return net


def _check_row(tx: ParsedTx, divisor: int) -> None:
    """行内検算。数量×単価≒約定代金、約定代金±費用≒受渡金額。"""
    if tx.quantity and tx.unit_price and tx.gross_amount:
        expected = abs(tx.quantity) * abs(tx.unit_price) / Decimal(divisor)
        tol = ROW_TOLERANCE + abs(tx.quantity) * Decimal("0.5") / Decimal(divisor)
        if abs(expected - tx.gross_amount) > tol:
            tx.warnings.append(
                f"行内検算が一致しません（数量×単価 {expected:.0f} ≠ 約定代金 {tx.gross_amount:.0f}）"
            )
            tx.confidence -= PENALTY_CHECK_FAILED

    if tx.gross_amount is not None and tx.net_amount is not None:
        costs = (tx.fee or Decimal(0)) + (tx.tax or Decimal(0))
        gap = abs(abs(tx.net_amount) - tx.gross_amount)
        if abs(gap - costs) > ROW_TOLERANCE:
            tx.warnings.append(
                f"受渡金額と約定代金の差 {gap:.0f} が手数料・税額 {costs:.0f} と一致しません"
            )
            tx.confidence -= PENALTY_MINOR
