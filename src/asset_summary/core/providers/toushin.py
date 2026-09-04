"""投信協会（toushin-lib.fwg.ne.jp）プロバイダ（投資信託・source='toushin'）。

- 検索: POST fundDataSearch（JSON）→ resultInfoMapList[] の
  isinCd / associFundCd / fundStNm（全角混じり → NFKC 正規化して返す）。
- NAV履歴: GET csv-file-download → Shift-JIS(cp932) CSV。
  列: 年月日, 基準価額(円), 純資産総額（百万円）, 分配金, 決算期。
  日付は「YYYY年MM月DD日」。常に設定来の全履歴が返る。
  基準価額は JPY・1万口あたり（divisor はモデル側 price_unit_divisor=10000）。
- T+1公表（最新行は前営業日）。再取得ポリシー（1銘柄1日1回まで）はファサード側。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from . import base
from .base import HistoryResult, WarnFn

SEARCH_URL = "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST999900/fundDataSearch"
CSV_URL = "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"

_THROTTLE_KEY = "toushin"

_DATE_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")


def _noop(_msg: str) -> None:
    pass


def _nfkc(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


@dataclass
class SearchResult:
    """検索結果と到達可否。

    「協会へ届かなかった」と「届いたが該当が無い」はどちらも items が空になる。
    利用者に理由を出せるよう、ここで区別する。
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    reachable: bool = True


def search(
    query: str,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """銘柄検索。[{"name","isin","assoc_cd","ref","category","company"}] を返す。

    ref は price_source_ref にそのまま入る 'ISIN:協会コード' 形式。
    到達可否も要るときは search_funds を使う。
    """
    return search_funds(query, warn=warn, client=client).items


def search_funds(
    query: str,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> SearchResult:
    """search と同じ検索。到達できたかどうかも返す。"""
    w = warn or _noop
    body = {
        "t_keyword": query,
        "t_kensakuKbn": "1",
        "startNo": 0,
        "draw": 1,
        "searchBtnClickFlg": True,
    }
    resp = base.request(
        "POST", SEARCH_URL, key=_THROTTLE_KEY, json=body, warn=w, client=client
    )
    if resp is None:
        return SearchResult(reachable=False)
    try:
        data = resp.json()
    except ValueError:
        w("toushin: 検索レスポンス(JSON)を解釈できません")
        return SearchResult(reachable=False)
    items = ((data or {}).get("searchResultInfo") or {}).get("resultInfoMapList") or []
    out: list[dict[str, Any]] = []
    for r in items:
        if not isinstance(r, dict):
            continue
        isin = str(r.get("isinCd") or "").strip()
        assoc_cd = str(r.get("associFundCd") or "").strip()
        if not isin or not assoc_cd:
            continue
        out.append(
            {
                "name": _nfkc(r.get("fundStNm")),
                "isin": isin,
                "assoc_cd": assoc_cd,
                "ref": f"{isin}:{assoc_cd}",
                "category": str(r.get("fundCategory") or ""),
                "company": _nfkc(r.get("entrustCmpNm")),
            }
        )
    return SearchResult(items=out)


def fetch_history(
    ref: str,
    warn: WarnFn | None = None,
    client: httpx.Client | None = None,
) -> HistoryResult:
    """基準価額CSV（設定来の全履歴）を取得して日次系列を返す。

    ref: 'ISIN:協会コード'。covered は空で返す（被覆記録はファサードが
    「設定来〜昨日」ポリシーで行う）。
    """
    w = warn or _noop
    isin, sep, assoc_cd = ref.partition(":")
    isin, assoc_cd = isin.strip(), assoc_cd.strip()
    if not sep or not isin or not assoc_cd:
        w(f"toushin: price_source_ref の形式が不正です（'ISIN:協会コード' 想定): {ref}")
        return HistoryResult(reachable=False)
    resp = base.request(
        "GET",
        CSV_URL,
        key=_THROTTLE_KEY,
        params={"isinCd": isin, "associFundCd": assoc_cd},
        warn=w,
        client=client,
    )
    if resp is None:
        return HistoryResult(reachable=False)
    text = resp.content.decode("cp932", errors="replace")
    prices: dict[date, Decimal] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        m = _DATE_RE.match(_nfkc(row[0]))
        if not m:
            continue  # ヘッダ行など
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        raw = row[1].strip().replace(",", "")
        if not raw:
            continue
        try:
            prices[d] = Decimal(raw)
        except InvalidOperation:
            continue
    if not prices:
        w(f"toushin {ref}: 基準価額CSVから価格行を読み取れませんでした")
    return HistoryResult(prices, "JPY", [])
