# Safe keep-alive for Gemini API (Task Scheduler every 1 minute)
# - Does NOT kill a live server just because health is slow (busy request)
# - Only restarts if process missing OR health fails repeatedly
# - Ensures supervisor.py is running (primary durability layer)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root
$logDir = Join-Path $Root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "keepalive.log"
$failStamp = Join-Path $logDir "health_fail_count.txt"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    try { Add-Content -Path $logFile -Value $line -Encoding UTF8 } catch {}
}

function Test-Healthy {
    try {
        # Longer timeout — do not treat busy event-loop as dead too quickly
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 12
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-ProcIds([string]$pattern) {
    $ids = @()
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern) } |
        ForEach-Object { $ids += $_.ProcessId }
    return $ids
}

function Ensure-Supervisor {
    $sup = @(Get-ProcIds 'supervisor\.py')
    if ($sup.Count -gt 0) {
        return $false
    }
    Write-Log "Supervisor missing — starting supervisor.py --background"
    try {
        # Use pythonw if available (no console window)
        $py = "python"
        $pythonw = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"
        if (Test-Path $pythonw) { $py = $pythonw }
        Start-Process -FilePath $py -ArgumentList "supervisor.py","--background" -WorkingDirectory $Root -WindowStyle Hidden
        return $true
    } catch {
        Write-Log ("Failed to start supervisor: {0}" -f $_.Exception.Message)
        # Fallback: start api_server directly
        Start-Process -FilePath "python" -ArgumentList "api_server.py" -WorkingDirectory $Root -WindowStyle Hidden
        return $true
    }
}

# 1) Always ensure supervisor is alive (primary)
$startedSup = Ensure-Supervisor

# 2) Health check with hysteresis (avoid killing busy server)
$healthy = Test-Healthy
if ($healthy) {
    if (Test-Path $failStamp) { Remove-Item $failStamp -Force -ErrorAction SilentlyContinue }
    exit 0
}

# Health failed — count consecutive failures
$fails = 1
if (Test-Path $failStamp) {
    try { $fails = [int](Get-Content $failStamp -Raw) + 1 } catch { $fails = 1 }
}
Set-Content -Path $failStamp -Value "$fails" -Encoding ascii
Write-Log ("Health FAIL count={0}" -f $fails)

# Give supervisor time to restart child (2-3 cycles)
if ($fails -lt 2) {
    Write-Log "Waiting for supervisor recovery (need 2 consecutive fails before hard restart)"
    # Nudge: if no api_server process at all, supervisor should spawn soon
    $api = @(Get-ProcIds 'api_server\.py')
    if ($api.Count -eq 0) {
        Ensure-Supervisor | Out-Null
    }
    exit 0
}

Write-Log "Hard recovery: stop docker, restart supervisor stack"
try { docker stop gemini-api 2>$null | Out-Null } catch {}

# Kill supervisors and api servers (use procId not $PID automatic variable!)
$toKill = @()
$toKill += Get-ProcIds 'supervisor\.py'
$toKill += Get-ProcIds 'api_server\.py'
foreach ($procId in $toKill) {
    if ($procId -and $procId -gt 0) {
        Write-Log ("Stopping PID {0}" -f $procId)
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 2

@("api_keys.json", "gemini_accounts.json", "gemini_agents.json", "dashboard_config.json") | ForEach-Object {
    $p = Join-Path $Root $_
    if (-not (Test-Path $p)) { "{}" | Set-Content $p -Encoding utf8 }
}
if (-not (Test-Path (Join-Path $Root "static"))) {
    New-Item -ItemType Directory -Path (Join-Path $Root "static") | Out-Null
}

$env:PORT = "8000"
$env:ACCESS_LOG = "0"
Ensure-Supervisor | Out-Null

for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Healthy) {
        Write-Log ("Hard recovery OK after {0}s" -f ($i * 2 + 2))
        if (Test-Path $failStamp) { Remove-Item $failStamp -Force -ErrorAction SilentlyContinue }
        exit 0
    }
}
Write-Log "Hard recovery still unhealthy — next minute will retry"
exit 1
