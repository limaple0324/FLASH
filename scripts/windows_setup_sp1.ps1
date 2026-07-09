$ErrorActionPreference = "Stop"

Write-Host "FLASH SP1 Windows Setup" -ForegroundColor Cyan
Write-Host "Project: $PSScriptRoot\.."

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

function Test-RealPython {
    param([string]$PythonExe)

    if (-not (Test-Path $PythonExe)) {
        return $false
    }

    $output = & $PythonExe -c "print('FLASH OK')" 2>$null
    return ($output -eq "FLASH OK")
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
        throw "winget is not available. Please install Python 3.12 manually from python.org, then run this script again."
    }

    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements

    $pythonExe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-RealPython $pythonExe)) {
        throw "Python installation finished, but Python could not be verified at $pythonExe. Restart PowerShell and run this script again."
    }
}

Write-Host "Using Python: $pythonExe" -ForegroundColor Green
& $pythonExe --version

Write-Host "Installing requirements..." -ForegroundColor Cyan
& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install -r requirements.txt

Write-Host "Running FLASH SP1 bootstrap..." -ForegroundColor Cyan
& $pythonExe main.py

Write-Host "Running tests..." -ForegroundColor Cyan
& $pythonExe -m pytest

Write-Host "FLASH SP1 verification complete." -ForegroundColor Green
