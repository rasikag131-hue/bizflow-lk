param(
    [int]$Port = 8000,
    [switch]$Lan
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonLauncher -and -not $pythonCommand) {
    throw 'Python 3.10+ was not found. Install Python from python.org, enable the PATH option, then reopen PowerShell.'
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host 'Creating a project virtual environment…' -ForegroundColor Cyan
    if ($pythonLauncher) {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
}

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}

Write-Host 'Installing BizFlow LK dependencies (first run may take a few minutes)…' -ForegroundColor Cyan
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

Write-Host 'Setting up local-only secrets and the first administrator…' -ForegroundColor Cyan
& $venvPython scripts/setup_local.py
if ($LASTEXITCODE -ne 0) { throw 'Local environment setup failed.' }

$bindAddress = '127.0.0.1'
if ($Lan) { $bindAddress = '0.0.0.0' }
Write-Host "Starting BizFlow LK at http://127.0.0.1:$Port" -ForegroundColor Green
if ($Lan) {
    Write-Host 'LAN mode is enabled. Other devices can connect using this PC’s LAN IP if Windows Firewall allows it. Do not expose this development server directly to the public internet.' -ForegroundColor Yellow
}
Write-Host 'Press Ctrl+C to stop the server.' -ForegroundColor DarkGray
& $venvPython -m uvicorn app.main:app --host $bindAddress --port $Port
