"""取引履歴取込の司令塔: 解析 → プレビュー → 列対応の修正 → 確定。

既存の service.py（MF PDF）と同じ 2 段階契約:

- build_tx_preview(store, data, filename, account_name) -> dict
    解析して previewed バッチに保存し、列対応・銘柄照合・取引一覧を返す。
    元データは保持しない代わりに、グリッドを parse_report に入れて remap を可能にする。

- remap_tx_preview(store, batch_id, column_overrides, security_map) -> dict
    利用者が直した対応で再解析する。

- commit_tx_batch(store, batch_id, ...) -> dict
    単一トランザクションで取引を投入し、取得原価を再計算し、書式と銘柄名を学習する。

判定エンジン（txlog）は DB を知らない純関数なので、DB に触れるのはここだけ。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..core.cost_basis import CostWarning, GroupResult, reconcile_all
from ..core.models import (
    AssetClass,
    Coverage,
    ImportBatch,
    PriceSourceStatus,
    Security,
    Transaction,
    TxType,
    Unit,
)
from ..core.store import Store, StoreError
from .base import make_name_key, normalize_code
from .service import DuplicateImportError
from .txlog import contracts as C
from .txlog.contracts import CanonicalField as F
from .txlog.classify import has_no_movement
from .txlog.engine import detect_format, parse_grid
from .txlog.grid import load_grid, load_pasted
from .txlog.shapes import split_leading_code

SOURCE_KIND = "broker_csv"
ALIAS_SOURCE = "broker_csv"

# プレビューで保持するグリッドの上限（parse_report は TEXT 列なので青天井にしない）
MAX_PREVIEW_CELLS = 20_000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# ----------------------------------------------------------------------
# 既知の銘柄・口座（判定エンジンへの唯一の DB 入力）
# ----------------------------------------------------------------------


def build_universe(store: Store) -> C.KnownUniverse:
    """DB にある銘柄・口座を判定エンジンに渡せる形にする。

    これが「当日の評価額を取得できている時点で銘柄は固定できている」を
    実装に落としている部分。列判定でも行の照合でも同じ集合を使う。
    """
    securities = [
        C.KnownSecurity(
            id=s.id,
            code=s.code,
            name=s.name,
            name_key=s.name_key,
            price_unit_divisor=s.price_unit_divisor,
            currency=s.currency,
            asset_class=s.asset_class.value,
        )
        for s in store.list_securities()
        if s.id is not None
    ]
    by_alias: dict[str, C.KnownSecurity] = {}
    by_id = {s.id: s for s in securities}
    # 同じ別名が複数の source_kind にあるときは取引履歴側を優先する
    # （by_alias は先勝ちなので並び順で決める）。
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT alias_key, security_id FROM security_aliases
               ORDER BY CASE WHEN source_kind = ? THEN 0 ELSE 1 END, alias_key""",
            (ALIAS_SOURCE,),
        ).fetchall()
    for row in rows:
        sec = by_id.get(row["security_id"])
        if sec is not None:
            by_alias.setdefault(row["alias_key"], sec)

    return C.KnownUniverse(
        securities=tuple(securities),
        by_code={s.code: s for s in securities if s.code},
        by_name_key={s.name_key: s for s in securities},
        by_alias=by_alias,
        known_codes=frozenset(s.code for s in securities if s.code),
        account_names=frozenset(a.name for a in store.list_accounts()),
    )


# ----------------------------------------------------------------------
# 銘柄の照合
# ----------------------------------------------------------------------


def resolve_security(
    store: Store, universe: C.KnownUniverse, code_raw: str | None, name_raw: str
) -> tuple[int | None, str]:
    """(security_id, 照合方法)。code → alias → name_key。

    証券会社ごとに銘柄名の表記が揺れても、コードか名寄せキーで既存銘柄に
    たどり着ければ同じ銘柄として扱える。確定時に別名を覚えるので、
    次回以降は名前が違っていても一発で当たる。

    照合は **すべて KnownUniverse の中だけで完結させる**（DB を引かない）。
    build_universe が銘柄も別名も全件読んでいるので DB に問い合わせても
    同じ答えしか返らず、一方で 1 行につき 1 接続を開くことになる。
    数千行のファイルで行数ぶんの接続を開いていた。store は呼び出し側の
    都合で受け取っているだけで、ここでは使わない。
    """
    code = normalize_code(code_raw, set(universe.known_codes)) if code_raw else None
    if code is None and name_raw:
        lead, _rest = split_leading_code(name_raw)
        if lead:
            code = normalize_code(lead, set(universe.known_codes))
    if code:
        sec = universe.by_code.get(code)
        if sec is not None:
            return (sec.id, "code")

    name = name_raw or ""
    if name:
        _lead, rest = split_leading_code(name)
        for candidate in (name, rest):
            key = make_name_key(candidate)
            if not key:
                continue
            sec = universe.by_alias.get(key) or universe.by_name_key.get(key)
            if sec is not None:
                return (sec.id, "name")
    return (None, "unmatched")


def _price_tolerance(value: Decimal) -> Decimal:
    """表示のまるめ 1 単位ぶんの許容差。

    CSV の単価は表示上まるめられているので、最後の桁 1 つ分だけ許す。
    相対誤差にすると基準価額 7,849 で 7 円まで許してしまい、似た投信を
    区別できなくなる。
    """
    exponent = value.as_tuple().exponent
    return Decimal(1).scaleb(exponent) if isinstance(exponent, int) else Decimal(1)


# 翌営業日の基準価額を探しにいく上限（金・土・日・祝を跨げる長さ）。
_NEXT_SESSION_DAYS = 4


def _next_session_price(
    series: dict[str, Decimal], day: str
) -> Decimal | None:
    """その日より後、最初に価格のある日の値（土日祝を跨ぐので数日ぶん探す）。"""
    base = date.fromisoformat(day)
    for step in range(1, _NEXT_SESSION_DAYS + 1):
        got = series.get((base + timedelta(days=step)).isoformat())
        if got is not None:
            return got
    return None


def _best_alignment(
    series: dict[str, Decimal], pairs: list[tuple[str, Decimal]]
) -> tuple[int, int, bool]:
    """(一致した数, 検証できた数, 翌営業日ぶんを使ったか)。

    約定単価は「約定日の基準価額」か「その翌営業日の基準価額」のどちらか。
    海外資産型は翌営業日に約定するし、証券会社によって申込日を約定日欄に
    書くこともある。実データのある海外REIT型ファンドでは
    **同じ銘柄の中で時期によって混在** していた — 2011〜2013 年は翌営業日、
    2020 年以降は当日。1 つのずらし方で全期間そろえる規則では説明できない。

    そこで行ごとにどちらかを許す。緩めた分の誤検出は、実データの全銘柄 ×
    候補すべてで測って確かめてある（別銘柄が全日一致することは無かった）。
    価格が無い日は「検証できない」として数に入れない（不一致と区別する）。
    """
    matched = tested = 0
    shifted = False
    for day, price in pairs:
        tol = _price_tolerance(price)
        same = series.get(day)
        nxt = _next_session_price(series, day)
        if same is None and nxt is None:
            continue
        tested += 1
        if same is not None and abs(same - price) <= tol:
            matched += 1
        elif nxt is not None and abs(nxt - price) <= tol:
            matched += 1
            shifted = True
    return (matched, tested, shifted)


def verify_by_price(
    store: Store, security_id: int, pairs: list[tuple[str, Decimal]],
    cache: dict[int, dict[str, Decimal]] | None = None,
) -> tuple[int, int]:
    """(一致した数, 検証できた数)。取引の約定日・単価を既存銘柄の価格と突き合わせる。

    名前の似ている別銘柄を排除するための裏取り。fund_autolink が投信の候補を
    基準価額で確かめているのと同じ考えで、そこでは為替ヘッジ有無の紛らわしい
    亜種を正しく区別できている。

    provider へは取りに行かない。取込のたびに外部へ問い合わせると遅く、
    レート制限にも当たる。ここで見るのは手元に既にある価格だけ。
    """
    series = _price_series(store, security_id, cache)
    if series is None:
        return (0, 0)
    matched, tested, _shifted = _best_alignment(series, pairs)
    return (matched, tested)


def _price_series(
    store: Store, security_id: int, cache: dict[int, dict[str, Decimal]] | None
) -> dict[str, Decimal] | None:
    series = (cache or {}).get(security_id)
    if series is None:
        sec = store.get_security(security_id)
        if sec is None:
            return None
        series, _ccy = store.price_series_for_security(sec, start=None, end=None)
        if cache is not None:
            cache[security_id] = series
    return series


# ファンドの名前を構成する「資産クラス」と「地域」の語。ファンドがこの語を
# 跨いで改名することは無い（債券ファンドが株式ファンドに名前を変えることは
# 無い）ので、両者に語があって食い違えば別物と確定できる。単なるブランド名の
# 一致（eMAXIS / STAM / 三井住友）に候補が引っ張られるのを止める。
# 「外国」は先進国とも全世界とも読めるため入れない。1 文字の語も誤爆するので
# 入れない（「米」は「米グロース」以外にも現れる）。
_CATEGORY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("株式",), ("債券",), ("リート", "reit"), ("バランス",),
)
_REGION_GROUPS: tuple[tuple[str, ...], ...] = (
    ("国内", "日本"), ("先進国",), ("新興国",),
    ("全世界", "オールカントリー"), ("米国",), ("中国", "チャイナ"),
)


def _bucket_of(name: str, groups: tuple[tuple[str, ...], ...]) -> int | None:
    hits = [i for i, tokens in enumerate(groups) if any(t in name for t in tokens)]
    return hits[0] if len(hits) == 1 else None    # 複数当たる名前は判定に使わない


def _category_conflict(a: str, b: str) -> bool:
    """資産クラスか地域の語が両方にあって食い違うか。"""
    from ..core.fund_autolink import normalize as fund_normalize

    na, nb = fund_normalize(a), fund_normalize(b)
    for groups in (_CATEGORY_GROUPS, _REGION_GROUPS):
        ba, bb = _bucket_of(na, groups), _bucket_of(nb, groups)
        if ba is not None and bb is not None and ba != bb:
            return True
    return False


def suggest_securities(
    universe: C.KnownUniverse,
    name_raw: str,
    limit: int = 5,
    *,
    store: Store | None = None,
    price_pairs: list[tuple[str, Decimal]] | None = None,
    cache: dict[int, dict[str, Decimal]] | None = None,
) -> list[dict[str, Any]]:
    """未照合の銘柄名に近い既存銘柄を挙げる（利用者に選ばせるため）。

    名前が似ているだけの別銘柄を掴まないよう、価格が手元にある候補は
    約定単価と突き合わせて裏を取る。一致すれば強い証拠、食い違えば
    名前が似ていても別銘柄だと分かる。
    """
    from ..core.fund_autolink import name_score, normalize as fund_normalize

    if not name_raw:
        return []
    target = fund_normalize(name_raw)
    scored = []
    for sec in universe.securities:
        score = name_score(target, fund_normalize(sec.name))
        if score >= 0.5:
            scored.append((score, sec))
    scored.sort(key=lambda kv: -kv[0])

    out: list[dict[str, Any]] = []
    for score, sec in scored[:limit]:
        item: dict[str, Any] = {
            "security_id": sec.id, "name": sec.name, "code": sec.code,
            "score": round(score, 3), "price_checked": 0, "price_matched": 0,
            "price_verdict": "unknown", "price_shifted": False,
        }
        if store is not None and price_pairs:
            series = _price_series(store, sec.id, cache)
            if series is not None:
                matched, tested, shifted = _best_alignment(series, price_pairs)
                item["price_checked"] = tested
                item["price_matched"] = matched
                item["price_shifted"] = shifted
                if tested:
                    item["price_verdict"] = "match" if matched == tested else (
                        "partial" if matched else "mismatch"
                    )
        # 価格で何も言えないときだけ、名前による否定を使う。価格の証拠
        # （match/partial/mismatch）があるならそちらが強い — Smart-iゴールド
        # FHなし は識別部分の字面（FH ⇔ 為替ヘッジ）がまるで違うが、価格
        # 3/3 一致で同一と確定している。名前の判定を先に立てると、こういう
        # 略称の同一ファンドを落とす。
        if item["price_verdict"] == "unknown":
            if _category_conflict(name_raw, sec.name):
                item["category_conflict"] = True
            elif _refined_name_score(name_raw, sec.name, score) < 0.5:
                # ブランド名（共有する接頭辞）と一般語（ファンド等）を除いた
                # 識別部分が一致しない。「三井住友」「ファンド」の一致だけで
                # 候補に立っていた。
                item["name_refuted"] = True
        out.append(item)

    # 価格で裏の取れた候補を先頭へ、否定された候補（価格不一致・カテゴリ違い）を末尾へ。
    _order = {"match": 0, "partial": 1, "unknown": 2, "mismatch": 3}
    out.sort(key=lambda x: (
        4 if (x.get("category_conflict") or x.get("name_refuted"))
        else _order[x["price_verdict"]], -x["score"]))
    return out


# 価格の裏取りから外す取引種別 —「単価がその日の時価でないと分かっている」もの。
#
# 入庫・出庫の単価は移管元から引き継いだ *取得単価* であって、その日の
# 基準価額ではない。実データで確かめると、同じ運用会社の投信 9 本は
# 売買 75 行が 75 行とも一致したのに対し、入出庫 19 行は 19 行とも
# 外れた（比 1.20〜1.29 = 値上がりぶん）。混ぜると裏取りが「半分一致」に
# 落ちて、正しい候補まで採用できなくなる。
# 配当・特別分配金・分割・現金の移動も単価の意味が違うので同じ理由で外す。
#
# 逆に **区分を判別できなかった行（other）は外さない**。マネックスは投信の
# つみたて買付で「取引」列を空欄にするため、積立中心の銘柄では大半の行が
# それになる。外すと裏を取る材料がほぼ無くなる。判別できない行を
# 混ぜて価格が合わなければ「一致」にならず自動紐付けを見送るだけなので、
# 失敗の向きは安全側（結びつけ損なう）であって誤結合ではない。
_NON_MARKET_PRICE_TYPES = frozenset({
    TxType.TRANSFER_IN.value, TxType.TRANSFER_OUT.value,
    TxType.DIVIDEND.value, TxType.RETURN_OF_CAPITAL.value,
    TxType.SPLIT.value, TxType.CASH_IN.value, TxType.CASH_OUT.value,
})


def _price_confirms(suggestion: dict[str, Any]) -> bool:
    """その候補を「価格が裏づけた」と見てよいか。

    全日一致が原則。ただし検証できた日数が十分あるときだけ、数日の食い違いを
    許す（長期の履歴には価格の訂正などで数日ずれる行が混じるため）。
    """
    matched = suggestion["price_matched"]
    tested = suggestion["price_checked"]
    if matched < C.MIN_PRICE_VERIFY_DAYS:
        return False
    if matched == tested:
        return True
    return (tested >= C.MIN_PRICE_VERIFY_DAYS_WITH_STRAYS
            and matched / tested >= C.PRICE_VERIFY_PASS_RATE)


def _auto_link(
    store: Store,
    universe: C.KnownUniverse,
    txs: list[Any],
    account_id: int | None = None,
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """未照合の銘柄名に候補を出し、価格で裏の取れたものは自動で結びつける。

    返り値は (銘柄名 → security_id, 銘柄名 → 候補などの明細)。

    証券会社は銘柄名を切り詰める（マネックスは約16文字で「ファンド」→「F」）。
    長い履歴では百近い銘柄になり、表記揺れを 1 件ずつ選ばせるのは現実的でない。
    一方 **約定単価がその日の既存銘柄の価格と全日一致する** なら、名前が
    略されていても同じ銘柄と見てよい。名前の類似度だけでは踏み込めない
    ところを価格が裏づける形で、根拠（何日中何日一致したか）は画面に出して
    解除できるようにする。

    裏の取れた候補が 2 つ以上あるときは、**取込先の口座が実際に持っている
    ほう** を選ぶ。同じファンドを複数の証券会社で持っていると、MF PDF が
    証券会社ごとの表記をそのまま書くために銘柄が別々に登録され、価格系列が
    同一なので価格だけでは区別できない（同じ ISIN の投信が、証券会社ごとの
    表記で 2 銘柄に分かれて登録される）。取引はどれか 1 つの
    口座のものなので、その口座の保有が決め手になる。

    それでも 1 つに絞れなければ自動では決めない。
    """
    entries: dict[str, dict[str, Any]] = {}
    pairs: dict[str, list[tuple[str, Decimal]]] = {}
    explained: set[int] = set()
    resolved_codes: dict[str, int] = {}
    zero_codes: dict[str, set[str]] = {}
    for tx in txs:
        name = tx.security_name_raw
        if not name or tx.tx_type in (TxType.CASH_IN.value, TxType.CASH_OUT.value):
            continue
        code = (tx.security_code_raw or "").strip()
        resolved, _how = resolve_security(store, universe, tx.security_code_raw, name)
        if resolved is not None:
            explained.add(resolved)
            if code:
                resolved_codes.setdefault(code, resolved)
            continue
        if tx.raw.get("margin"):
            # 信用取引は設計上取り込まない。その銘柄の行が信用だけなら、
            # 結びつけを決めても使い道が無い — 決めることの無い銘柄を表に
            # 並べない（実データでも十数銘柄が判断を求める顔で並んでいた）。
            continue
        if has_no_movement(tx):
            # 増減ゼロの行は数えないが、コードは改称の数珠つなぎの証拠になる。
            # 実データでは、改称前後の名前をつなぐ十数行がすべてゼロ額の
            # 分配金行で、捨てると 3 代にわたる改称の連結がちぎれた。
            if code:
                zero_codes.setdefault(name, set()).add(code)
            continue
        entry = entries.setdefault(name, {
            "name": name, "code": tx.security_code_raw,
            "count": 0, "suggestions": [],
            "net_quantity": Decimal(0), "undetermined": 0,
            "first_date": None, "last_date": None,
        })
        if code:
            entry.setdefault("_codes", set()).add(code)
        entry["count"] += 1
        if tx.tx_type == TxType.OTHER.value:
            # 増減の向きが分からない行。数量を足すと残高を誤って出すので数だけ数える。
            entry["undetermined"] += 1
        elif tx.quantity is not None:
            entry["net_quantity"] += tx.quantity
        if tx.trade_date is not None:
            day = tx.trade_date.isoformat()
            if entry["first_date"] is None or day < entry["first_date"]:
                entry["first_date"] = day
            if entry["last_date"] is None or day > entry["last_date"]:
                entry["last_date"] = day
        if (tx.tx_type not in _NON_MARKET_PRICE_TYPES
                and not tx.raw.get("off_market_price")
                and tx.trade_date is not None and tx.unit_price):
            pairs.setdefault(name, []).append(
                (tx.trade_date.isoformat(), tx.unit_price)
            )

    for name, codes in zero_codes.items():
        if name in entries:
            entries[name]["_codes"] = entries[name].get("_codes", set()) | codes
    _merge_entries_by_code(entries, pairs)

    held = store.security_ids_in_account(account_id) if account_id else set()

    expanded_resolved: dict[str, int] = {}
    for c, sid in resolved_codes.items():
        for k in _expand_code(c):
            expanded_resolved.setdefault(k, sid)

    cache: dict[int, dict[str, Decimal]] = {}
    links: dict[str, int] = {}
    for name, entry in entries.items():
        # ファイル内の同じコードが既存銘柄に解決しているなら、この名前も
        # その銘柄の旧称。名前や価格を見るまでもなく確定する。
        by_code = {
            expanded_resolved[k]
            for c in entry.get("_codes", ())
            for k in _expand_code(c)
            if k in expanded_resolved
        }
        if len(by_code) == 1:
            sid = by_code.pop()
            sec = next((s for s in universe.securities if s.id == sid), None)
            if sec is not None:
                links[name] = sid
                for alias in entry.get("aliases", []):
                    links[alias] = sid
                entry["auto_linked"] = {
                    "security_id": sid, "name": sec.name, "code": sec.code,
                    "score": None, "price_checked": 0, "price_matched": 0,
                    "price_verdict": "unknown", "price_shifted": False,
                    "account_match": False, "code_match": True,
                }
        entry["suggestions"] = suggest_securities(
            universe, name, store=store,
            price_pairs=pairs.get(name), cache=cache,
        )
        confirmed = [s for s in entry["suggestions"] if _price_confirms(s)]
        by_account = False
        if len(confirmed) > 1 and held:
            narrowed = [s for s in confirmed if s["security_id"] in held]
            if len(narrowed) == 1:
                confirmed, by_account = narrowed, True
        if len(confirmed) == 1 and "auto_linked" not in entry:
            links[name] = int(confirmed[0]["security_id"])
            for alias in entry.get("aliases", []):
                links[alias] = links[name]
            entry["auto_linked"] = {**confirmed[0], "account_match": by_account}
        elif len(confirmed) > 1:
            entry["ambiguous"] = [s["name"] for s in confirmed]

        # ファイルが示す残高。「売り切った銘柄」と「名前が違うだけでまだ持って
        # いる銘柄」を分ける唯一の手がかりで、これが無いと利用者は判断できない。
        # 実データでは残高の残る二十数銘柄が「候補なし」に埋もれており、
        # まとめて売却済み登録すると保有中の銘柄を二重に作るところだった
        # （いずれも DB には別表記で存在していた）。
        entry["net_quantity"] = _s(entry["net_quantity"])
        entry["closed_out"] = (
            entry["undetermined"] == 0 and entry["net_quantity"] == "0"
        )

    # 同じファイルの中で、候補の銘柄が別のコードで現れているなら別物と確定。
    # コードは改称でも変わらないので、名前や価格より強い証拠になる。
    # （解決済みの行と、結びつけ済みエントリの両方からコードを集める）
    codes_of_sid: dict[int, set[str]] = {}
    for c, sid in resolved_codes.items():
        codes_of_sid.setdefault(sid, set()).add(c)
    for linked_name, sid in links.items():
        ent = entries.get(linked_name)
        if ent:
            codes_of_sid.setdefault(sid, set()).update(ent.get("_codes", ()))
    for entry in entries.values():
        ecodes = entry.get("_codes") or set()
        if not ecodes or "auto_linked" in entry:
            continue
        for s in entry["suggestions"]:
            scodes = set(codes_of_sid.get(s["security_id"], set()))
            if s.get("code"):
                scodes.add(s["code"])
            if (s["price_verdict"] == "unknown" and scodes
                    and not _codes_overlap(ecodes, scodes)):
                s["code_conflict"] = True
    for entry in entries.values():
        entry.pop("_codes", None)

    explained |= set(links.values())
    unexplained = _unexplained_holdings(store, universe, account_id, explained)
    _link_by_quantity(entries, links, unexplained)
    return links, entries, unexplained


# 銘柄を区別する力の無い一般語。どのファンドにも付きうるので、名前一致の
# 最終判定からは外す（「ファンド」が一致しても同じ銘柄の証拠にならない）。
_GENERIC_NAME_WORDS = (
    "インデックスファンド", "投資信託", "インデックス", "ファンド",
    "オープン", "投信", "index", "fund",
)


def _refined_name_score(name_a: str, name_b: str, base: float) -> float:
    """ブランド名と一般語を除いた「識別部分」だけの類似度。

    「三井住友ニューチャイナファンド」と「三井住友・DC外国リートインデックス
    ファンド」は、共有する「三井住友」と、どれにでも付く「ファンド」が
    類似度を持ち上げているだけで、識別部分（ニューチャイナ / DC外国リート）は
    まるで違う。ブランドの辞書は持たない — **両者が共有する接頭辞**が
    ブランド・シリーズ名そのもので、共有している以上その部分は 2 つを
    区別できない。

    片方の識別部分が空になったら（一方が他方の接頭辞）、それは強い一致
    なので元の類似度をそのまま使う。
    """
    from difflib import SequenceMatcher

    from ..core.fund_autolink import normalize as fund_normalize

    a, b = fund_normalize(name_a), fund_normalize(name_b)
    match = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    # 共通の「接頭辞」だけをブランドとみなす（途中の一致は識別情報かもしれない）
    prefix = match.size if (match.a == 0 and match.b == 0) else 0
    if prefix >= 2:
        a, b = a[prefix:], b[prefix:]
    for w in _GENERIC_NAME_WORDS:
        a, b = a.replace(w, ""), b.replace(w, "")
    if not a or not b:
        return base
    ratio = SequenceMatcher(None, a, b).ratio()
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        ratio = max(ratio, len(shorter) / len(longer))
    return round(ratio, 4)


_OLD_FUND_CODE_RE = re.compile(r"^0(\d{4})0000$")


def _expand_code(code: str) -> set[str]:
    """同じ銘柄を指しうるコード表記の集合。

    マネックスの投信コードには新旧 2 形式があり、9 桁の旧形式は
    「0 + 4桁コード + 0000」の詰め物（同じファンドが 0XXXX0000 と XXXX の
    両方で現れる）。株式の 5 桁末尾 0（XXXX0 → XXXX）は
    normalize_code が処理する。展開して比べれば、新旧の時代をまたいでも
    同じ銘柄と分かる。
    """
    keys = set()
    code = (code or "").strip()
    if not code:
        return keys
    keys.add(code)
    m = _OLD_FUND_CODE_RE.match(code)
    if m:
        keys.add(m.group(1))
    normalized = normalize_code(code)
    if normalized:
        keys.add(normalized)
    return keys


def _codes_overlap(a: set[str], b: set[str]) -> bool:
    ea = set().union(*(_expand_code(c) for c in a)) if a else set()
    eb = set().union(*(_expand_code(c) for c in b)) if b else set()
    return bool(ea) and bool(eb) and not ea.isdisjoint(eb)


def _merge_entries_by_code(
    entries: dict[str, dict[str, Any]],
    pairs: dict[str, list[tuple[str, Decimal]]],
) -> None:
    """同じ銘柄コードを共有する名前を 1 つのエントリに束ねる（破壊的）。

    改称や移管があっても証券会社の内部コードは変わらない。実データでは
    運用会社の変更で 3 代にわたり改称されたファンドが、2 つのコード
    （9 桁の旧コードと 4 桁の新コード）で数珠つなぎになっていた。
    共有で連結するので、こうした乗り換えも 1 つに畳める。

    束ねないと、同じ銘柄が別々の行に割れて「ファイル上の増減」が
    +120,000 / +20,000 / -140,000 のように分かれて見え、売り切ったことが
    読み取れない（束ねれば ±0）。表示名は最後に取引された名前 — それが
    現在の正式名である可能性が最も高い。
    """
    # コード → 名前集合 から連結成分を作る（Union-Find の簡易版）
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_code: dict[str, str] = {}
    for name, entry in entries.items():
        for code in entry.get("_codes", ()):
            for key in _expand_code(code):
                if key in by_code:
                    union(name, by_code[key])
                else:
                    by_code[key] = name

    groups: dict[str, list[str]] = {}
    for name in entries:
        groups.setdefault(find(name), []).append(name)

    for members in groups.values():
        if len(members) < 2:
            continue
        # 最後に取引された名前を代表にする
        primary = max(members, key=lambda n: entries[n]["last_date"] or "")
        merged = entries[primary]
        merged["aliases"] = sorted(n for n in members if n != primary)
        for name in merged["aliases"]:
            other = entries.pop(name)
            merged["count"] += other["count"]
            merged["undetermined"] += other["undetermined"]
            merged["net_quantity"] += other["net_quantity"]
            merged["_codes"] = merged.get("_codes", set()) | other.get("_codes", set())
            for key, better in (("first_date", min), ("last_date", max)):
                vals = [v for v in (merged[key], other[key]) if v]
                merged[key] = better(vals) if vals else None
            if name in pairs:
                pairs.setdefault(primary, []).extend(pairs.pop(name))


def _unexplained_holdings(
    store: Store,
    universe: C.KnownUniverse,
    account_id: int | None,
    explained: set[int],
) -> list[dict[str, Any]]:
    """その口座の保有のうち、CSV のどの行にも結びついていないもの。

    DESIGN の方針は「MF PDF のスナップショットを総量の錨とし、CSV で説明できる
    分を差し引いた残余を期首ロットとして復元する」。同じ引き算を銘柄の照合にも
    使う — 残った保有は、CSV のどれかの名前がそれを指しているはずだからだ。

    数十銘柄の一覧から選ばせるのではなく、この数件から選べばよくなる
    （実データでも、口座の保有のうち残ったのは数件だった）。
    """
    if not account_id:
        return []
    by_id = {sec.id: sec for sec in universe.securities}
    out: list[dict[str, Any]] = []
    for sid, qty in store.current_quantities_in_account(account_id).items():
        if sid in explained:
            continue
        sec = by_id.get(sid)
        if sec is None:
            continue
        # 現金・ポイント・年金は取引履歴に銘柄として現れない（入出金の行に
        # 銘柄名は無い）。「未対応」と出しても対応する行が存在せず、利用者を
        # 迷わせるだけなので、この一覧の対象から外す。
        if sec.asset_class in (AssetClass.CASH.value, AssetClass.POINT.value,
                               AssetClass.PENSION.value):
            continue
        out.append({"security_id": sid, "name": sec.name, "code": sec.code,
                    "quantity": _s(qty)})
    out.sort(key=lambda h: h["name"])
    return out


def _link_by_quantity(
    entries: dict[str, dict[str, Any]],
    links: dict[str, int],
    unexplained: list[dict[str, Any]],
) -> None:
    """増減が未説明の保有と同じ数量なら、その銘柄として結びつける。

    名前が略されていて価格も手元に無いときの最後の手がかり。実データでは
    略称と正式名称の組がこれで決まった — 名前の類似度は 0.43 で
    しきい値に届かず、候補にすら出ていなかった。

    数量が一致する組が **双方向で 1 対 1 のときだけ** 採る。同じ数量の保有が
    複数あれば決め手にならない（実データでも、同数量の別銘柄 2 つが
    どちらも同じ保有に一致して、選べなかった例がある）。
    """
    open_names = [
        name for name, e in entries.items()
        if name not in links and e["undetermined"] == 0
        and e["net_quantity"] not in (None, "0")
        and not e["net_quantity"].startswith("-")
    ]
    for holding in unexplained:
        same = [n for n in open_names
                if entries[n]["net_quantity"] == holding["quantity"]]
        if len(same) != 1:
            continue
        name = same[0]
        rival = [h for h in unexplained
                 if h["quantity"] == holding["quantity"] and h is not holding]
        if rival:
            continue
        links[name] = int(holding["security_id"])
        entries[name]["auto_linked"] = {
            "security_id": holding["security_id"], "name": holding["name"],
            "code": holding["code"], "score": None,
            "price_checked": 0, "price_matched": 0, "price_verdict": "unknown",
            "price_shifted": False, "account_match": True,
            "quantity_match": holding["quantity"],
        }


# ----------------------------------------------------------------------
# 売却済み銘柄の新規登録
#
# 「既存銘柄に無い」は売却済みとは限らず、MF PDF が拾っていない口座の銘柄
# かもしれない（保有中の銘柄を別名で二重に作ると保有が割れる）。だから
# 自動では作らず、利用者が明示的に選んだときだけ作る。
# ----------------------------------------------------------------------


def _infer_divisor(rows: list[dict[str, Any]]) -> int:
    """その銘柄の取引から 1 口あたりか 1万口あたりかを決める。

    投信の基準価額は 1万口あたりなので、取り違えると取得原価が 10000 倍ずれる。
    数量×単価÷divisor が金額と合うほうを採る。
    """
    best, best_hits = 1, 0
    for divisor in (1, 10000):
        hits = 0
        for row in rows:
            qty, price = _dec(row.get("quantity")), _dec(row.get("unit_price"))
            amount = _dec(row.get("gross_amount")) or _dec(row.get("net_amount"))
            if not qty or not price or not amount:
                continue
            expected = abs(qty) * abs(price) / Decimal(divisor)
            tol = Decimal(1) + abs(qty) * Decimal("0.5") / Decimal(divisor)
            if abs(expected - abs(amount)) <= tol:
                hits += 1
        if hits > best_hits:
            best, best_hits = divisor, hits
    return best


def _infer_currency(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        ccy = row.get("currency") or "JPY"
        counts[ccy] = counts.get(ccy, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else "JPY"


def build_new_security(
    name: str, rows: list[dict[str, Any]], known_codes: set[str]
) -> Security:
    """売却済みとして登録する銘柄。取引から読み取れる範囲で属性を埋める。

    inactive=True にして保有一覧・総資産には出さない。価格ソースは要らない
    （もう持っていないので評価しない）。分からない属性は決め打たず、
    利用者が手動登録画面で直せるようにしておく。
    """
    code = None
    for row in rows:
        code = normalize_code(row.get("security_code"), known_codes)
        if code:
            break
    divisor = _infer_divisor(rows)
    if code:
        asset_class, unit = AssetClass.STOCK_JP, Unit.SHARE
    elif divisor == 10000:
        asset_class, unit = AssetClass.FUND_JP, Unit.KUCHI
    else:
        asset_class, unit = AssetClass.OTHER, Unit.SHARE
    return Security(
        code=code,
        name=name,
        name_key=make_name_key(name),
        asset_class=asset_class,
        currency=_infer_currency(rows),
        unit=unit,
        price_unit_divisor=divisor,
        price_source_status=PriceSourceStatus.NOT_REQUIRED,
        inactive=True,
    )


# ----------------------------------------------------------------------
# 同一性の鍵
# ----------------------------------------------------------------------


def make_dedup_key(
    *,
    account_id: int,
    security_key: str,
    trade_date: str,
    tx_type: str,
    quantity: str | None,
    unit_price: str | None,
    gross_amount: str | None,
    lot_label: str | None,
    occurrence: int,
    broker_ref: str | None = None,
) -> str:
    """取引の同一性。期間が重なる再取込を冪等にするための鍵。

    原文行のハッシュにはしない — 列順や備考が変わった再出力で同じ約定が
    二重計上されてしまう。意味のある値だけを正規化して混ぜる。
    occurrence は「同じ内容がこのファイル内で何度目か」で、同日同内容の
    本当に別々の約定を潰さないために要る。
    """
    if broker_ref:
        payload = f"{account_id}|ref:{broker_ref}"
    else:
        payload = "|".join(
            [
                str(account_id), f"sec:{security_key}", trade_date, tx_type,
                quantity or "", unit_price or "", gross_amount or "",
                lot_label or "", str(occurrence),
            ]
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ----------------------------------------------------------------------
# 解析 → プレビュー
# ----------------------------------------------------------------------


def _load(data: bytes | None, text: str | None, filename: str | None, sheet: str | None):
    if text is not None:
        return load_pasted(text, filename=filename)
    return load_grid(data or b"", filename=filename, sheet=sheet)


def _grid_payload(grid) -> dict[str, Any]:
    cells = grid.height * max(grid.width, 1)
    if cells > MAX_PREVIEW_CELLS:
        return {"truncated": True, "rows": [], "types": None}
    return {
        "truncated": False,
        "rows": [list(r) for r in grid.rows],
        "types": [list(r) for r in grid.types] if grid.types else None,
        "meta": {
            "kind": grid.meta.kind,
            "encoding": grid.meta.encoding,
            "delimiter": grid.meta.delimiter,
            "delimiter_mode": grid.meta.delimiter_mode,
            "sheet_name": grid.meta.sheet_name,
            "sheet_names": list(grid.meta.sheet_names),
            "filename": grid.meta.filename,
            "warnings": list(grid.meta.warnings),
        },
    }


def _grid_from_payload(payload: dict[str, Any]):
    meta = payload.get("meta") or {}
    return C.SheetGrid(
        rows=tuple(tuple(r) for r in payload.get("rows", [])),
        types=tuple(tuple(r) for r in payload["types"]) if payload.get("types") else None,
        meta=C.SourceMeta(
            kind=meta.get("kind", "csv"),
            encoding=meta.get("encoding"),
            delimiter=meta.get("delimiter"),
            delimiter_mode=meta.get("delimiter_mode", "char"),
            sheet_name=meta.get("sheet_name"),
            sheet_names=tuple(meta.get("sheet_names") or ()),
            filename=meta.get("filename"),
            warnings=tuple(meta.get("warnings") or ()),
        ),
    )


def build_tx_preview(
    store: Store,
    data: bytes | None = None,
    filename: str = "upload.csv",
    *,
    text: str | None = None,
    account_name: str | None = None,
    sheet: str | None = None,
) -> dict[str, Any]:
    """取引履歴を解析してプレビューを返し、previewed バッチとして保存する。"""
    payload_bytes = data if data is not None else (text or "").encode("utf-8")
    sha256 = hashlib.sha256(payload_bytes).hexdigest()
    existing = store.find_committed_batch_by_sha(sha256)
    if existing is not None:
        raise DuplicateImportError(
            existing.id,
            existing.as_of_date.isoformat() if existing.as_of_date else None,
        )

    grid = _load(data, text, filename, sheet)
    universe = build_universe(store)
    fmt = detect_format(grid, universe)

    # 覚えている書式があれば対応を先に当てる。ただし算術検算は必ずやり直す —
    # 証券会社が見出しはそのままに列の意味を変えた場合、覚えた対応をそのまま
    # 使うと誤りを永久に引きずるため。
    profile_applied = None
    if fmt.fingerprint:
        profile = store.get_format_profile(fmt.fingerprint)
        if profile and profile.get("mapping"):
            overrides = {int(k): v for k, v in profile["mapping"].items()}
            candidate = detect_format(grid, universe, overrides=overrides)
            if _identities_ok(candidate):
                fmt = candidate
                profile_applied = profile.get("label") or fmt.fingerprint
            else:
                fmt = C.DetectedFormat(
                    **{**fmt.__dict__,
                       "warnings": fmt.warnings + (
                           "保存済みの書式と数値の検算が合わなかったため、再判定しました",
                       )},
                )

    batch_id = str(uuid.uuid4())
    report = _preview_payload(
        store, grid, fmt, universe, account_name, batch_id, profile_applied
    )
    store.create_batch(
        ImportBatch(
            id=batch_id,
            source_kind=SOURCE_KIND,
            filename=filename,
            file_sha256=sha256,
            status="previewed",
            parse_report={
                "filename": filename,
                "account_name": account_name,
                "grid": _grid_payload(grid),
                **report,
            },
        )
    )
    return {"batch_id": batch_id, "ok": True, **report}


def _identities_ok(fmt: C.DetectedFormat) -> bool:
    usable = [c for c in fmt.identities if c.conclusive]
    if not usable:
        return True     # 検算できないなら覚えた対応を否定する根拠も無い
    return all(c.pass_rate >= C.IDENTITY_PASS_RATE for c in usable)


def _preview_payload(
    store: Store,
    grid,
    fmt: C.DetectedFormat,
    universe: C.KnownUniverse,
    account_name: str | None,
    batch_id: str,
    profile_applied: str | None,
) -> dict[str, Any]:
    result = parse_grid(grid, universe, fmt=fmt)
    account = store.get_account_by_name(account_name) if account_name else None

    # 行を組む前に決める。dedup_key に銘柄が入るので、後から結びつけると
    # 同じ約定なのに 1 回目と 2 回目で鍵が変わり、再取込で二重計上になる。
    auto_links, tried, unexplained = _auto_link(
        store, universe, result.transactions,
        account_id=account.id if account else None,
    )
    for holding in unexplained:
        holding["claimed_by"] = next(
            (n for n, sid in auto_links.items() if sid == holding["security_id"]), None
        )

    rows: list[dict[str, Any]] = []
    seen_counts: dict[str, int] = {}

    for tx in result.transactions:
        # 増減ゼロの行（利息の付かない月のＭＲＦ再投資など）は見せない。
        # パース段階では残す — 銘柄コードが改称をつなぐ証拠になるため。
        if has_no_movement(tx):
            continue
        security_id, how = resolve_security(
            store, universe, tx.security_code_raw, tx.security_name_raw
        )
        if security_id is None and tx.security_name_raw:
            linked = auto_links.get(tx.security_name_raw)
            if linked is not None:
                security_id, how = linked, "price"
        security_key = str(security_id) if security_id else f"raw:{make_name_key(tx.security_name_raw)}"
        signature = "|".join([
            security_key, tx.trade_date.isoformat() if tx.trade_date else "",
            tx.tx_type, _s(tx.quantity) or "", _s(tx.unit_price) or "",
        ])
        occurrence = seen_counts.get(signature, 0)
        seen_counts[signature] = occurrence + 1

        dedup_key = make_dedup_key(
            account_id=account.id if account else 0,
            security_key=security_key,
            trade_date=tx.trade_date.isoformat() if tx.trade_date else "",
            tx_type=tx.tx_type,
            quantity=_s(tx.quantity),
            unit_price=_s(tx.unit_price),
            gross_amount=_s(tx.gross_amount),
            lot_label=tx.account_type_raw,
            occurrence=occurrence,
        )
        cash_only = tx.tx_type in (TxType.CASH_IN.value, TxType.CASH_OUT.value)
        rows.append({
            "row": tx.row_index,
            "dedup_key": dedup_key,
            "trade_date": tx.trade_date.isoformat() if tx.trade_date else None,
            "settle_date": tx.settle_date.isoformat() if tx.settle_date else None,
            "tx_type": tx.tx_type,
            "security_id": security_id,
            "security_name": tx.security_name_raw,
            "security_code": tx.security_code_raw,
            "matched_by": how,
            "quantity": _s(tx.quantity),
            "unit_price": _s(tx.unit_price),
            "gross_amount": _s(tx.gross_amount),
            "net_amount": _s(tx.net_amount),
            "fee": _s(tx.fee),
            "tax": _s(tx.tax),
            "split_ratio": _s(tx.split_ratio),
            "currency": tx.currency,
            "lot_label": tx.account_type_raw,
            "note": tx.note,
            "account_raw": tx.account_raw,
            "confidence": round(tx.confidence, 3),
            "warnings": list(tx.warnings),
            "cash_only": cash_only,
            "included": (
                not cash_only
                and tx.confidence >= C.CONFIDENCE_INCLUDE_THRESHOLD
                and security_id is not None
            ),
            "raw": tx.raw,
        })

    # 自動で結びついたものは「決めてください」から外し、根拠つきで別に見せる。
    # 黙って結びつけると利用者が確かめようがないので、解除できる形で出す。
    unmatched = [e for e in tried.values() if "auto_linked" not in e]
    auto_linked = [e for e in tried.values() if "auto_linked" in e]

    known = store.existing_dedup_keys([r["dedup_key"] for r in rows])
    duplicates = 0
    for row in rows:
        if row["dedup_key"] in known:
            row["duplicate"] = True
            row["included"] = False
            duplicates += 1

    # 警告には 2 種類ある — 利用者の判断が要るもの（action）と、何をしたかの
    # 報告（info）。全部を同じ顔で並べると、対処の要る 2 件が 6 件の中に埋もれる。
    # warnings（文字列の配列）は既存の契約のまま残し、重み付きの notices を併設する。
    notices: list[dict[str, str]] = []

    def _notice(level: str, text: str) -> None:
        notices.append({"level": level, "text": text})

    for text in result.report.warnings:
        # 判定エンジン由来の文字列。信用取引と推定の報告は情報、それ以外
        # （ヘッダが見つからない・確認が必要など）は判断が要る。
        info = text.startswith("信用取引の行") or "推定しました" in text
        _notice("info" if info else "action", text)
    if not (account_name or "").strip():
        # 取引履歴のファイルは自社名を列に持たないことがほとんどなので、
        # 口座は利用者に選んでもらうしかない。まだ存在しない口座名でもよい
        # （確定時に作る）ので、判定は「名前が指定されたか」で行う。
        _notice("action",
            "口座を選んでください。取引履歴のファイルには証券会社名が入っていない"
            "ことが多いため、こちらで指定する必要があります"
        )
    if auto_linked:
        _notice("info",
            f"約定単価が既存銘柄の価格と一致したので {len(auto_linked)} 銘柄を"
            "自動で結びつけました（根拠を確認して解除できます）"
        )
    ambiguous = [e for e in unmatched if e.get("ambiguous")]
    if ambiguous:
        _notice("action",
            f"価格の一致する既存銘柄が複数ある銘柄が {len(ambiguous)} 件あります。"
            "同じ銘柄が二重に登録されている可能性があるので、どちらに結びつけるかは"
            "選んでください"
        )
    def _live_suggestions(e: dict[str, Any]) -> list[dict[str, Any]]:
        # 否定された候補は「候補がある」と数えない — 数えると、白黒ついて
        # いるのに個別判断を求めることになる。否定は 2 通り: 同日の価格が
        # 食い違う（別銘柄と確定）、資産クラス・地域の語が食い違う（ファンドは
        # 債券から株式へ改名しない）。
        return [s for s in e["suggestions"]
                if s.get("price_verdict") != "mismatch"
                and not s.get("category_conflict")
                and not s.get("code_conflict")
                and not s.get("name_refuted")]

    still_held = [
        e for e in unmatched if not e["closed_out"] and e["undetermined"] == 0
        and not _live_suggestions(e)
    ]
    def _listed(entries_: list[dict[str, Any]]) -> str:
        # どれのことかを警告に書く。件数だけ言われても調べ始められない。
        listed = "、".join(
            f"{e['name']}（残 {e['net_quantity']}）" for e in entries_[:5]
        )
        return listed + (f" ほか {len(entries_) - 5} 件" if len(entries_) > 5 else "")

    # 残がプラス＝現物の行だけ見ればまだ持っているはず。ただし、この口座に
    # 未説明の保有が残っていなければ「まだ持っている」は成立しない — 現渡など
    # 信用取引（取込対象外）で処分された可能性が高い。前者と後者では求める
    # 操作が逆（結びつけ先を探す ↔ 売却済みとして畳む）なので、文言を分ける。
    positive = [e for e in still_held if not e["net_quantity"].startswith("-")]
    unclaimed_holdings = [h for h in unexplained if not h.get("claimed_by")]
    if positive:
        for e in positive:
            e["needs_attention"] = True
        if unclaimed_holdings:
            _notice("action",
                f"ファイル上まだ残高のある銘柄が {len(positive)} 件"
                f"（{_listed(positive)}）、既存銘柄に結びついていません。売却済みと"
                "して登録すると保有中の銘柄を二重に作ることになるので、既存銘柄を"
                "選んでください"
            )
        else:
            _notice("action",
                f"現物の取引だけでは残高が残る計算の銘柄が {len(positive)} 件"
                f"（{_listed(positive)}）ありますが、この口座に未説明の保有は"
                "ありません。現渡など信用取引（取込対象外）で処分された可能性が"
                "高いので、「売却済みとして登録」か「取り込まない」を選んでください"
            )
    # 残がマイナス＝買付より売却が多い。取得の記録がこのファイルに無い。
    negative = [e for e in still_held if e["net_quantity"].startswith("-")]
    if negative:
        for e in negative:
            e["needs_attention"] = True
        _notice("action",
            f"買付より売却が多い銘柄が {len(negative)} 件あります"
            f"（{_listed(negative)}）。取得の記録がこのファイルに無いか、現引など"
            "信用取引で取得した分か、別名の銘柄と結びつけ損ねている可能性が"
            "あります。心当たりが無ければ「取り込まない」のままで構いません"
        )
    # 判断の要る行を表の先頭へ。数十行の中から探させない。
    unmatched.sort(key=lambda e: 0 if e.get("needs_attention") else 1)
    if unmatched:
        # 「N 銘柄あります」だけでは、何をどう対処すべきか読み取れない。
        # 内訳と、それぞれに求める操作を書く。
        closed_nocand = sum(
            1 for e in unmatched if e["closed_out"] and not _live_suggestions(e)
        )
        closed_withcand = sum(
            1 for e in unmatched if e["closed_out"] and _live_suggestions(e)
        )
        parts = []
        if closed_nocand:
            parts.append(
                f"{closed_nocand} 銘柄は増減 ±0・候補なしで、一括の"
                "「売却済みとして登録」で片づきます"
            )
        if closed_withcand:
            parts.append(
                f"{closed_withcand} 銘柄は ±0 ですが、同日の価格で白黒つけられない"
                "似た名前の既存銘柄があるため一括の対象外です。別物なら"
                "「売却済みとして登録」、同じ銘柄なら結びつけを選んでください"
            )
        detail = "。".join(parts) if parts else "1 件ずつ結びつけ先を選んでください"
        _notice("action",
            f"既存の銘柄に結びつかない銘柄が {len(unmatched)} 件あります。{detail}")
    if duplicates:
        _notice("info",
            f"すでに取り込み済みの取引が {duplicates} 件あります（除外しています）")
    cash_rows = sum(1 for r in rows if r.get("cash_only"))
    if cash_rows:
        _notice("info",
            f"入出金の行が {cash_rows} 件あります。保有数にも取得原価にも関係しないため"
            "取込対象から外しています"
        )
    warnings = [n["text"] for n in notices]

    return {
        "detection": {**fmt.to_dict(), "profile_applied": profile_applied},
        "source": {
            "kind": grid.meta.kind,
            "encoding": grid.meta.encoding,
            "delimiter": grid.meta.delimiter,
            "delimiter_mode": grid.meta.delimiter_mode,
            "sheet_name": grid.meta.sheet_name,
            "sheet_names": list(grid.meta.sheet_names),
        },
        "account_name": account_name,
        "account_id": account.id if account else None,
        "rows": rows,
        "unmatched_securities": unmatched,
        "auto_linked_securities": auto_linked,
        "unexplained_holdings": unexplained,
        # 結びつけ先が「今も保有している銘柄」かどうかを画面で見せるための対応表。
        # 保有中の銘柄に結びつけるのか、売却済みとして登録するのかは判断が
        # 逆方向なので、これが見えないと利用者は 1 件ずつ思い出すことになる。
        "held_quantities": (
            {str(sid): _s(q)
             for sid, q in store.current_quantities_in_account(account.id).items()}
            if account else {}
        ),
        # 画面が「取込対象に戻してよいか」を判断するのに要る値。直書きすると
        # ここと画面で別々に育ってしまうので、判定側から渡す。
        "thresholds": {
            "include_confidence": C.CONFIDENCE_INCLUDE_THRESHOLD,
            "unknown_type_penalty": C.PENALTY_UNKNOWN_TYPE,
        },
        "duplicate_count": duplicates,
        "skipped_rows": result.report.skipped_rows,
        "field_choices": [f.value for f in F],
        "warnings": warnings,
        "notices": notices,
    }


def remap_tx_preview(
    store: Store,
    batch_id: str,
    *,
    column_overrides: dict[str, str] | None = None,
    security_map: dict[str, int] | None = None,
    account_name: str | None = None,
) -> dict[str, Any]:
    """利用者が直した列対応・銘柄紐付けで再プレビューする。"""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise StoreError(f"バッチが見つかりません: {batch_id}")
    if batch.status != "previewed":
        raise StoreError(f"このバッチは編集できません（status={batch.status}）")

    payload = batch.parse_report.get("grid") or {}
    if payload.get("truncated"):
        raise StoreError(
            "行数が多いためこのファイルは再解析できません。列の対応を直すには"
            "取り込み直してください"
        )

    grid = _grid_from_payload(payload)
    universe = build_universe(store)
    overrides = {int(k): v for k, v in (column_overrides or {}).items()}
    fmt = detect_format(grid, universe, overrides=overrides or None)

    name = account_name if account_name is not None else batch.parse_report.get("account_name")
    report = _preview_payload(store, grid, fmt, universe, name, batch_id, None)

    if security_map:
        _apply_security_map(report, security_map)

    merged = {**batch.parse_report, **report, "account_name": name,
              "column_overrides": column_overrides or {},
              "security_map": {k: v for k, v in (security_map or {}).items()}}
    store.update_batch(batch_id, parse_report=merged)
    return {"batch_id": batch_id, "ok": True, **report}


def _apply_security_map(report: dict[str, Any], security_map: dict[str, int]) -> None:
    """未照合の銘柄名 → security_id の対応を行に反映する。

    画面から返ってくる名前は表記が揺れうる（全角括弧・空白など）ので、
    名寄せキーでも引けるようにしておく。
    """
    # 束ねたエントリへの指定は旧称の行にも効かせる。画面は代表名しか送らない。
    expanded = dict(security_map)
    for ent in (list(report.get("unmatched_securities") or [])
                + list(report.get("auto_linked_securities") or [])):
        names = [ent["name"], *ent.get("aliases", [])]
        hit = next((n for n in names if n in expanded), None)
        if hit is not None:
            for n in names:
                expanded.setdefault(n, expanded[hit])
    security_map = expanded
    by_key = {make_name_key(k): v for k, v in security_map.items() if make_name_key(k)}

    def _lookup(name: str) -> tuple[bool, int | None]:
        if name in security_map:
            return (True, security_map[name])
        key = make_name_key(name)
        if key and key in by_key:
            return (True, by_key[key])
        return (False, None)

    for row in report["rows"]:
        # 価格で自動紐付けした行は利用者が上書きできる（解除も含む）。
        # 名寄せで確定した行は動かさない — そちらのほうが根拠が強い。
        if row["security_id"] is not None and row.get("matched_by") != "price":
            continue
        found, mapped = _lookup(row["security_name"] or "")
        if not found:
            continue
        if mapped:
            row["security_id"] = int(mapped)
            row["matched_by"] = "manual"
            row["included"] = (
                row["confidence"] >= C.CONFIDENCE_INCLUDE_THRESHOLD
                and not row.get("duplicate")
            )
        else:
            # 明示的に「取り込まない」。自動で結びつけた分もここで外す。
            row["security_id"] = None
            row["matched_by"] = "unmatched"
            row["included"] = False
    report["unmatched_securities"] = [
        u for u in report["unmatched_securities"] if not _lookup(u["name"])[0]
    ]
    report["auto_linked_securities"] = [
        u for u in report.get("auto_linked_securities", []) if not _lookup(u["name"])[0]
    ]


# ----------------------------------------------------------------------
# 確定
# ----------------------------------------------------------------------


def commit_tx_batch(
    store: Store,
    batch_id: str,
    *,
    account_name: str | None = None,
    include_keys: list[str] | None = None,
    exclude_keys: list[str] | None = None,
    security_map: dict[str, int] | None = None,
    new_securities: list[str] | None = None,
    type_overrides: dict[str, str] | None = None,
    apply_cost_basis: bool = True,
) -> dict[str, Any]:
    """取引を台帳へ投入し、取得原価を再計算し、書式と銘柄名を学習する。

    new_securities は「売却済みとして登録する」と利用者が選んだ銘柄名。
    自動では作らない — 既存銘柄に無いのは売却済みとは限らず、MF PDF が拾って
    いない口座の銘柄かもしれないため（保有中の銘柄を別名で作ると保有が割れる）。
    """
    batch = store.get_batch(batch_id)
    if batch is None:
        raise StoreError(f"バッチが見つかりません: {batch_id}")
    if batch.status != "previewed":
        raise StoreError(f"バッチはコミットできない状態です（status={batch.status}）")
    if batch.file_sha256:
        existing = store.find_committed_batch_by_sha(batch.file_sha256)
        if existing is not None:
            raise DuplicateImportError(
                existing.id,
                existing.as_of_date.isoformat() if existing.as_of_date else None,
            )

    report = dict(batch.parse_report)
    name = account_name or report.get("account_name")
    if not (name or "").strip():
        raise StoreError(
            "口座名が指定されていません。取引履歴のファイルには証券会社名が"
            "入っていないことが多いため、取込前に口座を選んでください"
        )
    account = store.get_or_create_account(name, kind="broker")

    rows: list[dict[str, Any]] = list(report.get("rows") or [])

    # 「売却済みとして登録」を選ばれた銘柄を先に作り、以降は通常の紐付けと同じ扱い
    created: dict[str, int] = {}
    if new_securities:
        # 同じコードで束ねた旧称は同じ銘柄。名前ごとに作ると 1 つのファンドが
        # 3 つに割れる（3 代にわたり改称されたファンドの実例）。
        alias_sets: dict[str, frozenset[str]] = {}
        for ent in (list(report.get("unmatched_securities") or [])
                    + list(report.get("auto_linked_securities") or [])):
            names = frozenset([ent["name"], *ent.get("aliases", [])])
            for n in names:
                alias_sets[n] = names
        known_codes = {s.code for s in store.list_securities() if s.code}
        for sec_name in dict.fromkeys(new_securities):
            if sec_name in created:
                continue      # 別名側が先に処理されて作成済み
            names = alias_sets.get(sec_name, frozenset([sec_name]))
            owned = [r for r in rows if r.get("security_name") in names]
            if not owned:
                continue
            sid = store.create_security(
                build_new_security(sec_name, owned, known_codes)
            )
            for n in names:
                created[n] = sid

    combined = {**(security_map or {}), **created}
    if combined:
        _apply_security_map(report, {k: int(v) for k, v in combined.items()})
        rows = report["rows"]
    for row in rows:
        override = (type_overrides or {}).get(row["dedup_key"])
        if override:
            # まとめて指定が効いてよいのは取引区分が空欄だった行だけ。信用取引や
            # ラベルの読めなかった行まで書き換えると、対象外にした行が現物の
            # 売買として台帳に入る。画面側も同じ条件で絞っているが、API を直接
            # 叩かれても守れるようにここでも確かめる。
            raw = row.get("raw") or {}
            if raw.get("margin") or raw.get("tx_type_raw"):
                continue
            row["tx_type"] = override

    included = set(include_keys) if include_keys is not None else None
    excluded = set(exclude_keys or [])

    universe = build_universe(store)
    txs: list[Transaction] = []
    aliases: dict[str, int] = {}
    skipped_unmatched = 0
    skipped_cash = 0

    for row in rows:
        key = row["dedup_key"]
        # 入出金は保有にも取得原価にも関係しないので取り込まない。
        # 「銘柄未確定」に混ぜず別に数えて、本当に対応が要る行を埋もれさせない。
        if row.get("cash_only") or row["tx_type"] in (
            TxType.CASH_IN.value, TxType.CASH_OUT.value
        ):
            skipped_cash += 1
            continue
        # すでに台帳にある行は必ず insert_transactions へ渡す。挿入は
        # ON CONFLICT で弾かれるので二重計上にはならず、代わりに
        # 「このバッチもこの行を見た」が transaction_batches に残る。
        # ここで飛ばしてしまうと、期間が重なる前のバッチを巻き戻したときに
        # このバッチが依拠する行まで消える。
        duplicate = bool(row.get("duplicate"))
        if not duplicate:
            if included is not None and key not in included:
                continue
            if key in excluded:
                continue
        if row["security_id"] is None:
            skipped_unmatched += 1
            continue
        if not row["trade_date"]:
            skipped_unmatched += 1
            continue

        # 口座が確定したので鍵を作り直す（プレビュー時は 0 で組んでいる）
        security_key = str(row["security_id"])
        dedup_key = make_dedup_key(
            account_id=account.id,
            security_key=security_key,
            trade_date=row["trade_date"],
            tx_type=row["tx_type"],
            quantity=row.get("quantity"),
            unit_price=row.get("unit_price"),
            gross_amount=row.get("gross_amount"),
            lot_label=row.get("lot_label"),
            occurrence=_occurrence(rows, row),
        )
        txs.append(
            Transaction(
                dedup_key=dedup_key,
                account_id=account.id,
                security_id=row["security_id"],
                trade_date=date.fromisoformat(row["trade_date"]),
                settle_date=date.fromisoformat(row["settle_date"]) if row.get("settle_date") else None,
                tx_type=TxType(row["tx_type"]),
                quantity=_dec(row.get("quantity")),
                unit_price=_dec(row.get("unit_price")),
                gross_amount=_dec(row.get("gross_amount")),
                fee=_dec(row.get("fee")),
                tax=_dec(row.get("tax")),
                net_amount=_dec(row.get("net_amount")),
                split_ratio=_dec(row.get("split_ratio")),
                currency=row.get("currency") or "JPY",
                lot_label=row.get("lot_label"),
                note=row.get("note"),
                origin=SOURCE_KIND,
                raw=row.get("raw") or {},
            )
        )
        # 証券会社ごとの表記ゆれを別名として覚える。次回は名前が違っても当たる。
        if row.get("matched_by") in ("manual", "name", "code") and row["security_name"]:
            alias = make_name_key(row["security_name"])
            if alias:
                aliases[alias] = row["security_id"]

    inserted, duplicates = store.insert_transactions(txs, batch_id)
    for alias_key, security_id in aliases.items():
        store.add_alias(alias_key, security_id, source_kind=ALIAS_SOURCE)

    detection = report.get("detection") or {}
    fingerprint = detection.get("fingerprint")
    if fingerprint and detection.get("columns"):
        store.save_format_profile(
            fingerprint,
            mapping={
                str(c["index"]): c["field"]
                for c in detection["columns"] if c["field"] != F.IGNORE.value
            },
            header_labels=list(detection.get("headers") or []),
            options={
                "divisor": detection.get("divisor", 1),
                "sign_convention": detection.get("sign_convention"),
                "date_order": detection.get("date_order"),
            },
            label=report.get("filename"),
        )

    store.update_batch(
        batch_id, status="committed", committed_at=_utcnow(),
        parse_report={**report, "account_name": name},
    )

    cost = {"reconciled": 0, "unreconciled": 0, "warnings": []}
    if apply_cost_basis:
        cost = recompute_cost_basis(store, batch_id=batch_id)

    return {
        "ok": True,
        "inserted": inserted,
        "skipped_duplicates": duplicates,
        "skipped_unmatched": skipped_unmatched,
        "skipped_cash": skipped_cash,
        "aliases_learned": len(aliases),
        "created_securities": len(created),
        **cost,
    }


def _occurrence(rows: list[dict[str, Any]], target: dict[str, Any]) -> int:
    """同一内容の行がこのファイル内で何度目か（同日同内容の約定を潰さない）。"""
    n = 0
    for row in rows:
        if row is target:
            return n
        if (row.get("security_id") == target.get("security_id")
                and row.get("trade_date") == target.get("trade_date")
                and row.get("tx_type") == target.get("tx_type")
                and row.get("quantity") == target.get("quantity")
                and row.get("unit_price") == target.get("unit_price")
                and row.get("lot_label") == target.get("lot_label")):
            n += 1
    return n


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


# ----------------------------------------------------------------------
# 取得原価の再計算
# ----------------------------------------------------------------------


def recompute_cost_basis(store: Store, batch_id: str | None = None) -> dict[str, Any]:
    """台帳とスナップショットを突き合わせ、取得原価を書き直す。

    holding_cost_basis は transactions から常に再生成できる派生値なので、
    いつ呼んでも冪等。バッチの巻き戻し後にも呼べばよい。
    """
    transactions = store.list_transactions()
    lots = store.current_holdings()
    securities = store.securities_by_id()
    results, warnings = reconcile_all(transactions, lots, securities)

    rows = [_cost_basis_row(r) for r in results]
    store.replace_cost_basis(rows, batch_id=batch_id)

    reconciled = sum(1 for r in results if r.coverage is not Coverage.UNRECONCILED)
    return {
        "reconciled": reconciled,
        "unreconciled": len(results) - reconciled,
        "applied_to_pl": sum(1 for r in results if r.applies_to_pl),
        "warnings": [w.message for w in warnings]
        + [w.message for r in results for w in r.warnings],
    }


def _cost_basis_row(r: GroupResult) -> dict[str, Any]:
    return {
        "account_id": r.account_id,
        "security_id": r.security_id,
        "as_of_date": r.as_of_date.isoformat(),
        "coverage": r.coverage.value,
        "applies_to_pl": r.applies_to_pl,
        "avg_cost": _s(r.avg_cost),
        "acquired_on": r.acquired_on.isoformat() if r.acquired_on else None,
        "acquired_on_src": r.acquired_on_src,
        "covered_quantity": _s(r.covered_quantity),
        "residual_quantity": _s(r.residual_quantity),
        "residual_avg_cost": _s(r.residual_avg_cost),
        "realized_pl": _s(r.realized_pl),
        "income_total": _s(r.income_total),
        "withheld_tax": _s(r.withheld_tax),
        "lot_scope": r.lot_scope,
        "tx_count": r.tx_count,
        "first_tx_date": r.first_tx_date.isoformat() if r.first_tx_date else None,
        "last_tx_date": r.last_tx_date.isoformat() if r.last_tx_date else None,
        "warnings": [w.to_dict() for w in r.warnings],
    }


def cost_basis_events(store: Store, security_id: int) -> list[dict[str, Any]]:
    """銘柄詳細用のロット内訳（期首ロットを含むタイムライン）。"""
    transactions = [
        t for t in store.list_transactions(security_id=security_id)
    ]
    lots = [h for h in store.current_holdings() if h.security_id == security_id]
    securities = store.securities_by_id()
    results, _ = reconcile_all(transactions, lots, securities)
    out: list[dict[str, Any]] = []
    for r in results:
        for e in r.events:
            out.append({
                "account_id": r.account_id,
                "date": e.trade_date.isoformat() if e.trade_date else None,
                "kind": e.kind,
                "quantity": _s(e.quantity),
                "unit_cost": _s(e.unit_cost),
                "amount": _s(e.amount),
                "note": e.note,
            })
    return out
