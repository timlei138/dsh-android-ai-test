# Launch the case WebUI.
#
# Usage:
#   .\start-webui.cmd                  start and open browser
#   .\start-webui.cmd -Port 8771       use another port
#   .\start-webui.cmd -NoBrowser       server only
#
# NOTE: this file is intentionally pure ASCII.
# Windows PowerShell reads a .ps1 without a BOM as GBK, which corrupts any
# Chinese characters and turns them into syntax errors. Rather than depend on
# every editor preserving a BOM, just keep the file ASCII-only.

param(
    [int]$Port = 8770,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "Virtualenv not found: $py" -ForegroundColor Red
    Write-Host "Create it first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    Read-Host "Press Enter to exit"
    exit 1
}

# Keep the skill root consistent with the code.
#
# ANDROID_SKILL_ROOT points at the `assets` directory, NOT the repo root --
# that is the convention every other entry point uses, and store.db_path()
# appends "storage/runs.db" to it. Setting it to the repo root silently
# writes the database to the wrong place and the UI shows zero runs.
# (That exact bug happened once while building this script.)
if (-not $env:ANDROID_SKILL_ROOT) {
    $env:ANDROID_SKILL_ROOT = Join-Path $root 'assets'
}

$pyArgs = @('-u', (Join-Path $root 'webui.py'), '--port', "$Port")
if ($NoBrowser) { $pyArgs += '--no-browser' }

Write-Host ""
Write-Host "  Case WebUI  ->  http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host "  (Ctrl+C to stop)" -ForegroundColor DarkGray

# -u: unbuffered. Without it the startup lines can sit in the pipe buffer
# and the user sees nothing, then starts the server again.
& $py @pyArgs
