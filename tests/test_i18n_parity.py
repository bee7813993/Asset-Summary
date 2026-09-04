"""ja / en の翻訳キーが一致していることを機械的に守る。

i18n.js は 2 つの辞書を手で並べて持っているので、片方だけにキーを足すのは
起きやすい。UI 文字列は必ず t() を通す規約なので、欠けたキーはそのまま
キー文字列が画面に出る。
"""

from __future__ import annotations

import re
from pathlib import Path

I18N = Path(__file__).resolve().parents[1] / "src/asset_summary/web/static/i18n.js"
_KEY_RE = re.compile(r'^\s{4}"([^"]+)":', re.M)


def _dicts() -> tuple[set[str], set[str]]:
    src = I18N.read_text(encoding="utf-8")
    ja_at, en_at = src.index("  ja: {"), src.index("  en: {")
    assert ja_at < en_at, "i18n.js の並びが変わりました"
    return (
        set(_KEY_RE.findall(src[ja_at:en_at])),
        set(_KEY_RE.findall(src[en_at:])),
    )


def test_ja_and_en_have_the_same_keys():
    ja, en = _dicts()
    assert ja - en == set(), f"en に無いキー: {sorted(ja - en)}"
    assert en - ja == set(), f"ja に無いキー: {sorted(en - ja)}"


def test_no_duplicate_keys_within_a_dictionary():
    """同じキーを 2 回書かない。

    JS のオブジェクトリテラルは後勝ちで黙って上書きするため、既にあるキー名を
    別の用途で足すと元の画面の文言が変わる。実際に、列対応表の見出し
    tx.thEvidence（判定の根拠）が、銘柄の結びつけ表へ同名で足したせいで
    「根拠」に置き換わっていた。ja/en の一致だけでは検出できない。
    """
    src = I18N.read_text(encoding="utf-8")
    ja_at, en_at = src.index("  ja: {"), src.index("  en: {")
    for label, block in (("ja", src[ja_at:en_at]), ("en", src[en_at:])):
        keys = _KEY_RE.findall(block)
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        assert dupes == [], f"{label} に重複したキー: {dupes}"


def test_transaction_import_keys_exist():
    ja, _en = _dicts()
    for key in (
        "tx.uploadTitle", "tx.mappingTitle", "tx.previewTitle",
        "tx.costBasisTitle", "tx.coverageFull", "tx.coveragePartial",
        "tx.explainPartial", "tx.type.buy", "tx.field.trade_date",
    ):
        assert key in ja, f"{key} が i18n に無い"


def test_every_data_i18n_attribute_has_a_key():
    """index.html の data-i18n が i18n.js に存在すること。"""
    html = (I18N.parent / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n(?:-placeholder|-title)?="([^"]+)"', html))
    ja, _en = _dicts()
    missing = sorted(used - ja)
    assert missing == [], f"i18n.js に無いキーが index.html で使われています: {missing}"
