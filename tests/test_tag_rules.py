"""自動配分ルール（tag_rules）のテスト。

銘柄名は一般に流通する公募投信・ETFの商品名・著名企業の社名（いずれも
公開情報）または架空名のみ。保有実態・数量・評価額・口座情報は含めない
（DESIGN.md「個人情報防護」の方針。保有に固有の行は架空名に一般化済み）。

表は「配分」だけでなく「どのルールが・どの層で」当たったかも固定する。
値だけのテストでは、キーワード変更で 2840 が code→class 経由に落ちても
偶然同じ配分になって通ってしまう — 判定経路の固定がそれを検出する。
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal

import pytest

from asset_summary.core import tag_rules
from asset_summary.core.tag_rules import (
    CODE_RULES,
    KNOWN_TAG_NAMES,
    RULES,
    Rule,
    match_rule,
    norm_for_rules,
)

D = Decimal


def _resolve(asset_class: str, code: str | None, name: str):
    """(配分{タグ名:Decimal}|None, rule_id|None, matched_by|None, fallback, 警告)"""
    m = match_rule(name, code, asset_class)
    if m is None:
        return None, None, None, False, False
    return (
        dict(m.rule.tags),
        m.rule.id,
        m.matched_by,
        m.rule.fallback,
        m.fund_shape_warning,
    )


def _alloc(d: dict[str, str] | None) -> dict[str, Decimal] | None:
    return None if d is None else {k: D(v) for k, v in d.items()}


# ----------------------------------------------------------------------
# 判定表。行 = (asset_class, code, 銘柄名, 期待配分, 期待rule_id, 期待matched_by)
# 期待配分 None = ルールなし（未分類のまま残り、未分類バナーで可視化される）
# ----------------------------------------------------------------------

W100 = {"世界株": "100"}
JP100 = {"国内株式": "100"}
HI100 = {"ハイリスク": "100"}
BD100 = {"債券": "100"}
GD100 = {"ゴールド": "100"}
RT100 = {"リート": "100"}
CA100 = {"現金": "100"}
ALLC = {"世界株": "95", "国内株式": "5"}

# --- 保有商品相当の回帰表（個別株・ポイント等の固有行は架空名に一般化） ---
HELD_CASES = [
    ("cash", None, "現金・預金", CA100, "class.cash", "class"),
    ("metal", None, "金（現物）", GD100, "gold.generic", "keyword"),
    ("metal", None, "銀インゴット", GD100, "class.metal", "class"),
    ("real_estate", None, "自宅マンション", {"不動産": "100"}, "class.realestate", "class"),
    ("point", None, "架空ポイント", CA100, "class.point", "class"),
    ("point", None, "架空マイレージ(マイル)", CA100, "class.point", "class"),
    # 投信（公募投信の商品名パターン）
    ("fund_jp", None, "SBIiシェアーズ全世界債券インデックス(8108)", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "eMAXIS Slim全世界株オール(8782)", ALLC, "world.allcountry", "keyword"),
    ("fund_jp", None, "ピクテ・ゴールド(為替ヘッジなし) (9073)", GD100, "gold.generic", "keyword"),
    ("fund_jp", None, "ピムコインH無年2", BD100, "bond.pimco", "keyword"),
    ("fund_jp", None, "eMAXIS Slim国内株式(TOPIX)", JP100, "jp.equity", "keyword"),
    ("fund_jp", None, "eMAXIS Slimバランス(8資産均等型)",
     {"世界株": "25", "国内株式": "12.5", "債券": "37.5", "リート": "25"},
     "balanced.8assets", "keyword"),
    ("fund_jp", None, "SBI・iシェアーズ・全世界債券インデックス・ファンド", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・TOPIX", JP100, "jp.equity", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・国内債券", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・J-REIT", RT100, "reit.generic", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・外国株式", W100, "world.developed", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・外国債券", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・外国REIT", RT100, "reit.generic", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・新興国株式", HI100, "high.emerging", "keyword"),
    ("fund_jp", None, "野村インデックスファンド・新興国債券", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "iTrustインド株式", HI100, "high.india", "keyword"),
    ("fund_jp", None, "iFreeNEXT FANG+インデックス", HI100, "high.fangplus", "keyword"),
    ("fund_jp", None, "eMAXIS Slim米国株式(S&P500)", W100, "world.us", "keyword"),
    ("fund_jp", None, "iFreeNEXT NASDAQ100インデックス", W100, "world.nasdaq", "keyword"),
    ("fund_jp", None, "eMAXIS Slim全世界株式(オール・カントリー)", ALLC, "world.allcountry", "keyword"),
    ("fund_jp", None, "ニッセイNASDAQ100インデックスファンド<購入・換金手数料なし>", W100, "world.nasdaq", "keyword"),
    ("fund_jp", None, "Smart-iゴールドファンド(為替ヘッジなし)", GD100, "gold.generic", "keyword"),
    ("fund_jp", None, "ニッセイ・S米国グロース株式メガ10インデックスファンド<購入・換金手数料なし>",
     HI100, "high.mega10", "keyword"),
    ("fund_jp", None, "マネックス・ゴールド・ファンド", GD100, "gold.generic", "keyword"),
    ("fund_jp", None, "Tracers MSCIオール・カントリー・ゴールドプラス", HI100, "high.goldplus", "keyword"),
    ("fund_jp", None, "楽天・プラチナ・ファンド(為替ヘッジなし)(楽天・プラチナ(為替ヘッジなし))",
     GD100, "gold.platinum", "keyword"),
    ("fund_jp", None, "Tracers S&P500ゴールドプラス", HI100, "high.goldplus", "keyword"),
    ("fund_jp", None, "Tracers NASDAQ100ゴールドプラス", HI100, "high.goldplus", "keyword"),
    ("fund_jp", None, "iFreeレバレッジFANG+", HI100, "high.fangplus", "keyword"),
    # 実際にあった誤配分: 「スト『リート』」が リート に部分一致していた
    ("fund_jp", None, "ステート・ストリート・ゴールド・オープン(為替ヘッジなし)",
     GD100, "gold.generic", "keyword"),
    # 年金（証券会社別名の重複括弧付き）
    ("pension", None, "SBI・全世界株式インデックス・ファンド(SBI・全世界株式インデックス・ファンド)",
     ALLC, "world.allcountry", "keyword"),
    ("pension", None, "SBI-PIMCO世界債券アクティブファンド(DC) (SBI-PIMCO世界債券アクティブファンド(DC))",
     BD100, "bond.generic", "keyword"),
    ("pension", None,
     "ニッセイJリートインデックスファンド(購入・換金手数料なし)(ニッセイJリートインデックス(購入・換金手数料なし))",
     RT100, "reit.generic", "keyword"),
    ("pension", None, "三井住友・DC外国リートインデックスファンド(三井住友・DC外国リートインデックスファンド)",
     RT100, "reit.generic", "keyword"),
    ("pension", None, "三菱UFJ純金ファンド(愛称:ファインゴールド)(三菱UFJ純金ファンド)",
     GD100, "gold.generic", "keyword"),
    # 国内上場ETF: 証券コードの上書きが最優先（略称の表記変更に対する保険）
    ("stock_jp", "521A", "IF FANG+ゴールド", HI100, "high.fangplus", "code"),
    ("stock_jp", "2840", "IFナス100H無", W100, "world.nasdaq", "code"),
    ("stock_jp", "180A", "GX超⻑期米国債", BD100, "bond.generic", "code"),
    ("stock_jp", "2511", "NF外債ヘッジ無", BD100, "bond.generic", "code"),
    ("stock_jp", "447A", "SSゴールドヘッジ無", GD100, "gold.generic", "code"),
    ("stock_jp", "1541", "純プラチナ上場信託(現物国内保管型)", GD100, "gold.platinum", "code"),
    ("stock_jp", "1475", "Iシェアーズ・コアTOPIX", JP100, "jp.equity", "code"),
    # 同じETFがコード無しでもキーワード層だけで正しく解決すること
    ("stock_jp", None, "IF FANG+ゴールド", HI100, "high.fangplus", "keyword"),
    ("stock_jp", None, "IFナス100H無", W100, "world.nasdaq", "keyword"),
    ("stock_jp", None, "GX超⻑期米国債", BD100, "bond.generic", "keyword"),
    ("stock_jp", None, "NF外債ヘッジ無", BD100, "bond.generic", "keyword"),
    ("stock_jp", None, "SSゴールドヘッジ無", GD100, "gold.generic", "keyword"),
    ("stock_jp", None, "純プラチナ上場信託(現物国内保管型)", GD100, "gold.platinum", "keyword"),
    ("stock_jp", None, "Iシェアーズ・コアTOPIX", JP100, "jp.equity", "keyword"),
    # 個別株はフォールバック（クラス推定）。未登録コードは素通りすること
    ("stock_jp", None, "架空商事", JP100, "fb.stock_jp", "class"),
    ("stock_jp", "9999", "架空ホールディングス", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "テスト電力HD", JP100, "fb.stock_jp", "class"),
]

# --- 未保有の実在商品でのカバレッジ表（到達度の固定。公開商品名のみ） ---
MARKET_CASES = [
    # 国内株ETF
    ("stock_jp", None, "TOPIX連動型上場投資信託", JP100, "jp.equity", "keyword"),
    ("stock_jp", None, "日経225連動型上場投資信託", JP100, "jp.equity", "keyword"),
    ("stock_jp", None, "MAXIS トピックス上場投信", JP100, "jp.equity", "keyword"),
    ("stock_jp", None, "NEXT FUNDS 日経平均高配当株50指数連動型上場投信", JP100, "jp.equity", "keyword"),
    ("stock_jp", None, "NEXT FUNDS 野村日本株高配当70連動型上場投信", JP100, "jp.equity", "keyword"),
    ("stock_jp", None, "東証グロース市場250ETF", JP100, "jp.equity", "keyword"),
    # 海外株ETF（国内上場）
    ("stock_jp", None, "NEXT FUNDS NASDAQ-100(為替ヘッジなし)連動型上場投信", W100, "world.nasdaq", "keyword"),
    ("stock_jp", None, "MAXIS ナスダック100上場投信", W100, "world.nasdaq", "keyword"),
    ("stock_jp", None, "MAXIS ナスダック100上場投信(為替ヘッジあり)", W100, "world.nasdaq", "keyword"),
    ("stock_jp", None, "上場インデックスファンド米国株式(S&P500)", W100, "world.us", "keyword"),
    ("stock_jp", None, "SPDR S&P500 ETF", W100, "world.us", "keyword"),
    ("stock_jp", None, "iシェアーズ S&P 500 米国株 ETF", W100, "world.us", "keyword"),
    ("stock_jp", None, "MAXIS米国株式(S&P500)上場投信", W100, "world.us", "keyword"),
    ("stock_jp", None, "NEXT FUNDS ダウ・ジョーンズ工業株30種平均株価連動型上場投信", W100, "world.us", "keyword"),
    ("stock_jp", None, "iシェアーズ・コア MSCI 先進国株(除く日本)ETF", W100, "world.exjapan", "keyword"),
    ("stock_jp", None, "NEXT FUNDS 外国株式・MSCI-KOKUSAI指数連動型上場投信", W100, "world.exjapan", "keyword"),
    ("stock_jp", None, "上場インデックスファンド世界株式(MSCI ACWI)除く日本", W100, "world.exjapan", "keyword"),
    ("stock_jp", None, "MAXIS全世界株式(オール・カントリー)上場投信", ALLC, "world.allcountry", "keyword"),
    ("stock_jp", None, "iシェアーズ・コア MSCI エマージング・マーケット ETF", HI100, "high.emerging", "keyword"),
    ("stock_jp", None, "NEXT FUNDS 新興国株式・MSCIエマージング指数連動型上場投信", HI100, "high.emerging", "keyword"),
    # 債券ETF
    ("stock_jp", None, "iシェアーズ・コア 米国債7-10年 ETF", BD100, "bond.generic", "keyword"),
    ("stock_jp", None, "iシェアーズ 米国債20年超 ETF(為替ヘッジあり)", BD100, "bond.generic", "keyword"),
    ("stock_jp", None, "上場インデックスファンド米国債券", BD100, "bond.generic", "keyword"),
    ("stock_jp", None, "iシェアーズ 米ドル建て投資適格社債 ETF", BD100, "bond.generic", "keyword"),
    # リートETF
    ("stock_jp", None, "NEXT FUNDS 東証REIT指数連動型上場投信", RT100, "reit.generic", "keyword"),
    ("stock_jp", None, "iシェアーズ・コア Jリート ETF", RT100, "reit.generic", "keyword"),
    ("stock_jp", None, "NEXT FUNDS 外国REIT・S&P先進国REIT指数連動型上場投信", RT100, "reit.generic", "keyword"),
    # 貴金属ETF
    ("stock_jp", None, "純金上場信託(現物国内保管型)", GD100, "gold.generic", "keyword"),
    ("stock_jp", None, "SPDRゴールド・シェア", GD100, "gold.generic", "keyword"),
    ("stock_jp", None, "純銀上場信託(現物国内保管型)", GD100, "gold.platinum", "keyword"),
    ("stock_jp", None, "純パラジウム上場信託(現物国内保管型)", GD100, "gold.platinum", "keyword"),
    # レバレッジ・インバース
    ("stock_jp", None, "NEXT FUNDS 日経平均レバレッジ・インデックス連動型上場投信", HI100, "high.leverage", "keyword"),
    ("stock_jp", None, "NEXT FUNDS 日経平均ダブルインバース・インデックス連動型上場投信", HI100, "high.leverage", "keyword"),
    ("stock_jp", None, "NEXT NOTES 日経平均ダブルインバースETN", HI100, "high.leverage", "keyword"),
    ("stock_jp", None, "iFreeETF 日経レバレッジ指数", HI100, "high.leverage", "keyword"),
    # キーワードが無いテーマ型ETF → フォールバック + 投信っぽさ警告（別テストで検証）
    ("stock_jp", None, "iシェアーズ MSCI ジャパン高配当利回り ETF", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "上場インデックスファンド日本経済貢献株", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "グローバルX US テック・トップ20 ETF", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "iシェアーズ 米国連続増配株 ETF", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "NEXT FUNDS NOMURA原油ロング/ショート", JP100, "fb.stock_jp", "class"),
    # 個別株（フォールバック・警告なし）
    ("stock_jp", None, "トヨタ自動車", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "三菱商事", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "任天堂", JP100, "fb.stock_jp", "class"),
    ("stock_jp", None, "オリエンタルランド", JP100, "fb.stock_jp", "class"),
    # 主要インデックス投信
    ("fund_jp", None, "eMAXIS Slim 先進国株式インデックス", W100, "world.developed", "keyword"),
    ("fund_jp", None, "eMAXIS Slim 新興国株式インデックス", HI100, "high.emerging", "keyword"),
    ("fund_jp", None, "eMAXIS Slim 国内債券インデックス", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "eMAXIS Slim 先進国債券インデックス", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "eMAXIS Slim 国内リートインデックス", RT100, "reit.generic", "keyword"),
    ("fund_jp", None, "eMAXIS Slim 先進国リートインデックス", RT100, "reit.generic", "keyword"),
    ("fund_jp", None, "eMAXIS Slim 全世界株式(除く日本)", W100, "world.exjapan", "keyword"),
    ("fund_jp", None, "eMAXIS Slim 全世界株式(3地域均等型)",
     {"世界株": "66.6", "国内株式": "33.4"}, "world.3region", "keyword"),
    ("fund_jp", None, "SBI・V・S&P500インデックス・ファンド", W100, "world.us", "keyword"),
    ("fund_jp", None, "SBI・V・全米株式インデックス・ファンド", W100, "world.us", "keyword"),
    ("fund_jp", None, "楽天・全米株式インデックス・ファンド", W100, "world.us", "keyword"),
    ("fund_jp", None, "楽天・全世界株式インデックス・ファンド", ALLC, "world.allcountry", "keyword"),
    ("fund_jp", None, "楽天・プラス・オールカントリー株式インデックス・ファンド", ALLC, "world.allcountry", "keyword"),
    ("fund_jp", None, "たわらノーロード 先進国株式", W100, "world.developed", "keyword"),
    ("fund_jp", None, "ニッセイ外国株式インデックスファンド", W100, "world.developed", "keyword"),
    ("fund_jp", None, "野村つみたて外国株投信", W100, "world.developed", "keyword"),
    ("fund_jp", None, "大和住銀DC国内株式ファンド", JP100, "jp.equity", "keyword"),
    ("fund_jp", None, "Tracers 日経平均高配当株50インデックス", JP100, "jp.equity", "keyword"),
    ("fund_jp", None, "iFreeNEXT インド株インデックス", HI100, "high.india", "keyword"),
    ("fund_jp", None, "iFreeレバレッジ NASDAQ100", HI100, "high.leverage", "keyword"),
    ("fund_jp", None, "楽天日本株4.3倍ブル", HI100, "high.leverage", "keyword"),
    ("fund_jp", None, "フィデリティ・USハイ・イールド・ファンド", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "日興 インデックスファンド海外債券ヘッジなし", BD100, "bond.generic", "keyword"),
    ("fund_jp", None, "ダイワ・US-REIT・オープン", RT100, "reit.generic", "keyword"),
    ("fund_jp", None, "新光US-REITオープン(愛称:ゼウス)", RT100, "reit.generic", "keyword"),
    ("fund_jp", None, "SBI・iシェアーズ・ゴールドファンド", GD100, "gold.generic", "keyword"),
    ("fund_jp", None, "ステート・ストリート・米国株式インデックス・ファンド", W100, "world.us", "keyword"),
    ("fund_jp", None, "ゴールドマン・サックス・日本株式ファンド", JP100, "jp.equity", "keyword"),
    ("fund_jp", None, "インデックスファンド海外新興国(エマージング)株式", HI100, "high.emerging", "keyword"),
    # アクティブ投信・特殊型は意図的に未一致（静かに間違えず、未分類で可視化）
    ("fund_jp", None, "ひふみプラス", None, None, None),
    ("fund_jp", None, "セゾン・グローバルバランスファンド", None, None, None),
    ("fund_jp", None, "農林中金<パートナーズ>おおぶね", None, None, None),
    ("fund_jp", None, "インベスコ 世界厳選株式オープン", None, None, None),
    ("fund_jp", None, "netWIN GSテクノロジー株式ファンド", None, None, None),
    ("fund_jp", None, "アライアンス・バーンスタイン・米国成長株投信", None, None, None),
    ("fund_jp", None, "グローバル・ハイクオリティ成長株式ファンド(愛称:未来の世界)", None, None, None),
    ("fund_jp", None, "ピクテ・グローバル・インカム株式ファンド", None, None, None),
    ("fund_jp", None, "三井住友TAM-世界経済インデックスファンド", None, None, None),
    ("fund_jp", None, "SBI日本高配当株式(分配)ファンド", None, None, None),
    ("fund_jp", None, "東京海上・円資産バランスファンド", None, None, None),
    ("fund_jp", None, "eMAXIS Neo 宇宙開発", None, None, None),
    ("pension", None, "架空DC総合バランス型", None, None, None),
    # 暗号資産・外国株
    ("crypto", None, "ビットコイン", {"暗号資産": "100"}, "class.crypto", "class"),
    ("crypto", None, "テザー(USDT)", {"ステーブルコイン": "100"}, "crypto.stable", "keyword"),
    ("stock_foreign", None, "架空モーターズ", W100, "fb.stock_foreign", "class"),
]

# フォールバックのうち「名前が投信・ETFらしい」と警告すべき行
FALLBACK_WARN_NAMES = {
    "iシェアーズ MSCI ジャパン高配当利回り ETF",
    "上場インデックスファンド日本経済貢献株",
    "グローバルX US テック・トップ20 ETF",
    "iシェアーズ 米国連続増配株 ETF",
    "NEXT FUNDS NOMURA原油ロング/ショート",
}

ALL_CASES = HELD_CASES + MARKET_CASES


@pytest.mark.parametrize(
    "asset_class,code,name,exp_alloc,exp_rule,exp_by",
    ALL_CASES,
    ids=[f"{c[0]}-{c[2][:40]}" for c in ALL_CASES],
)
def test_rule_table(asset_class, code, name, exp_alloc, exp_rule, exp_by):
    alloc, rule_id, matched_by, fallback, warning = _resolve(asset_class, code, name)
    assert alloc == _alloc(exp_alloc)
    assert rule_id == exp_rule
    assert matched_by == exp_by
    # フォールバックは fb.* だけ / 警告はフォールバック行の投信様の名前だけ
    assert fallback == (exp_rule is not None and exp_rule.startswith("fb."))
    assert warning == (name in FALLBACK_WARN_NAMES)


# ----------------------------------------------------------------------
# 誤爆・順序のトラップ（表でも通るが、意図を名前付きで固定する）
# ----------------------------------------------------------------------

def test_reit_keyword_does_not_match_street():
    """実際にあった誤配分の真因: 裸の「リート」は「スト『リート』」に当たる。

    リート系キーワードは必ず修飾付き（jリート/外国リート/reit等）で書くこと。
    """
    alloc, rule_id, _, _, _ = _resolve(
        "fund_jp", None, "ステート・ストリート・ゴールド・オープン(為替ヘッジなし)"
    )
    assert rule_id == "gold.generic"
    assert alloc == _alloc(GD100)
    # ゴールドを含まない「ストリート」名でもリートには落ちない（未一致が正解）
    assert _resolve("fund_jp", None, "架空ストリート・ファンド")[1] is None


def test_ordering_traps():
    r = lambda name, cls="fund_jp": _resolve(cls, None, name)[1]
    # ゴールドプラス（複合レバレッジ型）は ゴールド より先に判定
    assert r("Tracers MSCIオール・カントリー・ゴールドプラス") == "high.goldplus"
    # FANG+ゴールド は FANG+ が勝つ
    assert r("IF FANG+ゴールド", "stock_jp") == "high.fangplus"
    # 全世界"債券" は 全世界株 に触れない（債券が上）
    assert r("SBIiシェアーズ全世界債券インデックス") == "bond.generic"
    # 新興国"債券" は 新興国株 に触れない
    assert r("野村インデックスファンド・新興国債券") == "bond.generic"
    # メガ10 は 米国 系より先
    assert r("ニッセイ・S米国グロース株式メガ10インデックスファンド") == "high.mega10"
    # 外国"REIT" は 外国株 より先
    assert r("野村インデックスファンド・外国REIT") == "reit.generic"
    # "除く日本" は 全世界株(95/5) より先（日本比率5%を付けない）
    assert r("eMAXIS Slim 全世界株式(除く日本)") == "world.exjapan"
    # レバレッジ日経 は 日経(国内株) より先
    assert r("NEXT FUNDS 日経平均レバレッジ・インデックス連動型上場投信", "stock_jp") == "high.leverage"


def test_emerging_bond_never_becomes_high_risk():
    """エマージング系債券ファンドは exclude で弾いて債券に落とす。"""
    assert _resolve("fund_jp", None, "架空エマージング・ボンド・ファンド")[1] == "bond.generic"
    # 「エマージングマーケット」を含んでいても債券語があればハイリスクにしない
    assert _resolve("fund_jp", None, "架空エマージング・マーケット債券ファンド")[1] == "bond.generic"


def test_no_false_positive_goldman():
    alloc, rule_id, _, _, _ = _resolve("fund_jp", None, "ゴールドマン・サックス・日本株式ファンド")
    assert rule_id == "jp.equity"
    assert alloc == _alloc(JP100)


def test_gold_point_is_not_gold():
    """「ゴールドポイント」等のポイント名を金に分類しない。"""
    alloc, rule_id, _, _, _ = _resolve("point", None, "ヨドバシゴールドポイント")
    assert rule_id == "class.point"
    assert alloc == _alloc(CA100)


# ----------------------------------------------------------------------
# フォールバックの安全装置
# ----------------------------------------------------------------------

# 投信っぽさ検知の誤検知チェック用: 著名企業の社名（保有とは無関係の一般リスト）
_COMPANY_NAMES = [
    "トヨタ自動車", "ソニーグループ", "任天堂", "三菱商事", "キーエンス",
    "日本電信電話", "ソフトバンクグループ", "イオン", "日本航空",
    "オリエンタルランド", "三井住友フィナンシャルグループ", "信越化学工業",
    "東京電力ホールディングス", "日本製鉄", "楽天グループ", "みずほフィナンシャルグループ",
    "ビックカメラ", "すかいらーくホールディングス", "KDDI", "第一生命ホールディングス",
]


def test_fund_shape_tripwire_no_false_positive():
    """個別株の社名がフォールバック警告に誤検知されないこと。"""
    for name in _COMPANY_NAMES:
        alloc, rule_id, _, fallback, warning = _resolve("stock_jp", None, name)
        assert rule_id == "fb.stock_jp", name
        assert warning is False, name


def test_fund_shape_tripwire_catches_etfs():
    """キーワード網から漏れたETF・ETNがフォールバックに落ちたら必ず警告される。

    これが 2840 型の誤り（国内上場の外国資産ETF→国内株式）の再発防止線。
    """
    for name in FALLBACK_WARN_NAMES:
        alloc, rule_id, _, fallback, warning = _resolve("stock_jp", None, name)
        assert rule_id == "fb.stock_jp", name
        assert warning is True, name


def test_no_fallback_for_funds():
    """fund_jp / pension にフォールバックは無い。未知の投信は未一致のまま
    未分類バナーに現れる（静かに間違えない）。"""
    for cls in ("fund_jp", "pension"):
        assert match_rule("架空アクティブファンド", None, cls) is None
        assert match_rule("架空グローバル成長投信", None, cls) is None


# ----------------------------------------------------------------------
# ルール表そのものの整合性
# ----------------------------------------------------------------------

def test_every_rule_tag_name_is_declared():
    """タグ名のtypo（国内株 vs 国内株式 等）は全銘柄 missing-tag を招くため必ず検出する。"""
    used = {name for rule in RULES for name, _ in rule.tags}
    assert used == KNOWN_TAG_NAMES


def test_every_rule_sums_to_100():
    for rule in RULES:
        assert sum(w for _, w in rule.tags) == D("100"), rule.id


def test_rule_ids_unique():
    ids = [r.id for r in RULES]
    assert len(ids) == len(set(ids))


def test_code_rules_reference_existing_rules():
    ids = {r.id for r in RULES}
    for code, rule_id in CODE_RULES.items():
        assert rule_id in ids, code


def test_custom_rules_ignore_unknown_code_override():
    """rules= を差し替えたとき、CODE_RULES が参照するIDが無ければ素通しする
    （将来ルールをDB化する場合の拡張点）。"""
    custom = (Rule("only.gold", (("ゴールド", D("100")),), keywords=("ゴールド",)),)
    assert match_rule("IFナス100H無", "2840", "stock_jp", rules=custom) is None
    m = match_rule("架空ゴールドファンド", None, "fund_jp", rules=custom)
    assert m is not None and m.rule.id == "only.gold"


def test_resolve_allocation_reports_missing_tags():
    rule = next(r for r in RULES if r.id == "world.allcountry")
    alloc, missing = tag_rules.resolve_allocation(rule, [{"id": 7, "name": "世界株"}])
    assert alloc == {7: D("95")}
    assert missing == ["国内株式"]


def test_nfkc_does_not_fold_u2ed1():
    """「⻑」(U+2ED1 CJK RADICAL LONG) は NFKC で 長(U+9577) に畳まれない。
    これが norm_for_rules に部首の畳み込みが存在する理由。
    （エディタの自動正規化に耐えるよう、ここだけはエスケープで書く）"""
    assert unicodedata.normalize("NFKC", "⻑") == "⻑"
    normalized = norm_for_rules("GX超⻑期米国債")
    assert "超長期" in normalized  # 畳み込みが効いている
    assert "米国債" in normalized       # キーワード照合にも到達できる
