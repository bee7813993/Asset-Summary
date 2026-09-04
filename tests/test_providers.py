from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from asset_summary.core import price_history, price_store as ps, prices
from asset_summary.core.models import (
    AssetClass,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.core.providers import base, fx as fx_provider, metal, toushin, yahoo
from asset_summary.core.store import Store

D = Decimal


def d(s: str) -> date:
    return date.fromisoformat(s)


# ----------------------------------------------------------------------
# fixtures: MockTransport 注入・スリープ無効化
# ----------------------------------------------------------------------


@pytest.fixture()
def sleeps(monkeypatch) -> list[float]:
    """base._sleep を無効化し、要求された待ち時間を記録する。"""
    recorded: list[float] = []
    monkeypatch.setattr(base, "_sleep", recorded.append)
    base.reset_throttle()
    return recorded


@pytest.fixture()
def install_client(sleeps):
    """httpx.MockTransport ハンドラを共有クライアントとして注入する。"""
    clients: list[httpx.Client] = []

    def _install(handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        base.set_client(client)
        clients.append(client)
        return client

    yield _install
    base.set_client(None)
    for c in clients:
        c.close()


def yahoo_chart_payload(
    points,
    currency="JPY",
    tz="Asia/Tokyo",
    gmtoffset=32400,
    spot=None,
):
    """Yahoo v8 chart のJSON骨格。close と異なる adjclose も入れて生close使用を検証。"""
    ts, closes, adjs = [], [], []
    for day, px in points:
        local = datetime(
            day.year, day.month, day.day, 9, 0,
            tzinfo=timezone(timedelta(seconds=gmtoffset)),
        )
        ts.append(int(local.timestamp()))
        closes.append(px)
        adjs.append(None if px is None else px * 0.9)  # 配当調整済みの偽値
    meta = {
        "currency": currency,
        "exchangeTimezoneName": tz,
        "gmtoffset": gmtoffset,
        "regularMarketPrice": spot,
    }
    return {
        "chart": {
            "result": [
                {
                    "meta": meta,
                    "timestamp": ts,
                    "indicators": {
                        "quote": [{"close": closes}],
                        "adjclose": [{"adjclose": adjs}],
                    },
                }
            ],
            "error": None,
        }
    }


def make_sec(sec_id: int, stype: str, ref: str, **kw) -> Security:
    defaults = dict(
        id=sec_id,
        name=f"銘柄{sec_id}",
        name_key=f"sec{sec_id}",
        asset_class=AssetClass.STOCK_JP,
        currency="JPY",
        price_source_type=PriceSourceType(stype),
        price_source_ref=ref,
        price_source_status=PriceSourceStatus.LINKED,
    )
    defaults.update(kw)
    return Security(**defaults)


# ----------------------------------------------------------------------
# yahoo
# ----------------------------------------------------------------------


def test_yahoo_history_uses_raw_close_and_skips_null(install_client):
    payload = yahoo_chart_payload(
        [
            (d("2026-07-27"), 2953.0),
            (d("2026-07-28"), None),  # 休場等の null は skip
            (d("2026-07-29"), 3108.5),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "9433.T" in request.url.path
        return httpx.Response(200, json=payload)

    install_client(handler)
    res = yahoo.fetch_history("9433.T", d("2026-07-27"), d("2026-07-31"))
    # adjclose(×0.9) ではなく生 close
    assert res.prices == {d("2026-07-27"): D("2953.0"), d("2026-07-29"): D("3108.5")}
    assert res.currency == "JPY"
    assert res.covered == [(d("2026-07-27"), d("2026-07-31"))]


def test_yahoo_history_filters_to_requested_window(install_client):
    payload = yahoo_chart_payload(
        [(d("2026-07-25"), 100.0), (d("2026-07-27"), 110.0), (d("2026-08-02"), 120.0)]
    )
    install_client(lambda req: httpx.Response(200, json=payload))
    res = yahoo.fetch_history("9433.T", d("2026-07-27"), d("2026-07-31"))
    assert res.prices == {d("2026-07-27"): D("110.0")}


def test_yahoo_prelisting_falls_back_to_range_max(install_client):
    calls: list[str] = []
    listed = yahoo_chart_payload([(d("2024-01-04"), 100.0)])
    error_payload = {
        "chart": {
            "result": None,
            "error": {
                "code": "Not Found",
                "description": "Data doesn't exist for startDate = 1262304000, endDate = 1263081600",
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.params.get("range") == "max":
            return httpx.Response(200, json=listed)
        # 実APIは "Data doesn't exist" を HTTP 400 + JSONボディで返す
        return httpx.Response(400, json=error_payload)

    install_client(handler)
    res = yahoo.fetch_history("521A.T", d("2010-01-01"), d("2010-01-09"))
    assert len(calls) == 2
    assert res.prices == {}  # 上場前区間に価格なし
    # それでも要求範囲は被覆確定（再取得しない）
    assert res.covered == [(d("2010-01-01"), d("2010-01-09"))]


def test_yahoo_spot(install_client):
    payload = yahoo_chart_payload([], spot=2882.5)
    install_client(lambda req: httpx.Response(200, json=payload))
    assert yahoo.fetch_spot("9433.T") == (D("2882.5"), "JPY")


def test_yahoo_429_backoff_then_gives_up(install_client, sleeps):
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        return httpx.Response(429)

    install_client(handler)
    warns: list[str] = []
    res = yahoo.fetch_history(
        "9433.T", d("2026-07-01"), d("2026-07-05"), warn=warns.append
    )
    assert res.prices == {} and res.covered == []
    assert n["count"] == 3  # 初回 + 3s/6s バックオフ後の2回
    assert [s for s in sleeps if s in (3.0, 6.0)] == [3.0, 6.0]
    assert any("429" in w for w in warns)


# ----------------------------------------------------------------------
# toushin
# ----------------------------------------------------------------------


def test_toushin_search_normalizes_names(install_client):
    payload = {
        "searchResultInfo": {
            "resultInfoMapList": [
                {
                    "isinCd": "JP90C000GKC6",
                    "associFundCd": "03311187",
                    "fundStNm": "ｅＭＡＸＩＳ　Ｓｌｉｍ米国株式（Ｓ＆Ｐ５００）",
                    "fundCategory": "4",
                    "entrustCmpNm": "三菱ＵＦＪアセットマネジメント",
                },
                {"isinCd": "", "associFundCd": "X", "fundStNm": "無効行"},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = json.loads(request.read())
        assert body["t_keyword"] == "eMAXIS"
        assert body["t_kensakuKbn"] == "1"
        return httpx.Response(200, json=payload)

    install_client(handler)
    out = toushin.search("eMAXIS")
    assert len(out) == 1
    assert out[0]["name"] == "eMAXIS Slim米国株式(S&P500)"  # NFKC 正規化
    assert out[0]["isin"] == "JP90C000GKC6"
    assert out[0]["assoc_cd"] == "03311187"
    assert out[0]["ref"] == "JP90C000GKC6:03311187"
    assert out[0]["category"] == "4"


def test_toushin_history_parses_cp932_csv(install_client):
    csv_text = (
        "年月日,基準価額(円),純資産総額（百万円）,分配金,決算期\r\n"
        "2026年07月30日,42000,12073536,0,1\r\n"
        "2026年08月03日,42902,12073536,,1\r\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["isinCd"] == "JP90C000GKC6"
        assert request.url.params["associFundCd"] == "03311187"
        return httpx.Response(200, content=csv_text.encode("cp932"))

    install_client(handler)
    res = toushin.fetch_history("JP90C000GKC6:03311187")
    assert res.prices == {
        d("2026-07-30"): D("42000"),
        d("2026-08-03"): D("42902"),
    }
    assert res.currency == "JPY"


def test_toushin_bad_ref_warns_without_request(install_client):
    install_client(lambda req: pytest.fail("ネットワークを叩いてはいけない"))
    warns: list[str] = []
    res = toushin.fetch_history("JP90C000GKC6", warn=warns.append)  # コロン欠落
    assert res.prices == {}
    assert warns


# ----------------------------------------------------------------------
# frankfurter (fx)
# ----------------------------------------------------------------------


def test_frankfurter_history(install_client):
    payload = {
        "amount": 1.0,
        "base": "USD",
        "start_date": "2026-07-27",
        "end_date": "2026-07-31",
        "rates": {
            "2026-07-27": {"JPY": 163.64},
            "2026-07-28": {"JPY": 163.91},
            "2026-07-31": {"JPY": 160.24},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.frankfurter.dev"
        assert "2026-07-27..2026-07-31" in request.url.path
        assert request.url.params["base"] == "USD"
        return httpx.Response(200, json=payload)

    install_client(handler)
    rows = fx_provider.fetch_history("USD", d("2026-07-27"), d("2026-07-31"))
    assert rows == {
        d("2026-07-27"): D("163.64"),
        d("2026-07-28"): D("163.91"),
        d("2026-07-31"): D("160.24"),
    }


def test_frankfurter_failure_returns_none(install_client):
    install_client(lambda req: httpx.Response(500))
    warns: list[str] = []
    assert (
        fx_provider.fetch_history("USD", d("2026-07-01"), d("2026-07-05"), warn=warns.append)
        is None
    )
    assert warns


# ----------------------------------------------------------------------
# metal（純粋合成）
# ----------------------------------------------------------------------


def test_metal_compose_forward_fills_fx():
    futures = {d("2026-07-27"): D("3400"), d("2026-07-28"): D("3410")}
    fx = {d("2026-07-25"): D("150"), d("2026-07-28"): D("152")}
    out = metal.compose(futures, fx)
    assert out[d("2026-07-27")] == D("3400") * D("150") / metal.GRAMS_PER_TROY_OUNCE
    assert out[d("2026-07-28")] == D("3410") * D("152") / metal.GRAMS_PER_TROY_OUNCE


def test_metal_compose_skips_before_fx_start():
    # FX系列開始前の先物日は合成不能（backfillしない）
    assert metal.compose({d("2026-07-20"): D("3000")}, {d("2026-07-25"): D("150")}) == {}
    assert metal.compose({}, {}) == {}


# ----------------------------------------------------------------------
# price_history ファサード
# ----------------------------------------------------------------------


def test_ensure_price_history_yahoo_flow(store: Store, install_client):
    payload = yahoo_chart_payload(
        [(d("2026-07-27"), 2953.0), (d("2026-07-29"), 3108.0)]
    )
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        return httpx.Response(200, json=payload)

    install_client(handler)
    sec = make_sec(1, "yahoo", "9433.T")
    price_history.ensure_price_history(
        store, [sec], d("2026-07-27"), d("2026-07-31")
    )
    series, ccy = store.get_price_rows("yahoo", "9433.T")
    assert series == {"2026-07-27": D("2953.0"), "2026-07-29": D("3108.0")}
    assert ccy == "JPY"
    assert ps.get_ranges(store, "yahoo", "9433.T") == [
        (d("2026-07-27"), d("2026-07-31"))
    ]
    # 2回目は被覆済み → リクエストなし
    first = n["count"]
    price_history.ensure_price_history(
        store, [sec], d("2026-07-27"), d("2026-07-31")
    )
    assert n["count"] == first == 1


def test_ensure_price_history_skips_unlinked(store: Store, install_client):
    install_client(lambda req: pytest.fail("unlinked銘柄で叩いてはいけない"))
    sec = make_sec(
        1, "yahoo", "9433.T", price_source_status=PriceSourceStatus.UNLINKED
    )
    price_history.ensure_price_history(store, [sec], d("2026-07-01"), d("2026-07-31"))
    assert store.get_price_rows("yahoo", "9433.T")[0] == {}


def test_ensure_toushin_once_per_day(store: Store, install_client):
    csv_text = (
        "年月日,基準価額(円),純資産総額（百万円）,分配金,決算期\r\n"
        "2026年07月30日,42000,100,0,1\r\n"
        "2026年07月31日,42902,100,0,1\r\n"
    )
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        return httpx.Response(200, content=csv_text.encode("cp932"))

    install_client(handler)
    ref = "JP90C000GKC6:03311187"
    sec = make_sec(
        1,
        "toushin",
        ref,
        asset_class=AssetClass.FUND_JP,
        unit=Unit.KUCHI,
        price_unit_divisor=10000,
    )
    price_history.ensure_price_history(store, [sec], d("2026-07-29"), d("2026-07-31"))
    assert n["count"] == 1
    series, _ = store.get_price_rows("toushin", ref)
    assert series == {"2026-07-30": D("42000"), "2026-07-31": D("42902")}
    # 被覆は min(start, 設定来) 〜 昨日
    yesterday = date.today() - timedelta(days=1)
    assert ps.get_ranges(store, "toushin", ref) == [(d("2026-07-29"), yesterday)]
    # 同日中は、より広い範囲を要求して欠損があっても再取得しない（1日1回）
    price_history.ensure_price_history(store, [sec], d("2026-07-01"), d("2026-07-31"))
    assert n["count"] == 1


def test_ensure_metal_composes_and_records(store: Store, install_client):
    gold = yahoo_chart_payload(
        [(d("2026-07-27"), 3400.0), (d("2026-07-28"), 3410.0)],
        currency="USD",
        tz="America/New_York",
        gmtoffset=-14400,
    )
    fx_payload = {
        "base": "USD",
        "rates": {"2026-07-27": {"JPY": 163.64}, "2026-07-28": {"JPY": 163.91}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "query1.finance.yahoo.com":
            assert "GC=F" in request.url.path
            return httpx.Response(200, json=gold)
        if request.url.host == "api.frankfurter.dev":
            return httpx.Response(200, json=fx_payload)
        pytest.fail(f"想定外のリクエスト: {request.url}")

    install_client(handler)
    sec = make_sec(
        1, "metal", "XAU", asset_class=AssetClass.METAL, unit=Unit.GRAM
    )
    price_history.ensure_price_history(store, [sec], d("2026-07-27"), d("2026-07-28"))

    series, ccy = store.get_price_rows("metal", "XAU")
    assert ccy == "JPY"
    assert series["2026-07-27"] == D("3400.0") * D("163.64") / metal.GRAMS_PER_TROY_OUNCE
    assert series["2026-07-28"] == D("3410.0") * D("163.91") / metal.GRAMS_PER_TROY_OUNCE
    # 金属・構成要素それぞれ被覆記録
    assert ps.get_ranges(store, "metal", "XAU") == [(d("2026-07-27"), d("2026-07-28"))]
    assert ps.get_ranges(store, "yahoo", "GC=F") == [(d("2026-07-27"), d("2026-07-28"))]
    assert ps.get_ranges(store, "fx", "USD") == [(d("2026-07-20"), d("2026-07-28"))]


def test_ensure_fx_history_records_range_and_skips_jpy(store: Store, install_client):
    payload = {"base": "USD", "rates": {"2026-07-28": {"JPY": 163.91}}}
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        assert request.url.params["base"] == "USD"  # JPYは叩かない
        return httpx.Response(200, json=payload)

    install_client(handler)
    price_history.ensure_fx_history(
        store, ["JPY", "USD"], d("2026-07-27"), d("2026-07-31")
    )
    assert n["count"] == 1
    series, _ = store.get_price_rows("fx", "USD")
    assert series == {"2026-07-28": D("163.91")}
    assert ps.get_ranges(store, "fx", "USD") == [(d("2026-07-27"), d("2026-07-31"))]


def test_ensure_fx_history_chunks_long_ranges(store: Store, install_client):
    """10年超の欠損は Frankfurter の 10年/1コール上限に合わせて分割される。"""
    spans: list[tuple[date, date]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # パス末尾 "/{start}..{end}" を解析して要求スパンを記録
        s, _, e = request.url.path.rsplit("/", 1)[1].partition("..")
        spans.append((d(s), d(e)))
        return httpx.Response(200, json={"base": "USD", "rates": {}})

    install_client(handler)
    start, end = d("2010-01-04"), d("2026-08-01")  # 約16.6年
    price_history.ensure_fx_history(store, ["USD"], start, end)
    assert len(spans) == 2
    for s, e in spans:
        assert (e - s).days + 1 <= price_history.FX_MAX_DAYS_PER_CALL
    # チャンクは連続し、全体を過不足なく覆う
    assert spans[0][0] == start
    assert spans[1][0] == spans[0][1] + timedelta(days=1)
    assert spans[-1][1] == end
    assert ps.get_ranges(store, "fx", "USD") == [(start, end)]


def test_ensure_fx_history_failure_leaves_no_coverage(store: Store, install_client):
    install_client(lambda req: httpx.Response(500))
    warns: list[str] = []
    price_history.ensure_fx_history(
        store, ["USD"], d("2026-07-27"), d("2026-07-31"), warn=warns.append
    )
    assert ps.get_ranges(store, "fx", "USD") == []
    assert warns


def test_search_funds_facade(install_client):
    payload = {
        "searchResultInfo": {
            "resultInfoMapList": [
                {"isinCd": "JP1", "associFundCd": "01", "fundStNm": "テスト投信"}
            ]
        }
    }
    install_client(lambda req: httpx.Response(200, json=payload))
    out = price_history.search_funds("テスト")
    assert out == [
        {
            "name": "テスト投信",
            "isin": "JP1",
            "assoc_cd": "01",
            "ref": "JP1:01",
            "category": "",
            "company": "",
        }
    ]


# ----------------------------------------------------------------------
# prices ファサード（スポット）
# ----------------------------------------------------------------------


def test_fetch_spot_uses_ttl_cache(store: Store, install_client):
    payload = yahoo_chart_payload([], spot=2882.5)
    n = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["count"] += 1
        return httpx.Response(200, json=payload)

    install_client(handler)
    sec = make_sec(1, "yahoo", "9433.T")
    out1 = prices.fetch_spot(store, [sec])
    assert out1 == {1: D("2882.5")}
    assert n["count"] == 1
    # TTL内の2回目はネットワークを叩かない
    out2 = prices.fetch_spot(store, [sec])
    assert out2 == {1: D("2882.5")}
    assert n["count"] == 1


def test_fetch_spot_skips_unlinked_and_manual(store: Store, install_client):
    install_client(lambda req: pytest.fail("linked以外で叩いてはいけない"))
    secs = [
        make_sec(1, "yahoo", "9433.T", price_source_status=PriceSourceStatus.UNLINKED),
        make_sec(2, "manual", "2", price_source_status=PriceSourceStatus.LINKED),
        make_sec(3, "none", None, price_source_status=PriceSourceStatus.NOT_REQUIRED),
    ]
    assert prices.fetch_spot(store, secs) == {}


def test_fetch_spot_toushin_uses_latest_daily(store: Store, install_client):
    install_client(lambda req: pytest.fail("toushinスポットで叩いてはいけない"))
    ref = "JP90C000GKC6:03311187"
    store.upsert_daily_price("toushin", ref, "2026-08-01", D("42000"))
    store.upsert_daily_price("toushin", ref, "2026-08-03", D("42902"))
    sec = make_sec(
        7,
        "toushin",
        ref,
        asset_class=AssetClass.FUND_JP,
        unit=Unit.KUCHI,
        price_unit_divisor=10000,
    )
    assert prices.fetch_spot(store, [sec]) == {7: D("42902")}


def test_fetch_spot_metal_composition(store: Store, install_client):
    gold = yahoo_chart_payload([], currency="USD", spot=3400.0)
    fx_latest = {"base": "USD", "rates": {"JPY": 160.0}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "query1.finance.yahoo.com":
            return httpx.Response(200, json=gold)
        return httpx.Response(200, json=fx_latest)

    install_client(handler)
    sec = make_sec(5, "metal", "XAU", asset_class=AssetClass.METAL, unit=Unit.GRAM)
    out = prices.fetch_spot(store, [sec])
    assert out == {5: D("3400.0") * D("160.0") / metal.GRAMS_PER_TROY_OUNCE}


def test_fetch_fx_rates_jpy_and_fallback_to_daily(store: Store, install_client):
    install_client(lambda req: httpx.Response(500))  # frankfurter 停止を模擬
    store.upsert_daily_price("fx", "USD", "2026-08-01", D("150"))
    warns: list[str] = []
    out = prices.fetch_fx_rates(store, ["JPY", "USD", "EUR"], warn=warns.append)
    assert out["JPY"] == D("1")
    assert out["USD"] == D("150")  # キャッシュ済み日次値へフォールバック
    assert "EUR" not in out  # キャッシュも無ければ警告のみ
    assert any("EUR" in w for w in warns)
