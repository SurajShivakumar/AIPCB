# One-shot setup for AI PCB Copilot on Windows.
# Needs: KiCad 9 or 10, Node 18+, Python 3.11+, git. LTspice is optional (ngspice ships with KiCad).
#   powershell -ExecutionPolicy Bypass -File setup.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$engineTag = "v2.8.2"
$engine = Join-Path $root "vendor\KiCAD-MCP-Server"

function Find-KiCadPython {
    $roots = @("C:\Program Files\KiCad", "$env:LOCALAPPDATA\Programs\KiCad")
    $found = foreach ($r in $roots) {
        if (Test-Path $r) { Get-ChildItem $r -Directory | ForEach-Object { Join-Path $_.FullName "bin\python.exe" } | Where-Object { Test-Path $_ } }
    }
    $found | Sort-Object -Descending | Select-Object -First 1
}

Write-Host "== KiCad MCP engine ($engineTag)"
if (-not (Test-Path $engine)) {
    git clone --quiet --branch $engineTag https://github.com/mixelpixx/KiCAD-MCP-Server $engine
}
$kicadPy = Find-KiCadPython
if (-not $kicadPy) { throw "KiCad not found. Install KiCad 9+ from kicad.org." }
Write-Host "   KiCad Python: $kicadPy"
if (-not (Test-Path "$engine\venv\Scripts\python.exe")) {
    # system site-packages so the venv can still import KiCad's pcbnew module
    & $kicadPy -m venv --system-site-packages "$engine\venv"
}
& "$engine\venv\Scripts\python.exe" -m pip install --quiet -r "$engine\requirements.txt"
# board scripts (tools/): numpy + numba compile the maze router's search loop (~50x faster than pure Python)
& "$engine\venv\Scripts\python.exe" -m pip install --quiet numpy numba
& "$engine\venv\Scripts\python.exe" -c "import pcbnew; print('   pcbnew', pcbnew.Version())"
Push-Location $engine
npm install --no-audit --no-fund --silent
npm run build --silent
Pop-Location

Write-Host "== Copilot server"
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) { python -m venv "$root\.venv" }
& "$root\.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$root\.venv\Scripts\python.exe" -m pip install --quiet -r "$root\requirements.txt"

Write-Host "== Self-test"
& "$root\.venv\Scripts\python.exe" -m pytest -q
& "$root\.venv\Scripts\python.exe" -c "from copilot import spice; print('   simulators:', spice.available())"
Write-Host "`nDone. Open this folder in Claude Code (claude) and approve the 'kicad' and 'copilot' MCP servers."
