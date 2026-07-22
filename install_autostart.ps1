# Install Windows Scheduled Task: start Gemini API watchdog at logon
# Run PowerShell AS ADMINISTRATOR once:
#   powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$taskName = "GeminiAPI-Host-Watchdog"
$watchdog = Join-Path $Root "run_server_watchdog.ps1"

if (-not (Test-Path $watchdog)) {
    throw "Missing $watchdog"
}

# Remove old task if exists
schtasks /Delete /TN $taskName /F 2>$null | Out-Null

$ps = "powershell.exe"
$args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$watchdog`""

# At logon of current user, restart if needed
$action = New-ScheduledTaskAction -Execute $ps -Argument $args -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Installed scheduled task: $taskName" -ForegroundColor Green
Write-Host "It starts at logon and keeps the API on port 8000." -ForegroundColor Green
Write-Host "Start now:  schtasks /Run /TN $taskName" -ForegroundColor Yellow
Write-Host "Remove:     schtasks /Delete /TN $taskName /F" -ForegroundColor Yellow

# Start immediately
schtasks /Run /TN $taskName | Out-Null
Start-Sleep -Seconds 8
try {
    $h = Invoke-WebRequest "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 5
    Write-Host "Health OK: $($h.Content)" -ForegroundColor Green
} catch {
    Write-Host "Health not ready yet — check logs\ folder." -ForegroundColor Yellow
}
