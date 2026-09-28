# Start Account Radar locally. Offline and read-only:
# no live Claude, no HubSpot, no installs, no Git, no secrets.
#   powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)  # the ledger path is relative to the repo root
$env:PYTHONPATH = "src"

python -c "import revenue_agent.console"
if ($LASTEXITCODE -ne 0) { Write-Error "Python cannot import revenue_agent (is this the repo root?)"; exit 1 }

if (-not (Test-Path "runs\unattended.sqlite3")) {
    Write-Warning "No ledger yet. Run: python scripts\demo.py healthy   (offline, then restart this script)"
}

Write-Host ""
Write-Host "Account Radar starting. Open:"
Write-Host "  http://127.0.0.1:8765/?as=sofia   Sofia's morning brief (start here)"
Write-Host "  http://127.0.0.1:8765/            team morning brief"
Write-Host "  http://127.0.0.1:8765/control     Control Room"
Write-Host "  http://127.0.0.1:8765/sources     Data Sources"
Write-Host "Ctrl+C to stop."
Write-Host ""
python -m revenue_agent.console
