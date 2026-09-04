"""投資信託の価格ソース自動連携。

マネーフォワードPDFの投信名は投信協会の正式名称と表記が揺れる
（全角/半角・スペース有無・証券会社の内部コード付記・<購入・換金手数料なし>の
位置・別名の重複括弧など）。名前だけの照合は誤リンクの危険があるため、
2段構えで判定する:

1. 名前からノイズを除去した検索クエリで投信協会の候補を取得し、
   正規化文字列の類似度でスコアリングする
2. 有力候補の基準価額履歴を取得し、MF取込時に保存した記載基準価額
   （holding_snapshots.reported_price）と直近営業日で突き合わせる。
   円単位の一致は事実上の同一性証明であり、為替ヘッジあり/なし等の
   亜種は基準価額が異なるため自動的に排除される。

基準価額が一致した候補がちょうど1件のときだけ「自動確定(auto)」とし、
それ以外はユーザー確認(candidates)または見つからず(none)を返す。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher
from typing import Any, Callable

from .models import AssetClass, PriceSourceStatus, PriceSourceType, Security, Unit
from .providers import toushin
from .store import Store, StoreError

WarnFn = Callable[[str], None]

NAV_MATCH_WINDOW_DAYS = 7   # 記載基準価額と照合する過去営業日の幅
MAX_CANDIDATES = 3          # 基準価額照合まで行う候補数（1候補=CSV1リクエスト）
MIN_CANDIDATE_SCORE = 0.25  # これ未満の候補は照合対象にしない
SUGGEST_MIN_SCORE = 0.35    # 照合不一致時に「要確認」として出す下限

_PAREN_RE = re.compile(r"[（(]([^（）()]*)[）)]")
_BROKER_CODE_RE = re.compile(r"[（(]\d{1,6}[）)]")
_ANGLE_RE = re.compile(r"[<＜〈][^<>＜＞〈〉]*[>＞〉]")
# ASCII連続・CJK連続の境界でトークン分割するための正規表現
_TOKEN_RE = re.compile(r"[A-Za-z0-9&+.\-]+|[^\sA-Za-z0-9&+.\-・/／()（）]+")

_HEDGE_NO_RE = re.compile(r"為替ヘッジなし|ヘッジなし|ヘッジ無|H無|Hなし")
_HEDGE_YES_RE = re.compile(r"為替ヘッジあり|ヘッジあり|ヘッジ有|H有|Hあり")


def _nfkc(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", text or "")


def _strip_alias_parens(name: str) -> str:
    """末尾の「別名重複括弧」を除去する。

    例: '楽天・プラチナ・ファンド(為替ヘッジなし)(楽天・プラチナ(為替ヘッジなし))'
        → '楽天・プラチナ・ファンド(為替ヘッジなし)'
    末尾の括弧グループの中身が、それより前の本体と先頭数文字を共有していれば
    別名の重複とみなして落とす（愛称・略称の再掲パターン）。
    """
    s = name.strip()
    while True:
        m = re.search(r"[（(]((?:[^（）()]|[（(][^（）()]*[）)])*)[）)]\s*$", s)
        if not m:
            return s
        inner = m.group(1)
        body = s[: m.start()].strip()
        if len(body) >= 4 and len(inner) >= 4 and inner[:3] == body[:3]:
            s = body
            continue
        return s


_AIKYO_RE = re.compile(r"《[^《》]*》|【[^【】]*】")  # 投信協会の《愛称》表記


def clean_name(name: str) -> str:
    """検索・照合用にノイズを除去した表示名。"""
    s = _nfkc(name)
    s = _BROKER_CODE_RE.sub("", s)      # 証券会社の内部コード (8782) 等
    s = _AIKYO_RE.sub("", s)            # 《愛称》【…】
    s = _strip_alias_parens(s)          # 別名の重複括弧
    s = _ANGLE_RE.sub("", s)            # <購入・換金手数料なし> 等（位置が揺れる）
    return s.strip()


def normalize(name: str) -> str:
    """類似度計算用の正規化（空白・中黒・記号の揺れを吸収）。"""
    s = clean_name(name).lower()
    s = re.sub(r"[\s・･/／]", "", s)
    s = re.sub(r"[（(）)]", "", s)
    return s


def tokenize(name: str) -> list[str]:
    """検索クエリ用トークン（括弧の外側のみ）。"""
    s = clean_name(name)
    s = _PAREN_RE.sub(" ", s)  # 括弧の中身は初回クエリから除外
    return [t for t in _TOKEN_RE.findall(s) if len(t) >= 2]


def build_queries(name: str) -> list[str]:
    """検索クエリの候補（具体的→広い順）。fundDataSearch は半角スペース区切りのAND。"""
    tokens = tokenize(name)
    queries: list[str] = []

    def add(q: str) -> None:
        q = q.strip()
        if q and q not in queries:
            queries.append(q)

    if tokens:
        add(" ".join(tokens[:5]))
        if len(tokens) >= 3:
            add(" ".join(tokens[:2]))
        if len(tokens) >= 2:
            add(tokens[0])
        # 略記（中黒・スペース無し）でトークンが検索に掛からない場合の
        # フォールバック: 個々のトークン → 最長トークンの先頭3文字
        for t in sorted(tokens, key=len, reverse=True)[:2]:
            if len(t) >= 3:
                add(t)
        longest = max(tokens, key=len)
        if len(longest) >= 4:
            add(longest[:3])
    else:
        add(clean_name(name))
    return queries


def hedge_flag(name: str) -> str | None:
    s = _nfkc(name)
    if _HEDGE_NO_RE.search(s):
        return "no"
    if _HEDGE_YES_RE.search(s):
        return "yes"
    return None


def name_score(mf_name: str, official_name: str) -> float:
    """0-1 の名前類似度。片方がもう片方を包含する場合は長さ比で下駄を履かせる。"""
    a, b = normalize(mf_name), normalize(official_name)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        ratio = max(ratio, len(shorter) / len(longer))
    # 為替ヘッジの明示が食い違う候補は大きく減点（基準価額照合でも弾かれるが保険）
    ha, hb = hedge_flag(mf_name), hedge_flag(official_name)
    if ha and hb and ha != hb:
        ratio *= 0.3
    return round(ratio, 4)


def verify_nav(
    prices: dict[date, Decimal],
    reported: Decimal,
    as_of: date,
    window_days: int = NAV_MATCH_WINDOW_DAYS,
) -> tuple[bool, date | None, Decimal | None]:
    """候補の基準価額系列が、MF記載値と照合窓内で一致するか。

    返値: (一致したか, 一致した日, 直近の基準価額)
    MFの表示はT+1公表の最新値のため、基準日から数日以内のどこかの
    営業日と円単位で一致すれば同一ファンドとみなせる。
    """
    lower = as_of - timedelta(days=window_days)
    window = {d: p for d, p in prices.items() if lower <= d <= as_of}
    latest_nav = None
    if window:
        latest_nav = window[max(window)]
    for d in sorted(window, reverse=True):
        if window[d] == reported:
            return (True, d, latest_nav)
    return (False, None, latest_nav)


def dedupe_same_fund(store: Store, warn: WarnFn | None = None) -> list[dict[str, Any]]:
    """同じ投信協会ファンドに連携された重複銘柄を自動統合する。

    MF PDF は同じファンドを証券会社ごとの表記で書くため、別名の重複銘柄が
    できる。price_source_ref（ISIN:協会コード）が同じなら同一ファンド —
    名前類似度と違って誤判定の余地が無いため、手動確認なしで統合できる。
    統合の中身は store.merge_security と同じ（保有・取引・タグを1銘柄へ集め、
    旧名を alias として記憶するので、次回取込から重複は再発しない）。

    生き残る銘柄の選び方（上から優先）:
    1. inactive でない — 価格取得が止まっている売却済み登録の殻を残さない
    2. 証券会社コード付きの切り詰め名（例「…オール(8782)」）でない素の名前
    3. 現在保有数が多い — 名称変更で残った旧名の残骸より現役の名前
    4. 名前が長い → id が小さい（決定的にするためのタイブレーク）

    資産クラス等が食い違う組は merge_security が拒否する（warn に流して残す）。
    冪等: 重複が無ければ何もしない。

    返値: [{"target_id", "target_name", "merged_names", "transactions"}]
    transactions は移動した取引の件数 — 呼び出し側の原価再計算の要否判断に使う。
    """
    w = warn or (lambda _m: None)
    groups: dict[str, list[Security]] = {}
    for sec in store.list_securities():
        if sec.price_source_type == PriceSourceType.TOUSHIN and sec.price_source_ref:
            groups.setdefault(sec.price_source_ref, []).append(sec)
    dup_groups = {ref: secs for ref, secs in groups.items() if len(secs) > 1}
    if not dup_groups:
        return []

    qty: dict[int, Decimal] = {}
    for lot in store.current_holdings():
        qty[lot.security_id] = qty.get(lot.security_id, Decimal(0)) + lot.quantity

    out: list[dict[str, Any]] = []
    for _ref, secs in sorted(dup_groups.items()):
        secs.sort(
            key=lambda s: (
                s.inactive,
                1 if _BROKER_CODE_RE.search(_nfkc(s.name)) else 0,
                -(qty.get(s.id) or Decimal(0)),
                -len(s.name),
                s.id,
            )
        )
        target, rest = secs[0], secs[1:]
        merged_ids: list[int] = []
        merged_names: list[str] = []
        moved_tx = 0
        for src in rest:
            try:
                result = store.merge_security(src.id, target.id)
            except StoreError as e:  # ConflictError 含む — 属性が食い違う組は残す
                w(f"「{src.name}」を「{target.name}」に自動統合できませんでした: {e}")
                continue
            merged_ids.append(src.id)
            merged_names.append(src.name)
            moved_tx += result["transactions"]
        if merged_names:
            out.append(
                {
                    "target_id": target.id,
                    "target_name": target.name,
                    "merged_ids": merged_ids,
                    "merged_names": merged_names,
                    "transactions": moved_tx,
                }
            )
    return out


# ----------------------------------------------------------------------
# 年金（iDeCo・企業型DC）の口数逆算
#
# MF PDF の年金セクションは取得価額と評価額しか書かず、口数も基準価額も無い。
# そのため年金は quantity=1（件）・評価額は取込時の記載値で静的に持っていた。
# しかし投信協会のファンドへ連携すれば、評価額 ÷ 基準価額 × 10000 = 口数 が
# 逆算でき、以後は口数 × 日々の基準価額で自動評価できる（取込のたびに掛金
# 買付ぶんの口数も追随する）。
# ----------------------------------------------------------------------

# 口数逆算で照合する基準日からの遡り幅。iDeCo/MF の表示は T+1〜数営業日遅れの
# 基準価額を使うため、少し広めに取る（verify_nav の窓より長いのは、年金は
# 反映がさらに遅れることがあるため）
PENSION_NAV_WINDOW_DAYS = 14

# 年金の値動き照合: 「前回評価額 × 候補NAVの騰落率 + 掛金増分」と今回評価額の
# 相対誤差の上限。掛金分の再評価ずれ・基準日ずれ（窓内探索で吸収）を考慮した幅
PENSION_MOVEMENT_TOLERANCE = Decimal("0.005")
PENSION_MOVEMENT_MIN_ABS = Decimal("10")   # 少額保有での絶対床（円）
PENSION_MOVEMENT_MAX_PAIRS = 6             # 直近何期間ぶんを照合するか


def derive_pension_units(
    value_jpy: Decimal,
    series: dict[str, Decimal],
    as_of: str | None = None,
) -> tuple[Decimal, bool] | None:
    """評価額と基準価額系列から口数を逆算する。返値は (口数, 整数口で確定したか)。

    正しい基準日なら round(評価額×10000÷NAV) 口で評価額が円未満まで再現できる
    （口数は整数のため）。窓内のどの日でも再現できなければ、直近NAVでの比例
    口数（アンカー日に評価額が一致する小数口）へフォールバックする — 以後の
    評価は NAV 比で追随し、次回取込の逆算で確定値に置き換わる。
    """
    if value_jpy is None or value_jpy <= 0 or not series:
        return None
    days = sorted(d for d in series if as_of is None or d <= as_of)
    if not days:
        return None
    lower = (date.fromisoformat(days[-1]) - timedelta(days=PENSION_NAV_WINDOW_DAYS)).isoformat()
    window = [d for d in days if d >= lower]
    for d in reversed(window):
        nav = series[d]
        if nav <= 0:
            continue
        units = (value_jpy * 10000 / nav).to_integral_value(rounding=ROUND_HALF_UP)
        if units >= 1 and abs(units * nav / 10000 - value_jpy) < 1:
            return (units, True)
    for d in reversed(window):
        nav = series[d]
        if nav > 0:
            return ((value_jpy * 10000 / nav).quantize(Decimal("0.0001")), False)
    return None


def derive_pension_quantities(
    store: Store, warn: WarnFn | None = None
) -> list[dict[str, Any]]:
    """投信協会へ連携済みの年金銘柄のスナップショットを、実口数へ変換する。

    - price_unit_divisor=10000（1万口あたり）・unit=kuchi に揃える
    - 各スナップショットの quantity を評価額からの逆算口数で書き直す
      （quantity=1 の未導出行と、整数口で確定できた場合の更新のみ。
      比例口数のままの行を毎回書き換えて揺らさない）
    - 逆算の材料は reported_value_jpy（MF取込）。無ければ手動評価額
      （source='manual'）を使い、全行を変換できたら手動評価額は削除する
      — 残すと price_series_for_security が基準価額系列を評価額で上書きする
    冪等: 対象が無ければ何もしない。返値は変換した銘柄の一覧。
    """
    w = warn or (lambda _m: None)
    targets = [
        sec
        for sec in store.list_securities()
        if sec.asset_class == AssetClass.PENSION
        and sec.price_source_type == PriceSourceType.TOUSHIN
        and sec.price_source_ref
    ]
    if not targets:
        return []

    snaps_by_sec: dict[int, list[Any]] = {}
    for snap in store.all_snapshots():
        snaps_by_sec.setdefault(snap.security_id, []).append(snap)

    nav_cache: dict[str, dict[str, Decimal]] = {}
    out: list[dict[str, Any]] = []
    for sec in targets:
        if sec.price_unit_divisor != 10000 or sec.unit != Unit.KUCHI:
            store.update_security(sec.id, price_unit_divisor=10000, unit=Unit.KUCHI.value)
        ref = sec.price_source_ref
        if ref not in nav_cache:
            nav_cache[ref], _ = store.get_price_rows("toushin", ref)
        series = nav_cache[ref]
        anchors, _ = store.get_price_rows("manual", str(sec.id))
        anchor_days = sorted(anchors)

        updated = 0
        underived = 0
        ratio_lots = 0
        for snap in snaps_by_sec.get(sec.id, []):
            if snap.quantity == 0:
                continue  # 売却済みマーカーはそのまま
            as_of = snap.as_of_date.isoformat()
            value = snap.reported_value_jpy
            if value is None and anchor_days:
                i = bisect_right(anchor_days, as_of)
                value = anchors[anchor_days[i - 1]] if i else None
            derived = derive_pension_units(value, series, as_of) if value is not None else None
            if derived is None:
                if snap.quantity <= 1:
                    underived += 1
                continue
            units, confident = derived
            if snap.quantity <= 1 or (confident and units != snap.quantity):
                store.upsert_snapshot(snap.model_copy(update={"quantity": units}))
                updated += 1
                if not confident:
                    ratio_lots += 1
        if updated:
            out.append({"security_id": sec.id, "name": sec.name, "lots": updated})
        if ratio_lots:
            # 整数口で評価額を再現できない = 連携先が違うファンドの兆候
            # （正しいファンドなら評価額は 口数×NAV そのもののため）
            w(
                f"「{sec.name}」の評価額の一部（{ratio_lots}件）を連携先の"
                "基準価額で再現できませんでした。連携先のファンドが正しいか"
                "確認してください"
            )
        if anchors and not underived and updated:
            # 手動評価額は基準価額連携に置き換わった。評価はすべて口数×NAVで
            # 再構成できるため、系列を汚す前に落とす
            for day in anchor_days:
                store.delete_price_row("manual", str(sec.id), day)
            w(
                f"「{sec.name}」の手動評価額 {len(anchor_days)} 件を削除しました"
                "（基準価額連携に置き換え）"
            )
    return out


def _pension_value_pairs(
    store: Store, sec: Security
) -> list[tuple[date, Decimal, Decimal, date, Decimal, Decimal]]:
    """年金銘柄の (前回, 今回) スナップショット対。値動き照合の材料。

    年金は基準価額の記載が無く verify_nav が使えないが、取込が2回分以上あれば
    「前回評価額 × 候補ファンドのNAV騰落率 + 掛金増分 ≒ 今回評価額」が成り立つ
    はずで、これが候補の裏取りになる。ロット（口座, lot_seq）ごとに日付順の
    隣接対を作り、掛金増分を出すため両端に取得価額がある対だけ使う。
    """
    by_lot: dict[tuple[int, int], list[Any]] = {}
    for snap in store.all_snapshots():
        if snap.security_id != sec.id:
            continue
        if snap.quantity == 0 or snap.reported_value_jpy is None:
            continue
        by_lot.setdefault((snap.account_id, snap.lot_seq), []).append(snap)
    pairs: list[tuple[date, Decimal, Decimal, date, Decimal, Decimal]] = []
    for snaps in by_lot.values():
        snaps.sort(key=lambda s: s.as_of_date)
        for prev, cur in zip(snaps, snaps[1:]):
            if prev.avg_cost is None or cur.avg_cost is None:
                continue
            if prev.as_of_date == cur.as_of_date:
                continue
            pairs.append(
                (prev.as_of_date, prev.reported_value_jpy, prev.avg_cost,
                 cur.as_of_date, cur.reported_value_jpy, cur.avg_cost)
            )
    pairs.sort(key=lambda p: p[3])
    return pairs[-PENSION_MOVEMENT_MAX_PAIRS:]


def _movement_check(
    pairs: list[tuple[date, Decimal, Decimal, date, Decimal, Decimal]],
    prices: dict[date, Decimal],
) -> tuple[bool | None, int]:
    """(全期間の値動きを候補NAVで説明できたか, 照合できた期間数)。

    予測 = 前回評価額 × NAV(今回)/NAV(前回) + 掛金増分（取得価額の差）。
    MF/iDeCo の表示は数営業日遅れのNAVを使うため、前回・今回とも窓内の
    全組み合わせを試して最良誤差で判定する（正しいファンドは実際に使われた
    基準日の組で誤差ほぼゼロになる）。正しいファンドは全期間で説明できるので、
    1期間でも外れたら不一致。同じ指数に連動する別ファンドは区別できない
    — だから証明ではなく裏取りであり、自動確定には使わない。
    """
    checked = passed = 0
    window = timedelta(days=PENSION_NAV_WINDOW_DAYS)
    for t1, v1, c1, t2, v2, c2 in pairs:
        navs1 = [p for d, p in prices.items() if t1 - window <= d <= t1]
        navs2 = [p for d, p in prices.items() if t2 - window <= d <= t2]
        if not navs1 or not navs2 or not v2:
            continue
        dc = c2 - c1
        err = min(
            abs(v1 * n2 / n1 + dc - v2) for n1 in navs1 for n2 in navs2
        )
        checked += 1
        if err <= max(v2 * PENSION_MOVEMENT_TOLERANCE, PENSION_MOVEMENT_MIN_ABS):
            passed += 1
    if not checked:
        return (None, 0)
    return (passed == checked, checked)


def suggest_links(
    store: Store,
    warn: WarnFn | None = None,
    search: Callable[..., list[dict[str, Any]]] | None = None,
    fetch_history: Callable[..., Any] | None = None,
    max_candidates: int = MAX_CANDIDATES,
    security_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """未連携の投信について連携候補を判定する。

    security_ids を渡すと、その銘柄だけを対象にする（取込直後に新しくできた
    銘柄だけを見る用途。全件だと投信協会への照会が数分かかるため）。

    ネットワーク: 1銘柄あたり 検索1〜3回 + 上位候補のCSV最大 max_candidates 回
    （providers.base のスロットルで直列化）。候補のCSVは daily_prices には
    保存しない（誤連携候補の系列で汚さない。確定後に ensure が取得する）。
    """
    w = warn or (lambda _m: None)
    do_search = search or toushin.search_funds
    do_history = fetch_history or toushin.fetch_history
    nav_cache: dict[str, dict[date, Decimal]] = {}
    only = set(security_ids) if security_ids is not None else None

    out: list[dict[str, Any]] = []
    for sec in store.list_securities():
        if only is not None and sec.id not in only:
            continue
        fund_unlinked = (
            sec.price_source_status == PriceSourceStatus.UNLINKED
            and sec.asset_class in (AssetClass.FUND_JP, AssetClass.FUND_FOREIGN)
        )
        # 年金（iDeCo・企業型DC）の中身も投信なので連携できる。連携すると
        # 評価額から口数を逆算し、日々の基準価額で自動評価される
        # （derive_pension_quantities）。年金は基準価額の記載が無く NAV 照合が
        # 効かないため、自動確定にはせず候補から人が選ぶ
        pension_linkable = (
            sec.asset_class == AssetClass.PENSION
            and sec.price_source_status != PriceSourceStatus.LINKED
        )
        if not (fund_unlinked or pension_linkable):
            continue
        out.append(
            _suggest_one(store, sec, w, do_search, do_history, nav_cache, max_candidates)
        )
    return out


def _suggest_one(
    store: Store,
    sec: Security,
    w: WarnFn,
    do_search: Callable[..., list[dict[str, Any]]],
    do_history: Callable[..., Any],
    nav_cache: dict[str, dict[date, Decimal]],
    max_candidates: int,
) -> dict[str, Any]:
    # 1. 候補収集（クエリを具体的→広い順に試し、最初にヒットした時点で打ち切り）
    #    do_search は list でも SearchResult でも受ける（テストは list を渡す）
    results: dict[str, dict[str, Any]] = {}
    search_reachable = False
    for query in build_queries(sec.name):
        found = do_search(query, warn=w)
        items = getattr(found, "items", found)
        if getattr(found, "reachable", True):
            search_reachable = True
        for r in items:
            results.setdefault(r["ref"], r)
        if results:
            break

    scored_all = sorted(
        (
            {**r, "score": name_score(sec.name, r["name"])}
            for r in results.values()
        ),
        key=lambda r: -r["score"],
    )
    scored = [r for r in scored_all if r["score"] >= MIN_CANDIDATE_SCORE][:max_candidates]
    if not scored and scored_all:
        # 極端な略称（例: 証券会社独自の短縮名）では名前スコアが伸びないが、
        # 基準価額照合が決定打になるため上位候補は照合に回す
        scored = [r for r in scored_all if r["score"] >= 0.1][:max_candidates]

    # 2. 基準価額照合。年金は基準価額の記載が無いので、代わりに取込2回分以上の
    #    評価額の推移を候補NAVの騰落率で説明できるか（値動き照合）を試す
    reported = store.latest_reported_price(sec.id)
    movement_pairs = (
        _pension_value_pairs(store, sec)
        if sec.asset_class == AssetClass.PENSION
        else []
    )
    candidates: list[dict[str, Any]] = []
    nav_unreachable = False
    for r in scored:
        nav_match: bool | None = None
        nav_date: date | None = None
        nav_value: Decimal | None = None
        movement_match: bool | None = None
        movement_periods = 0
        if reported is not None or movement_pairs:
            if r["ref"] not in nav_cache:
                hist = do_history(r["ref"], warn=w)
                nav_cache[r["ref"]] = getattr(hist, "prices", {}) or {}
                if not getattr(hist, "reachable", True):
                    nav_unreachable = True
            prices = nav_cache[r["ref"]]
            if prices and reported is not None:
                nav_match, nav_date, nav_value = verify_nav(
                    prices, reported[0], reported[1]
                )
                if nav_match:
                    # 表示は「一致した日の値」（＝MF記載値そのもの）。直近NAVを
                    # 出すと「N円で一致」の N が記載値とずれて紛らわしい
                    nav_value = reported[0]
            if prices and movement_pairs:
                movement_match, movement_periods = _movement_check(
                    movement_pairs, prices
                )
        candidates.append(
            {
                "name": r["name"],
                "ref": r["ref"],
                "company": r.get("company", ""),
                "score": r["score"],
                "nav_match": nav_match,
                "nav_date": nav_date.isoformat() if nav_date else None,
                "nav_value": str(nav_value) if nav_value is not None else None,
                "reported_price": str(reported[0]) if reported else None,
                "movement_match": movement_match,
                "movement_periods": movement_periods,
            }
        )

    # 3. 判定（自動確定は「NAV一致がちょうど1件」かつ名前が最低限似ている場合のみ。
    #    NAV一致は強い証拠だが、名前が全く違う候補との偶然の一致を保険で弾く。
    #    値動き照合は同一指数の別ファンドを区別できないため自動確定には使わず、
    #    並び順と表示の裏取りに留める）
    # reason は「なぜその判定になったか」。連携できなかったときに、協会へ
    # 届かなかったのか該当が無かったのかを利用者に出せるようにする
    matches = [c for c in candidates if c["nav_match"] is True]
    reason = "nav_matched"
    if len(matches) == 1 and matches[0]["score"] >= 0.2:
        status = "auto"
        best_ref = matches[0]["ref"]
        # 一致した候補を先頭に
        candidates.sort(key=lambda c: (c["nav_match"] is not True, -c["score"]))
    elif len(matches) > 1:
        # 複数一致は理論上ほぼ無いが（同一日の基準価額が偶然同じ円額）、
        # 万一のときは自動確定せずユーザーに委ねる
        status = "candidates"
        reason = "nav_matched_multiple"
        best_ref = None
        candidates.sort(key=lambda c: (c["nav_match"] is not True, -c["score"]))
    else:
        # 値動きが説明できた候補は名前スコアが低くても落とさない
        # （正式名が大きく略される年金で、正しい候補が閾値割れすることがある）
        viable = [
            c for c in candidates
            if c["score"] >= SUGGEST_MIN_SCORE or c["movement_match"] is True
        ]
        if viable:
            status = "candidates"
            reason = "nav_unavailable" if nav_unreachable else "nav_mismatch"
        elif not search_reachable:
            # 協会へ一度も届いていない。該当が無いのとは別物なので分けて出す
            status = "unavailable"
            reason = "search_unreachable"
        elif candidates:
            status = "none"
            reason = "nav_unavailable" if nav_unreachable else "nav_mismatch"
        else:
            status = "none"
            reason = "not_found"
        best_ref = None
        candidates = viable if viable else candidates
        if any(c["movement_match"] is not None for c in candidates):
            candidates.sort(
                key=lambda c: (c["movement_match"] is not True, -c["score"])
            )

    return {
        "security_id": sec.id,
        "name": sec.name,
        "status": status,
        "reason": reason,
        "best_ref": best_ref,
        "candidates": candidates,
    }
