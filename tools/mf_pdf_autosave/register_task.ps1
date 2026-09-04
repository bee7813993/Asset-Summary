#Requires -Version 5.1
<#
.SYNOPSIS
    mf_pdf_autosave.py を Windows タスクスケジューラに登録する。

.DESCRIPTION
    マネーフォワードME「資産内訳」PDFの定期保存タスクを、現在のユーザーで
    「ログオン中のみ実行」として登録する（パスワードの保存は不要）。
    予定時刻にPCがスリープ・電源断だった場合は、次に使えるようになった時点で
    1回だけ追い付き実行される（StartWhenAvailable）。

    Python は リポジトリの .venv → PATH の順に探す。コンソール窓を出さない
    pythonw.exe を優先する（ログは保存先の mf_pdf_autosave.log に残る）。

.PARAMETER OutDir
    PDFの保存先フォルダ（必須）。存在しなければ実行時に作成される。

.PARAMETER At
    実行時刻（既定 07:30）。

.PARAMETER Frequency
    Daily（毎日・既定）または Weekly（毎週）。

.PARAMETER DaysOfWeek
    -Frequency Weekly のときの曜日（既定 Monday）。

.PARAMETER Keep
    保存先に残す世代数（0=無制限）。

.PARAMETER PythonExe
    使用する Python を明示指定したいとき。

.PARAMETER Unregister
    登録済みタスクを削除する。

.EXAMPLE
    .\register_task.ps1 -OutDir "C:\path\to\Asset Summary\data\mf_pdf"
.EXAMPLE
    .\register_task.ps1 -OutDir "D:\MoneyForward" -Frequency Weekly -DaysOfWeek Saturday -At 08:00 -Keep 12
.EXAMPLE
    .\register_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$OutDir,
    [string]$At = '07:30',
    [ValidateSet('Daily', 'Weekly')][string]$Frequency = 'Daily',
    [string[]]$DaysOfWeek = @('Monday'),
    [int]$Keep = 0,
    [string]$PythonExe,
    [string]$TaskName = 'MoneyForward資産内訳PDF保存',
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "タスク「$TaskName」を削除しました。"
    exit 0
}

if ([string]::IsNullOrWhiteSpace($OutDir)) {
    throw '-OutDir を指定してください。例: .\register_task.ps1 -OutDir "C:\path\to\Asset Summary\data\mf_pdf"'
}

$ToolDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ToolDir)
$ScriptPath = Join-Path $ToolDir 'mf_pdf_autosave.py'
if (-not (Test-Path $ScriptPath)) { throw "スクリプトが見つかりません: $ScriptPath" }

# ---- Python を探す（.venv 優先、窓を出さない pythonw を優先） ----
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $candidates = @(
        (Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'),
        (Join-Path $RepoRoot '.venv\Scripts\python.exe')
    )
    foreach ($name in 'pythonw.exe', 'python.exe') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    $PythonExe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $PythonExe) { throw 'Python が見つかりません。-PythonExe で指定してください。' }
}

# Playwright がその Python に入っているか軽く確認する（確認は窓の出る python.exe 側で行う）
$checker = if ($PythonExe -match 'pythonw\.exe$') { $PythonExe -replace 'pythonw\.exe$', 'python.exe' } else { $PythonExe }
if (-not (Test-Path $checker)) { $checker = $PythonExe }
# EAP=Stop のまま stderr をリダイレクトすると PS5.1 が例外化するため一時的に緩める
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $checker -c 'import playwright' 2>&1 | Out-Null
$playwrightMissing = ($LASTEXITCODE -ne 0)
$ErrorActionPreference = $prevEAP
if ($playwrightMissing) {
    Write-Warning "この Python に Playwright が入っていません: $PythonExe"
    Write-Warning "  $checker -m pip install -r `"$ToolDir\requirements.txt`""
    Write-Warning "  $checker -m playwright install chromium"
    throw '先に Playwright をセットアップしてください。'
}

# ---- タスク登録 ----
# 相対パスは PS のカレントロケーション基準で絶対化する
# （.NET の GetFullPath はプロセスCWD基準のため Set-Location に追従しない）
$OutDirFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutDir)
# 末尾の \ は引数の閉じクォートを壊す（CRT の argv 規則で \" がリテラル " になる）ため
# 落とす。ドライブルート（C:\ 等）だけは \ が必須なので、クォート時に \ を2倍にして渡す。
$OutDirFull = $OutDirFull.TrimEnd('\')
if ($OutDirFull -match '^[A-Za-z]:$') { $OutDirFull += '\' }
$outDirArg = '"' + $OutDirFull + $(if ($OutDirFull.EndsWith('\')) { '\' } else { '' }) + '"'
$argList = "`"$ScriptPath`" --out-dir $outDirArg"
if ($Keep -gt 0) { $argList += " --keep $Keep" }

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $argList -WorkingDirectory $ToolDir
$trigger = if ($Frequency -eq 'Weekly') {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $At
} else {
    New-ScheduledTaskTrigger -Daily -At $At
}
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'マネーフォワードME 資産内訳ページをPDF保存（Asset Summary 取込用）' -Force | Out-Null

$when = if ($Frequency -eq 'Weekly') { "毎週 $($DaysOfWeek -join ',') $At" } else { "毎日 $At" }
Write-Host "タスク「$TaskName」を登録しました。" -ForegroundColor Green
Write-Host "  実行:     $when"
Write-Host "  コマンド: $PythonExe $argList"
Write-Host "  ログ:     $OutDirFull\mf_pdf_autosave.log"
Write-Host ''
Write-Host '動作確認（今すぐ1回実行）:'
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host '削除:'
Write-Host "  .\register_task.ps1 -Unregister"
