<#
.SYNOPSIS
    Start Tee:dentity locally: the FastAPI backend and the Vite dev server, in one window.

.DESCRIPTION
    1. First run only: creates backend\.venv and installs Python packages, and runs npm install
       in frontend\ (skip with -SkipInstall).
    2. Starts the API (uvicorn) on 127.0.0.1:<ApiPort> and waits for /api/health.
    3. Starts the web app (Vite) on <WebPort>; it proxies /api to the API. If that port is busy,
       Vite picks the next free one and prints the URL.
    Press Ctrl+C to stop both servers.

    Windows blocks .ps1 files by default, so launch it like this from the teedentity folder:
        powershell -ExecutionPolicy Bypass -File .\run.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run.ps1 -ApiPort 8010 -WebPort 5180 -Open
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8000,     # FastAPI / uvicorn
    [int]$WebPort = 5173,     # Vite dev server (falls back to the next free port)
    [switch]$Reload,          # restart the API automatically when backend code changes
    [switch]$Open,            # open the browser once Vite is ready
    [switch]$SkipInstall      # never create the venv / run npm install
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-PortInUse([int]$Port) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return [bool]$listener
}

function Stop-Tree([System.Diagnostics.Process]$Process) {
    # uvicorn --reload runs the app in a child process; taskkill /T takes the whole tree down.
    if ($Process -and -not $Process.HasExited) {
        & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null
    }
}

# --------------------------------------------------------------------------- first-time setup

if (-not (Test-Path $venvPython)) {
    if ($SkipInstall) { throw "backend\.venv is missing. Run once without -SkipInstall." }
    Write-Step 'Creating the Python virtual environment (backend\.venv)'
    $venvDir = Join-Path $backend '.venv'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -m venv $venvDir
    } else {
        & python -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw 'Could not create the virtual environment. Install Python 3.13 and try again.'
    }
    Write-Step 'Installing backend packages'
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $backend 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed (see the output above).' }
}

if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    if ($SkipInstall) { throw "frontend\node_modules is missing. Run once without -SkipInstall." }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw 'npm was not found. Install Node.js 24 and try again.'
    }
    Write-Step 'Installing frontend packages (npm install)'
    Push-Location $frontend
    try {
        & npm.cmd install
        if ($LASTEXITCODE -ne 0) { throw 'npm install failed (see the output above).' }
    } finally {
        Pop-Location
    }
}

# --------------------------------------------------------------------------- API

if (Test-PortInUse $ApiPort) {
    throw "Port $ApiPort is already in use. Stop that process or pass -ApiPort <free port>."
}

$apiArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort")
if ($Reload) { $apiArgs += '--reload' }

Write-Step "Starting the API on http://127.0.0.1:$ApiPort"
$api = Start-Process -FilePath $venvPython -ArgumentList $apiArgs -WorkingDirectory $backend -NoNewWindow -PassThru

try {
    $health = $null
    $deadline = (Get-Date).AddSeconds(60)
    while (-not $health -and (Get-Date) -lt $deadline) {
        if ($api.HasExited) { throw "The API exited during startup (exit code $($api.ExitCode)). See the errors above." }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/api/health" -UseBasicParsing -TimeoutSec 2
            $health = $response.Content | ConvertFrom-Json
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $health) { throw 'The API did not answer /api/health within 60 seconds.' }
    if ($health.portkey_key_set) {
        Write-Step 'API is up (Portkey key found).'
    } else {
        Write-Warning 'API is up, but PORTKEY_API_KEY was not found. AI features will fail; see README "Prerequisites".'
    }

    # ----------------------------------------------------------------------- web app
    $env:TEE_API_URL = "http://127.0.0.1:$ApiPort"   # read by frontend\vite.config.ts (proxy target)
    $viteArgs = @('run', 'dev', '--', '--port', "$WebPort")
    if ($Open) { $viteArgs += '--open' }

    Write-Step "Starting the web app (Vite). Open the Local URL it prints. Ctrl+C stops both servers."
    Push-Location $frontend
    try {
        & npm.cmd @viteArgs
    } finally {
        Pop-Location
    }
} finally {
    Write-Step 'Stopping the API'
    Stop-Tree $api
}
