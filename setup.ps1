$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "🌐 Booting Agentic Browser Setup..." -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Create Virtual Environment
$venvPath = Join-Path $root "venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "[1/4] Creating Virtual Environment (venv)..." -ForegroundColor Yellow
    python -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ Failed to create virtual environment. Ensure python is in your PATH." -ForegroundColor Red
        exit
    }
    Write-Host "✅ Virtual environment created successfully." -ForegroundColor Green
} else {
    Write-Host "[1/4] Virtual Environment already exists." -ForegroundColor Green
}

# 2. Get Pip and Playwright paths
$pip = Join-Path $venvPath "Scripts\pip.exe"
$playwrightPath = Join-Path $venvPath "Scripts\playwright.exe"

# 3. Install requirements
Write-Host "`n[2/4] Installing required Python packages..." -ForegroundColor Yellow
& $pip install -r requirements.txt

# 4. Install Playwright browsers
Write-Host "`n[3/4] Installing Playwright browsers..." -ForegroundColor Yellow
& $playwrightPath install chromium
& $playwrightPath install chrome

Write-Host "`n==================================================" -ForegroundColor Cyan
Write-Host "✅ Setup Complete!" -ForegroundColor Green
Write-Host "You can now start the application by running 'start.bat'" -ForegroundColor Cyan
Write-Host "If you want to log into your accounts for the agent to use, run: 'venv\Scripts\python.exe setup_profile.py'" -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Cyan
