# MunshiG setup for Windows 10/11.
#
# Installs what MunshiG needs (Python 3.12, Node.js LTS, Ollama), sets up
# MunshiG in this folder, downloads the local AI models, adds a `munshig`
# command, and runs the offline demo.
#
# Run from PowerShell (no admin needed for most steps; winget may ask):
#   powershell -ExecutionPolicy Bypass -File .\munshig\scripts\setup-windows.ps1
# Options:
#   -SkipModels   don't download the AI models now (about 9 GB)
#   -SkipDemo     don't run the demo at the end

param(
    [switch]$SkipModels,
    [switch]$SkipDemo
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot          # the munshig folder
$ChatModel = "qwen3:4b"
$VisionModel = "qwen2.5vl:7b"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "    OK  $text" -ForegroundColor Green }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Winget-Install($id, $label) {
    if (-not (Have "winget")) {
        throw "winget isn't available. Install 'App Installer' from the Microsoft Store, then run this again."
    }
    Write-Host "    Installing $label (Windows may ask for permission)..."
    winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements
    # -1978335189 = already installed / no newer version
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        throw "Installing $label failed (winget exit code $LASTEXITCODE)."
    }
    Refresh-Path
}

function Quietly([scriptblock]$block) {
    # Windows PowerShell 5.1 turns a program's error output into a fatal error when
    # $ErrorActionPreference is Stop; run probes with it relaxed.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { return (& $block) } catch { return $null } finally { $ErrorActionPreference = $saved }
}

function Find-Python {
    # Returns the full path of a Python 3.11+ python.exe, or $null.
    # Prefer the py launcher; the plain "python" command can be the Microsoft Store stub.
    if (Have "py") {
        foreach ($v in @("3.13", "3.12", "3.11")) {
            $exe = Quietly { & py "-$v" -c "import sys; print(sys.executable)" 2>$null }
            if ($exe -and (Test-Path "$exe")) { return "$exe".Trim() }
        }
    }
    foreach ($v in @("313", "312", "311")) {
        $exe = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
        if (Test-Path $exe) { return $exe }
    }
    return $null
}

Write-Host "MunshiG setup" -ForegroundColor Cyan
Write-Host "Folder: $Root"
if (-not (Test-Path (Join-Path $Root "pyproject.toml"))) {
    throw "Run this script from inside the MunshiG folder (munshig\scripts\setup-windows.ps1)."
}

# 1. Python ----------------------------------------------------------------
Step "Python 3.11 or newer"
$python = Find-Python
if (-not $python) {
    Winget-Install "Python.Python.3.12" "Python 3.12"
    $python = Find-Python
    if (-not $python) { throw "Python was installed but isn't visible yet. Close this window, open a new PowerShell, and run the script again." }
}
Ok ("Python " + (& $python -c "import platform; print(platform.python_version())") + " at $python")

# 2. Node.js ---------------------------------------------------------------
Step "Node.js (for the WhatsApp link)"
$nodeOk = $false
if (Have "node") {
    $major = [int]((& node --version).TrimStart("v").Split(".")[0])
    $nodeOk = $major -ge 20
}
if (-not $nodeOk) {
    Winget-Install "OpenJS.NodeJS.LTS" "Node.js LTS"
    if (-not (Have "node")) { throw "Node.js was installed but isn't visible yet. Open a new PowerShell and run the script again." }
}
Ok ("Node.js " + (& node --version))

# 3. Ollama ----------------------------------------------------------------
Step "Ollama (runs the AI on this PC)"
if (-not (Have "ollama")) {
    Winget-Install "Ollama.Ollama" "Ollama"
    if (-not (Have "ollama")) {
        $guess = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
        if (Test-Path (Join-Path $guess "ollama.exe")) { $env:Path += ";$guess" }
    }
}
function Ollama-Up {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}
if (Have "ollama") {
    if (-not (Ollama-Up)) {
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 6
    }
    if (Ollama-Up) { Ok "Ollama is running" } else { Write-Host "    Ollama is installed but not running yet; open the Ollama app from the Start menu." -ForegroundColor Yellow }
}
else { Write-Host "    Ollama isn't visible yet; open a new PowerShell later and run: ollama pull $ChatModel" -ForegroundColor Yellow }

# 4. MunshiG itself ----------------------------------------------------------
Step "MunshiG (Python part)"
Push-Location $Root
try {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Creating the Python environment failed." }
    }
    & .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install --quiet -e .
    if ($LASTEXITCODE -ne 0) { throw "Installing MunshiG's Python packages failed." }
    Ok "installed into $Root\.venv"

    Step "MunshiG (WhatsApp bridge)"
    Push-Location (Join-Path $Root "bridge")
    try {
        # npm.cmd, not npm: the npm.ps1 shim can be blocked by PowerShell's script policy.
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "Setting up the WhatsApp bridge (npm ci) failed." }
    } finally { Pop-Location }
    Ok "bridge ready"
} finally { Pop-Location }

# 5. A `munshig` command for any new terminal --------------------------------
Step "Adding the 'munshig' command"
$bin = Join-Path $env:USERPROFILE "MunshiG-bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$exe = Join-Path $Root ".venv\Scripts\munshig.exe"
Set-Content -Path (Join-Path $bin "munshig.cmd") -Value "@echo off`r`n`"$exe`" %*" -Encoding ASCII
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (-not ($userPath -split ";" | Where-Object { $_ -eq $bin })) {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $bin), "User")
}
Refresh-Path
Ok "type 'munshig' in any new PowerShell window"

# 6. AI models ---------------------------------------------------------------
if ($SkipModels) {
    Write-Host "`nSkipped the AI models. Later run: ollama pull $ChatModel ; ollama pull $VisionModel" -ForegroundColor Yellow
} elseif (Have "ollama") {
    Step "Downloading AI models (about 9 GB; this is the slow part)"
    & ollama pull $ChatModel
    $chatOk = $LASTEXITCODE -eq 0
    & ollama pull $VisionModel
    if ($chatOk -and $LASTEXITCODE -eq 0) { Ok "models ready" }
    else { Write-Host "    A model download failed. Later run: ollama pull $ChatModel ; ollama pull $VisionModel" -ForegroundColor Yellow }
}

# 7. Demo --------------------------------------------------------------------
if (-not $SkipDemo) {
    Step "Running the offline demo (dummy data, pretend Tally)"
    & $exe demo
}

Write-Host "`nAll set. Next, connect your Tally:" -ForegroundColor Green
Write-Host "  1. Open TallyPrime and create a TEST company named exactly: Demo Traders"
Write-Host "  2. Press F1 (Help) > Settings > Connectivity > Client/Server configuration."
Write-Host "     Set 'TallyPrime acts as' to Both and the port to 9000. Restart Tally and open the company."
Write-Host "  3. In a new PowerShell window run:  munshig demo --tally 127.0.0.1:9000"
Write-Host "     and send the output to Claude."
