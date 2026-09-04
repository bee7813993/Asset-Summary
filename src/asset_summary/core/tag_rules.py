"""タグの自動配分ルール（銘柄名・証券コード・資産クラス → タグ配分の提案）。

Myポートフォリオのタグ配分を、コードに残るルール表として明文化する。
判定は3層の優先順位で行う:

1. 証券コード（最優先）… 唯一グローバルに一意・恒久で、証券会社の略称
   （「IFナス100H無」等）に左右されない。名前の表記が変わっても効く保険。
2. 銘柄名キーワード … 商品の中身（何に連動するか）を語る唯一の信号。
   fund_autolink.normalize() で正規化した名前への部分一致。
3. 資産クラス（最後の砦）… asset_class は「どの取引所に上場しているか」しか
   語らない。NASDAQ100連動ETF(2840)が国内上場というだけで国内株式に
   割り当てられた誤りは、この層を先に評価したことが原因。必ず最後に置き、
   fallback=True として UI で「クラス推定・要確認」と明示する。

設計判断（変更するときはテストの理由も更新すること）:
- 投信・年金（fund_jp / pension）にはフォールバックを置かない。未知の投信は
  未一致のまま残し、既存の未分類バナーで可視化する。静かに間違えるより、
  見えるところで放置される方が安全。
- stock_jp のフォールバックは個別株のために必要なので、代わりに名前が
  投信・ETFらしいときに警告フラグを立てる（_FUND_SHAPE_MARKERS）。
  2840型の誤り（国内上場の外国資産ETF→国内株式）は必ず警告として見える。
- キーワードは常に具体的に書く（「国内株」であって「国内」ではない）。
  裸の「リート」は禁止 — 「ステート・スト『リート』」に部分一致した実害あり。
  順序の必須制約は「ハイリスク層がゴールド層・世界株層より上」の2つだけで、
  それ以外は順序に依存しない（キーワードの具体性で担保している）。
- タグはユーザーが作る行で id が安定しないため、ルールは名前で参照し、
  適用のたびに名前→id を解決する。存在しないタグ名は missing として報告。
- fund_autolink.normalize() は変更しない（name_score → 投信自動連携の
  自動確定しきい値に影響するため）。U+2ED1 等の部首の畳み込みは
  本モジュールの norm_for_rules() でだけ行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from .fund_autolink import normalize as _normalize_name
from .store import Store

# CJK Radicals Supplement は NFKC で漢字に畳まれない（U+2ED1「⻑」等。
# Kangxi Radicals U+2F00〜 は214字すべて畳まれるのと対照的）。
# MF PDFの銘柄名に稀に混入するため、ルール判定用の正規化でだけ畳む。
# 実際に観測したのは 180A「GX超⻑期米国債」の U+2ED1。他は同型の予防。
_RADICAL_FOLD = str.maketrans({
    "⻑": "長",   # CJK RADICAL LONG ONE
    "⻒": "長",   # CJK RADICAL LONG TWO
    "⻄": "西",   # CJK RADICAL WEST TWO
    "⺌": "小",   # CJK RADICAL SMALL ONE
})


def norm_for_rules(name: str) -> str:
    """ルール照合用の正規化名（fund_autolink.normalize + 部首の畳み込み）。"""
    return _normalize_name(name).translate(_RADICAL_FOLD)


# ----------------------------------------------------------------------
# ルール定義
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    id: str                                  # 安定ID（テスト・API応答用）
    tags: tuple[tuple[str, Decimal], ...]    # ((タグ名, 配分%), ...) 合計100
    keywords: tuple[str, ...] = ()           # 正規化名への部分一致（OR）
    exclude: tuple[str, ...] = ()            # 一致しても除外する語（誤爆防止）
    asset_classes: tuple[str, ...] = ()      # AssetClass.value のいずれか
    fallback: bool = False                   # クラスからの推定（UIで要確認扱い）


@dataclass(frozen=True)
class MatchResult:
    rule: Rule
    matched_by: str      # "code" | "keyword" | "class" | "symbol"
    matched_value: str   # 一致したコード / 語 / asset_class 値 / コインのシンボル
    fund_shape_warning: bool = False  # フォールバックだが名前が投信・ETFらしい

    @property
    def reason(self) -> str:
        return f"{self.matched_by}:{self.matched_value}"


# タグ名（ルール表はこの定数だけを使う。typo は test_tag_rules が検出する）
WORLD = "世界株"
JP_EQUITY = "国内株式"
HIGH_RISK = "ハイリスク"
BOND = "債券"
GOLD = "ゴールド"
REIT = "リート"
CASH = "現金"
REAL_ESTATE = "不動産"
CRYPTO = "暗号資産"
STABLECOIN = "ステーブルコイン"

# RULES が参照してよいタグ名の一覧（テストで照合し、タグ名のtypoを検出する）
KNOWN_TAG_NAMES: frozenset[str] = frozenset({
    WORLD, JP_EQUITY, HIGH_RISK, BOND, GOLD, REIT,
    CASH, REAL_ESTATE, CRYPTO, STABLECOIN,
})


def _t(*pairs: tuple[str, str]) -> tuple[tuple[str, Decimal], ...]:
    return tuple((name, Decimal(weight)) for name, weight in pairs)


# 上から順に評価し、最初に一致した1件を採用する。
# 必須の順序制約は「ハイリスク層が ゴールド層・世界株層 より上」の2つだけ
# （例: Tracers…ゴールドプラス を ゴールド にしない / FANG+ を優先する）。
RULES: tuple[Rule, ...] = (
    # ---- ハイリスク（複合・レバレッジ・新興国。必ず先頭） ----
    Rule("high.fangplus", _t((HIGH_RISK, "100")), keywords=("fang+",)),
    Rule("high.goldplus", _t((HIGH_RISK, "100")), keywords=("ゴールドプラス",)),
    Rule("high.leverage", _t((HIGH_RISK, "100")),
         keywords=("レバレッジ", "レバナス", "倍ブル", "倍ベア", "ブル型", "ベア型",
                   "ブル2倍", "ブル3倍", "インバース")),
    Rule("high.mega10", _t((HIGH_RISK, "100")), keywords=("メガ10",)),
    # 新興国は「株」を要求する（新興国債券 を巻き込まない）。エマージング系の
    # 債券ファンドは exclude で弾き、下の債券ルールに落とす。
    Rule("high.emerging", _t((HIGH_RISK, "100")),
         keywords=("新興国株", "エマージング株", "エマージングマーケット", "エマージング市場"),
         exclude=("債券", "ボンド", "ソブリン", "国債")),
    Rule("high.india", _t((HIGH_RISK, "100")), keywords=("インド株",)),
    # ---- 債券（全世界債券・オールカントリー債券があるため世界株層より上） ----
    Rule("bond.generic", _t((BOND, "100")),
         keywords=("債券", "国債", "米国債", "外債", "外国債", "国内債", "新興国債",
                   "全世界債", "世界債", "先進国債", "社債", "ソブリン",
                   "ハイイールド", "ボンド", "bond")),
    Rule("bond.pimco", _t((BOND, "100")), keywords=("ピムコ", "pimco")),
    # ---- 貴金属 ----
    Rule("gold.platinum", _t((GOLD, "100")),
         keywords=("プラチナ", "純プラ", "パラジウム", "純銀", "シルバー"),
         exclude=("ポイント",)),
    Rule("gold.generic", _t((GOLD, "100")),
         keywords=("ゴールド", "純金", "金地金", "金現物", "gold"),
         # ゴールドマン・サックス系 / ゴールドポイント系 は金ではない
         exclude=("ゴールドマン", "goldman", "ポイント")),
    # ---- リート（裸の「リート」は禁止: 「スト『リート』」に誤爆する） ----
    Rule("reit.generic", _t((REIT, "100")),
         keywords=("reit", "jリート", "日本リート", "国内リート", "外国リート",
                   "先進国リート", "海外リート", "米国リート", "usリート")),
    # ---- 複合配分（インデックスの構成に合わせた按分） ----
    Rule("balanced.8assets",
         _t((WORLD, "25"), (JP_EQUITY, "12.5"), (BOND, "37.5"), (REIT, "25")),
         keywords=("8資産均等",)),
    Rule("world.3region", _t((WORLD, "66.6"), (JP_EQUITY, "33.4")),
         keywords=("3地域均等",)),
    # ---- 世界株（除く日本 は 全世界株 より上: 日本比率5%を付けない） ----
    Rule("world.exjapan", _t((WORLD, "100")),
         keywords=("除く日本", "exjapan", "kokusai", "コクサイ")),
    Rule("world.allcountry", _t((WORLD, "95"), (JP_EQUITY, "5")),
         keywords=("全世界株", "オルカン", "オールカントリー", "acwi")),
    Rule("world.nasdaq", _t((WORLD, "100")),
         keywords=("nasdaq", "ナスダック", "ナス100")),
    Rule("world.us", _t((WORLD, "100")),
         keywords=("米国株", "s&p500", "sp500", "全米株", "ダウ")),
    Rule("world.developed", _t((WORLD, "100")),
         keywords=("先進国株", "外国株", "グローバル株", "海外株")),
    # ---- 国内株 ----
    Rule("jp.equity", _t((JP_EQUITY, "100")),
         keywords=("topix", "トピックス", "日経", "国内株", "日本株", "jpx",
                   "グロース市場")),
    # ---- 暗号資産 ----
    Rule("crypto.stable", _t((STABLECOIN, "100")),
         keywords=("usdt", "usdc", "テザー", "ステーブル")),
    # ---- 資産クラスで確定するもの（キーワード不要） ----
    Rule("class.cash", _t((CASH, "100")), asset_classes=("cash",)),
    Rule("class.point", _t((CASH, "100")), asset_classes=("point",)),
    Rule("class.realestate", _t((REAL_ESTATE, "100")), asset_classes=("real_estate",)),
    Rule("class.crypto", _t((CRYPTO, "100")), asset_classes=("crypto",)),
    Rule("class.metal", _t((GOLD, "100")), asset_classes=("metal",)),
    # ---- フォールバック（クラス推定・要確認。fund_jp / pension には置かない） ----
    Rule("fb.stock_jp", _t((JP_EQUITY, "100")), asset_classes=("stock_jp",),
         fallback=True),
    Rule("fb.stock_foreign", _t((WORLD, "100")), asset_classes=("stock_foreign",),
         fallback=True),
)

# Crypto-Summary 由来のコインは、銘柄名も証券コードも持たずシンボルだけがある。
# ステーブルコインかどうかだけをシンボルの完全一致で判定し、それ以外は
# すべて「暗号資産」に入れる（分類はこの2つで足りる、という運用上の判断）。
#
# RULES のキーワード（部分一致）に足さないのは誤爆を防ぐため。"dai" や "usds"
# のような短い語は投信名にも現れ得るが、コインはシンボルが一意なので完全一致で
# 判定できる。判定先のルール自体は RULES にある既存の2行をそのまま使う。
STABLECOIN_SYMBOLS: frozenset[str] = frozenset({
    # 米ドル建て
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "PYUSD", "FDUSD", "GUSD",
    "USDD", "USDE", "USDS", "FRAX", "LUSD", "RLUSD", "SUSD", "CRVUSD", "USDB",
    # その他通貨建て
    "EURS", "EURT", "EURC", "JPYC", "XSGD", "XIDR", "BIDR",
})

CRYPTO_RULE_ID = "class.crypto"
STABLECOIN_RULE_ID = "crypto.stable"


def match_crypto_symbol(
    symbol: str, rules: Sequence[Rule] = RULES
) -> MatchResult | None:
    """コインのシンボル1件に適用するルールを決める（ステーブル or 暗号資産）。"""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    by_id = {r.id: r for r in rules}
    rule = by_id.get(
        STABLECOIN_RULE_ID if sym in STABLECOIN_SYMBOLS else CRYPTO_RULE_ID
    )
    if rule is None:   # カスタムルール列に無ければ未分類のまま
        return None
    return MatchResult(rule, "symbol", sym)


# 証券コードによる上書き（最優先）。配分の実体は RULES 側に一本化する。
# 証券会社の略称（「IFナス100H無」等）が表記変更でキーワードから外れても、
# フォールバック（国内株式）に落ちて2840の誤りが再発しないための保険。
# キーは importers.base.normalize_code と同じ ^[0-9][0-9A-Z]{3}$ 大文字形式。
CODE_RULES: dict[str, str] = {
    "2840": "world.nasdaq",    # IFナス100H無（NASDAQ100連動）
    "180A": "bond.generic",    # GX超⻑期米国債（名前の⻑はU+2ED1でNFKC不変）
    "2511": "bond.generic",    # NF外債ヘッジ無
    "447A": "gold.generic",    # SSゴールドヘッジ無
    "521A": "high.fangplus",   # IF FANG+ゴールド（ゴールドよりFANG+優先）
    "1541": "gold.platinum",   # 純プラチナ上場信託
    "1475": "jp.equity",       # iシェアーズ・コアTOPIX
}

# フォールバックで解決した銘柄の名前が投信・ETFらしいときの警告語彙。
# 個別株の社名に誤検知しないことを test_tag_rules で担保している。
_FUND_SHAPE_MARKERS = (
    "etf", "etn", "上場", "連動型", "指数", "インデックス",
    "iシェアーズ", "ishares", "tracers", "グローバルx",
    "nextfunds", "nextnotes", "ファンド", "spdr", "maxis",
    "投信", "信託",
)


# ----------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------

def match_rule(
    name: str,
    code: str | None,
    asset_class: str,
    rules: Sequence[Rule] = RULES,
) -> MatchResult | None:
    """銘柄1件に適用するルールを決める。一致なしは None（未分類のまま）。"""
    by_id = {r.id: r for r in rules}
    if code:
        rule_id = CODE_RULES.get(code.strip().upper())
        # カスタムルール列に該当IDが無いときはコード上書きを無効化して素通し
        if rule_id and rule_id in by_id:
            return MatchResult(by_id[rule_id], "code", code.strip().upper())
    n = norm_for_rules(name)
    for rule in rules:
        if rule.exclude and any(x in n for x in rule.exclude):
            continue
        hit = next((k for k in rule.keywords if k in n), None) if rule.keywords else None
        if rule.keywords and hit is None:
            continue
        if rule.asset_classes and asset_class not in rule.asset_classes:
            continue
        if hit is not None:
            return MatchResult(rule, "keyword", hit)
        warning = rule.fallback and any(m in n for m in _FUND_SHAPE_MARKERS)
        return MatchResult(rule, "class", asset_class, fund_shape_warning=warning)
    return None


def resolve_allocation(
    rule: Rule, tags: list[dict[str, Any]]
) -> tuple[dict[int, Decimal], list[str]]:
    """ルールのタグ名を現在のタグ行に解決する。

    タグ名→id は保存のたびに解決し直す（idはユーザー操作で変わり得るため）。
    戻り値: ({tag_id: 配分%}, 見つからなかったタグ名のリスト)
    """
    by_name = {t["name"]: t["id"] for t in tags}
    alloc: dict[int, Decimal] = {}
    missing: list[str] = []
    for tag_name, weight in rule.tags:
        tag_id = by_name.get(tag_name)
        if tag_id is None:
            missing.append(tag_name)
        else:
            alloc[tag_id] = weight
    return alloc, missing


def suggest_all(
    store: Store, external_assets: Sequence[tuple[str, str]] = ()
) -> list[dict[str, Any]]:
    """全銘柄の提案リスト（DBは変更しない）。

    external_assets: Crypto-Summary 由来のコイン [(asset_key, シンボル), ...]。
      銘柄行を持たないため、呼び出し側（web層）が生きた一覧を渡す。
    status: new（現配分が空） / change / unchanged / no-rule / missing-tag
    """
    tags = store.list_tags()
    tag_name = {t["id"]: t["name"] for t in tags}
    tag_order = {t["id"]: i for i, t in enumerate(tags)}
    current_map = store.security_tag_map()

    def _ser(alloc: dict[int, Decimal]) -> list[dict[str, Any]]:
        return [
            {"tag_id": tid, "name": tag_name.get(tid), "weight": str(w)}
            for tid, w in sorted(alloc.items(), key=lambda kv: tag_order.get(kv[0], 999))
        ]

    out: list[dict[str, Any]] = []
    for sec in store.list_securities():
        current = current_map.get(sec.id, {})
        m = match_rule(sec.name, sec.code, sec.asset_class.value)
        suggested: dict[int, Decimal] = {}
        missing: list[str] = []
        if m is None:
            status = "no-rule"
        else:
            suggested, missing = resolve_allocation(m.rule, tags)
            if missing:
                status = "missing-tag"
            elif not current:
                status = "new"
            elif current == suggested:   # Decimal同士の数値比較（100 == 100.0）
                status = "unchanged"
            else:
                status = "change"
        out.append({
            "security_id": sec.id,
            "name": sec.name,
            "code": sec.code,
            "asset_class": sec.asset_class.value,
            "status": status,
            "rule_id": m.rule.id if m else None,
            "matched_by": m.matched_by if m else None,
            "matched_value": m.matched_value if m else None,
            "reason": m.reason if m else None,
            "fallback": bool(m and m.rule.fallback),
            "fund_shape_warning": bool(m and m.fund_shape_warning),
            "current": _ser(current),
            "suggested": _ser(suggested),
            "missing_tags": missing,
        })

    # Crypto-Summary 由来のコイン。銘柄行が無いのでシンボルだけで判定する。
    external_current = store.external_tag_map()
    for asset_key, symbol in external_assets:
        current = external_current.get(asset_key, {})
        m = match_crypto_symbol(symbol)
        suggested = {}
        missing = []
        if m is None:
            status = "no-rule"
        else:
            suggested, missing = resolve_allocation(m.rule, tags)
            if missing:
                status = "missing-tag"
            elif not current:
                status = "new"
            elif current == suggested:
                status = "unchanged"
            else:
                status = "change"
        out.append({
            "security_id": asset_key,
            "name": symbol,
            "code": None,
            "asset_class": "crypto",
            "external": True,
            "status": status,
            "rule_id": m.rule.id if m else None,
            "matched_by": m.matched_by if m else None,
            "matched_value": m.matched_value if m else None,
            "reason": m.reason if m else None,
            "fallback": False,
            "fund_shape_warning": False,
            "current": _ser(current),
            "suggested": _ser(suggested),
            "missing_tags": missing,
        })
    return out
