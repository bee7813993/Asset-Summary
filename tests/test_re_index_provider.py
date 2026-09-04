"""不動産価格指数プロバイダ（xlsx 取得・パース）と ensure_re_index_history のテスト。

xlsx のフィクスチャはテスト内で zipfile を使って合成する。バイナリをリポジトリに
置かずに済むうえ、stdlib だけで OOXML を読めている事の証明にもなる。
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from asset_summary.core import price_history, price_store as ps, re_index
from asset_summary.core.models import (
    AssetClass,
    PriceSourceStatus,
    PriceSourceType,
    Security,
    Unit,
)
from asset_summary.core.providers import base
from asset_summary.core.providers import re_index as provider
from asset_summary.core.store import Store

D = Decimal

EXCEL_EPOCH = date(1899, 12, 30)


@pytest.fixture()
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(base, "_sleep", recorded.append)
    base.reset_throttle()
    return recorded


@pytest.fixture()
def install_client(sleeps):
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


# ----------------------------------------------------------------------
# xlsx フィクスチャの合成
# ----------------------------------------------------------------------

_TYPE_COLS = ["B", "E", "H", "K"]
_TYPE_LABELS = [
    "住宅総合",
    "住宅地",
    "戸建住宅",
    "マンション（区分所有）",
]


def _serial(day: date) -> int:
    return (day - EXCEL_EPOCH).days


def _sheet_xml(rows: list[tuple[date, list[str]]], marker: str) -> str:
    sst_ref = {label: i for i, label in enumerate(_TYPE_LABELS)}
    out = ['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    cells = "".join(
        f'<c r="{col}5" t="s"><v>{sst_ref[label]}</v></c>'
        for col, label in zip(_TYPE_COLS, _TYPE_LABELS)
    )
    out.append(f'<row r="5">{cells}</row>')
    out.append(f'<row r="9"><c r="B9" t="inlineStr"><is><t>{marker}</t></is></c></row>')
    r = 10
    for day, values in rows:
        cells = f'<c r="A{r}"><v>{_serial(day)}</v></c>'
        for col, value in zip(_TYPE_COLS, values):
            if value:
                cells += f'<c r="{col}{r}"><v>{value}</v></c>'
        out.append(f'<row r="{r}">{cells}</row>')
        r += 1
    # 実ファイルと同じく末尾に空行が付く
    out.append(f'<row r="{r}"><c r="A{r}"/></row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


def build_xlsx(
    sheets: dict[str, list[tuple[date, list[str]]]],
    marker: str = provider.HEADER_MARKER,
) -> bytes:
    """{シート名: [(月, [住宅総合, 住宅地, 戸建, マンション])]} から最小の xlsx を作る。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        names = list(sheets)
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            + "".join(
                f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                for i, n in enumerate(names)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml"/>'
                for i in range(len(names))
            )
            + "</Relationships>",
        )
        z.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + "".join(f"<si><t>{label}</t></si>" for label in _TYPE_LABELS)
            + "</sst>",
        )
        for i, n in enumerate(names):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(sheets[n], marker))
    return buf.getvalue()


MONTHS = [
    (date(2025, 10, 1), ["140", "120", "130", "210"]),
    (date(2025, 11, 1), ["147.29", "121", "131", "222.92"]),
    (date(2025, 12, 1), ["148.02", "122", "132", "225.14"]),
]

SHEETS = {
    "全国Japan季節調整": MONTHS,
    "南関東圏Tokyo including季節調整": MONTHS,
    "全国Japan原系列": MONTHS,
}


def ok_handler(calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=build_xlsx(SHEETS))

    return handler


# ----------------------------------------------------------------------
# パース
# ----------------------------------------------------------------------


def test_parses_regions_and_types():
    warns: list[str] = []
    out = provider._parse_workbook(build_xlsx(SHEETS), warns.append)
    # フィクスチャに無い地域は1本の警告にまとまる（地域ごとに出すと警告帯が埋まる）
    assert len(warns) == 1 and "見つからない地域" in warns[0]
    assert out["zenkoku:condo"] == {
        date(2025, 10, 1): D("210"),
        date(2025, 11, 1): D("222.92"),
        date(2025, 12, 1): D("225.14"),
    }
    assert out["nanto:residential"][date(2025, 12, 1)] == D("148.02")
    # 季節調整シートだけを採る（原系列は同じ地域名だが接尾辞が違う）
    assert set(out) == {
        f"{r}:{t}"
        for r in ("zenkoku", "nanto")
        for t in ("residential", "land", "detached", "condo")
    }


def test_drifting_serials_are_normalised_to_month_start():
    """都道府県シートはシリアルが月内を漂う。月初へ丸めないと系列が壊れる。"""
    drifted = [
        (date(2025, 10, 25), ["140", "120", "130", "210"]),
        (date(2025, 11, 26), ["141", "121", "131", "211"]),
        (date(2025, 12, 27), ["142", "122", "132", "212"]),
    ]
    out = provider._parse_workbook(
        build_xlsx({"東京都Tokyo季節調整": drifted}), lambda _m: None
    )
    assert sorted(out["tokyo:condo"]) == [
        date(2025, 10, 1),
        date(2025, 11, 1),
        date(2025, 12, 1),
    ]


def test_rejects_a_rebased_workbook():
    """基準が 2010=100 から変わったら、黙って別物を取り込まずに諦める。"""
    warns: list[str] = []
    out = provider._parse_workbook(
        build_xlsx(SHEETS, marker="average of 2020=100"), warns.append
    )
    assert out == {}
    assert any("2010=100" in w for w in warns)


def test_rejects_non_zip_payload():
    warns: list[str] = []
    assert provider._parse_workbook(b"<html>maintenance</html>", warns.append) == {}
    assert warns


def test_rejects_zip_bomb(monkeypatch):
    monkeypatch.setattr(provider, "MAX_UNCOMPRESSED_BYTES", 10)
    warns: list[str] = []
    assert provider._parse_workbook(build_xlsx(SHEETS), warns.append) == {}
    assert any("展開後サイズ" in w for w in warns)


def test_unknown_sheet_layout_warns():
    warns: list[str] = []
    out = provider._parse_workbook(
        build_xlsx({"火星Mars季節調整": MONTHS}), warns.append
    )
    assert out == {}
    assert warns


# ----------------------------------------------------------------------
# ダウンロード
# ----------------------------------------------------------------------


def test_fetch_all_uses_the_permalink(install_client):
    calls: list[str] = []
    install_client(ok_handler(calls))
    out = provider.fetch_all(warn=lambda _m: None)
    assert calls == [provider.INDEX_URL]
    assert out["zenkoku:condo"][date(2025, 12, 1)] == D("225.14")


def test_falls_back_to_scraping_the_landing_page(install_client):
    """住宅は /totikensangyo/content/、商業用は /common/ と接頭辞が違う。
    href をそのまま使い、並び順ではなく「最新データ」見出しを基準に選ぶ。"""
    landing = (
        '<h2 class="title">不動産価格指数</h2>'
        '<a href="/common/999999.xlsx">古い月報</a>'
        '<h3 class="title">最新データ</h3>'
        '<a href="/totikensangyo/content/001999999.xlsx">住宅</a>'
        '<a href="/common/001465568.xlsx">商業用不動産</a>'
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == provider.INDEX_URL:
            return httpx.Response(404)
        if url == provider.LANDING_URL:
            return httpx.Response(200, text=landing)
        return httpx.Response(200, content=build_xlsx(SHEETS))

    install_client(handler)
    out = provider.fetch_all(warn=lambda _m: None)
    assert calls[-1] == "https://www.mlit.go.jp/totikensangyo/content/001999999.xlsx"
    assert out["zenkoku:condo"][date(2025, 12, 1)] == D("225.14")


def test_landing_without_any_xlsx_gives_up(install_client):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == provider.INDEX_URL:
            return httpx.Response(404)
        return httpx.Response(200, text="<p>ただいま準備中です</p>")

    install_client(handler)
    warns: list[str] = []
    assert provider.fetch_all(warn=warns.append) == {}
    assert warns


def test_total_failure_returns_empty(install_client):
    install_client(lambda request: httpx.Response(500))
    warns: list[str] = []
    assert provider.fetch_all(warn=warns.append) == {}
    assert warns


# ----------------------------------------------------------------------
# ensure_re_index_history
# ----------------------------------------------------------------------


def _estate(store: Store, ref: str | None = "re_index:nanto:condo") -> Security:
    sec_id = store.create_security(
        Security(
            name="自宅マンション",
            name_key="じたくまんしょん",
            asset_class=AssetClass.REAL_ESTATE,
            unit=Unit.UNIT,
            price_source_type=PriceSourceType.MANUAL,
            price_source_status=PriceSourceStatus.MANUAL,
            price_source_ref=ref,
        )
    )
    return store.get_security(sec_id)


def test_ensure_stores_every_series_and_records_coverage(store: Store, install_client):
    calls: list[str] = []
    install_client(ok_handler(calls))
    price_history.ensure_re_index_history(store, [_estate(store)], date.today())

    rows, ccy = store.get_price_rows("re_index", "nanto:condo")
    assert rows["2025-12-01"] == D("225.14")
    assert ccy == "JPY"
    # 紐付けていない地域・種別も同じダウンロードで入る（地域変更で再取得しない）
    assert store.get_price_rows("re_index", "zenkoku:land")[0]
    assert ps.get_ranges(store, "re_index", "nanto:condo")


def test_ensure_downloads_at_most_once_a_day(store: Store, install_client):
    """公表が止まっている間、毎リクエスト MLIT を叩かないこと。"""
    calls: list[str] = []
    install_client(ok_handler(calls))
    sec = _estate(store)

    price_history.ensure_re_index_history(store, [sec], date.today())
    assert len(calls) == 1
    # 取り込めた最新月は2025-12で「古い」ままだが、試行済みなので再取得しない
    price_history.ensure_re_index_history(store, [sec], date.today())
    price_history.ensure_re_index_history(store, [sec], date.today())
    assert len(calls) == 1


def test_ensure_skips_entirely_when_the_index_is_fresh(store: Store, install_client):
    calls: list[str] = []
    install_client(ok_handler(calls))
    fresh = date.today() - timedelta(days=3)
    store.upsert_daily_price("re_index", "nanto:condo", fresh.isoformat(), D("220"))

    price_history.ensure_re_index_history(store, [_estate(store)], date.today())
    assert calls == []


def test_ensure_does_nothing_without_a_linked_property(store: Store, install_client):
    calls: list[str] = []
    install_client(ok_handler(calls))
    price_history.ensure_re_index_history(store, [_estate(store, ref=None)], date.today())
    assert calls == []


def test_ensure_failure_is_retried_tomorrow(store: Store, install_client):
    install_client(lambda request: httpx.Response(503))
    warns: list[str] = []
    price_history.ensure_re_index_history(
        store, [_estate(store)], date.today(), warn=warns.append
    )
    # 失敗時は被覆を記録しない（翌日に再試行できる）
    assert ps.get_ranges(store, "re_index", "nanto:condo") == []
    assert warns


def test_ensure_reupserts_the_whole_series(store: Store, install_client):
    """再開時に過去の値が改訂されうるので、差分追記ではなく全期間を入れ直す。"""
    store.upsert_daily_price("re_index", "nanto:condo", "2025-12-01", D("1"))
    calls: list[str] = []
    install_client(ok_handler(calls))
    price_history.ensure_re_index_history(store, [_estate(store)], date.today())
    rows, _ = store.get_price_rows("re_index", "nanto:condo")
    assert rows["2025-12-01"] == D("225.14")


def test_end_to_end_index_shapes_the_valuation(store: Store, install_client):
    """査定額1点＋指数 → 現在値が指数で延長される（段階2の眼目）。"""
    install_client(ok_handler([]))
    sec = _estate(store)
    store.upsert_daily_price("manual", str(sec.id), "2025-10-01", D("50000000"))
    price_history.ensure_re_index_history(store, [sec], date.today())

    series, _ = store.price_series_for_security(sec, end="2025-12-01")
    # 全国ではなく紐付けた南関東圏のマンション指数（210 → 225.14）で伸びる
    assert series["2025-10-01"] == D("50000000")
    expected = D("50000000") * D("225.14") / D("210")
    assert series["2025-12-01"] == expected
    assert re_index.parse_ref(sec.price_source_ref) == "nanto:condo"
