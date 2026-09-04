# コマンド一覧

## セットアップ

```powershell
cd "C:\path\to\Asset Summary"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Webサーバ

```powershell
asset-summary web                 # http://127.0.0.1:8010
asset-summary web --port 8020     # ポート変更
asset-summary web --lan           # LAN内（スマホ等）からアクセス可
asset-summary --db data\other.db web   # 別DBを使う（--db はサブコマンドの前）
```

## PDF取込（CLI）

```powershell
asset-summary import "data\マネーフォワード ME.pdf"                    # 基準日=PDFの更新日
asset-summary import "data\マネーフォワード ME.pdf" --as-of 2026-08-04
asset-summary import "data\マネーフォワード ME.pdf" --yes              # 確認省略
```

※ Web UI（インポートページ）の方がプレビュー・差分確認ができて安全。

## マネーフォワードME PDFの定期自動保存

詳細は [tools/mf_pdf_autosave/README.md](../tools/mf_pdf_autosave/README.md)。

```powershell
pip install -r tools\mf_pdf_autosave\requirements.txt          # 初回のみ
python -m playwright install chromium                          # 初回のみ
python tools\mf_pdf_autosave\mf_pdf_autosave.py --login        # 初回のみ（手動ログイン）
python tools\mf_pdf_autosave\mf_pdf_autosave.py --out-dir data\mf_pdf   # 手動で1回保存
.\tools\mf_pdf_autosave\register_task.ps1 -OutDir "C:\path\to\Asset Summary\data\mf_pdf" -At 07:30 -Keep 30   # 毎日07:30
.\tools\mf_pdf_autosave\register_task.ps1 -Unregister          # タスク削除
```

## サマリー表示

```powershell
asset-summary summary
asset-summary summary --currency USD
```

## テスト

```powershell
.venv\Scripts\python.exe -m pytest -q            # 全テスト
.venv\Scripts\python.exe -m pytest tests\test_mf_parser.py -q
.venv\Scripts\python.exe -m pytest --cov=asset_summary
```

## 文字化けするとき（cp932コンソール）

```powershell
$env:PYTHONIOENCODING = "utf-8"
```
