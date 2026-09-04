"""Domain models for Asset Summary.

Conventions shared with Crypto-Summary: all monetary/quantity values are
Decimal, persisted as TEXT in SQLite, converted at the edges.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AssetClass(str, Enum):
    CASH = "cash"
    STOCK_JP = "stock_jp"
    STOCK_FOREIGN = "stock_foreign"
    FUND_JP = "fund_jp"
    FUND_FOREIGN = "fund_foreign"
    BOND = "bond"
    METAL = "metal"
    REAL_ESTATE = "real_estate"
    CRYPTO = "crypto"
    PENSION = "pension"
    POINT = "point"
    OTHER = "other"


# UI表示用ラベルと固定色（クラス別ドーナツ・バッジで使用）
ASSET_CLASS_META: dict[str, dict[str, str]] = {
    "cash": {"ja": "預金・現金", "en": "Cash", "color": "#2f81f7"},
    "stock_jp": {"ja": "国内株式", "en": "JP Stocks", "color": "#3fb950"},
    "stock_foreign": {"ja": "外国株式", "en": "Foreign Stocks", "color": "#39c5cf"},
    "fund_jp": {"ja": "投資信託", "en": "JP Funds", "color": "#a371f7"},
    "fund_foreign": {"ja": "海外ファンド", "en": "Foreign Funds", "color": "#8957e5"},
    "bond": {"ja": "債券", "en": "Bonds", "color": "#6cb6ff"},
    "metal": {"ja": "貴金属", "en": "Metals", "color": "#e3b341"},
    "real_estate": {"ja": "不動産", "en": "Real Estate", "color": "#f0883e"},
    "crypto": {"ja": "暗号資産", "en": "Crypto", "color": "#f7931a"},
    "pension": {"ja": "年金", "en": "Pension", "color": "#db61a2"},
    "point": {"ja": "ポイント", "en": "Points", "color": "#8b949e"},
    "other": {"ja": "その他", "en": "Other", "color": "#6e7681"},
}


class Unit(str, Enum):
    SHARE = "share"        # 株
    KUCHI = "kuchi"        # 口（投信）
    GRAM = "gram"          # g（現物貴金属）
    CURRENCY = "currency"  # 通貨残高
    UNIT = "unit"          # 件（不動産・年金プラン等、quantity=1）
    POINT = "point"        # ポイント/マイル


class PriceSourceType(str, Enum):
    NONE = "none"
    YAHOO = "yahoo"
    TOUSHIN = "toushin"
    METAL = "metal"
    FX = "fx"
    COINGECKO = "coingecko"
    MANUAL = "manual"


class PriceSourceStatus(str, Enum):
    UNLINKED = "unlinked"          # 取込直後・価格ソース未設定
    LINKED = "linked"
    MANUAL = "manual"              # 手動評価のみ（不動産等）
    NOT_REQUIRED = "not_required"  # 現金・ポイント・年金


class Account(BaseModel):
    id: int | None = None
    name: str
    display_name: str | None = None
    kind: str = "other"      # bank | broker | pension | point | manual | other
    origin: str = "manual"   # mf | manual
    sort_order: int = 0


class Security(BaseModel):
    id: int | None = None
    code: str | None = None          # 正規化済み証券コード（4文字英数）。投信・現金等は None
    name: str
    name_key: str                    # 照合キー（importers.base.make_name_key）
    asset_class: AssetClass
    currency: str = "JPY"            # 価格・平均取得単価の建値通貨
    unit: Unit = Unit.SHARE
    price_unit_divisor: int = 1      # 投信は 10000（基準価額は1万口あたり）
    price_source_type: PriceSourceType = PriceSourceType.NONE
    price_source_ref: str | None = None   # '9433.T' / 'JP90C000GKC6:03311187' / 'XAU' 等
    price_source_status: PriceSourceStatus = PriceSourceStatus.UNLINKED
    inactive: bool = False


class HoldingSnapshot(BaseModel):
    """ある日時点の1保有ロット。全資産クラス共通の中核レコード。"""

    id: int | None = None
    account_id: int
    security_id: int
    lot_seq: int = 0                 # 同一(口座,銘柄)内の複数ロット（NISA/特定等）
    as_of_date: date
    quantity: Decimal                # '0' = 売却/解約済み
    avg_cost: Decimal | None = None  # 平均取得単価（建値通貨・divisor適用前）。年金は取得価額の総額
    reported_price: Decimal | None = None      # 取込時の現在値/基準価額
    reported_value_jpy: Decimal | None = None  # ソース記載の評価額（円）— 価格未リンク時のフォールバック
    reported_pl_jpy: Decimal | None = None     # ソース記載の評価損益（検算用）
    lot_label: str | None = None     # 預金の商品名 / 'NISA' 等（ユーザー編集可）
    origin: str = "manual"           # mf | manual
    batch_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ImportBatch(BaseModel):
    id: str
    source_kind: str = "mf_pdf"      # mf_pdf | manual | (将来: screenshot | csv)
    filename: str | None = None
    file_sha256: str | None = None
    as_of_date: date | None = None
    status: str = "previewed"        # previewed | committed | discarded
    parse_report: dict[str, Any] = Field(default_factory=dict)


SUPPORTED_CURRENCIES = ("JPY", "USD", "EUR", "GBP")


# ----------------------------------------------------------------------
# 取引履歴（証券会社CSV等）
#
# アプリの中核はあくまでスナップショット方式（DESIGN.md）。取引台帳は
# 「スナップショットをどう説明するか」を持つ補助であって、正ではない。
# 総量はMF PDFのスナップショットを錨とし、台帳で説明できない分を期首ロット
# として逆算する（core/cost_basis.py）。
# ----------------------------------------------------------------------


class TxType(str, Enum):
    BUY = "buy"                              # 買付・購入・積立
    SELL = "sell"                            # 売却・解約・償還
    DIVIDEND = "dividend"                    # 配当金・普通分配金（数量も原価も動かない）
    REINVEST = "reinvest"                    # 分配金再投資（買付として数量が増える）
    RETURN_OF_CAPITAL = "return_of_capital"  # 特別分配金・元本払戻金（原価だけ減る）
    SPLIT = "split"                          # 株式分割・併合
    TRANSFER_IN = "transfer_in"              # 入庫・移管受入（証券が増える。現金の動きなし）
    TRANSFER_OUT = "transfer_out"            # 出庫・移管払出（証券が減る）
    # 現金の移動（振替入金・出金など）。証券の入出庫とは別物で、保有数にも
    # 取得原価にも一切関係しない。銀行への資金移動や税の徴収がここに来る。
    # 証券の入出庫と同じ扱いにすると、口座の入出金で保有数が減ってしまう。
    CASH_IN = "cash_in"
    CASH_OUT = "cash_out"
    OTHER = "other"                          # 判別できなかった行（原文を保持）


# 現金の出入りだけで、保有には関係しない種別
CASH_ONLY_TX = (TxType.CASH_IN, TxType.CASH_OUT)


# 数量・取得原価を動かす種別（再生の対象）。DIVIDEND はインカムのみで対象外。
POSITION_MOVING_TX = (
    TxType.BUY,
    TxType.SELL,
    TxType.REINVEST,
    TxType.RETURN_OF_CAPITAL,
    TxType.SPLIT,
    TxType.TRANSFER_IN,
    TxType.TRANSFER_OUT,
)


class Coverage(str, Enum):
    """取引履歴がその保有をどこまで説明できているか。"""

    FULL = "full"                          # 期首ロットが残っていない＝CSV由来の単価が正
    PARTIAL = "partial"                    # CSVより前から保有。MFの単価を錨に残余を逆算
    PARTIAL_UNCOSTED = "partial_uncosted"  # 残余があるがMFに取得単価が無く逆算できない
    UNRECONCILED = "unreconciled"          # 数量が合わない等。原価は書かない


class Transaction(BaseModel):
    """1取引。数量は符号つき（買い正・売り負）、金額は建値通貨。"""

    id: int | None = None
    dedup_key: str | None = None
    account_id: int
    security_id: int | None = None   # None = 未照合（プレビューで対応付ける）
    trade_date: date                 # 約定日
    settle_date: date | None = None  # 受渡日
    tx_type: TxType = TxType.OTHER
    quantity: Decimal | None = None      # 符号つき増減。配当行は None
    unit_price: Decimal | None = None    # 約定単価（price_unit_divisor 適用前）
    gross_amount: Decimal | None = None  # 約定代金（絶対値）
    fee: Decimal | None = None           # 手数料（正）
    tax: Decimal | None = None           # 源泉徴収税額（正）。実現損益からは引かない
    net_amount: Decimal | None = None    # 受渡金額（符号つき: 買− / 売+ / 配当+）
    split_ratio: Decimal | None = None   # 分割・併合比率（新÷旧）。数量差分形式なら None
    currency: str = "JPY"
    lot_label: str | None = None         # 特定 / 一般 / NISA 等
    note: str | None = None
    origin: str = "broker_csv"           # broker_csv | manual
    broker_ref: str | None = None        # 約定番号・注文番号（あれば同一性の決め手）
    batch_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CostBasisOverride(BaseModel):
    """取引台帳から再計算した取得原価。transactions から常に再生成できる派生値。"""

    account_id: int
    security_id: int
    lot_seq: int = 0
    as_of_date: date                      # 突き合わせたスナップショットの基準日
    coverage: Coverage = Coverage.UNRECONCILED
    avg_cost: Decimal | None = None       # 再計算した取得単価（divisor 適用前）
    acquired_on: date | None = None       # 判明している最古の取得日
    acquired_on_src: str | None = None    # csv | mf_raw
    covered_quantity: Decimal | None = None
    residual_quantity: Decimal | None = None   # 「取得日不明」の期首数量
    residual_avg_cost: Decimal | None = None
    realized_pl: Decimal | None = None    # 期間内の実現損益（税引前）
    income_total: Decimal | None = None   # 配当・分配金の合計
    withheld_tax: Decimal | None = None
    lot_scope: str = "lot"                # lot | group（複数ロットを合算して扱った）
    tx_count: int = 0
    batch_id: str | None = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def applies_to_pl(self) -> bool:
        """損益計算に反映してよいか。

        完全被覆のときだけ。部分被覆では残余原価をMFの平均取得単価から逆算する以上、
        再計算値はMFと数学的に一致する（循環）ので、上書きする意味が無い。
        """
        return self.coverage == Coverage.FULL and self.avg_cost is not None
