"""口座間の移管（証券会社を移した銘柄）の判定と、1銘柄の推移への反映。

移管元の取引履歴の出庫（またはスナップショットで保有が 0 になった記録）と、
移管先の口座に最初に現れた記録を結び付ける。値はすべて架空。
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import asset_summary.web.app as web_app
from asset_summary.core import cost_basis, transfers
from asset_summary.core.models import (
    AssetClass,
    HoldingSnapshot,
    ImportBatch,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Transaction,
    TxType,
    Unit,
)

D = Decimal
A, B, C, SEC = 1, 2, 3, 10          # 移管元 / 1 回目の移管先 / 2 回目の移管先


def _tx(acct, day, tx_type, qty, price=None, sec=SEC):
    return Transaction(
        account_id=acct, security_id=sec, trade_date=date.fromisoformat(day),
        tx_type=tx_type, quantity=D(qty), unit_price=D(price) if price else None,
    )


def _snap(acct, day, qty, cost="1200", sec=SEC, lot=0):
    return HoldingSnapshot(
        account_id=acct, security_id=sec, lot_seq=lot, as_of_date=date.fromisoformat(day),
        quantity=D(qty), avg_cost=D(cost) if cost else None,
    )


# 移管元の取引履歴。買付と、分割ぶんの入庫のあと、貸株の出し入れと移管の出庫が
# 同じ日に並ぶ（差し引きで保有がすべて出ていく）
LEDGER_A = [
    _tx(A, "2019-03-07", TxType.BUY, "100", "2400"),
    _tx(A, "2025-03-28", TxType.TRANSFER_IN, "100", "1200"),       # 分割を入庫で記録
    _tx(A, "2025-12-23", TxType.TRANSFER_OUT, "100", "1200"),
    _tx(A, "2025-12-23", TxType.TRANSFER_IN, "100", "1200"),
    _tx(A, "2025-12-23", TxType.TRANSFER_OUT, "100", "1200"),
    _tx(A, "2025-12-23", TxType.TRANSFER_OUT, "100", "1200"),
    _tx(A, "2025-12-23", TxType.TRANSFER_IN, "100", "1200"),
    _tx(A, "2025-12-23", TxType.TRANSFER_OUT, "100", "1200"),
]


def _route(res):
    return [(link.from_account_id, link.to_account_id, link.date.isoformat(),
             link.quantity, link.origin) for link in res.links]


# ----------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------


def test_a_transfer_out_links_to_where_the_holding_first_appears():
    res = transfers.resolve(LEDGER_A, [_snap(B, "2026-08-04", "200")])
    assert _route(res) == [(A, B, "2025-12-23", D("200"), "auto")]
    link = res.links[0]
    assert link.source == "ledger"
    assert link.cost_match is True       # 出庫行の単価（引き継いだ取得単価）と一致
    assert res.unresolved == []


def test_a_chain_of_transfers_links_each_hop():
    """A → B → C。B の保有が 0 になった日に C に同じ数量が現れる。

    C も A の移管先の候補になるが、先に現れた B が A の移管先で、C は B からの移管。
    """
    snaps = [_snap(B, "2026-08-04", "200"), _snap(B, "2026-09-01", "0"),
             _snap(C, "2026-09-01", "200")]
    res = transfers.resolve(LEDGER_A, snaps)
    assert _route(res) == [
        (A, B, "2025-12-23", D("200"), "auto"),
        (B, C, "2026-09-01", D("200"), "auto"),
    ]
    assert res.links[1].source == "snapshot"


def test_a_move_through_an_account_without_records_links_to_the_last_one():
    """途中の口座（B）に記録が無ければ、A から最後の口座（C）へ直接つなぐ。

    1銘柄の保有数は口座を問わず足すので、推移はこれで正しい。
    """
    res = transfers.resolve(LEDGER_A, [_snap(C, "2026-09-01", "200")])
    assert _route(res) == [(A, C, "2025-12-23", D("200"), "auto")]


def test_moves_inside_the_account_are_not_transfers():
    """NISA → 特定の払出（別の日の入庫で戻る）と貸株の出し入れは口座の中の移動。"""
    txs = [
        _tx(A, "2020-01-10", TxType.BUY, "100", "1000"),
        _tx(A, "2023-12-29", TxType.TRANSFER_OUT, "100", "3000"),
        _tx(A, "2024-01-04", TxType.TRANSFER_IN, "100", "3000"),
        _tx(A, "2024-06-10", TxType.TRANSFER_OUT, "100", "3200"),
        _tx(A, "2024-06-10", TxType.TRANSFER_IN, "100", "3200"),
    ]
    assert transfers.ledger_transfer_outs(txs) == []


def test_a_reverse_split_recorded_as_out_and_in_is_not_asked_about():
    """出庫 100 と入庫 10（株式併合）。口座に保有が残り、数量の合う候補も無い。"""
    txs = [
        _tx(A, "2019-05-10", TxType.BUY, "100", "170"),
        _tx(A, "2020-09-29", TxType.TRANSFER_OUT, "100", "170"),
        _tx(A, "2020-09-29", TxType.TRANSFER_IN, "10", "1700"),
    ]
    res = transfers.resolve(txs, [_snap(B, "2026-08-04", "300")])
    assert res.links == [] and res.unresolved == []


def test_two_accounts_appearing_together_are_left_to_the_user():
    snaps = [_snap(B, "2026-08-04", "200"), _snap(C, "2026-08-04", "200")]
    res = transfers.resolve(LEDGER_A, snaps)
    assert res.links == []
    (out, cands), = res.unresolved
    assert out.quantity == D("200") and out.closed
    assert [c.arrival.account_id for c in cands] == [B, C]
    assert all(c.quantity_match for c in cands)


def test_a_different_unit_cost_means_it_was_bought_again_not_moved():
    """両方の取得単価が分かっていて食い違えば、自動では結び付けず候補として出す。"""
    res = transfers.resolve(LEDGER_A, [_snap(B, "2026-08-04", "200", cost="2600")])
    assert res.links == []
    (_out, cands), = res.unresolved
    assert cands[0].quantity_match and cands[0].cost_match is False


def test_an_unknown_carried_cost_does_not_block_the_link():
    """出庫行の単価が 0（取得単価不明）なら比べられないだけで、結び付けは妨げない。"""
    txs = [_tx(A, "2018-05-29", TxType.TRANSFER_IN, "300", None),
           _tx(A, "2025-12-23", TxType.TRANSFER_OUT, "300", None)]
    res = transfers.resolve(txs, [_snap(B, "2026-08-04", "300", cost="900")])
    assert _route(res) == [(A, B, "2025-12-23", D("300"), "auto")]
    assert res.links[0].cost_match is None


def test_a_holding_that_starts_with_a_buy_is_not_a_transfer():
    """移管先の候補の取引履歴が買付で始まるなら、それは買ったもの。"""
    txs = LEDGER_A + [_tx(B, "2026-01-15", TxType.BUY, "200", "2500")]
    res = transfers.resolve(txs, [_snap(B, "2026-08-04", "200")])
    assert res.links == []
    (_out, cands), = res.unresolved
    assert cands[0].arrival.kind == "buy"


def test_a_snapshot_drop_without_a_matching_arrival_is_just_a_sale():
    """保有が 0 になっただけでは売却と区別できないので、何も問い合わせない。"""
    snaps = [_snap(B, "2026-08-04", "200"), _snap(B, "2026-09-01", "0")]
    res = transfers.resolve([], snaps)
    assert res.links == [] and res.unresolved == []


def test_manual_decisions_override_the_automatic_ones():
    snaps = [_snap(B, "2026-08-04", "200"), _snap(C, "2026-08-04", "200")]
    manual = [{"id": 7, "security_id": SEC, "from_account_id": A, "to_account_id": C,
               "transfer_date": date(2025, 12, 23), "quantity": D("200")}]
    res = transfers.resolve(LEDGER_A, snaps, manual)
    assert _route(res) == [(A, C, "2025-12-23", D("200"), "manual")]
    assert res.links[0].manual_id == 7
    # 付け替え用に候補は残る（選んだ C も含めて）
    assert {c.arrival.account_id for c in res.candidates[(SEC, A, date(2025, 12, 23))]} == {B, C}

    dismissed = [{**manual[0], "to_account_id": None}]
    res = transfers.resolve(LEDGER_A, snaps, dismissed)
    assert res.links == [] and res.unresolved == []
    (out, decision), = res.dismissed
    assert out is not None and decision.id == 7


# 移管先の証券会社が、移管で受け入れたロットを元の取得日の日付で入庫として記録する
# （楽天証券の取引履歴がこう出す）。移管より前の記録は入庫だけ
BACKDATED_B = [
    _tx(B, "2019-03-07", TxType.TRANSFER_IN, "100", "1200"),
    _tx(B, "2025-03-28", TxType.TRANSFER_IN, "100", "1200"),
]


def test_transfer_ins_dated_at_acquisition_are_the_transfer_arriving():
    res = transfers.resolve(LEDGER_A + BACKDATED_B, [_snap(B, "2026-08-04", "200")])
    assert _route(res) == [(A, B, "2025-12-23", D("200"), "auto")]
    (cand,) = res.candidates[(SEC, A, date(2025, 12, 23))]
    assert cand.arrival.backdated and not cand.early and cand.cost_match
    assert set(res.links[0].absorbed) == {transfers.tx_key(t) for t in BACKDATED_B}


def test_other_records_before_the_transfer_mean_it_was_already_held():
    """移管より前に配当（や買付・記録）があれば、移管先の口座は前から持っていた。"""
    dividend = Transaction(account_id=B, security_id=SEC, trade_date=date(2022, 6, 20),
                           tx_type=TxType.DIVIDEND, net_amount=D("3000"))
    res = transfers.resolve(LEDGER_A + BACKDATED_B + [dividend], [_snap(B, "2026-08-04", "200")])
    assert res.links == []
    (_out, (cand,)), = res.unresolved
    assert cand.early and not cand.arrival.backdated


def test_a_chain_through_a_broker_that_dates_transfers_at_acquisition():
    """A → B（取得日の日付で入庫）→ C。B は今は持っていない（スナップショットが無い）。"""
    ledger_a = [
        _tx(A, "2025-11-10", TxType.BUY, "100", "971"),
        _tx(A, "2025-12-23", TxType.TRANSFER_OUT, "100", "971"),
    ]
    ledger_b = [
        _tx(B, "2025-11-10", TxType.TRANSFER_IN, "100", "971"),     # 取得日の日付
        _tx(B, "2026-07-16", TxType.TRANSFER_OUT, "100", "971"),
    ]
    snaps = [_snap(C, "2026-08-04", "100", cost="971")]
    txs = ledger_a + ledger_b
    res = transfers.resolve(txs, snaps)
    assert _route(res) == [
        (A, B, "2025-12-23", D("100"), "auto"),
        (B, C, "2026-07-16", D("100"), "auto"),
    ]
    paths = cost_basis.quantity_paths(txs, snaps, res.links)
    # B は取得日の日付の入庫ではなく、移管の日（12/23）から持っていたとして描く
    assert paths[(B, SEC)].at(date(2025, 12, 1)) == (D("0"), False)
    for day, want in (("2025-11-09", "0"), ("2025-11-10", "100"), ("2025-12-22", "100"),
                      ("2025-12-23", "100"), ("2026-07-15", "100"), ("2026-07-16", "100"),
                      ("2026-08-03", "100")):
        assert _held(paths, day) == D(want), day


# ----------------------------------------------------------------------
# 推移（保有数の経路）
# ----------------------------------------------------------------------


def _held(paths, day):
    """スナップショットより前（経路で描く期間）の保有数の合計。"""
    d = date.fromisoformat(day)
    return sum((p.at(d)[0] for p in paths.values() if d < p.before), D(0))


def test_the_chain_draws_one_continuous_line():
    snaps = [_snap(B, "2026-08-04", "200"), _snap(B, "2026-09-01", "0"),
             _snap(C, "2026-09-01", "200")]
    links = transfers.resolve(LEDGER_A, snaps).links
    paths = cost_basis.quantity_paths(LEDGER_A, snaps, links)
    # A は今は持っていない保有として取引履歴だけで全期間を描く
    assert paths[(A, SEC)].before == date.max
    assert _held(paths, "2019-03-06") == D("0")
    assert _held(paths, "2019-03-07") == D("100")
    assert _held(paths, "2025-03-28") == D("200")     # 分割
    assert _held(paths, "2025-12-22") == D("200")     # A
    assert _held(paths, "2025-12-23") == D("200")     # B へ移った日（二重に数えない）
    assert _held(paths, "2026-08-03") == D("200")     # B の最初の記録の前日
    # B・C は最初の記録から記録どおり。C の最初の記録より前は 0（B が持っていた）
    assert paths[(C, SEC)].at(date(2026, 8, 31)) == (D("0"), False)
    assert paths[(B, SEC)].at(date(2025, 12, 22)) == (D("0"), False)


def test_transfer_ins_dated_at_acquisition_are_counted_from_the_transfer():
    """取得日の日付の入庫をそのまま数えると、移管元の履歴と二重になる。"""
    snaps = [_snap(B, "2026-08-04", "200")]
    txs = LEDGER_A + BACKDATED_B
    links = transfers.resolve(txs, snaps).links
    paths = cost_basis.quantity_paths(txs, snaps, links)
    assert _held(paths, "2020-01-01") == D("100")      # A の 100 だけ（B の入庫は数えない）
    assert _held(paths, "2025-12-22") == D("200")
    assert _held(paths, "2025-12-23") == D("200")      # B に移った日
    assert paths[(B, SEC)].at(date(2025, 12, 22)) == (D("0"), False)


def test_a_linked_account_that_already_held_some_keeps_the_rest_as_carried_back():
    """移管先に移管前からの保有があれば、その分は遡った数量（破線）として残す。"""
    manual = [{"id": 1, "security_id": SEC, "from_account_id": A, "to_account_id": B,
               "transfer_date": date(2025, 12, 23), "quantity": D("200")}]
    snaps = [_snap(B, "2026-08-04", "300")]
    links = transfers.resolve(LEDGER_A, snaps, manual).links
    path = cost_basis.quantity_paths(LEDGER_A, snaps, links)[(B, SEC)]
    assert path.at(date(2025, 12, 22)) == (D("100"), True)
    assert path.at(date(2025, 12, 23)) == (D("300"), False)


def test_a_history_that_still_holds_without_records_is_not_drawn():
    """記録が無いのに取引履歴の上ではまだ持っている口座×銘柄は描かない（照合違いなど）。"""
    txs = [_tx(A, "2019-03-07", TxType.BUY, "100", "2400")]
    assert cost_basis.quantity_paths(txs, []) == {}
    assert cost_basis.quantity_path_status(txs, [], A, SEC) == "no_snapshot"
    sold = txs + [_tx(A, "2021-06-01", TxType.SELL, "100", "3000")]
    assert cost_basis.quantity_path_status(sold, [], A, SEC) == "closed"


# ----------------------------------------------------------------------
# Web
# ----------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = web_app.create_app(str(tmp_path / "t.db"))
    monkeypatch.setattr(web_app, "fetch_spot", lambda store, secs, warn=None: {})
    monkeypatch.setattr(
        web_app, "fetch_fx_rates", lambda store, ccys, warn=None: {c: D("150") for c in ccys}
    )
    monkeypatch.setattr(web_app, "ensure_price_history", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "ensure_fx_history", lambda *a, **k: None)
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def store(app):
    return app.state.store


@pytest.fixture()
def moved(store):
    """架空電機を A 証券で買い、B 証券へ移管。B は 2026-08-04 から記録がある。"""
    a = store.get_or_create_account("A証券", kind="broker")
    b = store.get_or_create_account("B証券", kind="broker")
    c = store.get_or_create_account("C証券", kind="broker")
    sec_id = store.create_security(
        Security(name="架空電機", name_key="かくうでんき", code="9990",
                 asset_class=AssetClass.STOCK_JP, unit=Unit.SHARE,
                 price_source_type=PriceSourceType.YAHOO, price_source_ref="9990.T",
                 price_source_status=PriceSourceStatus.LINKED)
    )
    store.create_batch(ImportBatch(id="ledger-a", source_kind="broker_csv"))
    store.insert_transactions(
        [
            Transaction(dedup_key=f"a{i}", account_id=a.id, security_id=sec_id,
                        trade_date=t.trade_date, tx_type=t.tx_type, quantity=t.quantity,
                        unit_price=t.unit_price)
            for i, t in enumerate(LEDGER_A)
        ],
        batch_id="ledger-a",
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=b.id, security_id=sec_id, as_of_date=date(2026, 8, 4),
                        quantity=D("200"), avg_cost=D("1200"), origin="mf")
    )
    for day, px in (("2019-03-07", "2400"), ("2025-03-28", "1300"), ("2026-08-03", "1500")):
        store.upsert_daily_price("yahoo", "9990.T", day, D(px), "JPY")
    return {"a": a.id, "b": b.id, "c": c.id, "sec": sec_id}


def test_security_history_joins_the_transfer(client, moved):
    data = client.get(
        "/api/portfolio-history",
        params={"range": "all", "scope": f"security:{moved['sec']}"},
    ).json()
    pts = {p["t"]: p for p in data["points"]}
    assert pts["2019-03-07"]["quantity"] == "100"          # A の買付
    assert pts["2025-12-22"]["quantity"] == "200"          # A（分割後）
    assert pts["2025-12-23"]["quantity"] == "200"          # B へ移った日
    assert pts["2026-08-03"]["quantity"] == "200"          # B の最初の記録の前日
    assert not any(p["backfilled"] for p in data["points"])  # 破線にならない
    assert D(pts["2025-12-23"]["value"]) == D("200") * D("1300")
    (move,) = data["transfers"]
    assert (move["from_account"], move["to_account"], move["date"]) == (
        "A証券", "B証券", "2025-12-23")
    assert move["origin"] == "auto" and move["quantity"] == "200"


def test_transfers_api_lists_links_and_lets_the_user_decide(client, store, moved):
    data = client.get("/api/transfers").json()
    (link,) = data["links"]
    assert (link["from_account"], link["to_account"], link["origin"]) == (
        "A証券", "B証券", "auto")
    assert link["cost_match"] is True
    assert data["unresolved"] == [] and data["dismissed"] == []

    # 「移管ではない」とすると結び付けが外れ、推移も B の最初の記録からの遡りに戻る
    event = {"security_id": moved["sec"], "from_account_id": moved["a"],
             "date": "2025-12-23", "quantity": "200"}
    res = client.post("/api/transfers", json={**event, "to_account_id": None})
    assert res.status_code == 200
    data = client.get("/api/transfers").json()
    assert data["links"] == []
    (dismissed,) = data["dismissed"]
    assert dismissed["found"] is True
    assert [c["account"] for c in dismissed["candidates"]] == ["B証券"]
    hist = client.get(
        "/api/portfolio-history",
        params={"range": "all", "scope": f"security:{moved['sec']}"},
    ).json()
    assert hist["transfers"] == []
    assert any(p["backfilled"] for p in hist["points"])

    # 元に戻すと自動の判定に戻る
    assert client.delete(f"/api/transfers/{dismissed['manual_id']}").status_code == 200
    assert client.get("/api/transfers").json()["links"][0]["origin"] == "auto"
    assert client.delete(f"/api/transfers/{dismissed['manual_id']}").status_code == 404


def test_transfers_api_validates_the_decision(client, moved):
    base = {"security_id": moved["sec"], "from_account_id": moved["a"],
            "date": "2025-12-23", "quantity": "200"}
    assert client.post("/api/transfers", json={**base, "to_account_id": moved["a"]}).status_code == 400
    assert client.post("/api/transfers", json={**base, "to_account_id": 9999}).status_code == 404
    assert client.post("/api/transfers", json={**base, "quantity": "0"}).status_code == 400
    assert client.post("/api/transfers", json={**base, "security_id": 9999}).status_code == 404


def test_merging_securities_keeps_the_decision(store, moved):
    other = store.create_security(
        Security(name="架空電機（旧名）", name_key="かくうでんききゅうめい",
                 asset_class=AssetClass.STOCK_JP, unit=Unit.SHARE)
    )
    store.set_transfer_link(security_id=other, from_account_id=moved["a"],
                            transfer_date=date(2025, 12, 23), quantity=D("200"),
                            to_account_id=moved["c"])
    store.merge_security(other, moved["sec"])
    (row,) = store.list_transfer_links()
    assert row["security_id"] == moved["sec"] and row["to_account_id"] == moved["c"]


def test_security_detail_marks_the_account_it_moved_out_of(client, moved):
    """移管元の口座の取得原価の行には移管先を添える（いまの保有の説明ではない）。"""
    assert client.post("/api/cost-basis/recompute").status_code == 200
    detail = client.get(f"/api/security/{moved['sec']}").json()
    (row,) = detail["cost_basis"]
    assert row["account"] == "A証券"
    assert (row["transferred_to"], row["transferred_on"]) == ("B証券", "2025-12-23")
    assert row["realized_pl"] in (None, "0")        # 移管は売却ではない
    assert not any(w["code"] == "CLOSED_POSITION" for w in row["warnings"])
    # 結び付いた移管は銘柄詳細の応答にも載る（口座別内訳の下に小さく出す。
    # 価格のグラフを表示していても出せるよう、推移の応答に頼らない）
    (move,) = detail["transfers"]
    assert (move["from_account"], move["to_account"], move["date"], move["origin"]) == (
        "A証券", "B証券", "2025-12-23", "auto")


def test_security_history_counts_backdated_transfer_ins_once(client, store, moved):
    """移管先の取引履歴が移管の入庫を取得日の日付で持っていても、二重に数えない。"""
    store.create_batch(ImportBatch(id="ledger-b", source_kind="broker_csv"))
    store.insert_transactions(
        [
            Transaction(dedup_key=f"b{i}", account_id=moved["b"], security_id=moved["sec"],
                        trade_date=t.trade_date, tx_type=t.tx_type, quantity=t.quantity,
                        unit_price=t.unit_price)
            for i, t in enumerate(BACKDATED_B)
        ],
        batch_id="ledger-b",
    )
    data = client.get(
        "/api/portfolio-history",
        params={"range": "all", "scope": f"security:{moved['sec']}"},
    ).json()
    pts = {p["t"]: p for p in data["points"]}
    assert pts["2020-01-01"]["quantity"] == "100"
    assert pts["2025-12-22"]["quantity"] == "200"
    assert pts["2025-12-23"]["quantity"] == "200"
    (move,) = data["transfers"]
    assert (move["from_account"], move["to_account"]) == ("A証券", "B証券")
    listed = client.get("/api/transfers").json()
    assert [c["backdated"] for c in listed["links"][0]["candidates"]] == [True]


@pytest.fixture()
def bought_more(store, moved):
    """A → B（取得日の日付で入庫）→ C。C は取引履歴が無く、最初の記録（2026-08-04）で
    移ってきた 100 より多い 200 を持つ（移管の後に C で買い足した）。"""
    sec_id = store.create_security(
        Security(name="架空商事", name_key="かくうしょうじ", code="9980",
                 asset_class=AssetClass.STOCK_JP, unit=Unit.SHARE,
                 price_source_type=PriceSourceType.YAHOO, price_source_ref="9980.T",
                 price_source_status=PriceSourceStatus.LINKED)
    )
    rows = [
        (moved["a"], "2020-05-01", TxType.BUY, "100"),
        (moved["a"], "2025-12-23", TxType.TRANSFER_OUT, "100"),
        (moved["a"], "2025-12-23", TxType.TRANSFER_IN, "100"),         # 貸株の戻し
        (moved["a"], "2025-12-23", TxType.TRANSFER_OUT, "100"),
        (moved["b"], "2020-05-01", TxType.TRANSFER_IN, "100"),         # 取得日の日付
        (moved["b"], "2026-07-16", TxType.TRANSFER_OUT, "100"),
    ]
    store.create_batch(ImportBatch(id="ledger-shoji", source_kind="broker_csv"))
    store.insert_transactions(
        [
            Transaction(dedup_key=f"s{i}", account_id=acct, security_id=sec_id,
                        trade_date=date.fromisoformat(day), tx_type=tx_type,
                        quantity=D(qty), unit_price=D("971"))
            for i, (acct, day, tx_type, qty) in enumerate(rows)
        ],
        batch_id="ledger-shoji",
    )
    store.upsert_snapshot(
        HoldingSnapshot(account_id=moved["c"], security_id=sec_id, as_of_date=date(2026, 8, 4),
                        quantity=D("200"), avg_cost=D("935"), origin="mf")
    )
    for day, px in (("2020-05-01", "971"), ("2026-08-03", "960")):
        store.upsert_daily_price("yahoo", "9980.T", day, D(px), "JPY")
    return {**moved, "sec": sec_id}


def _link_b_to_c(client, ids):
    return client.post("/api/transfers", json={
        "security_id": ids["sec"], "from_account_id": ids["b"], "date": "2026-07-16",
        "quantity": "100", "to_account_id": ids["c"],
    })


def test_security_detail_offers_the_undecided_transfer(client, bought_more):
    """移管先を自動で決められなかった移管元は、銘柄詳細にも候補付きで出る（その場で選べる）。"""
    ids = bought_more
    detail = client.get(f"/api/security/{ids['sec']}").json()
    (row,) = detail["transfer_unresolved"]
    assert (row["from_account"], row["date"], row["quantity"], row["source"]) == (
        "B証券", "2026-07-16", "100", "ledger")
    (cand,) = row["candidates"]
    assert (cand["account"], cand["quantity"], cand["quantity_match"], cand["cost_match"]) == (
        "C証券", "200", False, False)
    # 移管タブ（全銘柄）の同じ出来事と同じ形
    listed = client.get("/api/transfers").json()["unresolved"]
    assert [r for r in listed if r["security_id"] == ids["sec"]] == [row]

    assert _link_b_to_c(client, ids).status_code == 200
    assert client.get(f"/api/security/{ids['sec']}").json()["transfer_unresolved"] == []


def test_history_explains_what_the_destination_holds_beyond_the_transfer(client, bought_more):
    """移管先が移ってきた数量より多く持っていれば、残りは移管前から持っていた扱い（破線）で、
    そのことを知らせる。移管の後の買付を登録すると、その日から数える。"""
    ids = bought_more

    def history():
        data = client.get(
            "/api/portfolio-history",
            params={"range": "all", "scope": f"security:{ids['sec']}"},
        ).json()
        return data, {p["t"]: p for p in data["points"]}

    assert _link_b_to_c(client, ids).status_code == 200
    data, pts = history()
    assert (pts["2026-07-15"]["quantity"], pts["2026-07-15"]["backfilled"]) == ("200", True)
    assert (pts["2026-07-16"]["quantity"], pts["2026-07-16"]["backfilled"]) == ("200", False)
    assert data["transfer_remainders"] == [{
        "account_id": ids["c"], "account": "C証券", "quantity": "100",
        "first_date": "2026-08-04",
    }]

    # C は取引履歴をダウンロードできないので、買い足した分を手動で登録する
    res = client.post("/api/transactions", json={
        "account_id": ids["c"], "security_id": ids["sec"], "trade_date": "2026-07-25",
        "tx_type": "buy", "quantity": "100", "unit_price": "899",
    })
    assert res.status_code == 200
    data, pts = history()
    for day, want in (("2020-05-01", "100"), ("2025-12-23", "100"), ("2026-07-16", "100"),
                      ("2026-07-24", "100"), ("2026-07-25", "200"), ("2026-08-04", "200")):
        assert pts[day]["quantity"] == want, day
    assert not any(p["backfilled"] for p in data["points"])
    assert data["transfer_remainders"] == []


def test_an_undecided_transfer_is_not_offered_as_a_purchase(client, bought_more):
    """移管先を決めていないうちは、移管先の最初の記録を買付として登録するよう勧めない
    （移管が届いたものかもしれない）。移管ではないと決めたら、買ったものとして勧める。"""
    ids = bought_more

    def carried_back():
        return client.get(
            "/api/portfolio-history",
            params={"range": "all", "scope": f"security:{ids['sec']}"},
        ).json()["carried_back"]

    assert carried_back() == []                     # C は決まっていない移管の候補
    assert _link_b_to_c(client, ids).status_code == 200
    assert carried_back() == []                     # 移管先（残りは transfer_remainders で断る）
    res = client.post("/api/transfers", json={
        "security_id": ids["sec"], "from_account_id": ids["b"], "date": "2026-07-16",
        "quantity": "100", "to_account_id": None,
    })
    assert res.status_code == 200
    (row,) = carried_back()
    assert (row["account"], row["quantity"], row["until"], row["unit_price"]) == (
        "C証券", "200", "2026-08-04", "935")
