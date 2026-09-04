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
    SeriesLookup,
    aggregate_by_security,
    daily_series,
    summarize,
)

D = Decimal


def _sec(id: int, **kw) -> Security:
    defaults = dict(
        id=id,
        name=f"銘柄{id}",
        name_key=f"sec{id}",
        asset_class=AssetClass.STOCK_JP,
        code=str(1000 + id),
    )
    defaults.update(kw)
    return Security(**defaults)


def _snap(sec_id: int, **kw) -> HoldingSnapshot:
    defaults = dict(
        account_id=1,
        security_id=sec_id,
        as_of_date=date(2026, 8, 1),
        quantity=D("100"),
        avg_cost=D("1000"),
    )
    defaults.update(kw)
    return HoldingSnapshot(**defaults)


ACCOUNTS = {1: Account(id=1, name="テスト証券")}
SETTINGS = {"include_pension": "1", "include_points": "1"}


def test_series_lookup_backfill_and_ffill():
    lk = SeriesLookup({"2026-08-05": D("100"), "2026-08-10": D("110")})
    assert lk.at("2026-08-01") == D("100")   # backfill
    assert lk.at("2026-08-05") == D("100")
    assert lk.at("2026-08-07") == D("100")   # ffill
    assert lk.at("2026-08-10") == D("110")
    assert lk.at("2026-09-01") == D("110")
    assert lk.strictly_at_or_before("2026-08-01") is None


def test_summarize_basic_pl():
    secs = {1: _sec(1)}
    lots = [_snap(1)]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("1200")}, fx={}, settings=SETTINGS)
    h = out["holdings"][0]
    assert h["value"] == D("120000")
    assert h["pl"] == D("20000")
    assert out["total_value"] == D("120000")
    assert out["total_pl"] == D("20000")
    assert out["total_pl_pct"] == D("20")


def test_summarize_fund_divisor():
    secs = {
        1: _sec(
            1,
            code=None,
            asset_class=AssetClass.FUND_JP,
            unit=Unit.KUCHI,
            price_unit_divisor=10000,
        )
    }
    lots = [_snap(1, quantity=D("50000"), avg_cost=D("40000"))]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("44000")}, fx={}, settings=SETTINGS)
    h = out["holdings"][0]
    # 50,000口 × 44,000 ÷ 10,000 = 220,000円（1万口あたり単価の式）
    assert h["value"].quantize(D("1")) == D("220000")
    assert h["pl"] > 0


def test_summarize_pension_total_cost_basis():
    secs = {1: _sec(1, code=None, asset_class=AssetClass.PENSION, unit=Unit.UNIT)}
    lots = [
        _snap(1, quantity=D("1"), avg_cost=D("4000000"), reported_value_jpy=D("4600000"))
    ]
    out = summarize(lots, secs, ACCOUNTS, spot={}, fx={}, settings=SETTINGS)
    h = out["holdings"][0]
    assert h["value"] == D("4600000")
    assert h["pl"] == D("600000")


def test_summarize_points_toggle_excluded():
    secs = {
        1: _sec(1),
        2: _sec(2, code=None, asset_class=AssetClass.POINT, unit=Unit.POINT),
    }
    lots = [
        _snap(1),
        _snap(2, security_id=2, quantity=D("2256"), avg_cost=None, reported_value_jpy=D("2256")),
    ]
    out = summarize(lots, secs, ACCOUNTS, spot={1: D("1000")}, fx={}, settings=SETTINGS)
    assert out["total_value"] == D("102256")
    out2 = summarize(
        lots, secs, ACCOUNTS, spot={1: D("1000")}, fx={},
        settings={"include_pension": "1", "include_points": "0"},
    )
    assert out2["total_value"] == D("100000")
    # ポイント自体の行は残る（in_total=False）
    assert any(h["asset_class"] == "point" and not h["in_total"] for h in out2["holdings"])


def test_summarize_cash_fx():
    secs = {1: _sec(1, code=None, asset_class=AssetClass.CASH, unit=Unit.CURRENCY, currency="USD")}
    lots = [_snap(1, quantity=D("100"), avg_cost=None)]
    out = summarize(lots, secs, ACCOUNTS, spot={}, fx={"USD": D("150")}, settings=SETTINGS)
    assert out["holdings"][0]["value"] == D("15000")


def test_summarize_unpriced_fallback_reported_value():
    secs = {1: _sec(1, code=None, asset_class=AssetClass.FUND_JP, price_unit_divisor=10000)}
    lots = [_snap(1, avg_cost=None, reported_value_jpy=D("55555"))]
    out = summarize(lots, secs, ACCOUNTS, spot={}, fx={}, settings=SETTINGS)
    h = out["holdings"][0]
    assert h["value"] == D("55555")
    assert h["has_price"] is False
    assert "銘柄1" in out["unpriced"]


def test_daily_series_step_function_and_backfill():
    secs = {1: _sec(1)}
    snaps = [
        _snap(1, as_of_date=date(2026, 8, 5), quantity=D("100")),
        _snap(1, as_of_date=date(2026, 8, 10), quantity=D("200")),
    ]
    prices = {1: ({"2026-08-01": D("1000"), "2026-08-07": D("1100")}, "JPY")}
    out = daily_series(
        snaps, secs, ACCOUNTS, prices, {}, date(2026, 8, 1), date(2026, 8, 12), SETTINGS
    )
    by_date = {r["t"]: r["value"] for r in out}
    # 8/1: スナップショット以前 → 遡及（最初の保有数100） × 価格1000
    assert by_date["2026-08-01"] == D("100000")
    # 8/7: 保有100 × 価格1100
    assert by_date["2026-08-07"] == D("110000")
    # 8/10以降: 保有200 × 価格1100（価格はffill）
    assert by_date["2026-08-12"] == D("220000")


def test_daily_series_scope_filters():
    secs = {1: _sec(1), 2: _sec(2, asset_class=AssetClass.METAL, code=None)}
    snaps = [_snap(1), _snap(2, security_id=2, quantity=D("10"), avg_cost=None)]
    prices = {1: ({"2026-08-01": D("1000")}, "JPY"), 2: ({"2026-08-01": D("500")}, "JPY")}
    all_rows = daily_series(
        snaps, secs, ACCOUNTS, prices, {}, date(2026, 8, 1), date(2026, 8, 1), SETTINGS
    )
    assert all_rows[0]["value"] == D("105000")
    metal_only = daily_series(
        snaps, secs, ACCOUNTS, prices, {}, date(2026, 8, 1), date(2026, 8, 1), SETTINGS,
        scope=("class", "metal"),
    )
    assert metal_only[0]["value"] == D("5000")
    sec_only = daily_series(
        snaps, secs, ACCOUNTS, prices, {}, date(2026, 8, 1), date(2026, 8, 1), SETTINGS,
        scope=("security", "1"),
    )
    assert sec_only[0]["value"] == D("100000")


def test_daily_series_zero_quantity_after_sale():
    secs = {1: _sec(1)}
    snaps = [
        _snap(1, as_of_date=date(2026, 8, 1), quantity=D("100")),
        _snap(1, as_of_date=date(2026, 8, 5), quantity=D("0")),
    ]
    prices = {1: ({"2026-08-01": D("1000")}, "JPY")}
    out = daily_series(
        snaps, secs, ACCOUNTS, prices, {}, date(2026, 8, 4), date(2026, 8, 6), SETTINGS
    )
    by_date = {r["t"]: r["value"] for r in out}
    assert by_date["2026-08-04"] == D("100000")
    assert by_date["2026-08-06"] == D("0")


# ----------------------------------------------------------------------
# 前日比（day_change）
# ----------------------------------------------------------------------


def test_summarize_without_prev_spot_has_no_day_change():
    """前日値を渡さなければ従来どおり（day_change は全部 None）。"""
    out = summarize(
        [_snap(1)], {1: _sec(1)}, ACCOUNTS, spot={1: D("1200")}, fx={}, settings=SETTINGS
    )
    assert out["holdings"][0]["day_change"] is None
    assert out["total_day_change"] is None
    assert out["day_change_partial"] is False


def test_summarize_day_change_amount_and_pct():
    out = summarize(
        [_snap(1)], {1: _sec(1)}, ACCOUNTS,
        spot={1: D("1200")}, fx={}, settings=SETTINGS,
        prev_spot={1: D("1000")}, prev_as_of={1: "2026-08-12"},
    )
    h = out["holdings"][0]
    assert h["prev_value"] == D("100000")
    assert h["day_change"] == D("20000")
    assert h["day_change_pct"] == D("20")
    assert h["day_change_as_of"] == "2026-08-12"
    assert out["total_day_change"] == D("20000")
    assert out["classes"][0]["day_change"] == D("20000")
    assert out["day_change_partial"] is False


def test_summarize_day_change_partial_when_prev_missing():
    """前日値の無い銘柄があるときは、その分を足さずに partial を立てる。"""
    secs = {1: _sec(1), 2: _sec(2)}
    lots = [_snap(1), _snap(2, security_id=2)]
    out = summarize(
        lots, secs, ACCOUNTS, spot={1: D("1200"), 2: D("1200")}, fx={},
        settings=SETTINGS, prev_spot={1: D("1000")},
    )
    by_id = {h["id"]: h for h in out["holdings"]}
    assert by_id[1]["day_change"] == D("20000")
    assert by_id[2]["day_change"] is None
    assert out["total_day_change"] == D("20000")   # 銘柄2は足されない
    assert out["day_change_partial"] is True
    assert out["classes"][0]["day_change_partial"] is True


def test_summarize_day_change_uses_previous_fx_for_foreign_assets():
    """外貨建ては前日レートで換算する（前日比に為替変動も含める）。"""
    secs = {1: _sec(1, currency="USD", asset_class=AssetClass.STOCK_FOREIGN)}
    out = summarize(
        [_snap(1, avg_cost=D("10"))], secs, ACCOUNTS,
        spot={1: D("20")}, fx={"USD": D("150")}, settings=SETTINGS,
        prev_spot={1: D("20")}, prev_fx={"USD": D("140")},
    )
    h = out["holdings"][0]
    assert h["prev_value"] == D("100") * D("20") * D("140")
    assert h["day_change"] == D("100") * D("20") * (D("150") - D("140"))


def test_summarize_day_change_in_display_currency_uses_each_days_rate():
    """表示通貨換算は当日・前日それぞれのレートで行う。"""
    secs = {1: _sec(1)}
    out = summarize(
        [_snap(1)], secs, ACCOUNTS, spot={1: D("1200")}, fx={"USD": D("150")},
        settings=SETTINGS, jpy_per_display=D("150"),
        prev_spot={1: D("1000")}, prev_fx={"USD": D("100")},
        prev_jpy_per_display=D("100"),
    )
    h = out["holdings"][0]
    assert h["value"] == D("120000") / D("150")     # 当日レート
    assert h["prev_value"] == D("100000") / D("100")  # 前日レート
    assert h["day_change"] == h["value"] - h["prev_value"]


def test_summarize_cash_day_change_is_zero_without_previous_lots():
    """前日のスナップショットを渡さなければ、現金は動きようがない（数量＝金額）。"""
    secs = {1: _sec(1, asset_class=AssetClass.CASH, currency="JPY")}
    lots = [_snap(1, avg_cost=None, quantity=D("50000"))]
    out = summarize(
        lots, secs, ACCOUNTS, spot={}, fx={}, settings=SETTINGS,
        prev_spot={}, prev_fx={},
    )
    assert out["holdings"][0]["day_change"] == D("0")


def test_summarize_cash_day_change_is_the_balance_difference():
    """現金は数量そのものが金額。前日の残高との差が前日比になる。"""
    secs = {1: _sec(1, asset_class=AssetClass.CASH, currency="JPY")}
    lots = [_snap(1, avg_cost=None, quantity=D("50000"))]
    prev = [_snap(1, avg_cost=None, quantity=D("60000"), as_of_date=date(2026, 7, 31))]
    out = summarize(
        lots, secs, ACCOUNTS, spot={}, fx={}, settings=SETTINGS,
        prev_spot={}, prev_fx={}, prev_lots=prev,
    )
    h = out["holdings"][0]
    assert h["prev_value"] == D("60000")
    assert h["day_change"] == D("-10000")
    assert out["total_day_change"] == D("-10000")


def test_summarize_day_change_includes_quantity_changes():
    """買い増しぶんも前日比に入る（前日の数量 × 前日終値との差）。"""
    out = summarize(
        [_snap(1, quantity=D("120"))], {1: _sec(1)}, ACCOUNTS,
        spot={1: D("1000")}, fx={}, settings=SETTINGS,
        prev_spot={1: D("1000")},
        prev_lots=[_snap(1, quantity=D("100"), as_of_date=date(2026, 7, 31))],
    )
    h = out["holdings"][0]
    assert h["prev_value"] == D("100000")
    assert h["day_change"] == D("20000")


def test_summarize_day_change_cancels_out_a_cash_to_stock_transfer():
    """現金で株を買った日は、現金の減少と株の増加が相殺される。

    価格差だけを見る定義では、現金 −20万に対して株の増加ぶんが乗らず、
    総資産が20万減ったという嘘になる。
    """
    secs = {
        1: _sec(1),
        2: _sec(2, asset_class=AssetClass.CASH, currency="JPY"),
    }
    lots = [
        _snap(1, quantity=D("200")),                       # 100株 → 200株
        _snap(2, security_id=2, avg_cost=None, quantity=D("300000")),  # 50万 → 30万
    ]
    prev = [
        _snap(1, quantity=D("100"), as_of_date=date(2026, 7, 31)),
        _snap(2, security_id=2, avg_cost=None, quantity=D("500000"),
              as_of_date=date(2026, 7, 31)),
    ]
    out = summarize(
        lots, secs, ACCOUNTS, spot={1: D("2000")}, fx={}, settings=SETTINGS,
        prev_spot={1: D("2000")}, prev_lots=prev,
    )
    assert out["total_day_change"] == D("0")   # 振替なので総資産は動かない


def test_summarize_day_change_backfills_a_lot_with_no_previous_snapshot():
    """前日にまだ記録の無い保有は当日の数量で評価する（推移グラフの遡及と同じ）。

    初めて取り込んだ保有が「その日に増えた」ことにならないようにする。
    """
    out = summarize(
        [_snap(1)], {1: _sec(1)}, ACCOUNTS, spot={1: D("1200")}, fx={},
        settings=SETTINGS, prev_spot={1: D("1000")}, prev_lots=[],
    )
    h = out["holdings"][0]
    assert h["prev_value"] == D("100000")     # 100株 × 前日終値
    assert h["day_change"] == D("20000")      # 価格が動いたぶんだけ


def test_summarize_keeps_a_holding_that_went_to_zero_today():
    """当日ゼロになった保有は、その日だけ減少として残す。

    売却日に現金の増加だけが計上されると、見かけの利益が出てしまう。
    """
    secs = {1: _sec(1)}
    out = summarize(
        [_snap(1, quantity=D("0"), avg_cost=None)], secs, ACCOUNTS,
        spot={1: D("1200")}, fx={}, settings=SETTINGS, prev_spot={1: D("1000")},
        prev_lots=[_snap(1, quantity=D("100"), as_of_date=date(2026, 7, 31))],
    )
    h = out["holdings"][0]
    assert h["value"] == D("0")
    assert h["day_change"] == D("-100000")

    # 前日もゼロなら、もう出さない（売却の翌日以降）
    out = summarize(
        [_snap(1, quantity=D("0"), avg_cost=None)], secs, ACCOUNTS,
        spot={1: D("1200")}, fx={}, settings=SETTINGS, prev_spot={1: D("1000")},
        prev_lots=[_snap(1, quantity=D("0"), avg_cost=None, as_of_date=date(2026, 7, 31))],
    )
    assert out["holdings"] == []


def test_summarize_unpriced_holding_reports_no_day_change():
    """記載値フォールバックの銘柄に「前日比 0」という嘘を出さない。"""
    secs = {1: _sec(1)}
    lots = [_snap(1, reported_value_jpy=D("99000"))]
    out = summarize(
        lots, secs, ACCOUNTS, spot={}, fx={}, settings=SETTINGS, prev_spot={},
    )
    h = out["holdings"][0]
    assert h["value"] == D("99000")
    assert h["day_change"] is None


def test_aggregate_by_security_day_change_needs_all_accounts():
    """口座ごとの前日比が1つでも欠けたら、銘柄単位では出さない。"""
    secs = {1: _sec(1)}
    accounts = {1: Account(id=1, name="A"), 2: Account(id=2, name="B")}
    lots = [_snap(1), _snap(1, account_id=2)]
    full = summarize(
        lots, secs, accounts, spot={1: D("1200")}, fx={}, settings=SETTINGS,
        prev_spot={1: D("1000")},
    )
    merged = aggregate_by_security(full["holdings"])
    assert merged[0]["day_change"] == D("40000")

    # 片方の口座だけ評価額が無い（前日値も無い）ようにする
    partial_rows = [dict(h) for h in full["holdings"]]
    partial_rows[1]["day_change"] = None
    partial_rows[1]["prev_value"] = None
    assert aggregate_by_security(partial_rows)[0]["day_change"] is None


def test_daily_series_ratio_by_security_weights_each_holding():
    """タグ・Myポートフォリオの推移は銘柄ごとの計上率ぶんだけ積む。"""
    secs = {1: _sec(1), 2: _sec(2)}
    lots = [_snap(1), _snap(2, security_id=2)]
    price_series = {
        1: ({"2026-08-01": D("1000")}, "JPY"),
        2: ({"2026-08-01": D("2000")}, "JPY"),
    }
    points = daily_series(
        lots, secs, ACCOUNTS, price_series, {},
        date(2026, 8, 1), date(2026, 8, 1), SETTINGS,
        ratio_by_security={1: D("0.5")},
    )
    # 銘柄1が半分だけ、銘柄2は ratio に無いので集計から外れる
    assert points[0]["value"] == D("100") * D("1000") * D("0.5")
