# mf_pdf_autosave — マネーフォワードME 資産内訳PDFの定期保存

マネーフォワードMEの「資産 → 資産内訳」ページを、ブラウザの「印刷 → PDFに保存」と
同等のPDFとして指定フォルダへ自動保存するスタンドアロンツール。
出力PDFはそのまま Asset Summary の「マネーフォワードME PDF取込」（Web UI / CLI）で使える。

Asset Summary のサーバ機能からは完全に独立しており、本体を起動していなくても動く。
依存（Playwright）も本体パッケージには追加していない。

## 仕組み

- Playwright のヘッドレス Chromium が、このツール専用のブラウザプロファイル
  （既定: `~/.asset-summary/mf-profile`）で資産内訳ページを開き、`page.pdf()` で保存する
- 保存の前に「登録済み金融機関」ページの**一括更新**を実行し、全口座の
  「更新中」表示が消えるまで待ってから資産内訳を取得する（既定最大15分。
  `--no-refresh` で省略、`--refresh-timeout` で上限変更。時間内に終わらない
  場合はその時点の状態で保存し、**未完了の口座名と最終取得日**をログに残す）。
  確認は15秒間隔のリロードで行い、件数に変化が無くても1分ごとに経過をログに出す
- MFには「更新中」表示のまま終わらない口座があるため、**最終取得日が
  `--fresh-hours`（既定12時間）以内の口座は「更新中」でも更新完了とみなす**
  （どの口座を完了扱いにしたかはログに残る。`--fresh-hours 0` で無効化）
- ログインは**初回に1回だけ手動**で行う（`--login` で実際のブラウザ画面が開く）。
  メールアドレスやパスワードはどこにも保存せず、ログイン後のセッションCookieを含む
  プロファイルだけを使い回す
- セッションが切れたら終了コード `2` で失敗し、ログに「--login で再ログインして
  ください」と残る（再度 `--login` すれば復旧）

## セットアップ

Asset Summary 本体と同じ venv に入れて構わない（本体の動作には影響しない）。

```powershell
cd "C:\path\to\Asset Summary"
.venv\Scripts\Activate.ps1
pip install -r tools\mf_pdf_autosave\requirements.txt
python -m playwright install chromium
```

Linux / macOS:

```bash
cd /path/to/Asset-Summary
source .venv/bin/activate
pip install -r tools/mf_pdf_autosave/requirements.txt
python -m playwright install chromium
```

## 初回ログイン

```powershell
python tools\mf_pdf_autosave\mf_pdf_autosave.py --login
```

ブラウザ画面が開くので、マネーフォワードMEに普段どおりログインする（2段階認証も
そのまま操作できる）。ログイン後に資産内訳ページ（「資産総額」が表示されるページ）が
開くと自動的に完了し、動作確認として1回分のPDFがその場で保存される。
資産内訳ページが自動で開かない場合は、手動で「資産 → 資産内訳」を開けばよい。

## 手動実行

```powershell
python tools\mf_pdf_autosave\mf_pdf_autosave.py --out-dir "C:\path\to\Asset Summary\data\mf_pdf"
```

- 保存先を省略するとリポジトリの `data/mf_pdf/`（gitignore済み）に保存される
- ファイル名は既定で `マネーフォワードME_資産内訳_YYYY-MM-DD.pdf`
  （同日に再実行すると上書き。`--filename "MF_{datetime}.pdf"` のように変更可）
- 実行ログは `<保存先>/mf_pdf_autosave.log` に追記される

ブラウザ画面を見ながら動作確認したいときは `--headful` を付ける:

```powershell
python tools\mf_pdf_autosave\mf_pdf_autosave.py --headful
```

一括更新のクリック → 「更新中」が消えるまでの様子 → 資産内訳の表示までが実際の
ブラウザ画面で見える。ChromiumのPDF生成はヘッドレス専用のため、確認が済むと
ブラウザは自動で閉じ、続けてヘッドレスで（一括更新はやり直さずに）PDFが保存される。

## 定期実行（Windows タスクスケジューラ）

登録スクリプトを使うのが簡単（現在のユーザーで登録。パスワード保存は不要）:

```powershell
cd "C:\path\to\Asset Summary\tools\mf_pdf_autosave"

# 毎日 07:30 に保存、直近30世代だけ残す
.\register_task.ps1 -OutDir "C:\path\to\Asset Summary\data\mf_pdf" -At 07:30 -Keep 30

# 毎週土曜 08:00 なら
.\register_task.ps1 -OutDir "D:\MoneyForward" -Frequency Weekly -DaysOfWeek Saturday -At 08:00

# 今すぐ1回動かして確認
Start-ScheduledTask -TaskName 'MoneyForward資産内訳PDF保存'

# 削除
.\register_task.ps1 -Unregister
```

- タスクは「ログオン中のみ実行」で登録される。予定時刻にスリープ・電源断だった場合は、
  次にPCが使える状態になった時点で1回だけ追い付き実行される
- `pythonw.exe` を使うためコンソール窓は出ない。結果はログファイルで確認する

## 定期実行（Linux / macOS cron）

```cron
# 毎日 07:30
30 7 * * * /path/to/Asset-Summary/.venv/bin/python /path/to/Asset-Summary/tools/mf_pdf_autosave/mf_pdf_autosave.py --out-dir /path/to/save --keep 30
```

## Asset Summary への取込

保存されたPDFは通常どおり Web UI（インポートページ）でアップロードして
プレビュー・差分確認してから確定するのが安全。

CLIで取込まで自動化したい場合（プレビュー無しの確定取込になる点に注意）:

```powershell
$dir = "C:\path\to\Asset Summary\data\mf_pdf"
$latest = Get-ChildItem "$dir\*.pdf" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
asset-summary import $latest.FullName --yes
```

## オプション一覧

| オプション | 既定値 | 意味 |
|---|---|---|
| `--login` | — | ブラウザ画面を開いて手動ログイン（初回・セッション切れ時） |
| `--headful` | — | ブラウザ画面を表示して一括更新〜資産内訳の表示を確認（保存は確認後にヘッドレスで実施） |
| `--out-dir` | `<repo>/data/mf_pdf` | 保存先フォルダ（環境変数 `MF_PDF_OUT_DIR` でも指定可） |
| `--filename` | `マネーフォワードME_資産内訳_{date}.pdf` | `{date}`=YYYY-MM-DD、`{datetime}`=YYYY-MM-DD_HHMM |
| `--profile-dir` | `~/.asset-summary/mf-profile` | ログイン状態を保持するプロファイル（`MF_PDF_PROFILE_DIR`） |
| `--keep` | 0 | 残す世代数（0=無制限。パターン一致の古いPDFから削除） |
| `--url` | 資産内訳ページ | 取得するURL |
| `--refresh` / `--no-refresh` | 有効 | 保存前に一括更新を実行して完了（「更新中」の消滅）を待つ |
| `--refresh-timeout` | 900 | 一括更新の完了を待つ最大秒数（超過時は現状のまま保存し、未完了口座をログに残す） |
| `--fresh-hours` | 12 | 最終取得日がこの時間以内なら「更新中」表示でも完了とみなす（0で無効） |
| `--accounts-url` | 登録済み金融機関ページ | 一括更新を行うページのURL |
| `--ready-text` | `資産総額` | このテキストの表示をもって準備完了とみなす |
| `--timeout` | 90 | ページ表示待ち秒数 |
| `--settle` | 2 | 表示後の描画安定待ち秒数 |
| `--login-timeout` | 900 | `--login` で手動ログインを待つ秒数 |
| `--log-file` | `<保存先>/mf_pdf_autosave.log` | ログ追記先（`-` で無効） |
| `--browser-path` | 自動 | Chromium実行ファイルの明示指定（`MF_PDF_CHROMIUM_PATH`） |

終了コード: `0`=成功 / `2`=セッション切れ（`--login` が必要） / `1`=その他エラー。

## Claudeルーティン（クラウド定期実行）について

Claudeのルーティンはクラウド上のセッションで動くため、
**このPCのフォルダへの保存**と**このPCに保存したログイン済みプロファイルの利用**が
できない。マネーフォワードのセッションCookieをクラウド環境に置くのはセキュリティ上も
勧めない。そのため定期実行はこのPCのタスクスケジューラ（上記）で行う構成にしている。

## トラブルシューティング

- **終了コード 2 / 「セッションが切れています」** — `--login` で再ログインする。
  マネーフォワード側のセッション有効期限が切れると起きる（数週間〜数か月に1回程度）
- **「資産総額」が表示されない（終了コード 1）** — 失敗時は保存先に
  `debug_last.png`（画面キャプチャ）と `debug_last.html` が残るので、まず
  `debug_last.png` を開いて実際に何が表示されていたかを確認する
  （MF側の障害・メンテナンス・レイアウト変更・キャンペーンモーダル・
  ボット判定画面などが考えられる）。ログイン自体が生きていれば `--login` の
  やり直しは不要で、そのまま再実行すればよい。
  なお、ヘッドレス実行がヘッドフル時と違う扱いを受けないよう、通常Chromiumの
  新ヘッドレスモード＋User-Agentの調整（`HeadlessChrome`→`Chrome`）を
  自動で行っている
- **`--login` のウィンドウを開きっぱなしにしない** — プロファイルを排他ロックするため、
  開いたままだと定期実行側が起動できない
- **数値がPDF保存時点より古い** — 既定では保存前に「一括更新」を実行して
  完了を待つが、金融機関側の応答が遅いと `--refresh-timeout`（既定15分）を
  超えてその時点の値で保存されることがある（ログに「時間内に一括更新が
  終わりませんでした」と残る）。上限を延ばすか、時間帯を変えて再実行する。
  一部の金融機関は追加認証等で自動更新に失敗することがあり、その分は
  MFアプリ側で手動更新が必要
- **`interrupted by another navigation` で落ちる** — 一括更新のクリックが
  遅れて `/accounts` へのリダイレクトを起こし、資産内訳を開く操作と重なった
  もの。クリック後に遷移が落ち着くのを待ち、それでも重なったら開き直すように
  してある（「別のページ遷移と重なったため開き直します」とログに出る）
- **特定の口座がいつも「未完了の口座」に出る** — 「更新中」表示のまま
  終わらない口座は、最終取得日が新しければ `--fresh-hours` の仕組みで
  完了扱いになる。最終取得日も古いままの口座は MF 側で更新が止まっている
  （追加認証待ち等）ので、MFの画面から個別更新を試す
- **文字化け（cp932コンソール）** — `$env:PYTHONIOENCODING = "utf-8"` を設定する

## セキュリティ上の注意

- プロファイルフォルダ（既定 `~/.asset-summary/mf-profile`）には
  **ログイン済みセッションCookie**が入っている。共有PCでは使わない・
  フォルダを共有／コミットしない（リポジトリ外の場所が既定なのはこのため）
- 保存されるPDFは個人資産情報そのもの。保存先は `data/`（gitignore済み）か
  リポジトリ外の任意フォルダを使い、コミットしないこと
