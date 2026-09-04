from __future__ import annotations

from datetime import date
from decimal import Decimal

from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    PriceSourceStatus,
    Security,
    Unit,
)
from asset_summary.core.store import Store
from asset_summary.importers.base import make_name_key
from asset_summary.importers.matching import CASH_SECURITY_NAME, build_matches
from tests.fixtures.factories import (
    crypto_row,
    deposit,
    fund,
    make_result,
    pension,
    point,
    stock,
)


def _sec(name: str, code: str | None = None, **kw) -> Security:
    defaults = dict(
        name=name,
        name_key=make_name_key(name),
        asset_class=AssetClass.STOCK_JP,
        code=code,
    )
    defaults.update(kw)
    return Security(**defaults)


def _snap(store: Store, acct_id: int, sec_id: int, lot_seq: int = 0, *,
          qty="100", avg=None, label=None, origin="mf",
          as_of=date(2026, 7, 1)) -> None:
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=acct_id,
            security_id=sec_id,
            lot_seq=lot_seq,
            as_of_date=as_of,
            quantity=Decimal(qty),
            avg_cost=None if avg is None else Decimal(avg),
            lot_label=label,
            origin=origin,
        )
    )


def _rows_by_key(rows):
    return {r["key"]: r for r in rows}


# ----------------------------------------------------------------------
# コード正規化・照合順
# ----------------------------------------------------------------------


def test_code_normalization_truncates_daiwa_5digit(store: Store):
    rows, diff, _ = build_matches(store, make_result(
        stock("架空ナス100", "28400", "400", "2000", "2500", "1000000")
    ))
    assert rows[0]["code"] == "2840"
    assert rows[0]["price_source_type"] == "yahoo"
    assert rows[0]["price_source_ref"] == "2840.T"
    assert rows[0]["price_source_status"] == "linked"
    assert rows[0]["key"] == "acct:架空証券|sec:2840|lot:0"


def test_code_normalization_keeps_known_5char_code(store: Store):
    # 既存codes集合に生コードがあれば切詰めない（誤切詰め回避）
    sec_id = store.create_security(_sec("既存5桁", code="13010"))
    rows, _, _ = build_matches(store, make_result(
        stock("既存5桁", "13010", "10", "100", "110", "1100")
    ))
    assert rows[0]["code"] == "13010"
    assert rows[0]["security_id"] == sec_id


def test_resolution_order_code_beats_name_key(store: Store):
    by_code = store.create_security(_sec("コード銘柄", code="1111"))
    by_name = store.create_security(_sec("かくうファンド", code=None))
    rows, _, _ = build_matches(store, make_result(
        stock("かくうファンド", "1111", "10", "100", "110", "1100")
    ))
    assert rows[0]["security_id"] == by_code
    assert rows[0]["security_id"] != by_name


def test_resolution_alias_beats_name_key(store: Store):
    aliased = store.create_security(_sec("旧名称", code=None,
                                         asset_class=AssetClass.FUND_JP))
    shadow = store.create_security(
        _sec("シンメイ", code=None, asset_class=AssetClass.FUND_JP)
    )
    # alias を新名称に張る → name_key 一致銘柄(shadow)より優先される
    store.add_alias(make_name_key("シンメイ"), aliased)
    rows, _, _ = build_matches(store, make_result(
        fund("シンメイ", "1000", "10000", "11000", "1100")
    ))
    assert rows[0]["security_id"] == aliased
    assert rows[0]["security_id"] != shadow


# ----------------------------------------------------------------------
# ロット割当
# ----------------------------------------------------------------------


def test_lot_assignment_greedy_by_avg_cost(store: Store):
    acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    sec_id = store.create_security(_sec("架空工業", code="1234"))
    _snap(store, acct.id, sec_id, 0, qty="10", avg="100")
    _snap(store, acct.id, sec_id, 1, qty="5", avg="200")

    rows, _, _ = build_matches(store, make_result(
        stock("架空工業", "1234", "10", "205", "210", "2100"),   # → lot1 (cost近傍)
        stock("架空工業", "1234", "10", "95", "100", "1000"),    # → lot0
        stock("架空工業", "1234", "3", "500", "510", "1530"),    # → 空き lot2
    ))
    by_key = _rows_by_key(rows)
    r1 = by_key["acct:架空証券|sec:1234|lot:1"]
    assert r1["status"] == "qty_changed"       # qty 10 vs 旧5
    assert r1["old_quantity"] == "5" and r1["old_avg_cost"] == "200"
    r0 = by_key["acct:架空証券|sec:1234|lot:0"]
    assert r0["status"] == "cost_changed"      # qty同一・avg_cost 95 vs 100
    r2 = by_key["acct:架空証券|sec:1234|lot:2"]
    assert r2["status"] == "new"
    assert r2["old_quantity"] is None


def test_lot_assignment_unchanged(store: Store):
    acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    sec_id = store.create_security(_sec("架空工業", code="1234"))
    _snap(store, acct.id, sec_id, 0, qty="10", avg="100")
    rows, _, _ = build_matches(store, make_result(
        stock("架空工業", "1234", "10", "100", "150", "1500")
    ))
    assert rows[0]["status"] == "unchanged"


def test_deposit_lots_match_by_label(store: Store):
    acct = store.get_or_create_account("架空銀行", kind="bank", origin="mf")
    cash_id = store.create_security(_sec(
        CASH_SECURITY_NAME, code=None, asset_class=AssetClass.CASH,
        unit=Unit.CURRENCY,
        price_source_status=PriceSourceStatus.NOT_REQUIRED,
    ))
    _snap(store, acct.id, cash_id, 0, qty="1000", label="普通預金")
    _snap(store, acct.id, cash_id, 1, qty="5000", label="定期預金")

    rows, _, _ = build_matches(store, make_result(
        deposit("普通預金", "1200"),
        deposit("定期預金", "5000"),
        deposit("新型預金", "42"),
    ))
    by_key = _rows_by_key(rows)
    assert by_key["acct:架空銀行|sec:" + make_name_key(CASH_SECURITY_NAME) + "|lot:0"][
        "status"] == "qty_changed"
    assert by_key["acct:架空銀行|sec:" + make_name_key(CASH_SECURITY_NAME) + "|lot:1"][
        "status"] == "unchanged"
    new_row = by_key["acct:架空銀行|sec:" + make_name_key(CASH_SECURITY_NAME) + "|lot:2"]
    assert new_row["status"] == "new"
    assert new_row["lot_label"] == "新型預金"
    assert new_row["new_quantity"] == "42"


# ----------------------------------------------------------------------
# 改ページで末尾が欠けた銘柄名
# ----------------------------------------------------------------------


def test_truncated_name_resolves_to_existing_security(store: Store):
    """改ページで末尾が欠けた名称を、同じ銘柄として扱う。

    MF は改ページ位置でセルの続きをページ下端の装飾に重ねて描くため、末尾の
    数文字が落ちた名前で届くことがある（実例では「し))」が欠落）。
    """
    full = "楽天・プラチナ・ファンド(為替ヘッジなし)(楽天・プラチナ(為替ヘッジなし))"
    cut = "楽天・プラチナ・ファンド(為替ヘッジなし)(楽天・プラチナ(為替ヘッジな"
    sec_id = store.create_security(_sec(full, code=None, asset_class=AssetClass.FUND_JP))

    assert store.resolve_security(name_key=make_name_key(cut)) == sec_id
    rows, _, _ = build_matches(store, make_result(fund(cut, "132963", "9401", "7056", "93819")))
    assert rows[0]["security_id"] == sec_id      # 旧実装では None → 別銘柄が作られた


def test_short_name_is_not_merged_into_a_longer_one(store: Store):
    """正当に短い別銘柄は寄せない。括弧が釣り合っていれば途中で切れていない。"""
    a = store.create_security(_sec("ソフトバンク", code="9434"))
    store.create_security(_sec("ソフトバンクグループ", code="9984"))
    assert store.resolve_security(name_key=make_name_key("ソフトバンク")) == a
    # 括弧の無い短い名前は前方一致の対象にしない
    assert store.resolve_security(name_key=make_name_key("ソフトバンクグル")) is None


def test_truncated_name_with_several_candidates_is_not_guessed(store: Store):
    """前方一致が複数あるときは判断できないので寄せない。"""
    store.create_security(_sec("架空ファンド(為替ヘッジなし)(A)", code=None,
                               asset_class=AssetClass.FUND_JP))
    store.create_security(_sec("架空ファンド(為替ヘッジなし)(B)", code=None,
                               asset_class=AssetClass.FUND_JP))
    cut = make_name_key("架空ファンド(為替ヘッジなし)(")
    assert store.resolve_security(name_key=cut) is None


# ----------------------------------------------------------------------
# ポイントの有効期限ローリング
# ----------------------------------------------------------------------


def _point_sec(name: str) -> Security:
    return _sec(name, code=None, asset_class=AssetClass.POINT, unit=Unit.POINT,
                price_source_status=PriceSourceStatus.NOT_REQUIRED)


def test_point_lot_matched_when_expiry_rolls(store: Store):
    """JREポイントは有効期限が毎月ずれる。同じロットとして扱うこと。"""
    acct = store.get_or_create_account("VIEW CARD", kind="point", origin="mf")
    sec_id = store.create_security(_point_sec("JREポイント(通常)"))
    _snap(store, acct.id, sec_id, 0, qty="44675", label="JREポイント|2028/07/31")

    rows, _, _ = build_matches(store, make_result(
        point("JREポイント(通常)", "44689", "44689", ptype="JREポイント",
              expiry="2028/08/31", inst="VIEW CARD"),
    ))
    assert [r["status"] for r in rows] == ["qty_changed"]
    row = rows[0]
    assert row["lot_seq"] == 0
    assert (row["old_quantity"], row["new_quantity"]) == ("44675", "44689")
    assert row["lot_label"] == "JREポイント|2028/08/31"   # 新しい期限へ更新される


def test_point_lots_with_different_expiries_stay_apart(store: Store):
    """楽天の期間限定ポイントは期限だけが違うロットが同時に並ぶ。潰さないこと。"""
    acct = store.get_or_create_account("楽天市場", kind="point", origin="mf")
    name = "楽天ポイント(期間限定ポイント)"
    sec_id = store.create_security(_point_sec(name))
    buckets = [("2026/09/30", "500"), ("2026/11/30", "10"), ("2027/01/30", "32")]
    for i, (expiry, qty) in enumerate(buckets):
        _snap(store, acct.id, sec_id, i, qty=qty, label=f"{name}|{expiry}")

    rows, _, _ = build_matches(store, make_result(*[
        point(name, qty, qty, ptype=name, expiry=expiry, inst="楽天市場")
        for expiry, qty in buckets
    ]))
    assert [r["status"] for r in rows] == ["unchanged"] * 3
    assert [r["lot_seq"] for r in rows] == [0, 1, 2]


def test_point_lots_rolling_together_pair_in_expiry_order(store: Store):
    """複数ロットが同時にローリングしても期限順に対応づけ、新規化しないこと。"""
    acct = store.get_or_create_account("楽天市場", kind="point", origin="mf")
    name = "楽天ポイント(期間限定ポイント)"
    sec_id = store.create_security(_point_sec(name))
    _snap(store, acct.id, sec_id, 0, qty="500", label=f"{name}|2026/09/30")
    _snap(store, acct.id, sec_id, 1, qty="10", label=f"{name}|2026/11/30")

    rows, _, _ = build_matches(store, make_result(
        point(name, "480", "480", ptype=name, expiry="2026/10/31", inst="楽天市場"),
        point(name, "12", "12", ptype=name, expiry="2026/12/31", inst="楽天市場"),
    ))
    assert [r["status"] for r in rows] == ["qty_changed"] * 2
    assert [(r["lot_seq"], r["old_quantity"], r["new_quantity"]) for r in rows] == [
        (0, "500", "480"), (1, "10", "12")
    ]


def test_point_of_different_kind_is_not_paired(store: Store):
    """種類そのものが変わった場合は救済しない（別物として新規＋消失）。"""
    acct = store.get_or_create_account("架空カード", kind="point", origin="mf")
    sec_id = store.create_security(_point_sec("架空ポイント"))
    _snap(store, acct.id, sec_id, 0, qty="100", label="旧ポイント|2027/01/31")

    rows, _, _ = build_matches(store, make_result(
        point("架空ポイント", "200", "200", ptype="新ポイント", expiry="2027/02/28"),
    ))
    assert {r["status"] for r in rows} == {"new", "missing"}


# ----------------------------------------------------------------------
# missing 検出（origin=mf に限定）
# ----------------------------------------------------------------------


def test_missing_limited_to_mf_origin(store: Store):
    mf_acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    manual_acct = store.get_or_create_account("手動口座", kind="manual", origin="manual")
    sec_a = store.create_security(_sec("消える銘柄", code="1234"))
    sec_b = store.create_security(_sec("手動銘柄", code="5678"))
    sec_c = store.create_security(_sec("手動行", code="9012"))
    sec_d = store.create_security(_sec("売却済み", code="3456"))
    _snap(store, mf_acct.id, sec_a, 0, qty="100", avg="500", origin="mf")
    _snap(store, manual_acct.id, sec_b, 0, qty="10", origin="manual")     # 手動口座 → 対象外
    _snap(store, mf_acct.id, sec_c, 0, qty="10", origin="manual")        # 手動行 → 対象外
    _snap(store, mf_acct.id, sec_d, 0, qty="0", origin="mf")             # 数量0 → 対象外

    rows, diff, _ = build_matches(store, make_result())  # 空のPDF
    missing = [r for r in rows if r["status"] == "missing"]
    assert len(missing) == 1
    m = missing[0]
    assert m["key"] == "acct:架空証券|sec:1234|lot:0"
    assert m["name"] == "消える銘柄"
    assert m["old_quantity"] == "100"
    assert m["new_quantity"] is None
    assert m["included"] is True  # 既定提案は売却(数量0)
    assert m["account_id"] == mf_acct.id and m["security_id"] == sec_a


def test_matched_rows_not_reported_missing(store: Store):
    acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    sec_id = store.create_security(_sec("架空工業", code="1234"))
    _snap(store, acct.id, sec_id, 0, qty="100", avg="500")
    rows, _, _ = build_matches(store, make_result(
        stock("架空工業", "1234", "100", "500", "600", "60000")
    ))
    assert all(r["status"] != "missing" for r in rows)


# ----------------------------------------------------------------------
# セクション→属性のマッピング
# ----------------------------------------------------------------------


def test_stock_yahoo_autolink_and_fund_unlinked(store: Store):
    rows, _, _ = build_matches(store, make_result(
        stock("架空工業", "1234", "100", "500", "600", "60000"),
        stock("コード不明", None, "10", "100", "110", "1100"),
        fund("架空ファンド", "10000", "15000", "16000", "16000"),
    ))
    st, nocode, fd = rows
    assert st["price_source_type"] == "yahoo"
    assert st["price_source_ref"] == "1234.T"
    assert st["price_source_status"] == "linked"
    assert st["asset_class"] == "stock_jp" and st["price_unit_divisor"] == 1
    assert nocode["code"] is None
    assert nocode["price_source_type"] == "none"
    assert nocode["price_source_status"] == "unlinked"
    assert fd["asset_class"] == "fund_jp"
    assert fd["unit"] == "kuchi"
    assert fd["price_unit_divisor"] == 10000
    assert fd["price_source_status"] == "unlinked"
    assert fd["reported_price"] == "16000"


def test_deposit_aggregates_to_single_cash_security(store: Store):
    rows, _, _ = build_matches(store, make_result(
        deposit("普通預金", "1000", inst="架空銀行"),
        deposit("豪ドル普通", "4", inst="架空銀行"),
        deposit("現金", "500", inst="別の銀行"),
    ))
    assert all(r["name"] == CASH_SECURITY_NAME for r in rows)
    assert all(r["code"] is None for r in rows)
    assert all(r["asset_class"] == "cash" and r["unit"] == "currency" for r in rows)
    assert rows[0]["account"] == "架空銀行" and rows[0]["account_kind"] == "bank"
    assert rows[0]["lot_label"] == "普通預金"
    assert rows[0]["new_quantity"] == "1000"   # 円残高が数量
    assert rows[2]["account"] == "別の銀行"
    # 同一銀行内はロット分離
    assert rows[0]["lot_seq"] == 0 and rows[1]["lot_seq"] == 1
    assert rows[2]["lot_seq"] == 0


def test_pension_and_point_attributes(store: Store):
    rows, _, _ = build_matches(store, make_result(
        pension("架空DCファンド", "100000", "120000", "20000"),
        point("架空ポイント", "1000", "1000", expiry="2027/01/31"),
    ))
    pen, pt = rows
    assert pen["account"] == "年金" and pen["account_kind"] == "pension"
    assert pen["asset_class"] == "pension" and pen["unit"] == "unit"
    assert pen["new_quantity"] == "1"
    assert pen["new_avg_cost"] == "100000"      # 取得価額の総額
    assert pen["reported_value_jpy"] == "120000"
    assert pen["price_source_status"] == "not_required"
    assert pt["account_kind"] == "point"
    assert pt["asset_class"] == "point" and pt["unit"] == "point"
    assert pt["lot_label"] == "ポイント|2027/01/31"
    assert pt["price_source_status"] == "not_required"


def test_crypto_rows_default_excluded(store: Store):
    rows, diff, sections = build_matches(store, make_result(
        crypto_row("BTC残高", "500"),
        stock("架空工業", "1234", "100", "500", "600", "60000"),
    ))
    cr = [r for r in rows if r["section"] == "crypto"][0]
    assert cr["included"] is False
    assert cr["asset_class"] == "other"
    sec_by_name = {s["section"]: s for s in sections}
    assert sec_by_name["crypto"]["default_include"] is False
    assert sec_by_name["stock"]["default_include"] is True


def test_low_confidence_row_excluded_by_default(store: Store):
    rows, _, _ = build_matches(store, make_result(
        stock("怪しい行", "1234", "10", "100", "110", "9999", confidence=0.65),
        stock("正常な行", "5678", "10", "100", "110", "1100"),
    ))
    assert rows[0]["included"] is False
    assert rows[1]["included"] is True


# ----------------------------------------------------------------------
# 口座名を読み取れなかったときの安全弁
# ----------------------------------------------------------------------


def test_row_without_institution_excluded_by_default(store: Store):
    result = make_result(
        stock("架空工業", "1234", "100", "500", "600", "60000", inst=""),
        stock("正常な行", "5678", "10", "100", "110", "1100"),
    )
    rows, _, _ = build_matches(store, result)
    broken, ok = rows
    assert broken["account"] == ""
    assert broken["included"] is False
    assert "保有金融機関" in broken["raw"]["warnings"][0]
    assert ok["included"] is True
    assert any("読み取れなかった行が 1 件" in w for w in result.report.warnings)


def test_same_code_as_new_and_missing_warns(store: Store):
    """口座名を読めないと同じ銘柄が「新規」と「PDFに無し」の対になる。"""
    acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    sec_id = store.create_security(_sec("架空工業", code="1234"))
    _snap(store, acct.id, sec_id, 0, qty="100", avg="500")

    result = make_result(stock("架空工業", "1234", "100", "500", "600", "60000", inst=""))
    rows, _, _ = build_matches(store, result)
    assert {r["status"] for r in rows} == {"new", "missing"}
    assert any("同じ銘柄コード（1234）" in w for w in result.report.warnings)


def test_institution_read_correctly_produces_no_warning(store: Store):
    acct = store.get_or_create_account("架空証券", kind="broker", origin="mf")
    sec_id = store.create_security(_sec("架空工業", code="1234"))
    _snap(store, acct.id, sec_id, 0, qty="100", avg="500")

    result = make_result(stock("架空工業", "1234", "150", "500", "600", "90000"))
    rows, _, _ = build_matches(store, result)
    assert [r["status"] for r in rows] == ["qty_changed"]
    assert result.report.warnings == []


def test_diff_rows_shape(store: Store):
    _, diff, sections = build_matches(store, make_result(
        stock("架空工業", "1234", "100", "500", "600", "60000", "10000"),
    ))
    row = diff[0]
    assert set(row) == {
        "key", "status", "section", "name", "code", "account", "lot_label",
        "old_quantity", "new_quantity", "old_avg_cost", "new_avg_cost",
        "value", "confidence", "included",
    }
    assert row["value"] == "60000"
    assert sections[0]["label"] == "株式(現物)"
    assert sections[0]["count"] == 1
    assert sections[0]["total"] == "60000"
