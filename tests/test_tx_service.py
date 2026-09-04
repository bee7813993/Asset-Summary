"""取込パイプライン（プレビュー → 確定 → 巻き戻し）の検証。

Store を実際に使う統合寄りのテスト。冪等性と巻き戻しの安全がここの主題。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from asset_summary.core.models import (
    AssetClass,
    Coverage,
    HoldingSnapshot,
    PriceSourceStatus,
    Security,
    Unit,
)
from asset_summary.core.store import Store, StoreError
from asset_summary.importers import tx_service
from asset_summary.importers.service import DuplicateImportError

BROKER = "架空証券"

HEADER = "約定日,受渡日,銘柄コード,銘柄名,取引区分,数量,単価,約定代金,手数料,受渡金額,口座区分\n"
ROW_1 = "2026/01/05,2026/01/07,1234,架空商事,買付,100,2000,200000,0,200000,特定\n"
ROW_2 = "2026/02/10,2026/02/12,1234,架空商事,売却,50,2400,120000,0,120000,特定\n"
ROW_3 = "2026/03/03,2026/03/05,1234,架空商事,買付,25,2200,55000,0,55000,特定\n"


@pytest.fixture()
def seeded(store: Store):
    """MF PDF 取込済みを模した状態: 口座・銘柄・スナップショットがある。"""
    account = store.get_or_create_account(BROKER, kind="broker")
    security_id = store.create_security(
        Security(code="1234", name="架空商事", name_key="架空商事",
                 asset_class=AssetClass.STOCK_JP)
    )
    store.upsert_snapshot(
        HoldingSnapshot(
            account_id=account.id, security_id=security_id,
            as_of_date=date(2026, 8, 1),
            quantity=Decimal("300"), avg_cost=Decimal("1500"), origin="mf",
            raw={"meta": {"acquired_on": "2019/03/14"}},
        )
    )
    return store, account.id, security_id


def _csv(*rows: str) -> bytes:
    return (HEADER + "".join(rows)).encode("cp932")


def _import(store: Store, data: bytes, filename: str = "trades.csv") -> dict:
    preview = tx_service.build_tx_preview(store, data, filename, account_name=BROKER)
    return tx_service.commit_tx_batch(store, preview["batch_id"], account_name=BROKER)


# ----------------------------------------------------------------------
# プレビュー
# ----------------------------------------------------------------------


def test_preview_matches_securities_against_the_existing_universe(seeded):
    store, _account_id, security_id = seeded
    preview = tx_service.build_tx_preview(
        store, _csv(ROW_1, ROW_2, ROW_3), "trades.csv", account_name=BROKER
    )
    assert preview["unmatched_securities"] == []
    assert all(r["security_id"] == security_id for r in preview["rows"])
    assert all(r["included"] for r in preview["rows"])


def test_preview_flags_unmatched_securities_with_suggestions(seeded):
    store, _a, _s = seeded
    data = _csv(ROW_1, "2026/04/01,2026/04/03,9999,謎の銘柄,買付,10,100,1000,0,1000,特定\n")
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    unmatched = preview["unmatched_securities"]
    assert len(unmatched) == 1
    assert unmatched[0]["name"] == "謎の銘柄"
    # 未照合の行は既定で取込対象外
    assert any(r["included"] is False for r in preview["rows"] if r["security_id"] is None)


def test_preview_without_an_account_warns(seeded):
    store, _a, _s = seeded
    preview = tx_service.build_tx_preview(store, _csv(ROW_1), "t.csv")
    assert any("口座" in w for w in preview["warnings"])


def test_reimporting_the_same_file_is_rejected(seeded):
    store, _a, _s = seeded
    data = _csv(ROW_1, ROW_2)
    _import(store, data)
    with pytest.raises(DuplicateImportError):
        tx_service.build_tx_preview(store, data, "trades.csv", account_name=BROKER)


# ----------------------------------------------------------------------
# 確定
# ----------------------------------------------------------------------


def test_commit_stores_transactions_and_learns_the_broker_name(seeded):
    store, account_id, security_id = seeded
    result = _import(store, _csv(ROW_1, ROW_2, ROW_3))
    assert result["inserted"] == 3
    assert result["aliases_learned"] >= 1

    txs = store.list_transactions()
    assert [t.tx_type.value for t in txs] == ["buy", "sell", "buy"]
    assert txs[0].quantity == Decimal("100")
    assert txs[1].quantity == Decimal("-50")
    assert txs[0].account_id == account_id
    assert txs[0].security_id == security_id
    assert txs[0].lot_label == "特定"


def test_commit_without_an_account_is_refused(seeded):
    store, _a, _s = seeded
    preview = tx_service.build_tx_preview(store, _csv(ROW_1), "t.csv")
    with pytest.raises(StoreError, match="口座"):
        tx_service.commit_tx_batch(store, preview["batch_id"])


def test_commit_computes_cost_basis_by_subtraction(seeded):
    """取引が保有の一部しか覆っていないときの引き算。"""
    store, account_id, security_id = seeded
    _import(store, _csv(ROW_1, ROW_2, ROW_3))

    basis = store.list_cost_basis()[0]
    assert basis["coverage"] == Coverage.PARTIAL.value
    # 期首 225株が売却で 190.38株に按分され、逆算単価は 1185.86
    assert Decimal(basis["residual_quantity"]).quantize(Decimal("0.01")) == Decimal("190.38")
    assert Decimal(basis["residual_avg_cost"]).quantize(Decimal("0.01")) == Decimal("1185.86")
    # 部分被覆なので損益には反映しない（再計算値は MF と同じ値になるため）
    assert basis["applies_to_pl"] is False
    assert store.cost_basis_overrides() == {}


def test_full_coverage_override_reaches_the_pl_calculation(store: Store):
    """取引が全期間を覆うときだけ、再計算した取得単価が損益に効く。"""
    account = store.get_or_create_account(BROKER, kind="broker")
    security_id = store.create_security(
        Security(code="1234", name="架空商事", name_key="架空商事",
                 asset_class=AssetClass.STOCK_JP)
    )
    # MF に取得単価が無い保有（いまは損益計算から丸ごと外れている）
    store.upsert_snapshot(
        HoldingSnapshot(account_id=account.id, security_id=security_id,
                        as_of_date=date(2026, 8, 1), quantity=Decimal("75"),
                        avg_cost=None, origin="mf")
    )
    _import(store, _csv(ROW_1, ROW_2, ROW_3))

    basis = store.list_cost_basis()[0]
    assert basis["coverage"] == Coverage.FULL.value
    assert basis["applies_to_pl"] is True
    overrides = store.cost_basis_overrides()
    # 買付 200,000 − 売却で按分して抜けた 100,000 + 買付 55,000 = 155,000 を 75株で割る
    assert overrides[(account.id, security_id)] == Decimal("155000") / Decimal("75")


def test_acquired_on_is_recovered_from_the_mf_snapshot(seeded):
    """MF PDF が取り込んでいた取得日は raw に眠っていた。部分被覆ではそれを使う。"""
    store, _a, _s = seeded
    _import(store, _csv(ROW_1, ROW_2, ROW_3))
    basis = store.list_cost_basis()[0]
    assert basis["acquired_on"] == "2019-03-14"
    assert basis["acquired_on_src"] == "mf_raw"


# ----------------------------------------------------------------------
# 冪等性
# ----------------------------------------------------------------------


def test_overlapping_reimport_does_not_double_count(seeded):
    """期間が重なる再取込は冪等。証券会社は重複する期間しか出せないことがある。"""
    store, _a, _s = seeded
    first = _import(store, _csv(ROW_1, ROW_2), "part1.csv")
    assert first["inserted"] == 2

    second = _import(store, _csv(ROW_2, ROW_3), "part2.csv")
    assert second["inserted"] == 1
    assert second["skipped_duplicates"] == 1
    assert store.count_transactions() == 3


def test_preview_marks_already_imported_rows(seeded):
    store, _a, _s = seeded
    _import(store, _csv(ROW_1, ROW_2), "part1.csv")
    preview = tx_service.build_tx_preview(
        store, _csv(ROW_2, ROW_3), "part2.csv", account_name=BROKER
    )
    assert preview["duplicate_count"] == 1
    dup = [r for r in preview["rows"] if r.get("duplicate")]
    assert len(dup) == 1 and dup[0]["included"] is False


def test_identical_same_day_trades_are_both_kept(seeded):
    """同日・同数量・同単価の別々の約定を 1 件に潰さない。"""
    store, _a, _s = seeded
    row = "2026/01/05,2026/01/07,1234,架空商事,買付,100,2000,200000,0,200000,特定\n"
    result = _import(store, _csv(row, row))
    assert result["inserted"] == 2
    assert store.count_transactions() == 2


# ----------------------------------------------------------------------
# 巻き戻し
# ----------------------------------------------------------------------


def test_rollback_removes_transactions_and_cost_basis(seeded):
    store, _a, _s = seeded
    preview = tx_service.build_tx_preview(
        store, _csv(ROW_1, ROW_2, ROW_3), "trades.csv", account_name=BROKER
    )
    batch_id = preview["batch_id"]
    tx_service.commit_tx_batch(store, batch_id, account_name=BROKER)
    assert store.count_transactions() == 3
    assert store.list_cost_basis()

    store.delete_batch(batch_id)
    assert store.count_transactions() == 0
    assert store.list_cost_basis() == []


def test_rollback_keeps_rows_that_another_batch_also_saw(seeded):
    """期間が重なる 2 バッチのうち古い方を巻き戻しても、新しい方の行は残す。

    重複としてスキップされた行は所有者が古いバッチのままなので、素朴に
    batch_id で消すと新しいバッチが依拠するデータが無言で欠ける。
    """
    store, _a, _s = seeded
    first = tx_service.build_tx_preview(
        store, _csv(ROW_1, ROW_2), "part1.csv", account_name=BROKER
    )
    tx_service.commit_tx_batch(store, first["batch_id"], account_name=BROKER)
    second = tx_service.build_tx_preview(
        store, _csv(ROW_2, ROW_3), "part2.csv", account_name=BROKER
    )
    tx_service.commit_tx_batch(store, second["batch_id"], account_name=BROKER)
    assert store.count_transactions() == 3

    store.delete_batch(first["batch_id"])
    remaining = {t.trade_date.isoformat(): t.batch_id for t in store.list_transactions()}
    # ROW_2 は両方のバッチが見た行なので残り、生きているバッチへ付け替わる
    assert "2026-02-10" in remaining
    assert remaining["2026-02-10"] == second["batch_id"]
    assert "2026-03-03" in remaining
    assert "2026-01-05" not in remaining        # 第1バッチだけの行は消える


def test_import_history_counts_transactions_for_csv_batches(seeded):
    store, _a, _s = seeded
    _import(store, _csv(ROW_1, ROW_2, ROW_3))
    batches = store.list_batches()
    csv_batch = next(b for b in batches if b["source_kind"] == "broker_csv")
    assert csv_batch["row_count"] == 3


# ----------------------------------------------------------------------
# アーキテクチャの回帰ガード
# ----------------------------------------------------------------------


def test_mf_import_after_csv_still_detects_sold_lots(seeded):
    """取引台帳を入れても、MF の消失銘柄検出は壊れない。

    再計算した取得原価をスナップショット行として書く設計にしていたら、
    matching._missing_rows は origin='mf' しか見ないので、その銘柄は
    売却されても永久に検出されなくなっていた。別テーブルに置いた理由。
    """
    from asset_summary.importers.matching import build_matches
    from tests.fixtures.factories import make_result, stock

    store, _account_id, _security_id = seeded
    _import(store, _csv(ROW_1, ROW_2, ROW_3))

    # 次回の MF PDF にはこの銘柄が無い（全部売った）
    empty = make_result(stock("別銘柄", "5678", 10, 100, 110, 1100, inst=BROKER))
    _rows, diff, _sections = build_matches(store, empty)
    missing = [d for d in diff if d["status"] == "missing"]
    assert any(d["code"] == "1234" for d in missing), "消失銘柄が検出されていない"


def test_transactions_never_become_holding_snapshots(seeded):
    """台帳は補助であってスナップショットの正ではない（DESIGN.md の方針）。"""
    store, _a, _s = seeded
    before = len(store.all_snapshots())
    _import(store, _csv(ROW_1, ROW_2, ROW_3))
    assert len(store.all_snapshots()) == before


# ----------------------------------------------------------------------
# 列対応の修正と書式の学習
# ----------------------------------------------------------------------


def test_remap_applies_user_column_overrides(seeded):
    store, _a, _s = seeded
    preview = tx_service.build_tx_preview(
        store, _csv(ROW_1, ROW_2), "t.csv", account_name=BROKER
    )
    remapped = tx_service.remap_tx_preview(
        store, preview["batch_id"], column_overrides={"6": "gross_amount"}
    )
    mapping = {c["field"]: c["index"] for c in remapped["detection"]["columns"]}
    assert mapping["gross_amount"] == 6


def test_remap_links_an_unmatched_security(seeded):
    store, _a, security_id = seeded
    data = _csv("2026/04/01,2026/04/03,,架空商事（株）,買付,10,100,1000,0,1000,特定\n")
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    assert preview["unmatched_securities"]

    remapped = tx_service.remap_tx_preview(
        store, preview["batch_id"], security_map={"架空商事（株）": security_id}
    )
    assert remapped["unmatched_securities"] == []
    assert all(r["security_id"] == security_id for r in remapped["rows"])


def test_security_map_at_commit_imports_previously_unmatched_rows(seeded):
    """プレビューで未照合だった行も、確定時に紐付ければ取り込まれる。

    画面側は紐付けた時点でチェックを戻すが、include_keys を省いた
    呼び出しでも同じ結果になることを保証しておく。
    """
    store, _a, security_id = seeded
    data = _csv(
        "2026/05/01,2026/05/03,,架空商事（株）,買付,10,2100,21000,0,21000,特定\n",
        "2026/05/08,2026/05/10,,架空商事（株）,買付,20,2150,43000,0,43000,特定\n",
        "2026/05/15,2026/05/17,,架空商事（株）,売却,5,2200,11000,0,11000,特定\n",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    assert preview["unmatched_securities"]

    result = tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER,
        security_map={"架空商事(株)": security_id},
    )
    assert result["inserted"] == 3
    assert result["skipped_unmatched"] == 0
    assert all(t.security_id == security_id for t in store.list_transactions())


def test_committing_learns_the_format_profile(seeded):
    store, _a, _s = seeded
    preview = tx_service.build_tx_preview(
        store, _csv(ROW_1, ROW_2), "t.csv", account_name=BROKER
    )
    fingerprint = preview["detection"]["fingerprint"]
    tx_service.commit_tx_batch(store, preview["batch_id"], account_name=BROKER)

    profile = store.get_format_profile(fingerprint)
    assert profile is not None
    assert profile["mapping"]["0"] == "trade_date"


def test_learned_alias_matches_a_differently_written_name_next_time(seeded):
    """証券会社ごとの表記ゆれは、一度紐付ければ次から自動で当たる。"""
    store, _a, security_id = seeded
    data = _csv("2026/04/01,2026/04/03,,架空商事（株）,買付,10,100,1000,0,1000,特定\n")
    preview = tx_service.build_tx_preview(store, data, "a.csv", account_name=BROKER)
    tx_service.remap_tx_preview(
        store, preview["batch_id"], security_map={"架空商事（株）": security_id}
    )
    tx_service.commit_tx_batch(store, preview["batch_id"], account_name=BROKER)

    again = tx_service.build_tx_preview(
        store,
        _csv("2026/05/01,2026/05/03,,架空商事（株）,買付,10,110,1100,0,1100,特定\n"),
        "b.csv", account_name=BROKER,
    )
    assert again["unmatched_securities"] == []
    assert again["rows"][0]["security_id"] == security_id


# ----------------------------------------------------------------------
# 再計算
# ----------------------------------------------------------------------


def test_recompute_is_idempotent(seeded):
    store, _a, _s = seeded
    _import(store, _csv(ROW_1, ROW_2, ROW_3))
    first = store.list_cost_basis()
    tx_service.recompute_cost_basis(store)
    assert store.list_cost_basis() == first


def test_cost_basis_events_include_the_opening_lot(seeded):
    store, _a, security_id = seeded
    _import(store, _csv(ROW_1, ROW_2, ROW_3))
    events = tx_service.cost_basis_events(store, security_id)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "opening"
    assert "buy" in kinds and "sell" in kinds
    assert events[0]["note"] and "取得日不明" in events[0]["note"]


# ----------------------------------------------------------------------
# 売却済み銘柄の登録
#
# 「既存銘柄に無い」は売却済みとは限らず、MF PDF が拾っていない口座の
# 保有中銘柄かもしれない。だから自動では作らず、選ばれたときだけ作る。
# ----------------------------------------------------------------------

SOLD_HEADER = "約定日,銘柄名,取引区分,数量,単価,約定代金,手数料,受渡金額\n"
SOLD_ROWS = (
    "2026/01/05,架空撤退商事,買付,100,1000,100000,110,100110\n"
    "2026/02/10,架空撤退商事,買付,100,1200,120000,132,120132\n"
    "2026/03/03,架空撤退商事,売却,200,1500,300000,330,299670\n"
)


def _sold_csv() -> bytes:
    return (SOLD_HEADER + SOLD_ROWS).encode("cp932")


def test_unknown_securities_are_not_created_automatically(seeded):
    """勝手に銘柄を作らない。保有中の銘柄が別名で二重に増えるのを防ぐため。"""
    store, _a, _s = seeded
    before = len(store.list_securities())
    preview = tx_service.build_tx_preview(store, _sold_csv(), "sold.csv", account_name=BROKER)
    assert [u["name"] for u in preview["unmatched_securities"]] == ["架空撤退商事"]

    result = tx_service.commit_tx_batch(store, preview["batch_id"], account_name=BROKER)
    assert len(store.list_securities()) == before      # 作られていない
    assert result["inserted"] == 0
    assert result["skipped_unmatched"] == 3


def test_registering_as_sold_creates_an_inactive_security(seeded):
    store, _a, _s = seeded
    preview = tx_service.build_tx_preview(store, _sold_csv(), "sold.csv", account_name=BROKER)
    result = tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER,
        new_securities=["架空撤退商事"],
    )
    assert result["created_securities"] == 1
    assert result["inserted"] == 3

    sec = next(s for s in store.list_securities() if s.name == "架空撤退商事")
    assert sec.inactive is True                    # 保有一覧・総資産に出さない
    assert sec.price_source_status is PriceSourceStatus.NOT_REQUIRED


def test_a_sold_security_reports_realized_pl_without_touching_the_portfolio(seeded):
    """売却済み銘柄は実現損益だけを持ち、保有には一切影響しない。"""
    store, _a, _s = seeded
    preview = tx_service.build_tx_preview(store, _sold_csv(), "sold.csv", account_name=BROKER)
    tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER,
        new_securities=["架空撤退商事"],
    )
    sec = next(s for s in store.list_securities() if s.name == "架空撤退商事")

    basis = next(b for b in store.list_cost_basis(security_id=sec.id))
    # 100@1000 + 100@1200 を 200@1500 で売却。手数料込みの取得費 220,242、
    # 譲渡価額 300,000 − 売却手数料 330 = 299,670 → 実現損益 79,428
    assert Decimal(basis["realized_pl"]) == Decimal("79428")
    # 保有していないので損益計算に混ざらない
    assert basis["applies_to_pl"] is False
    assert (store.get_or_create_account(BROKER, kind="broker").id, sec.id) \
        not in store.cost_basis_overrides()
    assert all(h.security_id != sec.id for h in store.current_holdings())


def test_new_security_infers_the_fund_divisor_from_its_own_transactions(seeded):
    """投信を 1 口あたりで作ると取得原価が 10000 倍ずれる。取引から決める。"""
    store, _a, _s = seeded
    data = (
        SOLD_HEADER
        + "2026/01/05,架空撤退ファンド,買付,10000,16000,16000,0,16000\n"
        + "2026/02/10,架空撤退ファンド,買付,20000,16500,33000,0,33000\n"
        + "2026/03/03,架空撤退ファンド,売却,30000,17000,51000,0,51000\n"
    ).encode("cp932")
    preview = tx_service.build_tx_preview(store, data, "fund.csv", account_name=BROKER)
    tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER,
        new_securities=["架空撤退ファンド"],
    )
    sec = next(s for s in store.list_securities() if s.name == "架空撤退ファンド")
    assert sec.price_unit_divisor == 10000
    assert sec.asset_class is AssetClass.FUND_JP


def test_new_security_keeps_the_transaction_currency(seeded):
    """外貨建てを円建てで作ると約150倍ずれたまま原価に紛れ込む。"""
    store, _a, _s = seeded
    data = (
        "約定日,銘柄名,取引区分,数量,単価,約定代金,手数料,受渡金額,通貨\n"
        "2026/01/05,架空撤退ADR,買付,100,20,2000,5,2005,USD\n"
        "2026/02/10,架空撤退ADR,買付,100,22,2200,5,2205,USD\n"
        "2026/03/03,架空撤退ADR,売却,200,25,5000,5,4995,USD\n"
    ).encode("cp932")
    preview = tx_service.build_tx_preview(store, data, "adr.csv", account_name=BROKER)
    tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER,
        new_securities=["架空撤退ADR"],
    )
    sec = next(s for s in store.list_securities() if s.name == "架空撤退ADR")
    assert sec.currency == "USD"


def test_choosing_neither_leaves_the_rows_out(seeded):
    """『取り込まない』のままなら台帳にも銘柄にも何も残らない。"""
    store, _a, _s = seeded
    before = len(store.list_securities())
    preview = tx_service.build_tx_preview(store, _sold_csv(), "sold.csv", account_name=BROKER)
    tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER, new_securities=[],
    )
    assert len(store.list_securities()) == before
    assert store.count_transactions() == 0


# ----------------------------------------------------------------------
# 価格による候補の裏取り
#
# 名前の類似度は「架空全世界株式ファンド」と「架空全世界債券ファンド」を
# 区別できない。約定単価をその銘柄の価格と突き合わせれば決着する。
# fund_autolink が投信の候補を基準価額で確かめているのと同じ考え。
# ----------------------------------------------------------------------


@pytest.fixture()
def twin_funds(store: Store):
    """名前がそっくりで価格だけが違う 2 銘柄。"""
    account = store.get_or_create_account(BROKER, kind="broker")
    ids = {}
    for label, base in (("株式", 16000), ("債券", 11000)):
        name = f"架空全世界{label}ファンド"
        sid = store.create_security(
            Security(name=name, name_key=name, asset_class=AssetClass.FUND_JP,
                     unit=Unit.KUCHI, price_unit_divisor=10000)
        )
        ids[label] = sid
        store.upsert_snapshot(
            HoldingSnapshot(account_id=account.id, security_id=sid,
                            as_of_date=date(2026, 8, 1), quantity=Decimal("10000"),
                            avg_cost=Decimal(str(base)), origin="mf")
        )
        for i, day in enumerate(("2026-01-05", "2026-02-10", "2026-03-03")):
            store.upsert_daily_price("manual", str(sid), day, Decimal(str(base + i * 100)))
    return store, ids


BOND_PRICES = [
    ("2026-01-05", Decimal("11000")),
    ("2026-02-10", Decimal("11100")),
    ("2026-03-03", Decimal("11200")),
]


def test_price_verification_confirms_the_right_security(twin_funds):
    store, ids = twin_funds
    assert tx_service.verify_by_price(store, ids["債券"], BOND_PRICES) == (3, 3)


def test_price_verification_refutes_a_similarly_named_security(twin_funds):
    """名前が似ていても価格が違えば別銘柄だと分かる。"""
    store, ids = twin_funds
    matched, tested = tx_service.verify_by_price(store, ids["株式"], BOND_PRICES)
    assert tested == 3 and matched == 0


def test_missing_price_data_is_inconclusive_not_a_mismatch(twin_funds):
    """価格の無い日は「検証できない」。不一致と混同すると候補を誤って捨てる。"""
    store, ids = twin_funds
    far = [("2020-01-06", Decimal("11000"))]
    assert tx_service.verify_by_price(store, ids["債券"], far) == (0, 0)


def test_price_tolerance_allows_display_rounding_only(twin_funds):
    """表示のまるめ 1 単位ぶんだけ許す。相対誤差にすると似た投信を区別できない。"""
    store, ids = twin_funds
    assert tx_service.verify_by_price(
        store, ids["債券"], [("2026-01-05", Decimal("11001"))]
    ) == (1, 1)
    assert tx_service.verify_by_price(
        store, ids["債券"], [("2026-01-05", Decimal("11010"))]
    ) == (0, 1)


def test_suggestions_rank_the_price_confirmed_candidate_first(twin_funds):
    store, ids = twin_funds
    universe = tx_service.build_universe(store)
    out = tx_service.suggest_securities(
        universe, "架空全世界ファンド", store=store, price_pairs=BOND_PRICES
    )
    assert out[0]["security_id"] == ids["債券"]
    assert out[0]["price_verdict"] == "match"
    refuted = next(x for x in out if x["security_id"] == ids["株式"])
    assert refuted["price_verdict"] == "mismatch"


def test_suggestions_without_prices_fall_back_to_name_similarity(twin_funds):
    """価格が無ければ従来どおり名前だけで並べ、裏取りは unknown とする。"""
    store, _ids = twin_funds
    universe = tx_service.build_universe(store)
    out = tx_service.suggest_securities(universe, "架空全世界ファンド", store=store)
    assert out and all(x["price_verdict"] == "unknown" for x in out)


def _fund_csv(*rows: str) -> bytes:
    head = "約定日,銘柄名,取引区分,数量,単価,約定代金,手数料,受渡金額\n"
    return (head + "".join(r + "\n" for r in rows)).encode("cp932")


BUY_ROWS = (
    "2026/01/05,架空全世界ファンド,買付,10000,11000,11000,0,11000",
    "2026/02/10,架空全世界ファンド,買付,10000,11100,11100,0,11100",
    "2026/03/03,架空全世界ファンド,買付,10000,11200,11200,0,11200",
)


def test_preview_auto_links_a_price_confirmed_security(twin_funds):
    """価格で裏が取れたら自動で結びつける。名前の表記揺れを人手で潰さずに済む。"""
    store, ids = twin_funds
    preview = tx_service.build_tx_preview(store, _fund_csv(*BUY_ROWS), "t.csv",
                                          account_name=BROKER)
    assert preview["unmatched_securities"] == []
    linked = preview["auto_linked_securities"]
    assert len(linked) == 1
    evidence = linked[0]["auto_linked"]
    assert evidence["security_id"] == ids["債券"]
    assert (evidence["price_matched"], evidence["price_checked"]) == (3, 3)

    assert all(r["security_id"] == ids["債券"] for r in preview["rows"])
    assert all(r["matched_by"] == "price" for r in preview["rows"])
    assert all(r["included"] for r in preview["rows"])


def test_transfer_rows_are_not_used_as_price_evidence(twin_funds):
    """入庫・出庫の単価は移管元から引き継いだ取得単価で、その日の時価ではない。

    実データでは同じ運用会社の投信で売買 75/75 が一致したのに入出庫 19/19 が
    外れ、混ぜたせいで裏取りが partial に落ちて正しい候補を採用できなく
    なっていた。
    """
    store, ids = twin_funds
    data = _fund_csv(
        *BUY_ROWS,
        "2026/01/05,架空全世界ファンド,入庫,10000,9000,90000,0,90000",
        "2026/02/10,架空全世界ファンド,出庫,10000,9100,91000,0,91000",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    evidence = preview["auto_linked_securities"][0]["auto_linked"]
    assert evidence["security_id"] == ids["債券"]
    assert (evidence["price_matched"], evidence["price_checked"]) == (3, 3)


def test_transfers_alone_are_not_enough_to_auto_link(twin_funds):
    """入出庫しか無ければ裏を取る材料が無い。名前が似ているだけで決めない。"""
    store, _ids = twin_funds
    data = _fund_csv(
        "2026/01/05,架空全世界ファンド,入庫,10000,11000,110000,0,110000",
        "2026/02/10,架空全世界ファンド,入庫,10000,11100,111000,0,111000",
        "2026/03/03,架空全世界ファンド,入庫,10000,11200,112000,0,112000",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    assert preview["auto_linked_securities"] == []
    assert len(preview["unmatched_securities"]) == 1


def test_auto_link_needs_enough_verified_days(twin_funds):
    """1 日の一致では決めない。偶然そろうことがある。"""
    store, _ids = twin_funds
    preview = tx_service.build_tx_preview(store, _fund_csv(BUY_ROWS[0]), "t.csv",
                                          account_name=BROKER)
    assert preview["auto_linked_securities"] == []
    sug = preview["unmatched_securities"][0]["suggestions"]
    assert sug[0]["price_verdict"] == "match" and sug[0]["price_checked"] == 1


def test_auto_link_is_skipped_when_two_securities_share_the_price(twin_funds):
    """同じ価格系列の銘柄が二重に登録されているときは、どちらかを勝手に選ばない。"""
    store, ids = twin_funds
    for i, day in enumerate(("2026-01-05", "2026-02-10", "2026-03-03")):
        store.upsert_daily_price("manual", str(ids["株式"]), day,
                                 Decimal(str(11000 + i * 100)))
    preview = tx_service.build_tx_preview(store, _fund_csv(*BUY_ROWS), "t.csv",
                                          account_name=BROKER)
    assert preview["auto_linked_securities"] == []
    entry = preview["unmatched_securities"][0]
    assert len(entry["ambiguous"]) == 2
    assert any("複数" in w for w in preview["warnings"])


def test_manual_choice_overrides_an_auto_link(twin_funds):
    """自動で結びつけた先が違えば選び直せる。根拠を見せる以上、解除できないと困る。"""
    store, ids = twin_funds
    preview = tx_service.build_tx_preview(store, _fund_csv(*BUY_ROWS), "t.csv",
                                          account_name=BROKER)
    out = tx_service.remap_tx_preview(
        store, preview["batch_id"],
        security_map={"架空全世界ファンド": ids["株式"]},
    )
    assert all(r["security_id"] == ids["株式"] for r in out["rows"])
    assert all(r["matched_by"] == "manual" for r in out["rows"])
    assert out["auto_linked_securities"] == []


def test_mapping_to_zero_undoes_an_auto_link(twin_funds):
    """「取り込まない」を選んだら自動紐付けも外れる。"""
    store, _ids = twin_funds
    preview = tx_service.build_tx_preview(store, _fund_csv(*BUY_ROWS), "t.csv",
                                          account_name=BROKER)
    out = tx_service.remap_tx_preview(
        store, preview["batch_id"], security_map={"架空全世界ファンド": 0},
    )
    assert all(r["security_id"] is None for r in out["rows"])
    assert not any(r["included"] for r in out["rows"])
    assert out["auto_linked_securities"] == [] and out["unmatched_securities"] == []


def _twin_at_other_broker(store: Store) -> int:
    """債券ファンドと価格系列がまったく同じ銘柄を、別の証券会社の保有として作る。

    同じファンドを 2 社で持つと、MF PDF が証券会社ごとの表記をそのまま書くため
    銘柄が別々に登録される。価格が同一なので価格だけでは区別できない。
    """
    other = store.get_or_create_account("架空証券B", kind="broker")
    name = "架空全世界債券ファンドB"
    sid = store.create_security(
        Security(name=name, name_key=name, asset_class=AssetClass.FUND_JP,
                 unit=Unit.KUCHI, price_unit_divisor=10000)
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=other.id, security_id=sid,
                        as_of_date=date(2026, 8, 1), quantity=Decimal("10000"),
                        avg_cost=Decimal("11000"), origin="mf")
    )
    for i, day in enumerate(("2026-01-05", "2026-02-10", "2026-03-03")):
        store.upsert_daily_price("manual", str(sid), day, Decimal(str(11000 + i * 100)))
    return sid


def test_the_account_decides_between_securities_with_the_same_prices(twin_funds):
    """価格が同じ候補が並んだら、取込先の口座が持っているほうを選ぶ。

    実例: 同じ ISIN の投信が、証券会社ごとの表記で 2 銘柄に分かれて
    登録されていた。取引はどれか 1 つの口座のものなので口座が決め手になる。
    """
    store, ids = twin_funds
    _twin_at_other_broker(store)
    preview = tx_service.build_tx_preview(store, _fund_csv(*BUY_ROWS), "t.csv",
                                          account_name=BROKER)
    linked = preview["auto_linked_securities"]
    assert len(linked) == 1
    assert linked[0]["auto_linked"]["security_id"] == ids["債券"]
    assert linked[0]["auto_linked"]["account_match"] is True
    assert all(r["security_id"] == ids["債券"] for r in preview["rows"])


def test_the_account_cannot_decide_when_it_holds_both(twin_funds):
    """同じ口座に両方あるなら決め手が無い。勝手に選ばず利用者に渡す。"""
    store, ids = twin_funds
    for i, day in enumerate(("2026-01-05", "2026-02-10", "2026-03-03")):
        store.upsert_daily_price("manual", str(ids["株式"]), day,
                                 Decimal(str(11000 + i * 100)))
    preview = tx_service.build_tx_preview(store, _fund_csv(*BUY_ROWS), "t.csv",
                                          account_name=BROKER)
    assert preview["auto_linked_securities"] == []
    assert len(preview["unmatched_securities"][0]["ambiguous"]) == 2
# ----------------------------------------------------------------------
# ファイルが示す残高
#
# 「売り切った銘柄」と「名前が違うだけでまだ持っている銘柄」を分ける唯一の
# 手がかり。実データでは残高の残る銘柄が候補なしに埋もれており、まとめて
# 売却済み登録すると保有中の銘柄を二重に作るところだった。
# ----------------------------------------------------------------------

UNKNOWN = "謎のファンド"


def _entry(preview, name=UNKNOWN):
    return next(e for e in preview["unmatched_securities"] if e["name"] == name)


def test_a_fully_sold_security_reports_a_closed_position(twin_funds):
    store, _ids = twin_funds
    data = _fund_csv(
        "2026/01/05," + UNKNOWN + ",買付,10000,11000,11000,0,11000",
        "2026/02/10," + UNKNOWN + ",売却,10000,11100,11100,0,11100",
    )
    e = _entry(tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER))
    assert e["net_quantity"] == "0"
    assert e["closed_out"] is True
    assert e["undetermined"] == 0
    assert (e["first_date"], e["last_date"]) == ("2026-01-05", "2026-02-10")


def test_a_still_held_security_reports_the_remaining_quantity(twin_funds):
    """残っているなら売却済みとして登録してはいけない。名前違いの可能性が高い。"""
    store, _ids = twin_funds
    data = _fund_csv(
        "2026/01/05," + UNKNOWN + ",買付,10000,11000,11000,0,11000",
        "2026/02/10," + UNKNOWN + ",売却,4000,11100,4440,0,4440",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    e = _entry(preview)
    assert e["net_quantity"] == "6000"
    assert e["closed_out"] is False
    held_warning = next(w for w in preview["warnings"] if "残高" in w)
    # どれのことか分からない警告では調べ始められない。名前と残数を書く。
    assert UNKNOWN in held_warning and "6000" in held_warning
    # 判断の要る行は結びつけ表の先頭に出す
    assert preview["unmatched_securities"][0]["name"] == UNKNOWN


def test_undetermined_rows_leave_the_balance_unknown(twin_funds):
    """区分が分からない行があるうちは残高を出さない。誤った数字は判断を誤らせる。"""
    store, _ids = twin_funds
    data = _fund_csv(
        "2026/01/05," + UNKNOWN + ",買付,10000,11000,11000,0,11000",
        "2026/02/10," + UNKNOWN + ",,4000,11100,4440,0,4440",
    )
    e = _entry(tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER))
    assert e["undetermined"] == 1
    assert e["closed_out"] is False


def test_transfers_out_count_towards_the_balance(twin_funds):
    """出庫は保有を減らす。数えないと「まだ持っている」と誤表示する。"""
    store, _ids = twin_funds
    data = _fund_csv(
        "2026/01/05," + UNKNOWN + ",買付,10000,11000,11000,0,11000",
        "2026/02/10," + UNKNOWN + ",出庫,10000,11100,111000,0,111000",
    )
    e = _entry(tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER))
    assert e["net_quantity"] == "0" and e["closed_out"] is True

def test_holdings_the_file_cannot_explain_are_listed(twin_funds):
    """スナップショットのうち CSV が説明できていない保有を出す。

    DESIGN の「スナップショットを錨に、CSV で説明できる分を差し引く」を銘柄の
    照合にも使う。残った保有は CSV のどれかの名前が指しているはずで、全銘柄から
    選ばせる代わりにここから選べばよくなる。
    """
    store, ids = twin_funds
    data = _fund_csv("2026/01/05,架空全世界ファンド,買付,10000,11000,11000,0,11000")
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    names = {h["name"] for h in preview["unexplained_holdings"]}
    # 債券は名前が結びつかないままなので未説明。株式も CSV に出てこないので未説明。
    assert "架空全世界債券ファンド" in names
    assert "架空全世界株式ファンド" in names


def test_a_unique_quantity_match_links_an_unexplained_holding(twin_funds):
    """名前も価格も決め手にならないとき、未説明の保有と同じ増減なら結びつける。

    実データでは略称と正式名称の組がこれで決まった。
    名前の類似度は 0.43 で候補にすら出ていなかった。
    """
    store, _ids = twin_funds
    account = store.get_or_create_account(BROKER, kind="broker")
    name = "架空ゴールド上場信託"
    sid = store.create_security(
        Security(name=name, name_key=name, asset_class=AssetClass.STOCK_JP)
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=account.id, security_id=sid,
                        as_of_date=date(2026, 8, 1), quantity=Decimal("777"),
                        avg_cost=Decimal("5000"), origin="mf")
    )
    # 名前が似ておらず価格も手元に無い。数量 777 だけが決め手になる。
    data = _fund_csv("2020/06/01,ぜんぜん違う名前,買付,777,9000,6993000,0,6993000")
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    linked = [e for e in preview["auto_linked_securities"]
              if e["name"] == "ぜんぜん違う名前"]
    assert len(linked) == 1
    assert linked[0]["auto_linked"]["security_id"] == sid
    assert linked[0]["auto_linked"]["quantity_match"] == "777"


def test_an_ambiguous_quantity_does_not_link(twin_funds):
    """同じ数量の未説明保有が 2 つあれば決め手にならない。"""
    store, _ids = twin_funds     # 株式・債券とも 10000 口で同数
    data = _fund_csv("2020/06/01,ぜんぜん違う名前,買付,10000,9000,9000,0,9000")
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    assert preview["auto_linked_securities"] == []

def test_cash_holdings_are_not_listed_as_unexplained(twin_funds):
    """現金・預金は取引履歴に銘柄として現れない。「未対応」と出しても
    対応する行が無く、利用者を迷わせるだけ。"""
    store, _ids = twin_funds
    account = store.get_or_create_account(BROKER, kind="broker")
    cash = store.create_security(
        Security(name="現金・預金", name_key="現金・預金", asset_class=AssetClass.CASH)
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=account.id, security_id=cash,
                        as_of_date=date(2026, 8, 1), quantity=Decimal("84151"),
                        avg_cost=None, origin="mf")
    )
    data = _fund_csv("2026/01/05,架空全世界ファンド,買付,10000,11000,11000,0,11000")
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    assert all(h["name"] != "現金・預金" for h in preview["unexplained_holdings"])


def test_an_initial_subscription_is_a_buy(twin_funds):
    """『募集』は新規設定ファンドの当初申込＝買付。

    実データにも募集 1 行だけの銘柄があり、語彙に無いと未判別になって、
    増減も数量照合も価格照合も全部働かなかった。
    """
    store, _ids = twin_funds
    account = store.get_or_create_account(BROKER, kind="broker")
    name = "架空新設ファンド"
    sid = store.create_security(
        Security(name=name, name_key=name, asset_class=AssetClass.FUND_JP,
                 unit=Unit.KUCHI, price_unit_divisor=10000)
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=account.id, security_id=sid,
                        as_of_date=date(2026, 8, 1), quantity=Decimal("50000"),
                        avg_cost=Decimal("10000"), origin="mf")
    )
    data = _fund_csv("2026/03/05,オルカンぽい略称,募集,50000,10000,50000,0,50000")
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    linked = [e for e in preview["auto_linked_securities"]
              if e["name"] == "オルカンぽい略称"]
    assert len(linked) == 1
    assert linked[0]["auto_linked"]["security_id"] == sid
    assert linked[0]["auto_linked"]["quantity_match"] == "50000"


def test_type_override_cannot_rewrite_a_margin_trade(seeded):
    """まとめて指定が効くのは取引区分が空欄だった行だけ。

    信用取引は保有数を壊すので対象外にしてある。override で現物の買付に
    書き換えられると、その安全策が画面のボタン 1 つで無効になる
    （実データでは数百行の空欄と一緒に、信用の数百行が化けるところだった）。
    """
    store, _a, _s = seeded
    data = _csv(
        ROW_1,
        "2026/04/01,2026/04/03,1234,架空商事,信用新規買い,100,2000,200000,0,200000,特定\n",
        "2026/05/01,2026/05/03,1234,架空商事,,50,2100,105000,0,105000,特定\n",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    margin_row = next(r for r in preview["rows"] if (r["raw"] or {}).get("margin"))
    blank_row = next(r for r in preview["rows"]
                     if r["tx_type"] == "other" and not (r["raw"] or {}).get("tx_type_raw"))
    overrides = {margin_row["dedup_key"]: "buy", blank_row["dedup_key"]: "buy"}
    keys = [r["dedup_key"] for r in preview["rows"]]
    result = tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER,
        include_keys=keys, type_overrides=overrides,
    )
    kinds = [(t.trade_date.isoformat(), t.tx_type.value) for t in store.list_transactions()]
    # 空欄の行は買付として入る。信用の行は明示的に選ばれれば記録としては
    # 入るが、override は効かず other のまま（other は原価計算に対して不活性）。
    assert ("2026-05-01", "buy") in kinds
    assert ("2026-04-01", "other") in kinds
    assert ("2026-04-01", "buy") not in kinds

def test_preview_carries_the_accounts_current_holdings(twin_funds):
    """結びつけ先が今も保有している銘柄かどうかを画面で見せるための対応表。

    保有中の銘柄に結びつけるのか、売却済みとして登録するのかは判断が
    逆方向なので、これが無いと利用者は 1 件ずつ思い出すことになる。
    """
    store, ids = twin_funds
    preview = tx_service.build_tx_preview(store, _fund_csv(*BUY_ROWS), "t.csv",
                                          account_name=BROKER)
    held = preview["held_quantities"]
    assert held[str(ids["債券"])] == "10000"
    assert held[str(ids["株式"])] == "10000"

    # 口座を指定しなければ空（誤った口座の保有を見せない）
    anon = tx_service.build_tx_preview(store, _fund_csv(BUY_ROWS[0]), "t2.csv")
    assert anon["held_quantities"] == {}

def test_names_sharing_a_file_code_merge_into_one_entry(twin_funds):
    """同じ銘柄コードを共有する名前は同一銘柄として 1 エントリに束ねる。

    実例: 運用会社の変更で 3 代にわたり改称されたファンドが、2 つの
    コードで数珠つなぎになっていた。束ねないと増減が +120,000 / +20,000 / -140,000 に割れて見え、
    売り切ったことが読み取れない。
    """
    store, _ids = twin_funds
    data = _csv(
        "2010/01/05,2010/01/07,90010,旧称ファンド,買付,100,1000,100000,0,100000,特定" + chr(10),
        "2015/06/01,2015/06/03,90010,中間の名前,買付,50,1200,60000,0,60000,特定" + chr(10),
        "2020/03/01,2020/03/03,91230,中間の名前,買付,30,1300,39000,0,39000,特定" + chr(10),
        "2024/09/01,2024/09/03,91230,最新の名前,売却,180,1500,270000,0,270000,特定" + chr(10),
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    merged = [e for e in preview["unmatched_securities"] if e.get("aliases")]
    assert len(merged) == 1
    e = merged[0]
    assert e["name"] == "最新の名前"                    # 最後に取引された名前が代表
    assert e["aliases"] == ["中間の名前", "旧称ファンド"]
    assert e["count"] == 4
    assert e["net_quantity"] == "0" and e["closed_out"] is True
    # 他の名前のエントリは残らない
    names = {x["name"] for x in preview["unmatched_securities"]}
    assert "旧称ファンド" not in names and "中間の名前" not in names


def test_registering_a_merged_entry_as_sold_creates_one_security(twin_funds):
    """束ねたエントリを売却済み登録すると、旧称の行も含めて 1 銘柄になる。"""
    store, _ids = twin_funds
    before = len(store.list_securities())
    data = _csv(
        "2010/01/05,2010/01/07,90010,旧称ファンド,買付,100,1000,100000,0,100000,特定" + chr(10),
        "2024/09/01,2024/09/03,90010,最新の名前,売却,100,1500,150000,0,150000,特定" + chr(10),
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    keys = [r["dedup_key"] for r in preview["rows"]]
    tx_service.commit_tx_batch(
        store, preview["batch_id"], account_name=BROKER,
        include_keys=keys, new_securities=["最新の名前"],
    )
    assert len(store.list_securities()) == before + 1
    sids = {t.security_id for t in store.list_transactions()}
    assert len(sids) == 1                               # 旧称の行も同じ銘柄に付いた

def test_zero_movement_rows_are_hidden_from_the_preview(twin_funds):
    """増減ゼロの行はプレビューに出さない（利用者が見ても判断することが無い）。"""
    store, _ids = twin_funds
    data = _csv(
        "2026/01/05,2026/01/07,1234,架空商事,買付,100,2000,200000,0,200000,特定" + chr(10),
        "2026/02/01,2026/02/01,99950,日興ＭＲＦ,ＭＲＦ再投資,0,0,0,0,0,特定" + chr(10),
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    assert len(preview["rows"]) == 1


def test_zero_rows_still_connect_renamed_securities(twin_funds):
    """ゼロ行の銘柄コードは改称の数珠つなぎの証拠として使う。

    実データでは改称前後をつなぐ十数行がすべてゼロ額の分配金行で、
    パース段階で捨てると 3 代にわたる改称の 3 名が 1 銘柄に畳めなかった。
    """
    store, _ids = twin_funds
    data = _csv(
        "2010/01/05,2010/01/07,90010,旧称ファンド,買付,100,1000,100000,0,100000,特定" + chr(10),
        # 中間の名前は旧コードで取引し、新コードはゼロ額の分配金行にしか現れない
        "2015/06/01,2015/06/03,90010,中間の名前,買付,100,1200,120000,0,120000,特定" + chr(10),
        "2018/01/10,2018/01/10,91230,中間の名前,分配金,0,0,0,0,0,特定" + chr(10),
        "2024/09/01,2024/09/03,91230,最新の名前,売却,200,1500,300000,0,300000,特定" + chr(10),
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    merged = [e for e in preview["unmatched_securities"] if e.get("aliases")]
    assert len(merged) == 1
    assert merged[0]["name"] == "最新の名前"
    assert merged[0]["aliases"] == ["中間の名前", "旧称ファンド"]
    assert merged[0]["closed_out"] is True

def test_notices_split_reports_from_things_that_need_a_decision(twin_funds):
    """警告には 2 種類ある — 判断が要るもの（action）と報告（info）。

    全部を同じ顔で並べると、対処の要る数件が報告の中に埋もれる。warnings
    （文字列の配列）は既存の契約のまま残し、重み付きの notices を併設する。
    """
    store, _ids = twin_funds
    data = _fund_csv(
        *BUY_ROWS,
        "2026/04/01,謎の新顔ファンド,買付,1000,9000,900,0,900",
        "2026/04/02,,ご入金,0,0,50000",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    notices = preview["notices"]
    assert [n["text"] for n in notices] == preview["warnings"]   # 中身と順序は同じ
    levels = {n["text"][:12]: n["level"] for n in notices}
    assert any(t.startswith("既存の銘柄に結びつかない") and lv == "action"
               for t, lv in ((n["text"], n["level"]) for n in notices))
    assert any(t.startswith("入出金の行が") and lv == "info"
               for t, lv in ((n["text"], n["level"]) for n in notices))
    assert any(t.startswith("約定単価が既存銘柄") and lv == "info"
               for t, lv in ((n["text"], n["level"]) for n in notices))

def test_price_refuted_candidates_do_not_block_bulk_registration(twin_funds):
    """同日の価格が食い違う候補は別銘柄と確定しており、「候補がある」と数えない。

    数えると、白黒ついているのに ±0 の売却済み銘柄が一括登録から外れ、
    個別判断を求められる。
    """
    store, _ids = twin_funds
    # 名前は両ファンドに似ているが、価格が両方と食い違う売り切りの銘柄
    data = _fund_csv(
        "2026/01/05,架空全世界ファンド,買付,10000,5000,5000,0,5000",
        "2026/02/10,架空全世界ファンド,買付,10000,5100,5100,0,5100",
        "2026/03/03,架空全世界ファンド,売却,20000,5200,10400,0,10400",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    e = preview["unmatched_securities"][0]
    assert e["closed_out"] is True
    assert e["suggestions"] and all(
        s["price_verdict"] == "mismatch" for s in e["suggestions"]
    )
    linkable = next(w for w in preview["warnings"] if "結びつかない" in w)
    assert "1 銘柄は増減 ±0・候補なし" in linkable

def test_asset_class_words_refute_brand_only_candidates(twin_funds):
    """資産クラス・地域の語が食い違う候補は別物。ファンドは債券から株式へ
    改名しない。ブランド名（eMAXIS 等）の一致だけで候補に立った別物を落とす。"""
    from asset_summary.importers.tx_service import _category_conflict

    assert _category_conflict("eMAXIS 国内債券", "eMAXIS Slim国内株式(TOPIX)")
    assert _category_conflict("STAM新興国株式インデックス",
                              "SBI・全世界株式インデックス・ファンド")
    assert _category_conflict("eMAXIS 先進国株式", "eMAXIS Slim米国株式(S&P500)")
    # 同じ軸で食い違っていないものは否定しない
    assert not _category_conflict("eMASlimオールカントリー",
                                  "eMAXIS Slim全世界株式(オール・カントリー)")
    assert not _category_conflict("野村インデックスF外国REIT",
                                  "野村インデックスファンド・外国REIT")
    # 地域（チャイナ）と資産クラス（リート）は軸が違うので断定できない
    assert not _category_conflict("三井住友ニューチャイナファンド",
                                  "三井住友・DC外国リートインデックスファンド")


def test_category_conflict_candidates_do_not_block_bulk(twin_funds):
    """カテゴリで否定された候補しか無い ±0 の銘柄は、一括登録の対象に入る。"""
    store, _ids = twin_funds
    # 「架空全世界債券ファンド」…twin_funds の債券と名前が似るが、
    # こちらは株式。価格の重なる日は無い（2020 年の取引）。
    data = _fund_csv(
        "2020/06/01,架空全世界株式ファンドS,買付,10000,9000,9000,0,9000",
        "2020/07/01,架空全世界株式ファンドS,売却,10000,9100,9100,0,9100",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    e = preview["unmatched_securities"][0]
    assert e["closed_out"] is True
    # 債券側の候補はカテゴリ違いで否定されている
    refuted = [s for s in e["suggestions"] if s.get("category_conflict")]
    assert any("債券" in s["name"] for s in refuted)

def test_shared_brand_and_generic_words_do_not_make_a_candidate(twin_funds):
    """ブランド名と一般語を除いた識別部分で最終判定する。

    「三井住友ニューチャイナファンド」と「三井住友・DC外国リート
    インデックスファンド」の類似度 0.56 は、共有する「三井住友」と、
    どれにでも付く「ファンド」が持ち上げているだけ。識別部分
    （ニューチャイナ / DC外国リート）はまるで違う（0.14）。

    ただし名前の判定は価格の裏取りより弱い。Smart-iゴールドFHなし は
    識別部分の字面（FH ⇔ 為替ヘッジ）が違うが、価格一致で同一と確定して
    いる — 名前で先に落とすと、こういう略称を持つ同一ファンドを失う。
    """
    from asset_summary.importers.tx_service import _refined_name_score

    assert _refined_name_score(
        "三井住友ニューチャイナファンド",
        "三井住友・DC外国リートインデックスファンド", 0.56) < 0.5
    assert _refined_name_score(
        "野村インデックスF外国REIT", "野村インデックスファンド・外国REIT", 0.85) >= 0.5
    assert _refined_name_score(
        "eMASlimオールカントリー", "eMAXIS Slim全世界株式(オール・カントリー)", 0.79) >= 0.5
    # 一方が他方の接頭辞に飲み込まれるときは元の類似度を保つ（略称と正式名称）
    assert _refined_name_score("ABC", "ABCホールディングス", 0.43) == 0.43


def test_name_refutation_yields_to_price_evidence(twin_funds):
    """識別部分が違っても、価格が一致した候補は落とさない（略称の同一ファンド）。"""
    store, ids = twin_funds
    # 債券と価格が 3 日一致する取引。名前の識別部分は字面がまるで違う。
    data = _fund_csv(
        "2026/01/05,架空全世界ZS,買付,10000,11000,11000,0,11000",
        "2026/02/10,架空全世界ZS,買付,10000,11100,11100,0,11100",
        "2026/03/03,架空全世界ZS,買付,10000,11200,11200,0,11200",
    )
    preview = tx_service.build_tx_preview(store, data, "t.csv", account_name=BROKER)
    linked = preview["auto_linked_securities"]
    assert len(linked) == 1 and linked[0]["auto_linked"]["security_id"] == ids["債券"]
