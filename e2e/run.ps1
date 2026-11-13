# Browser end-to-end checks against the production build, served by FastAPI. Windows twin of e2e/run.sh.
#   .\e2e\run.cmd                          -> uses the Chrome (or Edge) installed on this machine
#   set CHROME_PATH=C:\path\to\chrome.exe  -> another browser
#   set SHOTS=docs\screenshots             -> keep the screenshots there
# Starts three throw-away servers on ports 8743-8745: live data, offline with a seeded cache, offline with no data.
# The servers run without Alpaca keys, so the GLD tab is checked in its "not configured" state.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath (Join-Path $root 'backend\.venv\Scripts\uvicorn.exe'))) {
    Write-Host 'Run .\start.cmd once first (it creates backend\.venv).'; exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'frontend\dist\index.html'))) {
    Push-Location -LiteralPath (Join-Path $root 'frontend')
    try { & npm.cmd run build --silent; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' } } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'e2e\node_modules'))) {
    Push-Location -LiteralPath (Join-Path $root 'e2e')
    try { & npm.cmd ci --no-audit --no-fund --loglevel=error; if ($LASTEXITCODE -ne 0) { throw 'npm ci failed in e2e.' } } finally { Pop-Location }
}

if (-not $env:CHROME_PATH) {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )
    foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { $env:CHROME_PATH = $c; break } }
}
if (-not $env:CHROME_PATH) { Write-Host 'Chrome or Edge not found. Set CHROME_PATH.'; exit 1 }

function Test-PortInUse([int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', $p); return $true } catch { return $false } finally { $client.Close() }
}
foreach ($p in 8743, 8744, 8745) { if (Test-PortInUse $p) { Write-Host "Port $p is in use"; exit 1 } }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('gold-e2e-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path (Join-Path $tmp 'live'), (Join-Path $tmp 'empty') | Out-Null
$servers = @()

function Start-Server([int]$port, [hashtable]$vars) {
    # A child process inherits the environment at the moment it starts: set, start, restore.
    $vars['ALPACA_API_KEY_ID'] = $null
    $vars['ALPACA_API_SECRET_KEY'] = $null
    $saved = @{}
    foreach ($k in @($vars.Keys)) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k)
        [Environment]::SetEnvironmentVariable($k, $vars[$k])
    }
    try {
        return Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$port") `
            -WorkingDirectory (Join-Path $root 'backend') -NoNewWindow -PassThru `
            -RedirectStandardOutput (Join-Path $tmp "server-$port.out.log") -RedirectStandardError (Join-Path $tmp "server-$port.err.log")
    } finally {
        foreach ($k in @($saved.Keys)) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }
}

function Wait-Server([int]$port) {
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2 | Out-Null; return } catch { Start-Sleep -Milliseconds 500 }
    }
    throw "Server on port $port did not start. See $tmp\server-$port.err.log"
}

$code = 1
try {
    $servers += Start-Server 8743 @{ GOLD_DATA_DIR = (Join-Path $tmp 'live') }
    Wait-Server 8743
    try { Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8743/api/prices' -TimeoutSec 60 | Out-Null }
    catch { throw 'Live data fetch failed (needs internet access).' }
    Copy-Item -Recurse -LiteralPath (Join-Path $tmp 'live') -Destination (Join-Path $tmp 'cached')
    $servers += Start-Server 8744 @{ GOLD_OFFLINE = '1'; GOLD_DATA_DIR = (Join-Path $tmp 'cached') }
    Wait-Server 8744
    $servers += Start-Server 8745 @{ GOLD_OFFLINE = '1'; GOLD_DATA_DIR = (Join-Path $tmp 'empty') }
    Wait-Server 8745

    $env:LIVE = 'http://127.0.0.1:8743'
    $env:CACHED = 'http://127.0.0.1:8744'
    $env:EMPTY = 'http://127.0.0.1:8745'
    if ($env:SHOTS) {
        New-Item -ItemType Directory -Force -Path $env:SHOTS | Out-Null
        $env:OUT = (Resolve-Path -LiteralPath $env:SHOTS).Path
    } else {
        $env:OUT = Join-Path $tmp 'shots'
    }
    Push-Location -LiteralPath (Join-Path $root 'e2e')
    try { & node run.mjs; $code = $LASTEXITCODE } finally { Pop-Location }
} finally {
    # Stop each server together with its child interpreter, then remove the temporary data.
    foreach ($s in $servers) { if ($s -and -not $s.HasExited) { & taskkill.exe /PID $s.Id /T /F 2>&1 | Out-Null } }
    Start-Sleep -Milliseconds 300
    Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
}
exit $code
