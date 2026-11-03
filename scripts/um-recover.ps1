#Requires -Version 5.1
<#
.SYNOPSIS
    UM GenAI 本機反代（127.0.0.1:8788）一鍵診斷／修復。

.DESCRIPTION
    依序檢查：代理 /health → `status`（上游 UM 是否接受 token）→ 帶 `local-api-key` 的 /v1/models。
    若故障原因是上游 UM 登入 token 過期／被拒，加 -Fix 就會跑 um_opencode_setup.py login
    重新登入（會開一個瀏覽器視窗；Duo 兩階段驗證可能需要你按一下核准）。

    預設只診斷、不改任何東西。

.PARAMETER Fix
    真的執行登入修復（只在判定「上游 token 有問題」時才有意義）。

.PARAMETER Port
    代理埠，預設 8788。

.PARAMETER LoginTimeoutSec
    登入等待上限（秒），預設 170；要留給 Duo 人工核准的時間。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\um-recover.ps1
    只診斷。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\um-recover.ps1 -Fix
    診斷，必要時自動重新登入。

.NOTES
    在 DSH 的受限沙箱裡執行時，login 會因為要寫入 ~\.um-opencode
    而被系統拒絕（PermissionError: [WinError 5]）。請在一般 PowerShell / cmd 視窗執行。

    結束碼：0 = 可用（或已修好）、2 = 需要人工處理、3 = 代理沒在跑、4 = 環境／其他錯誤。
#>
[CmdletBinding()]
param(
  [switch]$Fix,
  [int]$Port = 8788,
  [int]$LoginTimeoutSec = 170
)

$ErrorActionPreference = 'Stop'

$install = Join-Path $env:USERPROFILE '.um-opencode'
$setup   = Join-Path $install 'um_opencode_setup.py'
$keyFile = Join-Path $install 'local-api-key'
$tokFile = Join-Path $install 'upstream-token'
$base    = "http://127.0.0.1:$Port"
$self    = $PSCommandPath

function Write-Step([string]$text) {
  Write-Host ''
  Write-Host "== $text ==" -ForegroundColor Cyan
}
function Write-Ok([string]$text)   { Write-Host "  OK   $text" -ForegroundColor Green }
function Write-Bad([string]$text)  { Write-Host "  FAIL $text" -ForegroundColor Red }
function Write-Warn([string]$text) { Write-Host "  WARN $text" -ForegroundColor Yellow }
function Write-Info([string]$text) { Write-Host "       $text" -ForegroundColor DarkGray }
function Write-Cmd([string]$text)  { Write-Host "  -> $text" -ForegroundColor White }

function Resolve-Python {
  # 先找「使用者範圍安裝」的最新 Python，再退回全機安裝，最後才靠 PATH。
  # 不寫死版本號與使用者名稱：別人的機器不會剛好也是 Python 3.12 或 C:\Python314。
  $candidates = @()
  if ($env:UM_PYTHON) { $candidates += $env:UM_PYTHON }
  $roots = @()
  if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
  $roots += 'C:\'
  foreach ($root in $roots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $candidates += @(Get-ChildItem -LiteralPath $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
      Sort-Object Name -Descending |
      ForEach-Object { Join-Path $_.FullName 'python.exe'; Join-Path $_.FullName 'pythonw.exe' })
  }
  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

function Get-ApiKey {
  if ($env:UM_GENAI_API_KEY) { return $env:UM_GENAI_API_KEY.Trim() }
  if (Test-Path -LiteralPath $keyFile) { return (Get-Content -LiteralPath $keyFile -Raw).Trim() }
  return $null
}

function Get-ProxyHealth {
  try { return Invoke-RestMethod -Uri "$base/health" -TimeoutSec 5 } catch { return $null }
}

function Get-TokenExpiry {
  if (-not (Test-Path -LiteralPath $tokFile)) { return $null }
  try {
    $jwt  = (Get-Content -LiteralPath $tokFile -Raw).Trim()
    $part = ($jwt -split '\.')[1]
    if (-not $part) { return $null }
    $part = $part.Replace('-', '+').Replace('_', '/')
    switch ($part.Length % 4) { 2 { $part += '==' } 3 { $part += '=' } }
    $payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($part)) | ConvertFrom-Json
    if (-not $payload.exp) { return $null }
    return [DateTimeOffset]::FromUnixTimeSeconds([int64]$payload.exp).ToLocalTime()
  } catch { return $null }
}

# /v1/models：Code = HTTP 狀態碼，-1 = 連線失敗
function Test-Api {
  param([string]$Key)
  $headers = @{}
  if ($Key) { $headers['Authorization'] = "Bearer $Key" }
  try {
    $r = Invoke-WebRequest -Uri "$base/v1/models" -Headers $headers -TimeoutSec 20 -UseBasicParsing
    return @{ Code = [int]$r.StatusCode; Detail = 'models 可用' }
  } catch {
    $status = 0
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    if ($status -eq 0) { $status = -1 }
    return @{ Code = $status; Detail = $_.Exception.Message }
  }
}

# 跑 setup 子命令，輸出即時透印，回傳結束碼
function Invoke-Setup {
  param([string[]]$SetupArgs)
  & $python $setup @SetupArgs 2>&1 | ForEach-Object { Write-Info ([string]$_) }
  return $LASTEXITCODE
}

# ---------------------------------------------------------------- 前置
if (-not (Test-Path -LiteralPath $setup)) {
  Write-Bad "找不到 $setup"
  exit 4
}

$python = Resolve-Python
if (-not $python) {
  Write-Bad '找不到 python.exe：請安裝 Python 3.10+ 並勾選 Add to PATH，或用 UM_PYTHON 環境變數指定路徑'
  exit 4
}

$key = Get-ApiKey
if (-not $key) {
  Write-Warn "找不到 local-api-key（$keyFile），也沒有 UM_GENAI_API_KEY 環境變數"
}

Write-Host "UM GenAI 反代診斷 — $base" -ForegroundColor White
Write-Info "setup : $setup"
Write-Info "python: $python"
Write-Info "模式  : $(if ($Fix) { '診斷＋必要時自動修復' } else { '只診斷（加 -Fix 才會動手）' })"

# ---------------------------------------------------------------- 1. 代理
Write-Step '1/3 代理 /health'
$health = Get-ProxyHealth
if (-not $health) {
  Write-Bad "$base 沒有回應"
  Write-Info '代理可能閒置被關掉了（外掛的按需模式），或根本沒在跑。'
  Write-Info '在 DSH 用一次 um-genai 模型就會被喚醒；也可以手動啟動：'
  Write-Cmd "$python `"$setup`" serve --port $Port"
  exit 3
}
if ($health.service -ne 'um-opencode-proxy') {
  Write-Warn "$Port 有服務回應，但不是 UM 代理（service=$($health.service)）"
  Write-Info '外掛會拒絕在這個埠啟動，先查是誰佔用。'
  exit 4
}
Write-Ok "um-opencode-proxy 正在服務（status=$($health.status)）"

# ---------------------------------------------------------------- 2. 上游 token
Write-Step '2/3 上游 UM 登入狀態（setup status）'
$statusOut     = @(& $python $setup status --port $Port 2>&1 | ForEach-Object { [string]$_ })
$statusText    = $statusOut -join "`n"
$statusCode    = $LASTEXITCODE
$upstreamOk    = $statusCode -eq 0 -and $statusText -match '(?im)^Upstream \(UM\)\s*:\s*healthy\b'
$upstreamRejected = $statusCode -eq 0 -and $statusText -match '(?im)^Upstream \(UM\)\s*:\s*HTTP (401|403)\b'
$statusOut | ForEach-Object { Write-Info $_ }

if ($upstreamOk) {
  Write-Ok '上游 UM 接受目前的 token'
} elseif ($upstreamRejected) {
  Write-Bad '上游 UM 不接受目前的 token（過期或被撤銷）'
} else {
  Write-Warn '無法確認上游 token：可能是網路或 gateway 故障，不能據此判定需要重新登入。'
}

$tokenExp = Get-TokenExpiry
if ($tokenExp) {
  $left  = [math]::Round(($tokenExp - (Get-Date)).TotalMinutes, 1)
  $label = "token 到期：$($tokenExp.ToString('yyyy-MM-dd HH:mm:ss'))（剩 $left 分鐘）"
  if ($left -le 0) { Write-Bad $label } else { Write-Info $label }
}

# ---------------------------------------------------------------- 3. 端到端 API
Write-Step '3/3 API /v1/models（帶 local-api-key）'
$api = Test-Api -Key $key
switch ($api.Code) {
  200 {
    if ($upstreamOk) {
      Write-Ok 'models 200 且 gateway 接受 token — 反代可用。'
      exit 0
    }
    if (-not $upstreamRejected) {
      Write-Warn '本機 models 可用，但上游狀態未確認；稍後重試，不自動重新登入。'
      exit 2
    }
    Write-Bad '本機 models 可用，但 gateway 已拒絕 token，需要重新登入。'
  }
  401 {
    if ($upstreamOk) {
      Write-Bad '401（上游 token 健康）⇒ 問題在 local-api-key，不在 UM 登入。'
      Write-Info "確認 $keyFile 的內容與 DSH profile 的 UM_GENAI_API_KEY 一致。"
      Write-Info '重新登入不會修好這個情況。'
      exit 2
    }
    if (-not $upstreamRejected) {
      Write-Warn '401 但未確認 gateway 拒絕 token；先檢查 local-api-key 與網路，不自動重新登入。'
      exit 2
    }
    Write-Bad '401 ⇒ 上游 UM 登入 token 過期／被拒，需要重新登入。'
  }
  -1 {
    Write-Bad "連線失敗：$($api.Detail)"
    exit 2
  }
  default {
    Write-Bad "HTTP $($api.Code)：$($api.Detail)"
    exit 2
  }
}

# ---------------------------------------------------------------- 修復
if (-not $Fix) {
  Write-Host ''
  Write-Warn '重新登入會開瀏覽器視窗，Duo 可能要人工核准。執行：'
  Write-Cmd "powershell -NoProfile -ExecutionPolicy Bypass -File `"$self`" -Fix"
  exit 2
}

Write-Host ''
Write-Host '  開始自動登入（瀏覽器視窗會打開；若跳出 Duo，請在手機上核准）…' -ForegroundColor Yellow
$rc = Invoke-Setup -SetupArgs @('login', '--timeout', "$LoginTimeoutSec")
if ($rc -ne 0) {
  Write-Bad "login 結束碼 $rc"
  Write-Info "可能是沒人在 $LoginTimeoutSec 秒內完成驗證，跑久一點："
  Write-Cmd "$python `"$setup`" login --timeout 300"
  exit 2
}

Start-Sleep -Milliseconds 500
$probeScript = Join-Path $PSScriptRoot 'um-renew.py'
& $python -B $probeScript --probe
if ($LASTEXITCODE -ne 0) {
  Write-Bad '登入後 gateway 仍未確認 token 可用。'
  exit 2
}
$api2 = Test-Api -Key $key
if ($api2.Code -eq 200) {
  Write-Ok '修好了：models 200，反代恢復正常。'
  $exp2 = Get-TokenExpiry
  if ($exp2) {
    Write-Info "新 token 到期：$($exp2.ToString('yyyy-MM-dd HH:mm:ss'))"
    Write-Info '代理不用重啟，下一個請求就會用新 token。'
  }
  exit 0
}

Write-Bad "登入回報成功，但 /v1/models 仍回 HTTP $($api2.Code)"
Write-Info "看 $install\proxy.log 裡 'token renewed on demand.' 之後有沒有 401。"
exit 2
