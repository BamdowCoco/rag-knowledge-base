# Local demo launcher for Windows PowerShell.
# ASCII only on purpose: Windows PowerShell 5.1 may misread UTF-8 scripts without a BOM.
# Usage: scripts\demo.ps1 (works from any working directory)
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host 'ERROR: docker was not found. Install Docker Desktop and start it first.'
    exit 1
}

if (-not (Test-Path '.env')) {
    Write-Host '[1/5] .env not found, copying .env.example to .env'
    try {
        Copy-Item -Path '.env.example' -Destination '.env' -ErrorAction Stop
    } catch {
        Write-Host "ERROR: failed to copy .env.example: $($_.Exception.Message)"
        exit 1
    }
} else {
    Write-Host '[1/5] .env already exists, leaving it untouched.'
}

Write-Host '[2/5] Building and starting containers...'
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'ERROR: docker compose up failed.'
    Write-Host 'Check that Docker Desktop is running and that ports 8000 and 8001 are free.'
    exit 1
}

Write-Host '[3/5] Stopping the mock syncer (the demo uses the real web corpus).'
docker compose stop syncer | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'WARN: could not stop syncer; source stats may still include mock docs.'
} else {
    Write-Host 'Mock syncer stopped. Start it again with: docker compose start syncer'
}

Write-Host '[4/5] Waiting a few seconds, then showing service status...'
Start-Sleep -Seconds 5
docker compose ps

Write-Host '[5/5] Optional: crawl 20 pages from the upstream site.'
$answer = Read-Host 'Run one crawler round (20 pages)? [y/N]'
if ($answer -match '^(y|yes)$') {
    Write-Host 'Running crawler with CRAWL_MAX_PAGES=20...'
    docker compose --profile crawler run --rm -e CRAWL_MAX_PAGES=20 crawler
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'WARN: crawler failed. The demo stack is still running; check the network or logs.'
    } else {
        Write-Host 'Crawler finished. Embedding continues in the background.'
    }
} else {
    Write-Host 'Skipped crawler round.'
}

Write-Host ''
Write-Host 'Demo is starting. Open these pages:'
Write-Host '  Chat page:  http://localhost:8001/'
Write-Host '  Admin page: http://localhost:8001/admin.html'
Write-Host '  API docs:   http://localhost:8000/docs'
Write-Host ''
Write-Host 'The embedder works in the background; if a page is not ready yet, wait and refresh.'
Write-Host 'Smoke test: python scripts\smoke.py'
