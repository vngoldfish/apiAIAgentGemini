# Ultimate 24/7 entrypoint: supervisor + scheduled keep-alive
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\start_host_24_7.ps1

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

Write-Host "==> Gemini-API 24/7 HARDENED HOST MODE" -ForegroundColor Cyan
Write-Host "    - Python supervisor (restart on crash immediately)" -ForegroundColor Green
Write-Host "    - Task Scheduler keep-alive every 1 minute" -ForegroundColor Green
Write-Host "    - No Docker (Windows cookie NAT issues)" -ForegroundColor Green
Write-Host "    Dashboard: http://127.0.0.1:8000" -ForegroundColor Yellow
Write-Host "    Chat:      http://127.0.0.1:8000/chat-test" -ForegroundColor Yellow
Write-Host ""

try { docker stop gemini-api 2>$null | Out-Null } catch {}
python -m pip install -q -r requirements.txt imageio-ffmpeg 2>$null

# Install / refresh scheduled tasks + start now
powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\install_keepalive_task.ps1"

