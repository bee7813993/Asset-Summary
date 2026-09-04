"""不動産価格指数（住宅）プロバイダ（source='re_index'、国土交通省）。

- 配布は xlsx のみ。CSV/JSON は存在せず、e-Stat にも登録されていない。
- **openpyxl は使わない**。xlsx は OOXML の zip なので stdlib の zipfile +
  ElementTree で読める（依存を1つ増やすほどの処理ではない）。
- 1ファイルに全16地域 × 4種別が入っているので、1回のダウンロードで全系列を投入する。
  地域を変えても再取得は要らない。

出典表記（PDL 1.0 の必須要件）:
    出典：「不動産価格指数」（国土交通省）を加工して作成
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .. import re_index as core_re_index
from . import base

WarnFn = base.WarnFn

# 「最新データ」の恒久リンク。中身だけ差し替わり、URL は2022年から変わっていない。
INDEX_URL = "https://www.mlit.go.jp/totikensangyo/content/001473668.xlsx"
LANDING_URL = "https://www.mlit.go.jp/totikensangyo/totikensangyo_tk5_000085.html"
SITE_ROOT = "https://www.mlit.go.jp"

THROTTLE_KEY = "re_index"

# 季節調整値を使う。原系列は季節要因で毎年同じ形に揺れるため、
# 査定額のアンカーを繋ぐ「相場の水準」としては季節調整済みの方が素直。
SHEET_SUFFIX = "季節調整"

# リベース検知用。基準が変わったらこの文字列も変わる。
HEADER_MARKER = "average of 2010=100"

# zip 爆弾ガード。実ファイルは非圧縮で約4.8MB。
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024

# Excel シリアル値のエポック（1900年うるう年バグを含む Excel の慣習）
EXCEL_EPOCH = date(1899, 12, 30)
# 妥当なシリアル値の下限（1970-01-01 相当）。ヘッダやサンプル数を日付と誤認しない
# ための番人。実データは全国が2008年、都道府県が1984年から。
MIN_SERIAL = 25569

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_XLSX_HREF = re.compile(r'href="([^"]+\.xlsx)"', re.IGNORECASE)


# ----------------------------------------------------------------------
# ダウンロード（MLIT を知る唯一の場所）
# ----------------------------------------------------------------------


def _download_workbook(w: WarnFn, client=None) -> bytes | None:
    """指数の xlsx を取る。恒久リンク → 駄目なら landing page から自己修復。"""
    resp = base.request("GET", INDEX_URL, key=THROTTLE_KEY, warn=w, client=client)
    if resp is not None and resp.content:
        return resp.content

    w("re_index: 既知のURLで取得できないため掲載ページから探します")
    page = base.request("GET", LANDING_URL, key=THROTTLE_KEY, warn=w, client=client)
    if page is None:
        return None
    href = _find_latest_xlsx(page.text)
    if href is None:
        w("re_index: 掲載ページに xlsx へのリンクが見つかりません")
        return None
    resp = base.request("GET", href, key=THROTTLE_KEY, warn=w, client=client)
    if resp is None or not resp.content:
        return None
    return resp.content


def _find_latest_xlsx(html: str) -> str | None:
    """「最新データ」見出し以降の最初の xlsx リンクを絶対URLで返す。

    住宅は /totikensangyo/content/ 配下、商業用は /common/ 配下と接頭辞が違うので
    href をそのまま使う（パスを組み立て直さない）。並び順は 住宅 → 商業用 → 件数・面積。
    """
    start = html.find("最新データ")
    m = _XLSX_HREF.search(html, start if start >= 0 else 0)
    if m is None:
        return None
    href = m.group(1)
    if href.startswith("http"):
        return href
    return SITE_ROOT + ("" if href.startswith("/") else "/") + href


# ----------------------------------------------------------------------
# パース（stdlib のみ）
# ----------------------------------------------------------------------


def _read_zip(data: bytes, w: WarnFn) -> zipfile.ZipFile | None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        w(f"re_index: xlsx として読めません ({exc})")
        return None
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        w(f"re_index: 展開後サイズが想定を超えています ({total} bytes)")
        return None
    return zf


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return [
        "".join(t.text or "" for t in si.iter(_NS + "t"))
        for si in root.iter(_NS + "si")
    ]


def _cell_text(cell: ET.Element, sst: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(_NS + "t"))
    v = cell.find(_NS + "v")
    if v is None or v.text is None:
        return ""
    if kind == "s":
        try:
            return sst[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text


def _column(ref: str | None) -> str:
    """セル参照 'K12' から列名 'K' を取り出す。"""
    if not ref:
        return ""
    return "".join(ch for ch in ref if ch.isalpha())


def _sheet_paths(zf: zipfile.ZipFile) -> dict[str, str]:
    """シート名 → zip 内のパス。"""
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = {
        r.get("Id"): r.get("Target")
        for r in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels")).iter(
            _PKG + "Relationship"
        )
    }
    out: dict[str, str] = {}
    for sheet in wb.iter(_NS + "sheet"):
        target = rels.get(sheet.get(_RNS + "id"))
        if not target:
            continue
        target = target.lstrip("/")
        out[sheet.get("name") or ""] = (
            target if target.startswith("xl/") else "xl/" + target
        )
    return out


def _parse_sheet(
    zf: zipfile.ZipFile, path: str, sst: list[str], w: WarnFn
) -> dict[str, dict[date, Decimal]] | None:
    """1シート（＝1地域）から種別ごとの月次系列を取り出す。

    レイアウト（2026-03 時点で実ファイルを確認）:
      行5  種別ラベル（住宅総合 / 住宅地 / 戸建住宅 / マンション（区分所有））
      行9  英語ヘッダ。'average of 2010=100' を基準リベースの検知に使う
      行10〜 A列=Excelシリアル（月初）、種別ラベルと同じ列に指数値
    """
    rows = list(ET.fromstring(zf.read(path)).iter(_NS + "row"))
    label_to_col: dict[str, str] = {}
    marker_seen = False
    data_start = None

    for i, row in enumerate(rows):
        cells = [(_column(c.get("r")), _cell_text(c, sst)) for c in row.iter(_NS + "c")]
        texts = [t for _, t in cells]
        if not label_to_col:
            for col, text in cells:
                stripped = text.strip()
                if stripped in core_re_index.INDEX_TYPES.values():
                    label_to_col[stripped] = col
        if not marker_seen and any(HEADER_MARKER in t for t in texts):
            marker_seen = True
        if data_start is None:
            first = dict(cells).get("A", "")
            if _serial_to_month(first) is not None:
                data_start = i
                break

    if not label_to_col or data_start is None:
        w(f"re_index: シートの構成が想定と違います ({path})")
        return None
    if not marker_seen:
        w(
            "re_index: ヘッダに "
            f"'{HEADER_MARKER}' が見当たりません。基準が変わった可能性があります"
        )
        return None

    col_to_type = {
        col: code
        for code, label in core_re_index.INDEX_TYPES.items()
        if (col := label_to_col.get(label))
    }
    series: dict[str, dict[date, Decimal]] = {c: {} for c in col_to_type.values()}

    for row in rows[data_start:]:
        cells = {_column(c.get("r")): _cell_text(c, sst) for c in row.iter(_NS + "c")}
        day = _serial_to_month(cells.get("A", ""))
        if day is None:
            continue  # 末尾の空行・注記行
        for col, code in col_to_type.items():
            value = _to_decimal(cells.get(col, ""))
            if value is not None and value > 0:
                series[code][day] = value
    return series


def _serial_to_month(text: str) -> date | None:
    """Excel シリアル値 → その月の1日。

    月初へ丸めるのは表示上の都合ではなく必須。全国・ブロックのシートは
    シリアルがきっちり月初だが、都道府県（東京・愛知・大阪）のシートは
    +31/+32 日ずつ進む「ずれた」シリアルで、1984-04-25, 1984-05-26, … と
    月内を漂う。実ファイルで確認したところ、月初へ丸めると501ヶ月が
    重複0・欠落0で綺麗に並ぶ（ずれは表示形式由来の見かけ上のもの）。
    """
    try:
        serial = int(float(text))
    except (TypeError, ValueError):
        return None
    if serial < MIN_SERIAL:
        return None
    return (EXCEL_EPOCH + timedelta(days=serial)).replace(day=1)


def _to_decimal(text: str) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_workbook(data: bytes, w: WarnFn) -> dict[str, dict[date, Decimal]]:
    """xlsx → {'<地域>:<種別>': {月初日: 指数}}。失敗は warn して空を返す。"""
    zf = _read_zip(data, w)
    if zf is None:
        return {}
    try:
        sst = _shared_strings(zf)
        paths = _sheet_paths(zf)
    except (KeyError, ET.ParseError) as exc:
        w(f"re_index: ワークブックの構造を読めません ({exc})")
        return {}

    out: dict[str, dict[date, Decimal]] = {}
    missing: list[str] = []
    for region, label in core_re_index.REGIONS.items():
        path = _sheet_for_region(paths, label)
        if path is None:
            missing.append(label)
            continue
        try:
            parsed = _parse_sheet(zf, path, sst, w)
        except ET.ParseError as exc:
            w(f"re_index: 「{label}」のシートを読めません ({exc})")
            continue
        if not parsed:
            continue
        for index_type, rows in parsed.items():
            if rows:
                out[core_re_index.index_source_id(region, index_type)] = rows
    if missing:
        # 地域ごとに warn すると画面の警告帯が埋まるので1本にまとめる
        w("re_index: シートが見つからない地域があります: " + "、".join(missing))
    return out


def _sheet_for_region(paths: dict[str, str], label: str) -> str | None:
    """'南関東圏Tokyo including季節調整' のような名前を地域名と接尾辞で引く。

    英語部分は表記が変わりうるので前方・後方一致で拾う。地域名どうしは
    どれも他の接頭辞になっていないため衝突しない。
    """
    for name, path in paths.items():
        if name.startswith(label) and name.endswith(SHEET_SUFFIX):
            return path
    return None


# ----------------------------------------------------------------------
# 公開API
# ----------------------------------------------------------------------


def fetch_all(
    warn: WarnFn | None = None, client=None
) -> dict[str, dict[date, Decimal]]:
    """全地域 × 全種別の月次指数を1回のダウンロードで取る。

    失敗は warn して空 dict（warnings-as-data。例外は外に出さない）。
    """
    w = warn or (lambda _msg: None)
    data = _download_workbook(w, client=client)
    if not data:
        return {}
    return _parse_workbook(data, w)
