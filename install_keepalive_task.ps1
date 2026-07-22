# Install durable Windows tasks for Gemini API 24/7
# 1) Every 1 minute: keep_alive.ps1 (ensures supervisor + health)
# 2) At user logon: start supervisor immediately
#
#   powershell -ExecutionPolicy Bypass -File .\install_keepalive_task.ps1

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$keep = Join-Path $Root "keep_alive.ps1"
$sup = Join-Path $Root "supervisor.py"

if (-not (Test-Path $keep)) { throw "Missing keep_alive.ps1" }
if (-not (Test-Path $sup)) { throw "Missing supervisor.py" }

# Clean old tasks
foreach ($n in @("GeminiAPI-KeepAlive", "GeminiAPI-Host-Watchdog", "GeminiAPI-Supervisor")) {
    schtasks /Delete /TN $n /F 2>$null | Out-Null
}

$ps = "powershell.exe"
$keepArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$keep`""
# Every 1 minute
schtasks /Create /TN "GeminiAPI-KeepAlive" /TR "`"$ps`" $keepArgs" /SC MINUTE /MO 1 /F /RL LIMITED | Out-Null
Write-Host "[OK] GeminiAPI-KeepAlive (every 1 min)" -ForegroundColor Green

# At logon — start supervisor in background
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "python" }
$supTr = "`"$py`" `"$sup`" --background"
schtasks /Create /TN "GeminiAPI-Supervisor" /TR $supTr /SC ONLOGON /F /RL LIMITED 2>$null | Out-Null
# ONLOGON may need user; fallback ONSTART not available without admin
Write-Host "[OK] GeminiAPI-Supervisor (at logon, if permitted)" -ForegroundColor Green

# Boot now
Write-Host "Starting stack now..." -ForegroundColor Cyan
& $ps -NoProfile -ExecutionPolicy Bypass -File $keep
Start-Sleep -Seconds 8
# force supervisor if still needed
& $py "$sup" --background
Start-Sleep -Seconds 10

try {
    $h = Invoke-WebRequest "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 10
    Write-Host "HEALTH OK" -ForegroundColor Green
    Write-Host $h.Content
} catch {
    Write-Host "Health pending — wait 30s and refresh" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Dashboard: http://127.0.0.1:8000"
Write-Host "Chat test: http://127.0.0.1:8000/chat-test"
Write-Host "Logs:      $Root\logs\supervisor.log  |  keepalive.log"
Write-Host "Tasks:     schtasks /Query /TN GeminiAPI-KeepAlive"
