"""列見出しと取引区分の語彙表（純粋なデータ + 正規化）。

ここは「よくある書き方」を集めた辞書に過ぎず、判定の当たり外れをここだけに
背負わせない。ヘッダが無い/語彙に無い書式は、値の形状（shapes.py）と既存銘柄
との一致、そして算術検算（columns.py）で決める。
"""

from __future__ import annotations

import re

from ...core.models import TxType
from ..base import nfkc
from .contracts import CanonicalField as F

# ヘッダ照合用の正規化: NFKC → 小文字 → 記号と空白を除去 → 末尾の単位を除去
_LABEL_STRIP_RE = re.compile(r"[\s　・･()（）\[\]【】｛｝{}/／\\,、.。:：;；_\-―ー–—]+")
_TRAILING_UNIT_RE = re.compile(r"(円|株|口|％|%|グラム|g)$")


def normalize_label(text: str) -> str:
    s = nfkc(str(text or "")).strip().lower()
    s = _LABEL_STRIP_RE.sub("", s)
    s = _TRAILING_UNIT_RE.sub("", s)
    return s


# 完全一致で 1.0、部分一致で 0.7。
# 同じ語が複数フィールドに現れてよい（例: 摘要 は TX_TYPE と NOTE の両方）。
# 競合は値の形状と既知データで解く。
HEADER_SYNONYMS: dict[F, tuple[str, ...]] = {
    F.TRADE_DATE: (
        "約定日", "約定日付", "約定年月日", "取引日", "取引年月日", "売買日", "注文日",
        "買付日", "売却日", "申込日", "基準日", "日付", "年月日", "日",
        "tradedate", "date", "transactiondate", "executiondate",
    ),
    F.SETTLE_DATE: (
        "受渡日", "受渡年月日", "受渡", "受渡日付", "決済日", "精算日", "入出金日",
        "settlementdate", "settledate", "valuedate",
    ),
    F.SECURITY_NAME: (
        "銘柄", "銘柄名", "銘柄名称", "ファンド名", "投信名", "商品名",
        "名称", "対象銘柄", "銘柄コード銘柄名", "ファンド", "証券名",
        "name", "security", "securityname", "fundname", "description",
    ),
    F.SECURITY_CODE: (
        "銘柄コード", "証券コード", "コード", "銘柄cd", "ティッカー", "シンボル",
        "ファンドコード", "協会コード", "isin", "isinコード",
        "code", "ticker", "symbol", "securitycode",
    ),
    F.TX_TYPE: (
        "取引区分", "取引種別", "取引種類", "取引内容", "取引明細", "取引", "売買区分",
        "売買種別", "売買", "区分", "種別", "種類", "摘要", "明細", "取引名",
        "side", "type", "transactiontype", "action",
    ),
    F.QUANTITY: (
        "数量", "約定数量", "受渡数量", "注文数量", "株数", "口数", "増減数量",
        "保有数増減", "数量株口", "約定株数", "約定口数", "取引数量",
        "quantity", "qty", "shares", "units", "amount単位",
    ),
    F.UNIT_PRICE: (
        "単価", "約定単価", "約定価格", "約定値段", "値段", "価格", "取得単価",
        "基準価額", "約定基準価額", "売買単価", "取引単価",
        "price", "unitprice", "nav", "executionprice",
    ),
    F.GROSS_AMOUNT: (
        "約定代金", "売買代金", "約定金額", "取引金額", "取引額", "買付金額",
        "売却金額", "代金", "総額", "約定額", "受渡代金前",
        "grossamount", "amount", "tradeamount", "principal",
    ),
    F.NET_AMOUNT: (
        "受渡金額", "受渡代金", "精算金額", "決済金額", "差引金額", "入出金額",
        "受取金額", "支払金額", "手取金額", "お預り金増減", "精算額",
        "netamount", "settlementamount", "netproceeds", "proceeds",
    ),
    F.FEE: (
        "手数料", "委託手数料", "売買手数料", "購入時手数料", "手数料等", "諸経費",
        "費用", "信託財産留保額", "手数料税込", "手数料消費税", "消費税",
        "commission", "fee", "fees", "charge",
    ),
    F.TAX: (
        "税額", "税金", "源泉徴収税額", "源泉税", "所得税", "住民税", "税",
        "地方税", "国税",
        "tax", "withholdingtax", "taxes",
    ),
    F.ACCOUNT: (
        "口座", "取引口座", "口座名", "金融機関", "証券会社", "取扱会社", "部店",
        "支店", "販売会社",
        "account", "broker", "institution",
    ),
    F.ACCOUNT_TYPE: (
        "口座区分", "口座種別", "預り区分", "預り", "課税区分", "nisa区分", "分別",
        "口座種類", "預区分",
        "accounttype",
    ),
    F.CURRENCY: (
        "通貨", "通貨コード", "決済通貨", "建値通貨", "取引通貨",
        "currency", "ccy",
    ),
    F.EXCHANGE_RATE: (
        "為替レート", "約定為替レート", "適用為替レート", "為替", "ttm", "適用レート",
        "exchangerate", "fxrate", "rate",
    ),
    F.NOTE: (
        "備考", "メモ", "摘要", "注記", "コメント",
        "remarks", "note", "notes", "memo",
    ),
}

# 正規化済みラベル → [(field, weight)] の逆引き。部分一致は照合時に別途見る。
_EXACT: dict[str, list[tuple[F, float]]] = {}
for _field, _words in HEADER_SYNONYMS.items():
    for _w in _words:
        _EXACT.setdefault(normalize_label(_w), []).append((_field, 1.0))


def header_scores(label: str) -> dict[F, float]:
    """ヘッダ語 1 つに対する各フィールドのスコア（0-1）。"""
    key = normalize_label(label)
    if not key:
        return {}
    out: dict[F, float] = {}
    for f, w in _EXACT.get(key, ()):
        out[f] = max(out.get(f, 0.0), w)
    if out:
        return out
    # 部分一致。安全なのは「見出しが同義語を含む」向き（'約定日時' ⊃ '約定日'）。
    # 逆向き（同義語が見出しを含む）は緩すぎる — データ値の '買付' が同義語
    # '買付日' に含まれてしまい、取引区分の値が並ぶ列をヘッダ行と誤認する。
    # 逆向きは 3 文字以上の見出しにだけ許す。
    for f, words in HEADER_SYNONYMS.items():
        best = 0.0
        for w in words:
            nw = normalize_label(w)
            if len(nw) < 2:
                continue
            if nw in key:
                best = max(best, 0.7 if len(nw) >= 3 else 0.55)
            elif len(key) >= 3 and key in nw:
                best = max(best, 0.55)
        if best:
            out[f] = max(out.get(f, 0.0), best)
    return out


# ----------------------------------------------------------------------
# 取引区分
# ----------------------------------------------------------------------

# 優先順に評価する。単純な先頭一致では '分割売却' が SPLIT に、
# '分配金再投資' が DIVIDEND になってしまうため、順序と共起で解く。
_REINVEST = ("再投資", "分配金再投資", "収益分配金再投資", "再投資買付", "累投",
             "自動けいぞく投資", "自動継続投資", "reinvest")
_ROC = ("特別分配金", "元本払戻金", "元本払戻", "returnofcapital")
_SPLIT = ("分割", "株式分割", "併合", "株式併合", "無償割当", "口数変更", "split")
_DIVIDEND = ("配当金", "配当", "分配金", "収益分配金", "普通分配金", "期末分配金",
             "利金", "利息", "dividend", "distribution", "coupon")
# 「買取」は証券会社が買い取る側の呼び方で、利用者から見れば売却
# （投信の買取請求）。_BUY の単独 "買" に先に食われないよう _SELL に置く。
# 実データでは数十行あり、買付にすると保有数も取得原価も壊れる
# （買付計と買取計が同数で残 0 になるはずの銘柄が、倍の保有に化ける）。
# 「換金」は投信の解約請求の言い方（SBI・楽天がこう書く）。ただし
# 「ポイント換金」はポイントを現金に替えた入金なので、先に別扱いする。
_SELL = ("売却", "売付", "売り", "解約", "解約請求", "買取請求", "買取", "償還",
         "満期償還", "換金請求", "換金", "売", "sell", "sold", "redemption")
# 「募集」は新規設定ファンドの当初申込（マネックスは 募集/公募 と書く）。
# 約定して数量が増えるので買付。語彙に無いと、その銘柄の唯一の行が未判別に
# なり、増減も数量照合も価格照合も全部働かなくなる。
_BUY = ("買付", "買い", "購入", "積立買付", "積立", "つみたて", "スポット購入",
        "金額買付", "口数買付", "募集", "公募", "当初申込", "買", "buy",
        "bought", "purchase", "ipo", "subscription")
# 証券そのものの移動（数量が動く。現金は動かない）
_TRANSFER_IN = ("入庫", "移管受入", "受入", "預入", "預り入", "振替入庫", "transferin")
_TRANSFER_OUT = ("出庫", "移管払出", "払出", "預出", "振替出庫", "transferout")

# 現金の移動（保有数にも取得原価にも関係しない）。銀行への資金移動や税の徴収。
# 「振替入庫」と「振替入金」は庫/金の一字しか違わないので、証券側を先に判定する。
_CASH_IN = ("振替入金", "自動振替入金", "入金", "預り金入金", "受取", "返金", "還付")
_CASH_OUT = ("振替出金", "自動振替出金", "出金", "払戻", "支払", "徴収")
# 向きの語が無い「振替」。行の金額の符号で向きを決める（classify.py）。
_CASH_AMBIGUOUS = ("振替", "振替金", "資金移動", "transfer")

# 信用取引。現物の売買と混ぜると保有数が壊れるので、売買より先に弾く。
_MARGIN = ("信用", "新規買", "新規売", "返済買", "返済売", "現引", "現渡",
           "建玉", "品受", "品渡")

# 数量の符号で売買を表す書式のための、増減を示す語
INFLOW_TOKENS = ("入", "受", "+", "＋", "in")
OUTFLOW_TOKENS = ("出", "払", "-", "−", "－", "out")


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def is_margin_trade(raw: str) -> bool:
    """信用取引か（新規建・返済・現引・現渡）。

    現物と同じ売買として扱うと保有数が壊れる。『半年新規買い』を買付にすると
    持っていない株を持っていることになり、『返済売り』で売れば二重に狂う。
    対象外にして人に判断させる。
    """
    return _has(normalize_label(raw), _MARGIN)


# normalize_label は括弧「文字」だけを消して中身を本文に残すため、補足が
# 本文に合流してしまう（ご入金（カード積立）→ ご入金カド積立）。ここでは
# 正規化の前に、括弧を中身ごと外す。
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")


def classify_tx_type(raw: str) -> tuple[TxType, float]:
    """取引区分の文字列 → (種別, 確信度)。判別できなければ (OTHER, 0.0)。

    共起を見るので '分割売却' は SELL、'分配金再投資' は REINVEST になる。

    日本の取引ラベルは **動作が先頭、括弧内は補足** — 『ご入金（カード積立）』は
    カード積立のための入金であって買付ではない。括弧を含めて全体を見ると
    補足の「積立」が買付に化けるので、まず括弧の中身を外して分類し、
    それで決まらないときだけ全体で分類する（『投信（買付）』のように
    動作が括弧の側にある書式のため）。
    """
    text = normalize_label(raw)
    if not text:
        return (TxType.OTHER, 0.0)

    head = normalize_label(_PAREN_RE.sub("", str(raw or "")))
    if head and head != text:
        kind, conf = _classify_text(head)
        if kind is not TxType.OTHER:
            return (kind, conf)
    return _classify_text(text)


def _classify_text(text: str) -> tuple[TxType, float]:

    buy_or_sell = _has(text, _BUY) or _has(text, _SELL)

    # 現渡・現引は信用取引の返済だが、他の信用と違って **現物を動かす**。
    # 現渡は持っている現物を渡して返済（＝処分）、現引は代金を払って現物を
    # 受け取る（＝取得）。対象外にすると、現物の増減がファイル上で合わなくなる
    # （実データでは、現物 +400/-200 に現渡 -200 で ±0 が完結する銘柄や、
    # 現引 +100 株がそのまま保有中の取得記録になっている銘柄があった）。
    # 単価は建単価であって当日の時価ではないため、価格照合の証拠には使わない
    # （classify.row_to_tx が off_market_price の印を付ける）。
    if _has(text, ("現渡", "品渡")):
        return (TxType.SELL, 1.0)
    if _has(text, ("現引", "品受")):
        return (TxType.BUY, 1.0)

    # 信用取引は現物の売買ではないので、売買より先に弾く。ただし弾くのは
    # 建玉を動かす行だけ。「振替（信用保証金へ）」は保証金の資金移動で、
    # 銘柄も数量も無い純粋な入出金。信用の字だけで弾くと「その他・銘柄未確定」
    # になり、利用者がどう対処すべきか分からない行になる（実データで数十行）。
    if _has(text, _MARGIN) and (buy_or_sell or _has(text, ("建",))):
        return (TxType.OTHER, 0.0)
    moved = _has(text, _TRANSFER_IN) or _has(text, _TRANSFER_OUT)

    # 「累投」「再投資」は口座の性格を表す語で、行の動作ではないことがある。
    # 『国内累投ＮＩＳＡ払出（特定へ）』は NISA から特定への移管で、同じ数量の
    # 『入庫』と対になっている。再投資（＝買付）と読むと、対の入庫と
    # あわせて保有が 2 倍に増える（実データでも移管ぶんが二重計上されていた）。
    # 動作を表す語がある側を採る — SPLIT や DIVIDEND を売買語で打ち消すのと同じ考え方。
    if _has(text, _REINVEST) and not moved:
        return (TxType.REINVEST, 1.0)
    if _has(text, _ROC):
        return (TxType.RETURN_OF_CAPITAL, 1.0)
    if _has(text, _SPLIT) and not buy_or_sell:
        return (TxType.SPLIT, 1.0)
    if _has(text, _DIVIDEND) and not buy_or_sell:
        return (TxType.DIVIDEND, 1.0)
    # ポイント・マイルの換金は証券の売却ではなく現金の受け取り
    if _has(text, ("ポイント換金", "ポイント交換", "マイル換金")):
        return (TxType.CASH_IN, 0.9)
    if _has(text, _SELL):
        return (TxType.SELL, 1.0)
    if _has(text, _BUY):
        return (TxType.BUY, 1.0)
    # 証券の入出庫が先。「振替入庫」を現金の「振替入金」より先に拾うため。
    if _has(text, _TRANSFER_IN):
        return (TxType.TRANSFER_IN, 0.8)
    if _has(text, _TRANSFER_OUT):
        return (TxType.TRANSFER_OUT, 0.8)
    # ここから現金の移動。保有には関係しないので取込対象から外す。
    if _has(text, _CASH_OUT):
        return (TxType.CASH_OUT, 0.9)
    if _has(text, _CASH_IN):
        return (TxType.CASH_IN, 0.9)
    if _has(text, _CASH_AMBIGUOUS):
        # 向きは金額の符号で決める。既定は入金側にしておく。
        return (TxType.CASH_IN, 0.5)
    return (TxType.OTHER, 0.0)


def is_cash_movement(kind: TxType) -> bool:
    return kind in (TxType.CASH_IN, TxType.CASH_OUT)


# 償還・解約は経済的には売却。原文を残したいので種別とは別に印を返す。
SELL_KIND_TOKENS = {"償還": "償還", "満期償還": "償還", "解約": "解約",
                    "現渡": "現渡", "品渡": "現渡",
                    "買取請求": "買取請求", "買取": "買取請求"}
DIVIDEND_KIND_TOKENS = {"特別分配金": "特別分配金", "元本払戻金": "元本払戻金"}


def sell_kind(raw: str) -> str | None:
    text = normalize_label(raw)
    for token, label in SELL_KIND_TOKENS.items():
        if normalize_label(token) in text:
            return label
    return None


def looks_like_tx_type(value: str) -> bool:
    """列がまるごと取引区分らしいかの判定に使う（1セル分）。"""
    return classify_tx_type(value)[0] is not TxType.OTHER


# ----------------------------------------------------------------------
# 口座区分・通貨
# ----------------------------------------------------------------------

ACCOUNT_TYPE_TOKENS = (
    "特定", "特定口座", "一般", "一般口座", "nisa", "ＮＩＳＡ", "つみたてnisa",
    "つみたて投資枠", "成長投資枠", "旧nisa", "ジュニアnisa", "非課税", "源泉あり",
    "源泉なし", "特定源泉あり", "特定源泉なし",
)

CURRENCY_TOKENS = (
    "jpy", "usd", "eur", "gbp", "aud", "nzd", "cad", "chf", "hkd", "cny",
    "円", "米ドル", "ドル", "ユーロ", "ポンド", "豪ドル",
)

CURRENCY_ALIASES = {
    "円": "JPY", "日本円": "JPY", "米ドル": "USD", "ドル": "USD",
    "ユーロ": "EUR", "ポンド": "GBP", "豪ドル": "AUD",
}


def looks_like_account_type(value: str) -> bool:
    text = normalize_label(value)
    return bool(text) and any(t in text for t in (normalize_label(x) for x in ACCOUNT_TYPE_TOKENS))


def _normalize_value(value: str) -> str:
    """セルの値の正規化。見出し用の normalize_label は使わないこと。

    normalize_label は末尾の単位（円・株・口）を落とすので、'円' という値が
    空文字になり、通貨列がまるごと通貨と認識されなくなる。
    """
    return nfkc(str(value or "")).strip().lower()


def looks_like_currency(value: str) -> bool:
    text = _normalize_value(value)
    return bool(text) and text in {_normalize_value(x) for x in CURRENCY_TOKENS}


def normalize_currency(value: str) -> str | None:
    raw = nfkc(str(value or "")).strip()
    if not raw:
        return None
    if raw in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[raw]
    upper = raw.upper()
    if len(upper) == 3 and upper.isalpha():
        return upper
    return None


# 値と単位を別の列に分ける書式（大和証券など）:
#   数量 | 数量（単位） | 単価 | 単価（単位） | 精算金額 | 精算金額（単位）
# 単位列は中身が通貨なら通貨列として使い、そうでなければ（株・口）捨てる。
# 拾わないと外貨建ての取引が黙って円扱いになる。
_UNIT_SUFFIX_RE = re.compile(r"[（(]\s*単位\s*[)）]\s*$")


def is_unit_column_header(label: str) -> bool:
    return bool(_UNIT_SUFFIX_RE.search(nfkc(str(label or "")).strip()))


def unit_column_base(label: str) -> str:
    """'精算金額（単位）' → '精算金額'。"""
    return _UNIT_SUFFIX_RE.sub("", nfkc(str(label or "")).strip())


# ----------------------------------------------------------------------
# 合計行・前文行の目印
# ----------------------------------------------------------------------

TOTAL_ROW_MARKERS = ("合計", "総計", "小計", "計", "total", "sum", "累計")
PREAMBLE_MARKERS = (
    "口座番号", "お客様", "出力日", "作成日", "検索条件", "期間", "支店",
    "以下", "単位", "照会", "ダウンロード",
)


def is_total_row_label(value: str) -> bool:
    text = normalize_label(value)
    if not text:
        return False
    return any(normalize_label(m) == text or text.startswith(normalize_label(m))
               for m in TOTAL_ROW_MARKERS)
