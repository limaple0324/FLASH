$ErrorActionPreference = "Stop"

Write-Host "FLASH SP1 Windows Setup" -ForegroundColor Cyan
Write-Host "Project: $PSScriptRoot\.."

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE: $FilePath $($Arguments -join ' ')"
    }
}

function Test-RealPython {
    param([string]$PythonExe)

    if (-not (Test-Path $PythonExe)) {
        return $false
    }

    $output = & $PythonExe -c "print('FLASH OK')" 2>$null
    return (($LASTEXITCODE -eq 0) -and ($output -eq "FLASH OK"))
}

$pythonCandidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python311\python.exe"
)

$pythonExe = $null
foreach ($candidate in $pythonCandidates) {
    if (Test-RealPython $candidate) {
        $pythonExe = $candidate
        break
    }
}

if (-not $pythonExe) {
    Write-Host "Python not found. Installing Python 3.12 with winget..." -ForegroundColor Yellow

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "winget is not available. Install Python 3.12 manually, then run this script again."
    }

    Invoke-NativeCommand $winget.Source install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements

    $pythonExe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-RealPython $pythonExe)) {
        throw "Python installation finished, but Python could not be verified at $pythonExe. Restart PowerShell and run this script again."
    }
}

Write-Host "Using Python: $pythonExe" -ForegroundColor Green
Invoke-NativeCommand $pythonExe --version

Write-Host "Installing requirements..." -ForegroundColor Cyan
Invoke-NativeCommand $pythonExe -m pip install --upgrade pip
Invoke-NativeCommand $pythonExe -m pip install -r requirements.txt

Write-Host "Running automated tests..." -ForegroundColor Cyan
Invoke-NativeCommand $pythonExe -m pytest -q

Write-Host "Checking imports and bootstrap without opening the desktop window..." -ForegroundColor Cyan
Invoke-NativeCommand $pythonExe -c "from main import build_services; from core.bootstrap import Bootstrap; from services.app_context import AppContext; build_services(); status=Bootstrap(context=AppContext).start(); assert status['sprint']=='SP1'; print('FLASH SP1 bootstrap verified:', status['version'])"

Write-Host "FLASH SP1 verification complete." -ForegroundColor Green
Write-Host "Starting the desktop verification window. Close it to finish." -ForegroundColor Cyan
Invoke-NativeCommand $pythonExe main.py
