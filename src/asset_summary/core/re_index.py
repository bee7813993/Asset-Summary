"""不動産の評価額導出（手入力の査定額 × 公的な不動産価格指数）。

不動産は査定額が年に数回しか入らないため、そのままだと日次系列が疎になり、
SeriesLookup の forward-fill / backfill が階段や水平一直線を描いてしまう。
本モジュールは「査定額をアンカーとして、指数の形で間を埋め、最終査定日より先も
指数で延長する」導出を **純関数として** 提供する（I/O も Store も持たない）。
providers/metal.py と同じ流儀（dict in / dict out）。

キーは date ではなく **ISO日付文字列**。Store.price_series_for_security と
portfolio.SeriesLookup が文字列のまま bisect するため（ISO文字列の辞書順＝時系列順）。

導出した日次の値は **DBに保存しない**。埋めた値はクエリ層で作るという既存方針
（portfolio.SeriesLookup の docstring）に従う。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal

# securities.price_source_ref に入れる接頭辞。
# 未連携投信が manual に昇格したときの ISIN と衝突させないために付ける。
REF_PREFIX = "re_index:"

# 「最初のアンカーより前」は無限に伸びるので上限を置く。
# _range_start の "all"（5年）＋1年の余裕。2008年まで6,500日を materialize しない。
MAX_DERIVE_DAYS = 366 * 6

# 地域コード → 表示名。国交省「不動産価格指数（住宅）」の収録範囲そのまま。
# 都道府県別は東京・愛知・大阪の3つしか無い（47都道府県は公表されていない）。
REGIONS: dict[str, str] = {
    "zenkoku": "全国",
    "hokkaido": "北海道地方",
    "tohoku": "東北地方",
    "kanto": "関東地方",
    "hokuriku": "北陸地方",
    "chubu": "中部地方",
    "kinki": "近畿地方",
    "chugoku": "中国地方",
    "shikoku": "四国地方",
    "kyushu": "九州・沖縄地方",
    "nanto": "南関東圏",
    "nagoya": "名古屋圏",
    "keihanshin": "京阪神圏",
    "tokyo": "東京都",
    "aichi": "愛知県",
    "osaka": "大阪府",
}

# 種別コード → 表示名。
INDEX_TYPES: dict[str, str] = {
    "residential": "住宅総合",
    "land": "住宅地",
    "detached": "戸建住宅",
    "condo": "マンション（区分所有）",
}


def index_source_id(region: str, index_type: str) -> str:
    """daily_prices.source_id（source='re_index'）を組み立てる。"""
    return f"{region}:{index_type}"


def make_ref(region: str, index_type: str) -> str:
    """securities.price_source_ref に入れる文字列を組み立てる。"""
    return f"{REF_PREFIX}{index_source_id(region, index_type)}"


def split_source_id(source_id: str) -> tuple[str, str] | None:
    """nanto:condo → (nanto, condo)。未知のコードは None。"""
    region, _, index_type = source_id.partition(":")
    if region in REGIONS and index_type in INDEX_TYPES:
        return (region, index_type)
    return None


def parse_ref(price_source_ref: str | None) -> str | None:
    """price_source_ref から指数の source_id を取り出す。指数連携でなければ None。"""
    if not price_source_ref or not price_source_ref.startswith(REF_PREFIX):
        return None
    source_id = price_source_ref[len(REF_PREFIX) :].strip()
    if not source_id or split_source_id(source_id) is None:
        return None
    return source_id


# ----------------------------------------------------------------------
# 指数（月次）
# ----------------------------------------------------------------------


def usable_points(monthly: dict[str, Decimal]) -> list[tuple[date, Decimal]]:
    """月次指数を (日付, 水準) の昇順リストにする。0以下の水準は除去する。

    指数の水準が0以下になることは無い。除算の前にここで落としておき、
    全部落ちたら「指数なし」の経路（アンカー間の線形補間）へ自然に倒れる。
    """
    return [(date.fromisoformat(d), v) for d, v in sorted(monthly.items()) if v > 0]


def index_at(
    points: list[tuple[date, Decimal]], dates: list[date], day: date
) -> Decimal | None:
    """指数の日次値。月と月の間は暦日で線形内挿する。

    月次を階段にすると「3月1日に80万円増えた」という存在しないイベントを捏造する。
    指数自体が3ヶ月窓の平滑推定なので日次の精度はどのみち虚構であり、
    イベントを作らない方を選ぶ。

    - 最初の観測月より前: None（backfill しない。2008年より前の指数は存在しない）
    - 最終観測月より後: 定数（トレンドの外挿はしない）
    """
    if not points:
        return None
    i = bisect_right(dates, day)
    if i == 0:
        return None
    if i >= len(points):
        return points[-1][1]
    t0, v0 = points[i - 1]
    t1, v1 = points[i]
    span = (t1 - t0).days
    if span <= 0:
        return v0
    w = Decimal((day - t0).days) / Decimal(span)
    return v0 + (v1 - v0) * w


def expand_monthly(
    monthly: dict[str, Decimal], start: str, end: str
) -> dict[str, Decimal]:
    """月次指数を [start, end] の日次へ展開する（テスト・確認用の薄いループ）。"""
    points = usable_points(monthly)
    dates = [p[0] for p in points]
    d1 = date.fromisoformat(end)
    out: dict[str, Decimal] = {}
    day = date.fromisoformat(start)
    while day <= d1:
        v = index_at(points, dates, day)
        if v is not None:
            out[day.isoformat()] = v
        day += timedelta(days=1)
    return out


# ----------------------------------------------------------------------
# 評価額の導出
# ----------------------------------------------------------------------


def _apply_drift(raw: Decimal, resid: Decimal, w: Decimal) -> Decimal:
    """チェーンリンクのドリフト補正。

    素朴な V_i * I(d)/I(t_i) は次のアンカーで不連続に飛ぶ。指数が説明しきれ
    なかったズレ（resid）を経過日数で按分して足すと、w=0 で V_i、w=1 で V_i+1 に
    **構造的に厳密一致**する（丸め任せではない）。

    乗法版 raw * r**w は exp/log が要り float に落ちる。このリポジトリは
    Decimal 一貫が方針で、テストも Decimal の厳密一致で書かれている。現実的な
    査定間隔（1〜3年）で残差は数%なので加法との差は無視できる。
    差し替えるならこの関数だけを置き換える。
    """
    return raw + w * resid


def _scaled(
    value: Decimal, at_anchor: Decimal | None, at_day: Decimal | None
) -> Decimal:
    """アンカー値に指数比を掛ける。指数が引けなければ据え置き（＝従来の挙動）。"""
    if at_anchor is None or at_day is None:
        return value
    return value * at_day / at_anchor


def _value_at(
    apts: list[tuple[date, Decimal]],
    adates: list[date],
    ianchor: list[Decimal | None],
    ipts: list[tuple[date, Decimal]],
    idates: list[date],
    day: date,
) -> Decimal:
    i = bisect_right(adates, day)

    if i == 0:  # 最初のアンカーより前
        return _scaled(apts[0][1], ianchor[0], index_at(ipts, idates, day))
    if i >= len(apts):  # 最後のアンカーより後（＝延長）
        return _scaled(apts[-1][1], ianchor[-1], index_at(ipts, idates, day))

    t0, v0 = apts[i - 1]
    t1, v1 = apts[i]
    i0, i1 = ianchor[i - 1], ianchor[i]
    span = (t1 - t0).days
    w = Decimal((day - t0).days) / Decimal(span) if span > 0 else Decimal(0)

    if i0 is not None and i1 is not None:
        iu = index_at(ipts, idates, day)
        if iu is not None:
            raw = v0 * iu / i0
            resid = v1 - v0 * i1 / i0
            return _apply_drift(raw, resid, w)
    # 指数が使えない区間はまるごと線形補間（境界で連続になる）
    return v0 + (v1 - v0) * w


def derive_series(
    anchors: dict[str, Decimal],
    monthly_index: dict[str, Decimal],
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Decimal]:
    """査定額（アンカー）と月次指数から日次の評価額系列を導出する。

    - アンカー0点 → {}。**指数があっても空**。指数は水準であって価格ではなく、
      査定額が無い物件の評価額は「不明」であって0ではない（総額から外れる挙動を守る）
    - d < t0 / d > t_last → 端のアンカーに指数比を掛ける（後者が「延長」）
    - t_i <= d <= t_i+1 → 指数の形に従いつつ、両端のアンカーを厳密に通る（チェーンリンク）
    - 指数が引けない区間は **その区間ごと** 線形補間に落とす。日単位で落とすと
      指数の切れ目で線が折れるが、区間単位なら構造的に連続になる

    start は **表示のための窓であって導出の入力ではない**。チェーンリンクには
    start より前のアンカーが要るため、アンカーは常に全期間を渡すこと
    （呼び出し側で絞らない）。窓の切り出しは本関数が最後に行う。
    """
    apts = [(date.fromisoformat(d), v) for d, v in sorted(anchors.items())]
    if not apts:
        return {}
    adates = [p[0] for p in apts]

    ipts = usable_points(monthly_index)
    idates = [p[0] for p in ipts]

    last_known = max(adates[-1], idates[-1]) if idates else adates[-1]
    first_known = min(adates[0], idates[0]) if idates else adates[0]
    d1 = date.fromisoformat(end) if end else last_known
    d0 = date.fromisoformat(start) if start else first_known
    floor = d1 - timedelta(days=MAX_DERIVE_DAYS)
    if d0 < floor:
        d0 = floor
    if d0 > d1:
        return {}

    # 各アンカー時点の指数（区間ごとの使用可否判定に使う）
    ianchor = [index_at(ipts, idates, t) for t in adates]

    out: dict[str, Decimal] = {}
    day = d0
    while day <= d1:
        out[day.isoformat()] = _value_at(apts, adates, ianchor, ipts, idates, day)
        day += timedelta(days=1)
    return out


def spot_from_anchor(
    anchors: dict[str, Decimal], monthly_index: dict[str, Decimal], day: str
) -> Decimal | None:
    """指定日1点の評価額。現在値の解決に使う。"""
    return derive_series(anchors, monthly_index, start=day, end=day).get(day)
