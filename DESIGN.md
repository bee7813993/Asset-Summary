# Asset Summary Webアプリ 実装計画

## Context

暗号資産以外の資産（国内外株式・投資信託・預金/現金・貴金属・不動産・年金・ポイント）のサマリーを表示するWebアプリを新規作成する。既存の Crypto-Summary と同じ見た目・構成の兄弟アプリとし、将来は Crypto-Summary / PBRLending-History-Check と統合する。データ取込は当面「マネーフォワードME 資産内訳ページのPDF」+「手動入力」。将来のスクショ・ファジー取込にも拡張できる構造にする。

**ユーザー回答で確定済み（2026-08-04）**:
1. 技術スタック: **Crypto-Summaryを踏襲**（Python 3.11+ / FastAPI / SQLite / vanilla JS + Chart.js CDN、ビルドレス）
2. 取込範囲: **年金・ポイントも対象**（暗号資産のみ除外。設定でトグル可、初期値ON）
3. 推移グラフ: **初回登録時の保有数で過去へ遡及表示**（過去の価格 × 初回スナップショット保有数）
4. 推移グラフに**取得コスト線は引かない**（2026-08-17）: 取得原価を持たない資産も評価額側には乗るため合計取得コストは常に過少で、評価額と並べても意味を持たない。線を外すと縦軸が評価額だけで決まり変動も見やすくなる

## 事前調査の要点（設計の根拠）

- **実PDF検証済み**: pdfplumber の `extract_words()` 座標は安定。数値セルはレコード先頭行に集約、折返し行は名称断片のみ。**投信の平均取得単価・基準価額は1万口あたり**（口数×基準価額÷10,000＝評価額と一致確認）。**PDFに基準日の記載なし**。大和証券の銘柄コードは5桁末尾0（28400→2840, 521A0→521A）。同一銘柄が複数行（NISA/特定と思われる、ラベルなし）。
- **価格ソース実地検証済み（2026-08-04）**: Yahoo v8 chart API が株式・ETF（新形式コード 521A.T 含む）・先物・FXで動作。投信協会の検索JSON+基準価額CSV（Shift-JIS・全履歴）が動作。Frankfurter（ECB）でFX履歴。**Stooq はJS proof-of-work壁で使用不可**。**exchangerate.host はキー必須化**。yfinanceライブラリは使わずhttpx直叩き。
- **Crypto-Summary の流用資産**: `core/ledger.py`（WAL PRAGMA群・バッチ削除）、`core/portfolio.py`、`web/app.py`（factory・API封筒規約）、`web/static/*` 一式（ハッシュルーター・ドーナツ/折れ線チャート・i18n・マスクモード・億万円表示）。**未実装ギャップ = 損益計算・過去FX** は本アプリで新規実装。

## プロジェクト骨格

```
Asset Summary/
├── .gitignore            # data/, *.pdf, *.db, *.db-wal, *.db-shm, .env 等（個人情報防護が最優先）
├── pyproject.toml        # name=asset-summary, script=asset-summary, deps: pydantic/click/rich/httpx/
│                         #   python-dotenv/pdfplumber/fastapi/uvicorn（webはcore依存に昇格）, dev: pytest
├── README.md / DESIGN.md / docs/{commands,verification_checklist}.md
├── data\                 # ★gitignore対象。マネーフォワード ME.pdf をここへ移動。assets.db もここ
├── src\asset_summary\
│   ├── cli.py            # click: web / import / summary（crypto cli.py の web_cmd 踏襲、loop="asyncio"）
│   ├── core\
│   │   ├── models.py     # pydantic v2: Security, Account, HoldingSnapshot, ParsedHolding, enums
│   │   ├── store.py      # 接続(PRAGMA群はledger.py踏襲) + 全DDL + CRUD + バッチ削除
│   │   ├── portfolio.py  # 階段関数評価・日次系列・損益（§評価ロジック）
│   │   ├── tagging.py    # タグ按分集計・Myポートフォリオ計上率（1銘柄100%、残りは未分類）
│   │   ├── tag_rules.py  # タグの自動配分ルール表（§タグ配分と自動配分。純関数+Store読取のみ）
│   │   ├── fund_autolink.py  # 未連携投信の自動判定（名前スコア+基準価額照合）
│   │   ├── prices.py     # fetch_prices(securities, currency, warn) ファサード
│   │   ├── price_history.py  # fetch_price_history(...) ファサード＋欠損範囲ロジック
│   │   ├── price_store.py    # daily_prices/fetched_ranges/spot_cache の読み書き・範囲マージ
│   │   └── providers\    # base.py(Protocol/throttle/backoff) yahoo.py toushin.py metal.py fx.py manual.py
│   ├── importers\
│   │   ├── base.py       # ParsedHolding/ParseResult/HoldingsImporter Protocol + 正規化 + read_csv_text移植
│   │   ├── mf_pdf.py     # 座標ベースパーサ（word list→ParseResult の純関数群）
│   │   └── matching.py   # code/alias/name_key照合・ロット割当・diff構築
│   └── web\
│       ├── app.py        # FastAPI factory（crypto web/app.py 骨格。認証なし・単一ユーザー）
│       └── static\       # index.html app.js i18n.js style.css（cryptoからコピーしてリスキン）
└── tests\                # conftest.py, fixtures/（架空データの合成wordリスト）, test_*.py
```

- **git init は .gitignore 作成・PDFの data\ 移動後に実施**。初回コミット前に `git status` で個人情報ファイル不在を確認。
- ポートは **8010**（Crypto-Summary=8000 と並走可能）。DB既定 `data/assets.db`。OAuth/Docker/税務sink/管理画面は v1 では作らない（factory構造は維持し追加余地を残す）。

## データモデル（SQLite、スナップショット方式）

イベントログではなく**スナップショット/ポジション方式**を採用。理由: MF PDFは残高計算書（取引を含まない）、登録前履歴は不要（確定要件）、評価損益は平均取得単価から直接計算可能。将来必要ならスナップショット差分→CanonicalTx調整イベントへ一方向導出可能でロックインしない。

**追記（取引履歴取込の追加時）**: 証券会社の取引履歴を取り込む `transactions` テーブルを足したが、
**スナップショット方式は変えていない**。台帳は「スナップショットをどう説明するか」を持つ補助であって、
保有数の正ではない。総量は常に MF スナップショットが錨で、台帳で説明できない分は
「取得日不明の期首ロット」として逆算する（`core/cost_basis.py`）。この線を引いた理由:

- 証券会社は直近数年分しか履歴を出せないことが多く、台帳を正にすると保有数が復元できない
- 再計算した取得原価をスナップショット行として書くと `matching._missing_rows` が `origin='mf'` しか
  ゼロ化しないため、その銘柄は売却されても永久に消失検出されなくなる。加えて `commit_batch` の
  UPSERT が同じキーの行を黙って上書きし所有バッチを付け替えるため、巻き戻しも壊れる。
  よって再計算結果は別テーブル `holding_cost_basis`（派生値・常に再生成可能）に置き、
  `portfolio.lot_cost_jpy` が override として読む
- 部分被覆では残余原価を MF の平均取得単価から逆算するので、再計算した平均取得単価は
  MF と数学的に一致する（恒等式）。したがって損益への上書きは**完全被覆かつ単一ロットのときだけ**。
  MF の lot_seq は取込時に平均取得単価の近さで機械的に割り当てたものなので、
  CSV の「特定/一般/NISA」を確実に対応づける術がなく、突合は (口座, 銘柄) 単位で行う

主要テーブル（Decimal はすべて TEXT 保存、PRAGMA は ledger.py 踏襲: WAL/busy_timeout=30000/synchronous=NORMAL）:

- **accounts**: name(UNIQUE・正規化済み機関名), display_name, kind(bank/broker/pension/point/manual/other), origin('mf'|'manual')
- **securities**: code(正規化済・部分UNIQUE), name, name_key(照合キー), asset_class(cash/stock_jp/stock_foreign/fund_jp/fund_foreign/bond/metal/real_estate/pension/point/other), currency, unit(share/kuchi/gram/currency/unit/point), **price_unit_divisor**(投信=10000), price_source_type(none/yahoo/toushin/metal/fx/manual), price_source_ref(ISIN:協会コード 等), price_source_status(unlinked/linked/manual/not_required), inactive
- **security_aliases**: (alias_key, source_kind)→security_id。MF側の名称変更・表記ゆれの名寄せ
- **import_batches**: id(uuid), source_kind('mf_pdf'), filename, file_sha256(committed時部分UNIQUE=二重取込防止), as_of_date, status(previewed/committed/discarded), parse_report(JSON)
- **holding_snapshots**（中核・全資産クラス統一）: (account_id, security_id, **lot_seq**, as_of_date) UNIQUE, quantity, avg_cost, reported_price, **reported_value_jpy**(未リンク銘柄のフォールバック評価), reported_pl_jpy(検算用), lot_label(預金の商品名/NISA等), origin, batch_id, raw(JSON)
  - **current_holdings ビュー** = キーごとの最新スナップショット
- **daily_prices**: (source, source_id, date)→price, currency。プロバイダ取得値のほか source='mf_reported'/'manual'（不動産手動評価もここ。manual_valuations専用テーブルは作らない）。FXも source='frankfurter', source_id='USD' で同居。**不動産価格指数**も source='re_index', source_id='<地域>:<種別>'（例 'nanto:condo'）で同居し、月初日に月次の水準を持つ
- **fetched_ranges**: (source, source_id, start_date, end_date)。「範囲内に日付が無い=非営業日」を再取得しないための被覆簿記（週末再取得問題の解）
- **spot_cache**: (source, source_id)→price, fetched_at。TTL 300秒
- **app_settings**: include_pension / include_points（初期値 '1'）/ default_currency / merge_cash（初期値 '1'=保有一覧で預金を「A銀行 他N件」の1行に合算。'0'で銀行ごとの行に戻す）

**銘柄同一性**:
- コード正規化: NFKC→5文字末尾'0'かつ先頭4文字が `^[0-9][0-9A-Z]{3}$` → 4文字へ切詰め（大和対応）。ただし生コードが既存codeに一致すればそのまま。生値はrawに保存
- 投信は name_key（NFKC→小文字→全空白・中黒除去→末尾`（\d+）`除去）で照合。照合順は **code → alias → name_key**
- 同一(口座,銘柄)の複数行は **lot_seq でロット分離保持**（統合しない。avg_cost近傍マッチで再取込時に対応付け。ミスしても合計は不変＝自己修復的。預金ロットのみ lot_label 完全一致で照合）
- 金・プラチナETF(1541/447A/521A)は初期値 stock_jp。asset_class はUIから銘柄ごとに変更可（metalへの再分類は好みで）
- **銘柄の統合（名寄せ）**: MF PDF は同じファンドを証券会社ごとの表記で書くため、複数機関で保有すると別銘柄として二重登録され得る（例: オルカンと「eMAXIS Slim全世界株オール(8782)」）。`store.merge_security(source, target)` が単一トランザクションで保有・取引・タグ・手動価格を target へ移し、source 名を alias に登録して source を削除する（`POST /api/securities/{id}/merge`、UI は管理→銘柄一覧の「統合」）。同一口座で lot_seq が重なる場合のみ空き番号へ振り直す（系列の同一性は保つ）。誤統合ガードとして asset_class・currency・unit・price_unit_divisor の一致を要求（年金は quantity=1/avg_cost=総額でデータの持ち方が違うため投信とは統合不可）
- **同一ファンド連携の自動統合**: toushin の price_source_ref（ISIN:協会コード）が同じ2銘柄は同一ファンドだと確定できる（名前類似度と違い誤判定の余地なし）ため、`fund_autolink.dedupe_same_fund` が確認なしで merge_security を実行する。トリガは lifespan 起動時（連携済み重複の既存DB救済）・`/api/fund-links/apply`・`PUT /api/securities`（toushin 連携時）。生存銘柄は inactive でない → 証券会社コード付き切り詰め名でない → 現在保有数が多い → 名前が長い、の順で選ぶ。属性が食い違う組は ConflictError を warn に流して残す（冪等）
- **年金の口数逆算（連携で静的評価→NAV自動評価へ）**: MF の年金セクションは取得価額と評価額のみ（口数・基準価額なし）で、従来は quantity=1・reported_value_jpy の静的評価だった。年金銘柄を toushin へ連携すると `fund_autolink.derive_pension_units` が 評価額÷NAV×10000 の口数を逆算する（整数口で評価額を円未満まで再現できる基準日を PENSION_NAV_WINDOW_DAYS 内で探す。見つからなければ直近NAVの比例口数でアンカーし、次回取込の逆算で確定値に置換）。`derive_pension_quantities` が全スナップショットを実口数へ書き直し、divisor=10000・unit=kuchi へ揃える（avg_cost=取得価額総額の意味は不変）。以後は既存の 口数×NAV 評価がそのまま効き、前日比・推移も自動。トリガは `PUT /api/securities`・`/api/fund-links/apply`・lifespan 起動時、および MF 取込プレビュー（matching._derive_pension_targets が new_quantity を逆算するので、取込のたび掛金買付ぶんの口数に追随する）。口数未導出（quantity<=1）の年金×toushin は portfolio 側のガードで記載値評価にフォールバックし、1口×NAV には潰れない。suggest_links は年金（未連携）も対象に含めるが、基準価額の記載が無く NAV 照合できないため自動確定はしない（候補から人が選ぶ）。代わりに取込2回分以上あれば**値動き照合**で候補の裏を取る（`_movement_check`: 前回評価額×候補NAVの騰落率+掛金増分(取得価額の差)≒今回評価額。T+1公表ずれは前後とも窓内全組合せの最良誤差で吸収、許容 0.5%・直近最大6期間、全期間一致のみ✓）。同一指数の別ファンドは区別できないため並び順と表示の裏取りに留め、値動き一致の候補は名前スコア閾値割れでも提示する。誤連携の検出はもう1枚: 取込時の口数逆算が整数口で評価額を再現できなければ（正しいファンドなら恒等的に再現できる）プレビューに警告を出す

## MF PDF インポート

**パーサ**: `extract_words()` 座標ベースの状態機械（extract_text行解析・extract_tablesは不採用）。
- セクション見出し完全一致（預金・現金/株式(現物)/投資信託/暗号資産/年金/ポイント）で状態遷移。セクション状態はページをまたいで持続。ヘッダ再出現時は列スパン（ヘッダ語のx範囲中点で境界）のみ再計算
- レコード検出 = アンカー列（株式:保有数&評価額が数値/預金:残高が円 等）を持つ行。折返し断片（銘柄名・機関名）は直前レコードへ結合。円グラフの重複文字ノイズ・`ヘルプ・サポート`フッタは除外
- **3層検算**: 行内（数量×単価÷divisor≒評価額±2円）→セクション（合計:N円と照合）→総額。結果とconfidence(0-1)を ParseReport としてバッチに保存、プレビューに表示。confidence<0.7 の行は既定でチェック外
- 中間形式 `ParsedHolding`（section/institution/name_raw/code_raw/quantity/avg_cost/price/value_jpy/pl_jpy/meta/confidence/warnings）は将来のスクショOCR/CSV取込と共用する Protocol（`HoldingsImporter.parse()->ParseResult`）

**フロー（2段階）**: `POST /api/import/pdf`（base64、crypto方式）→ sha256重複チェック → parse → previewed バッチ保存 → プレビューUI（セクション別件数チップ・diff表: 新規/数量変更/単価変更/変更なし/PDFに無し、行チェックon/off、**as_of日付入力=既定はPDFファイル更新日**）→ `POST /api/import/{batch_id}/commit` 単一トランザクション（unchanged も snapshot を書く。reported_price を daily_prices source='mf_reported' へも書く）→ 履歴一覧から `DELETE /api/import/batches/{id}` でバッチ単位巻き戻し。
- **暗号資産セクション**: パースして総額検算には使うが取込対象外（プレビューに「Crypto-Summaryの管轄」表示）
- **消失銘柄**: origin='mf' の口座×過去MF由来行に限定して検出し、「売却(数量0)」を既定提案・行ごとに「保持」へ変更可。手動資産は構造的に対象外

**手動入力**（すべて origin='manual' のスナップショット）: 銘柄/口座CRUD、保有登録（数量・平均取得単価・as_of）、現物貴金属（asset_class=metal, unit=gram, quantity=グラム数, avg_cost=円/g）、預金（通貨別cash銘柄、外貨は原通貨建て+FX換算）、不動産（quantity=1, avg_cost=取得価額、評価履歴は daily_prices source='manual' へ日付+評価額を登録）。**手動評価が唯一の情報源のときは疎な査定額をそのまま返さず、`core/re_index.py` が日次へ導出する**（`price_series_for_security` の分岐）。指数を紐付けていなければアンカー間の線形補間、紐付けていれば指数の形でチェーンリンク補間し、最終査定日より後も指数で延長する（全アンカーを厳密に通る）。紐付けは `securities.price_source_ref` に `'re_index:<地域>:<種別>'` を入れるだけで、price_source_type/status は manual のまま——列追加が要らない（本リポジトリに migration 機構は無い）。
- MF PDFの円換算済み外貨預金（「豪ドル普通 4円」）はJPY残高として保存（通貨推定はrawにヒント記録のみ）

## 価格データ層（全エンドポイント実地検証済み 2026-08-04）

| 資産クラス | 一次ソース | 備考 |
|---|---|---|
| 国内株・ETF | Yahoo v8 chart `https://query1.finance.yahoo.com/v8/finance/chart/{9433.T}?period1=..&period2=..&interval=1d` | UAヘッダのみ必要。新形式 521A.T/447A.T/180A.T 解決確認。**adjcloseではなく生closeを使う**（adjcloseは配当込みで評価額を歪める）。firstTradeDate以前は範囲クランプ。spotは同APIの meta.regularMarketPrice |
| 投信（基準価額） | 投信協会: 検索 `POST https://toushin-lib.fwg.ne.jp/FdsWeb/FDST999900/fundDataSearch`（JSON, t_keyword）→ NAV履歴 `GET .../FdsWeb/FDST030000/csv-file-download?isinCd=..&associFundCd=..` | CSVはShift-JIS・設定来全履歴・`YYYY年MM月DD日`形式・T+1公表。1万口あたり（divisor=10000）。分配金列あり |
| 貴金属(円/g) | 合成: Yahoo GC=F/SI=F/PL=F(USD/ozt) × USDJPY ÷ 31.1034768 | 田中貴金属は日次12ヶ月分のみで履歴に不足→任意のpremium_factor較正+当日小売表示をv2候補に |
| FX | Frankfurter `GET https://api.frankfurter.dev/v1/2015-01-01..2025-12-31?base=USD&symbols=JPY` | ECB参照レート・キー不要・10年を1コールで取得。週末欠損→クエリ層でforward-fill |
| 不動産・未リンク投信 | ManualProvider（daily_prices source='manual'/'mf_reported'） | 同一ファサードを通す（特別扱いなし） |
| 不動産価格指数 | 国交省 xlsx（`providers/re_index.py`、stdlib zipfile+ElementTree。openpyxl不要） | 月次・2010年平均=100・公表は数ヶ月遅れ。1DLで全16地域×4種別。1日1回まで再取得。PDL1.0で**出典表記が必須** |

- `PriceProvider` Protocol（fetch_spot / fetch_history、**建値通貨のまま返す**）+ price_source_type によるルーター。スロットル: yahoo 1.5s・toushin 1.0s・frankfurter 0.5s、429は3s/6sバックオフ後 warn で諦める（crypto流の warnings-as-data、例外にしない）
- **過去日は恒久キャッシュ・当日は spot 層のみ**（daily_prices に今日を入れない）。欠損判定は fetched_ranges 被覆で行う（価格行の有無ではなく）
- forward-fill は**保存せずクエリ層で実施**（休日カレンダーのハードコードなし）
- **投信リンクフロー**: 取込→unlinked銘柄→設定画面「価格ソース未設定」一覧→ fundDataSearch 検索（初期クエリ=銘柄名正規化）→候補選択→ price_source_ref='ISIN:協会コード' 保存→履歴遡及取得。ヘッジあり/なし等の亜種があるため自動確定はしない

## 評価・損益ロジック（core/portfolio.py、Python側でDecimal計算）

- `value(lot, d)`: cash→数量(×FX)。それ以外→ price(d, 直近過去へフォールバック) × qty ÷ divisor × FX。価格が無い場合は reported_value_jpy を「参考値」バッジ付きで使用
- `pl(lot, d)`: avg_cost無し(現金・ポイント)→対象外（合計から除外し「※取得単価未設定 n件を除く」注記）。年金→ value − 取得価額（総額ベース）。通常→ value − qty×avg_cost÷divisor×FX
- 日次系列: 日付dごとに「as_of≤d の最新スナップショット」の階段関数 × 価格(d)。**d が最古スナップショットより前は最古スナップショットの保有数で遡及**（確定要件3）。表示範囲は 7D/30D/90D/1Y/ALL。タグ・Myポートフォリオのスコープは `ratio_by_security`（銘柄→計上率）を掛けて按分する
- `day_change(lot)`: 現在の評価額 − **前日の評価額**（＝前日のスナップショット数量 × 前日終値）。数量も前日のものを使うのは、現金が「数量そのものが金額」で価格を持たず、当日の数量で評価すると前日比が定義上ゼロにしかならないため（2026-08-28 に「相場変動のみ」から変更）。株・投信も同じ規則なので、現金で株を買った日は現金の減少と株の増加が相殺され、振替が見かけの損失にならない。**前日にまだ記録の無いロットは当日の数量で評価する**（日次系列と同じ遡及ルール — 初めて取り込んだ保有が「その日に増えた」ことにならないように）。**当日ゼロになったロットはその日だけ減少として残す**（売却日に現金の増加だけが乗ると見かけの利益が出るため）。前日の保有は `store.holdings_as_of(基準日)` で引く。**基準日は暦ではなくデータ側の最新地点から数える**: `max(最新スナップショット日, 今日−1日) − 1日`（`web/app._prev_snapshot_day`）。「今日の前日」に固定すると、取込がもたらした変化は取込時刻から次の深夜0時までしか出ず、夜に取り込む運用では数時間で 0 に戻る（UTC コンテナ + 朝の取込で実際にそうなった）。0時時点の保有額は前日に取り込んだデータの金額なのだから、最新地点の1つ前と比べるのが正しい。取込が2日以上空くと基準が最新スナップショットに追いつき、数量差は自然に消えて相場ぶんだけになる（古い変化を出し続けない）。この定義により合計の前日比は**推移グラフの前日との差と一致する**（入出金・買付ぶんも含む）。価格の基準は「いまスポットとして採用している値の日付より前の最後の終値」で、`prices.fetch_prev_close()` がソース別に解決する — **投信・手動評価はスポット自体が日次系列の最新行なので、その1つ前**（一律「昨日」では現在値と同じ行を掴む）。外貨建ては前日FXレートで換算し、表示通貨換算も当日・前日それぞれのレートで行う（表示通貨で見た増減になる）。基準日が7日以上離れた系列は前日比を出さない（手動評価の年金・不動産で「前回登録時との差」が出るのを防ぐ）。**指数で延長した不動産にも前日比は出さない**——導出系列は読み出し時計算で daily_prices に書かないため、前日比の基準にはアンカー（査定額）しか見えず、この7日ルールがそのまま効く
- 取込スナップショットの `as_of`（自動取込は PDF の更新時刻の日付）は**サーバの暦**で決まる。コンテナを UTC で回すと、日本時間の朝に取り込んだぶんが前日の日付で入り、推移グラフの横軸や取込履歴が実際の日付とずれる（前日比の基準日はデータ側の最新地点から数えるので、こちらは暦に依存しない）。compose は `TZ=${TZ:-Asia/Tokyo}` を渡す
- 前日比は `daily_prices` を読むだけで**ネットワークを使わない**（履歴の充填は推移グラフの `ensure_price_history` に任せる）。履歴が無い銘柄は `day_change=None`＝画面では「—」。合計は前日値の判る保有だけを足し、欠けがあれば `day_change_partial` を立てて「※前日の値が判らない資産を除く」と断る
- Crypto-Summary 由来のコインは、CS の `/api/summary` が各資産に返す `prev_value`（＝**前日の残高 × 前営業日の終値**）をそのまま使う。CS 側も同じ「実差分」の定義に揃えてあるので、暗号資産の入出庫・売買も前日比に乗る。判定は `prev_balance` の有無 — これを返さない旧 CS の `prev_value` は「いまの残高 × 前日終値」＝相場変動のみで、そのときだけ暗号資産の定義が揃わない。CS はその日にゼロにした資産も `balance="0"` で返すので、AS 側でも売却当日の減少として1日だけ行に出る（AS 内の保有と同じ扱い）。追加リクエストは発生しない。`prev_value` を返さない旧 CS が相手のときだけ、`scope=total` の日次履歴1本から合計だけを求めるフォールバックに回る（コイン別の明細は「—」）
- 年金/ポイントは app_settings のトグルで集計から除外するだけ（データは常に保存、切替即時・可逆）

## タグ配分と自動配分（core/tagging.py + core/tag_rules.py）

タグは「1銘柄あたり合計100%」の配分率を持ち、オルカンのような複合投信を 世界株95/国内株式5 のように按分できる（タグ別合計=総額、重複計上なし）。合計が100%に満たない残りは**未分類**として集計され、警告バナーに出る。タグ割当は手動（銘柄へのタグ割当UI）に加え、**ルールベースの自動配分**（Myポートフォリオページ「自動配分」）で提案→確認→適用できる。

**判定の3層優先順位（tag_rules.py）**: ①証券コード（`CODE_RULES`、最優先。証券会社の略称「IFナス100H無」等が表記変更でキーワードから外れても効く保険）→ ②銘柄名キーワード（`fund_autolink.normalize()` 正規化名への部分一致）→ ③資産クラス（最後の砦・`fallback=True`）。asset_class は「どの取引所か」しか語らないため**必ず最後** — NASDAQ100連動ETF(2840)が国内上場というだけで国内株式扱いされた誤りの再発防止。

- **fund_jp / pension にフォールバックは置かない**。未知の投信は未一致のまま未分類バナーに現れる（静かに間違えるより見える方が安全）。stock_jp のフォールバックは個別株に必要なので、代わりに名前が投信・ETFらしい場合（etf/上場/インデックス等）に**警告フラグ**を立てて UI で「⚠ 投信・ETFの可能性」表示+既定未チェック
- **キーワードは常に具体的に**（`国内株` であって `国内` ではない）。裸の `リート` は禁止 — 「ステート・スト**リート**」に部分一致した実害あり。順序の必須制約は「ハイリスク層がゴールド層・世界株層より上」の2つだけ（ゴールドプラス/FANG+ゴールド対策）で、他は順序非依存（40,320通りの順列総当たりで確認）
- タグはユーザーが作る行で id が不安定なため**ルールは名前で参照**し、適用のたびに名前→id を解決。存在しないタグ名は missing-tag として報告（黙って落とさない）。適用はサーバ側でルールを再判定し、クライアントの配分値は信用しない
- 名前正規化は `tag_rules.norm_for_rules()` = `fund_autolink.normalize()` + CJK部首の畳み込み（U+2ED1「⻑」等は**NFKCで畳まれない**。180A「GX超⻑期米国債」で実際に観測）。`fund_autolink.normalize()` 自体は変更しない（name_score→投信自動連携の自動確定しきい値に影響するため）
- `/api/fund-links/suggest` と違い外部照会が無くローカル計算のみで一瞬なので、**ロック・進捗UIは不要**（コピペしないこと）
- 全世界株95/5・8資産均等25/12.5/37.5/25 等の配分値は指数構成のスナップショット（提案を人が確認する前提の近似）。class.metal→ゴールドも近似（銀等も含む）。到達度の実測: 未保有の実在85銘柄で 正解69 / 安全に未一致12 / 警告付きフォールバック4 / **黙って間違えるゼロ**（tests/test_tag_rules.py がこの表ごと固定）

## Web UI / API

ページ（ハッシュルーター、crypto流用）: `#dashboard`（ヒーロー総額+億万円+評価損益+前日比 / 警告帯 / 資産推移チャート / クラス別ドーナツ+口座別上位 / 主な保有テーブル）、`#classes`・`#accounts`（一覧→詳細）、`#holdings`（全保有+クラスフィルタ+検索→**銘柄詳細**）、`#portfolios`（タグ別サマリー・Myポートフォリオ・タグ管理・**自動配分**・銘柄へのタグ割当→タグ/ポートフォリオ詳細）、`#import`（アップロード→プレビューdiff→確定、取込履歴）、`#manage`（銘柄・保有/現金/貴金属/不動産/年金・ポイントのタブ式フォーム）、`#settings`（表示設定トグル・**価格ソース連携**・データ管理）。

- **詳細画面は全て同じ並び**: ヘッダ → メタ → 警告帯 → 推移チャート → ヒーロー/統計タイル → テーブル（クラス・口座・銘柄・CSコイン・タグ・Myポートフォリオ）。同じ意味の情報はどの画面でも同じ列・同じ遷移で出す — 「クラス詳細では詳しいのにタグ詳細では列が少なく遷移もできない」といった食い違いを作らない
- **保有テーブルは1つの描画関数**（`renderHoldingsRows`）に集約: 銘柄/口座/数量/平均取得単価/現在値/評価額/**前日比**/評価損益/損益率＋行クリックで銘柄詳細（CS由来コインはCSコイン詳細）へ。画面固有の列は `extraCols` で足す（タグ詳細の「計上額（計上率）」など）。ソート・モバイルの折りたたみもこの関数が面倒を見る
- 前日比と計上額は**1セル2行**（上に金額、下に%）。Yahooファイナンスの「前日差」と同じ見せ方

- 損益の表示規約: `+`緑 / `−`赤 / 不明は `—`。マスクモードは損益額も隠す（%は表示）。クラス別固定色。スタイルは crypto の style.css をほぼそのまま（パレット共通=将来統合時に1プロダクトに見える）
- API（crypto と同じ封筒規約: Decimal文字列・currency/warnings/generated_at・scope文字列 total|account:X|class:Y|security:N|tag:N|portfolio:N・既定通貨JPY）:
  `GET /api/meta` `GET /api/summary` `GET /api/classes` `GET /api/class-holdings` `GET /api/accounts` `PUT /api/accounts/{id}` `GET /api/account-holdings` `GET/POST/PUT/DELETE /api/securities` `POST /api/securities/{id}/merge`（名寄せ統合） `GET/POST/PUT/DELETE /api/holdings` `POST /api/securities/{id}/manual-price` `GET /api/portfolio-history?scope=&range=&currency=` `GET /api/security/{id}` `POST /api/import/pdf` `POST /api/import/{batch}/commit` `GET /api/import/history` `DELETE /api/import/batches/{id}` `GET /api/fund-search?q=` `POST /api/refresh-prices` `PUT /api/settings`
  タグ/Myポートフォリオ系: `GET/POST/PUT/DELETE /api/tags` `GET /api/security-tags` `PUT /api/securities/{id}/tags` `GET /api/tag-summary` `GET /api/tags/{id}/holdings` `GET/POST/PUT/DELETE /api/portfolios` `GET /api/portfolios/{id}` `POST /api/tag-rules/suggest` `POST /api/tag-rules/apply`（自動配分）、投信自動連携: `POST /api/fund-links/suggest` `POST /api/fund-links/apply`
- CLI: `asset-summary [--db] web|import PDF [--as-of][--yes]|summary`

## 実装フェーズ

- **M1 骨格+ストア+手動入力+ダッシュボード**: .gitignore→PDF移動→git init。pyproject、core/{models,store,portfolio}(履歴なし版)、web/app.py(summary/classes/accounts/securities/holdings)、static一式リスキン、tests/{test_store,test_web}。検証: pytest緑・手動登録→ダッシュボード反映・`git status`に個人情報なし
- **M2 PDF取込**: importers/{base,mf_pdf,matching}.py、取込API+プレビューUI、fixtures(架空データ合成wordリスト)。検証: 3層検算が実PDFで一致（総資産から暗号資産を除いた額）、二重取込防止、バッチ巻き戻し
- **M3 価格プロバイダ+履歴チャート**: price_store(範囲ロジック**テスト先行**)→fx.py→yahoo.py→ファサード→toushin.py+fund-search→metal.py。全チャート配線。検証: モックHTTPのpytest+実データで521A.T/投信NAV/金価格が引けること
- **M4 損益仕上げ+リンクUI+不動産**: 銘柄詳細ページ、設定ページ(トグル+価格ソース連携)、不動産評価履歴、ヒーロー損益行、マスク監査。検証: 未リンク投信のリンク一巡・トグルで総額変化・マスクで全金額秘匿
- **M5 堅牢化**: エッジケーステスト（空DB・部分PDF・同一as_of再取込・保有付き銘柄削除409）、docs、EN辞書、検証チェックリスト完走

## 検証方法

- pytest: パーサ（合成wordリスト: 折返し結合・ヘッダ2行・負損益・全角正規化・セクション跨ぎページ）、store/portfolio（階段関数・遡及・損益・Decimal往復）、API（TestClient+fetch_pricesモンキーパッチ、crypto の test_web.py 流儀）。価格キャッシュは autouse fixture で tmp_path へ
- 実機: `asset-summary web` → http://127.0.0.1:8010 → 実PDF取込→MFサイトの数値と突合 → docs/verification_checklist.md のスモーク（チャート4箇所・レンジタブ・マスク・言語・テーマ・モバイル展開行）
- Playwright は v1 では導入しない

## 将来統合（今やる最小限のみ）

- API形状互換（summary/portfolio-history のフィールド名・Decimal文字列・scope文法）+ `/api/meta` に `{"app":"asset-summary"}` 識別子 + summary 資産に asset_class 必須（crypto側は暗黙 'crypto'）
- localhost限定CORS許可のみ準備。共有DB・共通ライブラリ切出し・認証統合はやらない。統合ダッシュボードは両RESTを横断消費する将来アプリ

## 実装上の注意

- **個人情報防護**: 実PDF・DB・実データはコミット禁止（gitignore + フィクスチャは架空データ）。初回コミット前に必ず確認
- Windows: パスに空白（要引用符）、cp932コンソール（CLIはrich経由・生テキストをprintしない）、uvicorn `loop="asyncio"`（crypto cli.py の WinError 10054 対策を踏襲）、アップロードPDFはASCII名の一時ファイルへ
- Yahoo は非公式API（robots全禁止）: 恒久キャッシュ+1.5sスロットルで初回同期以外ほぼ叩かない設計を厳守。投信協会CSVは1銘柄1日1回まで
- MF PDF印刷時にブラウザの「ヘッダーとフッター」を有効にすると日付が入る（as_of自動化の将来余地としてREADMEに案内）
