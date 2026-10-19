# Start the gateway in the current window. Ctrl+C stops it.
#
#   .\scripts\start_gateway.ps1
#
# Run scripts\check_health.py or scripts\smoke_ai.py from a second terminal.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "No virtualenv at $python" -ForegroundColor Red
    Write-Host "Create it, then install requirements:" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

# The gateway reads config/gateway.yaml and .env relative to its own location,
# and imports the gateway package from src\, so it has to run from src\.
Push-Location (Join-Path $root 'src')
try {
    Write-Host 'Starting water-gateway. Press Ctrl+C to stop.' -ForegroundColor Cyan
    Write-Host 'Health:  http://127.0.0.1:9090/health'
    Write-Host 'AI:      http://127.0.0.1:9091/ai/status'
    Write-Host ''
    & $python -m gateway.main
}
finally {
    Pop-Location
}
