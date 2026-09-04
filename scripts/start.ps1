#Requires -Version 5.1
<#
.SYNOPSIS
    Asset Summary と Crypto-Summary を 1 コマンドで起動する（Windows / PowerShell）。

.DESCRIPTION
    初回に必要な準備（.env の作成、Crypto-Summary リポジトリの場所の解決、
    ログインモードの判定、サービストークンの生成）を済ませてから
    docker compose を実行し、両方が応答するまで待って URL を出す。
    2 回目以降は .env の内容をそのまま使う（勝手に上書きしない）。

.PARAMETER Check
    設定を確認するだけで起動しない。

.PARAMETER Down
    起動中のスタックを停止する。

.PARAMETER Cloud
    docker-compose.cloud.yml（公開用: 名前付きボリューム + Caddy + Google ログイン）で起動する。

.EXAMPLE
    .\scripts\start.ps1
.EXAMPLE
    .\scripts\start.ps1 -Down
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Down,
    [switch]$Cloud
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root '.env'
$ComposeFile = if ($Cloud) { 'docker-compose.cloud.yml' } else { 'docker-compose.yml' }

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "    $msg" }
function Write-Warn($msg) { Write-Host "    ! $msg" -ForegroundColor Yellow }

function Read-EnvFile($path) {
    $map = @{}
    if (Test-Path $path) {
        foreach ($line in Get-Content $path -Encoding UTF8) {
            if ($line -match '^\s*[A-Za-z_][A-Za-z0-9_]*\s*=') {
                $key, $value = $line -split '=', 2
                $map[$key.Trim()] = $value.Trim()
            }
        }
    }
    return $map
}

function Set-EnvValue($path, $key, $value) {
    # コメント行（#KEY=…）は説明として残し、有効な行だけを書き換える。
    $text = ''
    if (Test-Path $path) { $text = [System.IO.File]::ReadAllText($path) }
    $pattern = "(?m)^[ \t]*$key[ \t]*=.*$"
    $line = "$key=$value"
    if ($text -match $pattern) {
        $text = [regex]::Replace($text, $pattern, $line)
    } else {
        if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $text += "`r`n" }
        $text += "$line`r`n"
    }
    # BOM を付けない（docker compose が BOM 付き .env の 1 行目を読み違える）
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))
}

function New-ServiceToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Test-CsRepo($path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $false }
    return (Test-Path (Join-Path $path 'src/crypto_summary')) -and (Test-Path (Join-Path $path 'Dockerfile'))
}

Set-Location $Root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker が見つかりません。Docker Desktop を起動してから実行してください。"
}

if ($Down) {
    Write-Step "停止中"
    docker compose -f $ComposeFile down
    exit $LASTEXITCODE
}

# ---- .env ----------------------------------------------------------------
if (-not (Test-Path $EnvFile)) {
    Write-Step ".env を .env.example から作成"
    Copy-Item (Join-Path $Root '.env.example') $EnvFile
}
$envMap = Read-EnvFile $EnvFile

# ---- Crypto-Summary リポジトリの場所 --------------------------------------
$csContext = $envMap['CS_CONTEXT']
if (-not (Test-CsRepo $csContext)) {
    $candidates = @(
        (Join-Path $Root '..\Crypto-Summary'),
        (Join-Path $Root '..\CS\Crypto-Summary'),
        (Join-Path $Root '..\..\Crypto-Summary')
    )
    $found = $null
    foreach ($c in $candidates) {
        if (Test-CsRepo $c) { $found = (Resolve-Path $c).Path; break }
    }
    if (-not $found) {
        throw @"
Crypto-Summary リポジトリが見つかりません。
$EnvFile に場所を書いてから再実行してください（Windows でも / 区切りで書く）:
    CS_CONTEXT=C:/path/to/Crypto-Summary
"@
    }
    $csContext = $found -replace '\\', '/'
    Write-Step "Crypto-Summary を検出: $csContext"
    Set-EnvValue $EnvFile 'CS_CONTEXT' $csContext
} else {
    Write-Step "Crypto-Summary: $csContext"
}

# ---- ログインモードの判定（CS のデータディレクトリを見る） -----------------
# {google_sub}.db があればマルチユーザー（Google ログイン）、
# なければシングルユーザー（ログイン無し）として起動する。
$csData = Join-Path $csContext 'data'
$subDbs = @()
if (Test-Path $csData) {
    $subDbs = @(Get-ChildItem -Path $csData -Filter '*.db' -File -ErrorAction SilentlyContinue |
                Where-Object { $_.BaseName -match '^[0-9]{5,}$' } |
                Sort-Object Length -Descending)
}

# .env に DATA_DIR の行があれば、その選択を尊重する（空＝シングルユーザー）。
# 行そのものが無いときだけ台帳から判定して書き込む。
$pinned = $envMap.ContainsKey('DATA_DIR')
$multiUser = if ($pinned) {
    -not [string]::IsNullOrWhiteSpace($envMap['DATA_DIR'])
} else {
    $subDbs.Count -gt 0
}

if ($multiUser) {
    Write-Step "Crypto-Summary: マルチユーザー（Google ログイン）"
    if ($pinned) { Write-Info ".env の DATA_DIR に従いました" }
    if ($subDbs.Count -gt 0) {
        Write-Info "台帳: $($subDbs[0].Name)"
        if ($subDbs.Count -gt 1) {
            Write-Warn "台帳が $($subDbs.Count) 件あります。一番大きいものを使います（.env の CS_USER_SUB で変更可）。"
        }
    }
    if (-not $pinned) { Set-EnvValue $EnvFile 'DATA_DIR' '/data' }
    if ($subDbs.Count -gt 0 -and [string]::IsNullOrWhiteSpace($envMap['CS_USER_SUB'])) {
        Set-EnvValue $EnvFile 'CS_USER_SUB' $subDbs[0].BaseName
        Write-Info "CS_USER_SUB=$($subDbs[0].BaseName) を設定しました"
    }
    if ([string]::IsNullOrWhiteSpace($envMap['CS_SERVICE_TOKEN'])) {
        Set-EnvValue $EnvFile 'CS_SERVICE_TOKEN' (New-ServiceToken)
        Write-Info "CS_SERVICE_TOKEN を生成しました"
    }
} else {
    Write-Step "Crypto-Summary: シングルユーザー（ログイン無し）"
    if ($pinned) {
        Write-Info ".env の DATA_DIR が空のため、ログイン不要の構成で起動します"
    } else {
        Write-Info "台帳が無いか ledger.db のみのため、ログイン不要の構成で起動します"
        Set-EnvValue $EnvFile 'DATA_DIR' ''
    }
}

if ($Check) {
    Write-Step "確認のみ（起動しません）"
    docker compose -f $ComposeFile config --quiet
    Write-Info "compose の設定は妥当です: $ComposeFile"
    exit 0
}

# ---- ポートの先客を確認 ---------------------------------------------------
# Crypto-Summary を単独 compose で起動していると 8000 が埋まる。ビルドしてから
# 失敗すると分かりにくいので、先に名指しで知らせる。
$running = docker ps --format '{{.Names}}|{{.Ports}}' 2>$null
$conflicts = @($running | Where-Object { $_ -match ':(8000|8010)->' -and $_ -notmatch '^asset-stack' })
if ($conflicts.Count -gt 0) {
    Write-Warn "ポート 8000 / 8010 を他のコンテナが使っています:"
    foreach ($c in $conflicts) {
        $name, $ports = $c -split '\|', 2
        Write-Warn "  $name  ($ports)"
    }
    Write-Warn "先に停止してください。Crypto-Summary の単独スタックなら:"
    Write-Warn "  Push-Location '$csContext'; docker compose down; Pop-Location"
    exit 1
}

# ---- 起動 ----------------------------------------------------------------
Write-Step "docker compose up -d --build（初回はイメージのビルドで数分かかります）"
docker compose -f $ComposeFile up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Warn "起動に失敗しました。ポート 8000 / 8010 を他のスタックが使っていないか確認してください:"
    Write-Warn "  docker ps --format '{{.Names}}\t{{.Ports}}'"
    exit $LASTEXITCODE
}

Write-Step "応答を待っています"
# 疎通確認だけは 127.0.0.1 を使う。publish 先が IPv4 ループバックなので、
# localhost が先に ::1 へ解決される環境で空振りしないようにするため。
# 画面に出す URL は localhost（Google OAuth のリダイレクト URI に合わせる）。
$targets = @(
    @{ Name = 'Asset Summary'; Url = 'http://127.0.0.1:8010/api/health' },
    @{ Name = 'Crypto-Summary'; Url = 'http://127.0.0.1:8000/api/health' }
)
$deadline = (Get-Date).AddSeconds(120)
foreach ($t in $targets) {
    $ok = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri $t.Url -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if ($ok) { Write-Info "$($t.Name): OK" }
    else { Write-Warn "$($t.Name): 応答なし（docker compose logs で確認してください）" }
}

Write-Host ""
Write-Host "  Asset Summary : http://localhost:8010" -ForegroundColor Green
Write-Host "  Crypto-Summary: http://localhost:8000" -ForegroundColor Green
Write-Host ""
Write-Host "  停止: .\scripts\start.ps1 -Down"
Write-Host "  ログ: docker compose logs -f"
