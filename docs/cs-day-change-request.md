# Crypto-Summary への依頼: 資産別の「前日終値ベースの評価額」を /api/summary に載せる

> **対応済み**（CS `#8` / AS 側の追従も反映済み）。以下は経緯と、両者で合意した
> 応答形の記録として残しています。実装された応答形は CS の README / DESIGN と
> AS の `core/crypto_summary_client.py`（`cs_holding_rows`）が正です。
> CS は依頼の3フィールド＋`total_prev_value` に加えて、
> **評価額はあるが前日値が取れなかった資産を並べる `prev_missing`** を返します。

このファイルは **Crypto-Summary (CS) リポジトリ** で作業する人への依頼書です。
作業対象は Crypto-Summary リポジトリ（Asset Summary 側は変更不要ですが、
最後に「AS 側の追従」も書いてあります）。

## なぜ必要か

Asset Summary (AS) に「前日比（評価額の前日との差・金額と%）」を追加しました。
AS 内の銘柄は自前の日次価格から前日終値を引けるので追加コストゼロですが、
**CS 連携で流れてくるコインだけは前日値を貰う口が無い**ため、いまは
`/api/portfolio-history?scope=asset:<SYM>` を**コインの数だけ**呼んで
末尾の点から逆算しています。

そのため AS 側は妥協した挙動になっています:

| AS の画面 | いまの前日比 |
|---|---|
| 暗号資産クラス詳細 / CS疑似口座の口座詳細 | コインごとに履歴APIを並列で叩いて表示（コイン数ぶんのリクエスト） |
| ダッシュボードの「主な保有」/ 保有一覧 / Myポートフォリオの構成銘柄 | **「—」（出せない）** — ここで N 本叩くと重すぎるため |
| 総資産・暗号資産クラス・CS疑似口座の合計 | `scope=total` の履歴1本から算出（これは問題なし） |

`/api/summary` の各資産に前日値が入れば、**追加リクエスト0 で全画面が埋まり**、
コイン別の履歴呼び出しも不要になります。

## 依頼内容

`GET /api/summary` のレスポンスに、以下のフィールドを**追加**してください（既存フィールドは変更しない）。

```jsonc
{
  "currency": "JPY",
  "total_value": "4500000",
  "total_prev_value": "4200000",     // ← 追加: 下の prev_value の合計
  "assets": [
    {
      "asset": "BTC",
      "balance": "0.3",
      "price": "15000000",
      "value": "4500000",
      "has_price": true,
      "prev_price": "14000000",      // ← 追加: 前営業日の終値
      "prev_value": "4200000",       // ← 追加: balance × prev_price
      "prev_date": "2026-08-12"      // ← 追加: prev_price の基準日 (YYYY-MM-DD)
    }
  ]
}
```

- 前日値が引けない資産は **3つとも `null`**（0 を入れない。「動いていない」と「判らない」は別物）。
- `total_prev_value` は `prev_value` が取れた資産だけの合計。1件も取れなければ `null`。

### 前日値の定義（ここが一番大事）

**`prev_value` = 「いまの残高」×「前営業日の終値」** としてください。
「前日のスナップショットの残高」ではありません。

理由: AS の前日比は Yahoo ファイナンス・マネーフォワードと同じく
**「価格が動いたぶん」**を表す数字で、AS 内の銘柄は
`当日の数量 ×（現在値 − 前日終値）` で計算しています。CS 側が
前日の残高を使うと、前日に入金・出金したコインだけ「値動き」に
入出金額が混ざり、同じ列に並ぶ AS 銘柄と意味が変わってしまいます。

`prev_price` の「前営業日」は **当日より前で価格が取れた最新の日**です
（暗号資産は土日も値が付きますが、取得漏れの日があり得るので
「昨日固定」ではなく「当日より前の最新」で引いてください）。
その実際の日付を `prev_date` に入れてください — AS 側はこれを見て
「古すぎる基準日（7日以上前）は前日比として出さない」判定をします。

## 実装の当て所

`src/crypto_summary/web/app.py` の `_summary(db_path, currency)`（190行目付近）です。
必要な部品はすべて既にあります:

- `core/portfolio.py` の `daily_balances(ledger, ...)` — 日付ごとの残高スナップショット
  （※ 今回は「いまの残高」を使うので、実は**残高の履歴は不要**です。
  `_summary` が既に持っている `bals` をそのまま使ってください）
- `core/price_history.py` の `fetch_price_history(assets, currency, start, end, warn)`
  — 資産リストに対する日次終値。**過去日は `~/.crypto_summary_pricehist.json` に
  キャッシュ済みで、当日ぶんだけ揮発**する仕様なので、数日ぶんの窓を引くのは
  その日の初回だけネットワークに出て、以降はローカル参照で済みます。

擬似コード:

```python
# _summary() の中、assets を組み立てる直前あたり
today = date.today()
# 直近の欠測に耐えるよう数日ぶんの窓を取り、当日より前の最新日を採る
hist_start = today - timedelta(days=PREV_LOOKBACK_DAYS)   # 7 程度
hist = fetch_price_history(list(bals.keys()), currency, hist_start, today, warn=warnings.append)

def _prev_close(asset: str) -> tuple[Decimal, str] | None:
    days = hist.get(asset.upper()) or {}
    past = [d for d in days if d < today.isoformat()]
    if not past:
        return None
    d = max(past)
    return (days[d], d)
```

そのうえで各 asset の dict に `prev_price` / `prev_value` / `prev_date` を足し、
`total_prev_value` を集計します。

### 気をつけてほしいこと

1. **除外条件を `value` と揃える**。`_summary` は `_excluded_labels(db_path)` で
   除外した残高を使い、`_is_spam_token(...)` でスパムトークンを落とし、
   `_DUST` 未満を捨てています。`prev_value` も**同じ資産集合**に対してだけ
   計算してください（別集合だと合計が噛み合いません）。
2. **前日値が取れなくても 500 にしない**。`fetch_price_history` の失敗は
   既存どおり `warnings` に流し、該当資産は `null` のままにしてください。
   AS は `null` を「—」として扱うので、CS が古い版でも壊れません。
3. **`/api/summary` を重くしない**。窓は7日程度に留めてください。
   `range=90d` のような長い窓を引くと、キャッシュが効くまでの初回が遅くなります。
4. **`price` と `prev_price` の通貨を揃える**。`fetch_prices` と
   `fetch_price_history` はどちらも `currency` 直指定なので、
   同じ `currency` を渡していれば問題ありません。

### テスト

`tests/test_web.py` あたりに追加してください（`tests/test_service_token.py` に
`/api/summary` を service token で叩く例があります）。最低限:

- 前日終値がある資産で `prev_value == balance × prev_price` になり、`prev_date` が入る
- 前日終値が無い資産は3フィールドとも `null`、`total_prev_value` はそれを含めない
- 価格履歴が1件も取れない状態でも 200 で返り、`warnings` に理由が載る
- スパムトークン・除外ラベルの資産が `total_prev_value` に混ざらない
- `prev_date` が**当日ではない**こと（当日の値を掴んで前日比が常に0になる事故の防止）

### ドキュメント

`README.md` / `DESIGN.md` に `/api/summary` の応答形が書かれていれば、
追加フィールドを反映してください。

## AS 側の追従（CS のリリース後に AS リポジトリで行う作業）

CS が上記に対応したら、AS 側は以下だけで全画面が埋まります。

1. `src/asset_summary/core/crypto_summary_client.py` の `cs_holding_rows()` で、
   CS 応答の `prev_value` を読んで行の `prev_value` / `day_change` / `day_change_pct` /
   `day_change_as_of`（= `prev_date`）を埋める。
   `day_change = value − prev_value` で AS 内銘柄と定義が揃います。
2. 同ファイルの `cs_total_day_change()` は、`total_prev_value` があればそれを使い、
   無ければ現状どおり `scope=total` の履歴から求める**フォールバックを残す**
   （AS と CS は別々にデプロイされるため、古い CS でも動く必要があります）。
3. `cs_asset_day_changes()` / `fetch_cs_asset_histories()` と、
   `src/asset_summary/web/app.py` の `_fill_cs_asset_day_changes()` の呼び出しは
   **削除できます**（`/api/class-holdings?class=crypto` と
   `/api/account-holdings?account=Crypto-Summary` からの N 本のリクエストが消えます）。
   ただし CS 側の対応が確認できるまでは、`prev_value` が無いときの経路として残すのが安全です。
4. `tests/test_cs_web_integration.py` の
   `test_crypto_class_detail_fills_per_coin_day_change` などを、
   新しい `prev_value` 経由の期待値に更新する。

**残る N 本のリクエスト**: `/api/portfolio-history?scope=tag:<id>` /
`portfolio:<id>`（タグ・Myポートフォリオの推移グラフ）は、コインごとの
**系列**そのものを配分率で按分してマージするため、前日値だけでは代替できません。
ここはコイン別履歴の呼び出しが残ります（タグに入っている CS コインの数ぶん・
CS 側で TTL キャッシュ済み）。

## 補足: もっと汎用にするなら

「特定の日の評価額を出せるようにする」案（`GET /api/summary?as_of=YYYY-MM-DD`）でも
AS の要件は満たせますが、AS 側が `/api/summary` を**もう1回**呼ぶことになり、
CS 側も「その日の残高スナップショット」を作る必要があるため重くなります。
今回の用途は「前日比を出す」ことに限られるので、上記のフィールド追加を推奨します。
将来 `as_of` が欲しくなったら、同じ `fetch_price_history` +
`daily_balances` の組み合わせで素直に拡張できます。
