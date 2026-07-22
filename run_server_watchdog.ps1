# Gemini-API HOST watchdog - keeps api_server.py alive 24/7
# Auto-restarts on crash/exit. Logs to .\logs\
#
# Start (visible):
#   powershell -ExecutionPolicy Bypass -File .\run_server_watchdog.ps1
# Start (background):
#   powershell -ExecutionPolicy Bypass -File .\run_server_watchdog.ps1 -Background

param(
    [switch]$Background,
    [int]$Port = 8000,
    [int]$RestartDelaySec = 3
)

$Root = $PSScriptRoot
Set-Location $Root

if ($Background) {
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    Start-Process -FilePath "powershell.exe" -ArgumentList $arg -WorkingDirectory $Root -WindowStyle Minimized
    Write-Host "Watchdog started in background. Logs: $Root\logs\"
    exit 0
}

$logDir = Join-Path $Root "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir ("server_" + (Get-Date -Format "yyyyMMdd") + ".log")

function Write-Log {
    param([string]$msg)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

try {
    docker stop gemini-api 2>$null | Out-Null
} catch {}

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match 'api_server\.py') } |
    ForEach-Object {
        Write-Log ("Stopping old api_server PID {0}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 1

try {
    python -m pip install -q -r requirements.txt imageio-ffmpeg 2>$null
} catch {}

@("api_keys.json", "gemini_accounts.json", "gemini_agents.json", "dashboard_config.json") | ForEach-Object {
    $p = Join-Path $Root $_
    if (-not (Test-Path $p)) {
        "{}" | Set-Content $p -Encoding utf8
    }
}
$staticDir = Join-Path $Root "static"
if (-not (Test-Path $staticDir)) {
    New-Item -ItemType Directory -Path $staticDir | Out-Null
}

Write-Log ("Watchdog started. Port={0} log={1}" -f $Port, $logFile)
Write-Log ("Dashboard http://127.0.0.1:{0}  Chat http://127.0.0.1:{0}/chat-test" -f $Port)
Write-Log "Keep Chrome + Extension running for cookies."

$env:PORT = "$Port"
$failCount = 0

while ($true) {
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listeners) {
        foreach ($l in $listeners) {
            $opid = $l.OwningProcess
            if ($opid -and $opid -gt 0) {
                $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$opid" -ErrorAction SilentlyContinue).CommandLine
                if ($cmd -and ($cmd -match 'api_server|uvicorn|python')) {
                    Write-Log ("Port {0} busy by PID {1} - stopping" -f $Port, $opid)
                    Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue
                }
            }
        }
        Start-Sleep -Seconds 1
    }

    Write-Log "Starting api_server.py ..."
    $stdout = Join-Path $logDir "api_stdout.log"
    $stderr = Join-Path $logDir "api_stderr.log"

    $proc = Start-Process -FilePath "python" `
        -ArgumentList "api_server.py" `
        -WorkingDirectory $Root `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr

    Write-Log ("api_server PID={0}" -f $proc.Id)

    Wait-Process -Id $proc.Id -ErrorAction SilentlyContinue
    $code = $proc.ExitCode
    $failCount = $failCount + 1
    Write-Log ("api_server exited code={0} restart={1} delay={2}s" -f $code, $failCount, $RestartDelaySec)

    if (Test-Path $stderr) {
        $tail = Get-Content $stderr -Tail 30 -ErrorAction SilentlyContinue
        if ($tail) {
            Write-Log "--- last stderr ---"
            foreach ($line in $tail) {
                Write-Log $line
            }
            Write-Log "--- end stderr ---"
        }
    }

    Start-Sleep -Seconds $RestartDelaySec
}
