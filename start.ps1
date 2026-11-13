# One command to install (first run only), build and launch the whole app on Windows.
#   .\start.cmd                  -> http://localhost:8742   (start.cmd runs this script)
#   .\start.cmd -Port 9000       -> choose another port (or set PORT)
#   .\start.cmd -NoOpen          -> do not open the browser
#   set GOLD_OFFLINE=1           -> no external calls: cached data or a CSV upload only
# macOS and Linux use ./start.sh instead.
param(
    [int]$Port = 0,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Port -eq 0) { if ($env:PORT) { $Port = [int]$env:PORT } else { $Port = 8742 } }

function Find-Python {
    # Python 3.12 first (the version this project is developed on), then any 3.11+.
    $candidates = @(@('py', '-3.12'), @('py', '-3.13'), @('py', '-3.11'), @('python'), @('python3'))
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -and ([version]"$v" -ge [version]'3.11')) { return , $c }
        } catch { }
    }
    return $null
}

function Test-PortInUse([int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', $p); return $true } catch { return $false } finally { $client.Close() }
}

function Invoke-Checked([string]$what, [scriptblock]$block) {
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { Write-Host 'Node.js 20+ and npm are required.'; exit 1 }
if (Test-PortInUse $Port) { Write-Host "Port $Port is already in use. Run: .\start.cmd -Port <free port>"; exit 1 }

$venvPython = Join-Path $PSScriptRoot 'backend\.venv\Scripts\python.exe'
$uvicorn = Join-Path $PSScriptRoot 'backend\.venv\Scripts\uvicorn.exe'
if (-not (Test-Path -LiteralPath $uvicorn)) {
    $py = Find-Python
    if (-not $py) { Write-Host 'Python 3.11 or newer is required (python.org or "winget install Python.Python.3.12").'; exit 1 }
    Write-Host '==> Installing backend dependencies'
    $pyExe = $py[0]
    $pyArgs = @($py | Select-Object -Skip 1)
    Invoke-Checked 'Creating the virtual environment' { & $pyExe @pyArgs -m venv (Join-Path $PSScriptRoot 'backend\.venv') }
    Invoke-Checked 'Installing backend dependencies' { & $venvPython -m pip install -q --disable-pip-version-check -r (Join-Path $PSScriptRoot 'backend\requirements.txt') }
}

$frontend = Join-Path $PSScriptRoot 'frontend'
if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules'))) {
    Write-Host '==> Installing frontend dependencies'
    Push-Location -LiteralPath $frontend
    try { Invoke-Checked 'Installing frontend dependencies' { & npm.cmd ci --no-audit --no-fund --loglevel=error } } finally { Pop-Location }
}

$distIndex = Join-Path $frontend 'dist\index.html'
$needBuild = -not (Test-Path -LiteralPath $distIndex)
if (-not $needBuild) {
    $built = (Get-Item -LiteralPath $distIndex).LastWriteTimeUtc
    $sources = @(Get-ChildItem -LiteralPath (Join-Path $frontend 'src') -Recurse -File)
    foreach ($extra in @('index.html', 'package.json', 'vite.config.ts')) { $sources += Get-Item -LiteralPath (Join-Path $frontend $extra) }
    if (Test-Path -LiteralPath (Join-Path $frontend 'public')) { $sources += @(Get-ChildItem -LiteralPath (Join-Path $frontend 'public') -Recurse -File) }
    $needBuild = [bool]($sources | Where-Object { $_.LastWriteTimeUtc -gt $built } | Select-Object -First 1)
}
if ($needBuild) {
    Write-Host '==> Building frontend'
    Push-Location -LiteralPath $frontend
    try { Invoke-Checked 'Building the frontend' { & npm.cmd run build --silent } } finally { Pop-Location }
}

$url = "http://localhost:$Port"
Write-Host "==> Starting AI Gold Forecast. Open $url  (Ctrl+C to stop; keep this window open while using the app)"
$server = Start-Process -FilePath $uvicorn -ArgumentList @('app.main:app', '--host', '127.0.0.1', '--port', "$Port") `
    -WorkingDirectory (Join-Path $PSScriptRoot 'backend') -NoNewWindow -PassThru
$null = $server.Handle  # keeps the exit code readable after the process ends
try {
    # Open the browser once the server answers.
    for ($i = 0; $i -lt 120 -and -not $server.HasExited; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 | Out-Null
            if (-not $NoOpen) { Start-Process $url }
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    $server.WaitForExit()
} finally {
    # Stop the server together with its child interpreter if this script ends first.
    if (-not $server.HasExited) { & taskkill.exe /PID $server.Id /T /F 2>&1 | Out-Null }
}
exit $server.ExitCode
