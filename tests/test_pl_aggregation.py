"""損益集計の回帰テスト（レビューで発見した欠陥の再発防止）。

いずれも「評価額は全ロット、原価は原価ありロットのみ」を引き算していたことに
起因する誤りで、現金・ポイント残高や原価未設定ロットが含み益として計上されていた。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from asset_summary.core.models import (
    Account,
    AssetClass,
    HoldingSnapshot,
    Security,
    Unit,
)
from asset_summary.core.portfolio import (
    aggregate_by_security,
    daily_series,
    summarize,
)

D = Decimal
ACCOUNTS = {1: Account(id=1, name="テスト証券")}
SETTINGS = {"include_pension": "1", "include_points": "1"}


def _sec(id: int, **kw) -> Security:
    defaults = dict(
        id=id, name=f"銘柄{id}", name_key=f"sec{id}",
        asset_class=AssetClass.STOCK_JP, code=str(1000 + id),
    )
    defaults.update(kw)
    return Security(**defaults)


def _snap(sec_id: int, **kw) -> HoldingSnapshot:
    defaults = dict(
        account_id=1, security_id=sec_id, as_of_date=date(2026, 8, 1),
        quantity=D("100"), avg_cost=D("1000"),
    )
    defaults.update(kw)
    return HoldingSnapshot(**defaults)


def test_cash_balance_is_not_counted_as_profit():
    """現金残高は原価が無いため、合計損益に含み益として混入してはならない。"""
    secs = {
        1: _sec(1),
        2: _sec(2, code=None, asset_class=AssetClass.CASH, unit=Unit.CURRENCY),
    }
    lots = [
        _snap(1, quantity=D("100"), avg_cost=D("1000")),          # 原価10万・評価12万
        _snap(2, security_id=2, quantity=D("500000"), avg_cost=None),  # 現金50万
    ]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("1200")}, fx={}, settings=SETTINGS)
    assert out["total_value"] == D("620000")   # 12万 + 現金50万
    assert out["total_cost"] == D("100000")
    assert out["total_pl"] == D("20000")       # 現金50万を含まない
    assert out["total_pl_pct"] == D("20")


def test_points_are_not_counted_as_profit():
    secs = {
        1: _sec(1),
        2: _sec(2, code=None, asset_class=AssetClass.POINT, unit=Unit.POINT),
    }
    lots = [
        _snap(1),
        _snap(2, security_id=2, quantity=D("74345"), avg_cost=None,
              reported_value_jpy=D("74345")),
    ]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("1200")}, fx={}, settings=SETTINGS)
    assert out["total_value"] == D("194345")
    assert out["total_pl"] == D("20000")


def test_mixed_cost_lots_within_one_holding():
    """同一銘柄×口座に原価あり/なしロットが混在しても損益が壊れないこと。"""
    secs = {1: _sec(1)}
    lots = [
        _snap(1, lot_seq=0, quantity=D("100"), avg_cost=D("4000")),
        _snap(1, lot_seq=1, quantity=D("100"), avg_cost=None),
    ]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("5000")}, fx={}, settings=SETTINGS)
    h = out["holdings"][0]
    assert h["value"] == D("1000000")          # 表示上の評価額は全ロット
    assert h["cost"] == D("400000")
    assert h["pl"] == D("100000")              # 原価ありロット分のみ（旧: 600000）
    assert h["pl_pct"] == D("25")
    assert h["avg_cost"] == D("4000")          # 原価ありロットの数量で割る（旧: 2000）
    assert out["total_pl"] == D("100000")
    assert out["pl_excluded_count"] == 1


def test_account_level_pl_excludes_cash_in_same_account():
    """同一口座に現金と証券が同居しても口座損益が膨らまないこと。"""
    secs = {
        1: _sec(1),
        2: _sec(2, code=None, asset_class=AssetClass.CASH, unit=Unit.CURRENCY),
    }
    lots = [_snap(1), _snap(2, security_id=2, quantity=D("101671"), avg_cost=None)]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("1200")}, fx={}, settings=SETTINGS)
    by_class = {c["class"]: c for c in out["classes"]}
    assert by_class["stock_jp"]["pl"] == D("20000")
    assert by_class["cash"]["pl"] is None      # 原価が無いクラスは損益なし


def test_holding_without_any_price_has_no_value():
    """価格も記載値も無い保有は 0円ではなく「不明」として合計から外れること。"""
    secs = {
        1: _sec(1),
        2: _sec(2, code=None, asset_class=AssetClass.REAL_ESTATE, unit=Unit.UNIT),
    }
    lots = [
        _snap(1, quantity=D("100"), avg_cost=D("1000")),
        _snap(2, security_id=2, quantity=D("1"), avg_cost=D("45000000")),
    ]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("1200")}, fx={}, settings=SETTINGS)
    realestate = next(h for h in out["holdings"] if h["asset_class"] == "real_estate")
    assert realestate["value"] is None         # 旧: 0 として全損扱い
    assert realestate["pl"] is None
    assert out["total_value"] == D("120000")   # 不動産は合計に入れない
    assert out["total_pl"] == D("20000")       # 4,500万円の架空の損失が出ない


def test_zero_avg_cost_still_reports_pl():
    """取得単価0円（贈与等）でも合計損益が None にならないこと。"""
    secs = {1: _sec(1)}
    lots = [_snap(1, quantity=D("100"), avg_cost=D("0"))]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("500")}, fx={}, settings=SETTINGS)
    assert out["total_cost"] is None or out["total_cost"] == D("0")
    assert out["total_pl"] == D("50000")


# ----------------------------------------------------------------------
# 銘柄単位の合算（同一銘柄を複数口座で保有）
# ----------------------------------------------------------------------

TWO_ACCOUNTS = {1: Account(id=1, name="テスト証券"), 2: Account(id=2, name="別の証券")}


def test_same_security_in_two_accounts_is_merged():
    """521A のように同じ銘柄を2口座で持つケース（保有一覧では1行に見せる）。"""
    secs = {1: _sec(1)}
    lots = [
        _snap(1, account_id=1, quantity=D("101"), avg_cost=D("1862")),
        _snap(1, account_id=2, quantity=D("329"), avg_cost=D("1971")),
    ]
    out = summarize(lots, secs, TWO_ACCOUNTS, spot={1: D("2000")}, fx={}, settings=SETTINGS)
    assert len(out["holdings"]) == 2           # summarize は口座別のまま

    merged = aggregate_by_security(out["holdings"])
    assert len(merged) == 1
    h = merged[0]
    assert h["quantity"] == D("430")
    assert h["value"] == D("860000")           # 430 × 2000
    assert h["cost"] == D("101") * D("1862") + D("329") * D("1971")
    assert h["pl"] == h["value"] - h["cost"]
    # 数量加重の合成単価
    assert h["avg_cost"] == h["cost"] / D("430")
    # 口座は参考情報として内訳が残り、表示は「A 他N件」
    assert [a["account"] for a in h["accounts"]] == ["別の証券", "テスト証券"]
    assert h["account"] == "別の証券 他1件"
    assert h["account_id"] is None
    assert h["lot_count"] == 2
    # クラス別の銘柄数も1件（口座数で水増ししない）
    assert {c["class"]: c["holding_count"] for c in out["classes"]} == {"stock_jp": 1}


def test_aggregate_keeps_single_account_row_intact():
    secs = {1: _sec(1)}
    out = summarize([_snap(1)], secs, ACCOUNTS, spot={1: D("1200")}, fx={},
                    settings=SETTINGS)
    merged = aggregate_by_security(out["holdings"])
    assert len(merged) == 1
    assert merged[0]["account"] == "テスト証券"
    assert merged[0]["account_id"] == 1
    assert [a["account"] for a in merged[0]["accounts"]] == ["テスト証券"]


def test_aggregate_excludes_uncosted_lots_from_pl():
    """原価なしロットを持つ口座があっても、損益と合成単価が薄まらないこと。"""
    secs = {1: _sec(1)}
    lots = [
        _snap(1, account_id=1, lot_seq=0, quantity=D("100"), avg_cost=D("500")),
        _snap(1, account_id=1, lot_seq=1, quantity=D("50"), avg_cost=None),
        _snap(1, account_id=2, quantity=D("200"), avg_cost=D("600")),
    ]
    out = summarize(lots, secs, TWO_ACCOUNTS, spot={1: D("1000")}, fx={},
                    settings=SETTINGS)
    h = aggregate_by_security(out["holdings"])[0]
    assert h["quantity"] == D("350")
    assert h["value"] == D("350000")           # 表示上の評価額は全ロット
    # 損益は原価ありロット（100株+200株）の評価額とだけ突き合わせる
    assert h["cost"] == D("170000")            # 100×500 + 200×600
    assert h["costed_value"] == D("300000")    # (100+200) × 1000
    assert h["pl"] == D("130000")
    # 合成単価は原価ありロットの数量300で割る（350で割ると薄まる）
    assert h["avg_cost"] == D("170000") / D("300")


def test_aggregate_without_any_cost_has_no_pl():
    secs = {1: _sec(1, asset_class=AssetClass.REAL_ESTATE, unit=Unit.UNIT)}
    lots = [
        _snap(1, account_id=1, quantity=D("1000"), avg_cost=None,
              reported_value_jpy=D("1000")),
        _snap(1, account_id=2, quantity=D("2000"), avg_cost=None,
              reported_value_jpy=D("2000")),
    ]
    out = summarize(lots, secs, TWO_ACCOUNTS, spot={}, fx={}, settings=SETTINGS)
    h = aggregate_by_security(out["holdings"])[0]
    assert h["quantity"] == D("3000")
    assert h["pl"] is None
    assert h["cost"] is None
    assert h["avg_cost"] is None


def _cash_point_stock_fixture():
    secs = {
        1: _sec(1, code=None, asset_class=AssetClass.CASH, unit=Unit.CURRENCY),
        2: _sec(2, code=None, asset_class=AssetClass.POINT, unit=Unit.POINT),
        3: _sec(3),
    }
    lots = [
        _snap(1, account_id=1, quantity=D("1000"), avg_cost=None),
        _snap(1, account_id=2, quantity=D("2000"), avg_cost=None),
        _snap(2, security_id=2, account_id=1, quantity=D("300"), avg_cost=None,
              reported_value_jpy=D("300")),
        _snap(2, security_id=2, account_id=2, quantity=D("400"), avg_cost=None,
              reported_value_jpy=D("400")),
        _snap(3, security_id=3, account_id=1, quantity=D("10"), avg_cost=D("100")),
        _snap(3, security_id=3, account_id=2, quantity=D("20"), avg_cost=D("200")),
    ]
    return secs, lots


def test_cash_and_points_stay_per_account():
    """merge_cash=0 なら預金・ポイントは口座ごとに分けたまま（従来表示）。

    預金は全行が単一銘柄に集約されるため、合算すると銀行別の残高が
    一覧から消える。それを好まない利用者向けの表示。
    """
    secs, lots = _cash_point_stock_fixture()
    settings = {**SETTINGS, "merge_cash": "0"}
    out = summarize(lots, secs, TWO_ACCOUNTS, spot={3: D("300")}, fx={},
                    settings=settings)
    merged = aggregate_by_security(out["holdings"])
    by_class: dict[str, list] = {}
    for h in merged:
        by_class.setdefault(h["asset_class"], []).append(h)
    assert len(by_class["cash"]) == 2          # 口座ごとに残る
    assert len(by_class["point"]) == 2
    assert len(by_class["stock_jp"]) == 1      # 銘柄は合算される
    assert sorted(h["account"] for h in by_class["cash"]) == ["テスト証券", "別の証券"]
    # クラス別の件数も保有一覧の行数と一致する
    counts = {c["class"]: c["holding_count"] for c in out["classes"]}
    assert counts == {"cash": 2, "point": 2, "stock_jp": 1}


def test_cash_merges_into_one_row_when_enabled():
    """merge_cash=1（既定）なら預金は「A 他N件」の1行。ポイントは口座ごとのまま。"""
    secs, lots = _cash_point_stock_fixture()
    out = summarize(lots, secs, TWO_ACCOUNTS, spot={3: D("300")}, fx={},
                    settings=SETTINGS)  # merge_cash 未指定 = 既定でまとめる
    merged = aggregate_by_security(out["holdings"], merge_cash=True)
    by_class: dict[str, list] = {}
    for h in merged:
        by_class.setdefault(h["asset_class"], []).append(h)
    assert len(by_class["cash"]) == 1
    cash = by_class["cash"][0]
    assert cash["quantity"] == D("3000")
    assert cash["value"] == D("3000")
    # 残高の大きい口座が代表になり、内訳は accounts に残る
    assert cash["account"] == "別の証券 他1件"
    assert sorted(a["account"] for a in cash["accounts"]) == ["テスト証券", "別の証券"]
    assert len(by_class["point"]) == 2         # ポイントは口座ごとのまま
    # クラス別の件数も保有一覧の行数と一致する
    counts = {c["class"]: c["holding_count"] for c in out["classes"]}
    assert counts == {"cash": 1, "point": 2, "stock_jp": 1}


def test_merge_all_aggregates_even_per_account_classes():
    """銘柄詳細用の merge_all は預金・ポイントも常に口座横断で1行にする。"""
    secs, lots = _cash_point_stock_fixture()
    settings = {**SETTINGS, "merge_cash": "0"}
    out = summarize(lots, secs, TWO_ACCOUNTS, spot={3: D("300")}, fx={},
                    settings=settings)
    cash_rows = [h for h in out["holdings"] if h["asset_class"] == "cash"]
    merged = aggregate_by_security(cash_rows, merge_all=True)
    assert len(merged) == 1
    assert merged[0]["quantity"] == D("3000")
    assert len(merged[0]["accounts"]) == 2


def test_daily_series_without_fx_falls_back_to_reported_value():
    """FX履歴が無いとき外貨建て資産を1:1換算しないこと。"""
    secs = {1: _sec(1, currency="USD")}
    snaps = [
        _snap(1, quantity=D("100"), avg_cost=None, reported_value_jpy=D("3000000"))
    ]
    prices = {1: ({"2026-08-01": D("200")}, "USD")}
    out = daily_series(
        snaps, secs, ACCOUNTS, prices, {}, date(2026, 8, 1), date(2026, 8, 1), SETTINGS
    )
    assert out[0]["value"] == D("3000000")     # 旧: 20000（150分の1）
