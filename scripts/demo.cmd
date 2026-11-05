@echo off
rem Local demo launcher for Windows cmd.
rem ASCII only on purpose: cmd parses this file with the console code page.
rem Usage: scripts\demo.cmd (works from any working directory)
setlocal

cd /d "%~dp0.."
if errorlevel 1 (
    echo ERROR: cannot enter the repo root from "%~dp0..".
    pause
    exit /b 1
)

where docker >nul 2>&1
if errorlevel 1 (
    echo ERROR: docker was not found. Install Docker Desktop and start it first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [1/5] .env not found, copying .env.example to .env
    copy ".env.example" ".env" >nul
    if errorlevel 1 (
        echo ERROR: failed to copy .env.example. Check that the file exists.
        pause
        exit /b 1
    )
) else (
    echo [1/5] .env already exists, leaving it untouched.
)

echo [2/5] Building and starting containers...
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo ERROR: docker compose up failed.
    echo Check that Docker Desktop is running and that ports 8000 and 8001 are free.
    pause
    exit /b 1
)

echo [3/5] Stopping the mock syncer (the demo uses the real web corpus).
docker compose stop syncer >nul 2>&1
if errorlevel 1 (
    echo WARN: could not stop syncer; source stats may still include mock docs.
) else (
    echo Mock syncer stopped. Start it again with: docker compose start syncer
)

echo [4/5] Waiting a few seconds, then showing service status...
ping -n 6 127.0.0.1 >nul
docker compose ps

echo [5/5] Optional: crawl 20 pages from the upstream site.
set "RUN_CRAWLER="
set /p RUN_CRAWLER=Run one crawler round (20 pages)? [y/N]:
if /i "%RUN_CRAWLER%"=="y" (
    echo Running crawler with CRAWL_MAX_PAGES=20...
    docker compose --profile crawler run --rm -e CRAWL_MAX_PAGES=20 crawler
    if errorlevel 1 (
        echo WARN: crawler failed. The demo stack is still running; check the network or logs.
    ) else (
        echo Crawler finished. Embedding continues in the background.
    )
) else (
    echo Skipped crawler round.
)

echo.
echo Demo is starting. Open these pages:
echo   Chat page:  http://localhost:8001/
echo   Admin page: http://localhost:8001/admin.html
echo   API docs:   http://localhost:8000/docs
echo.
echo The embedder works in the background; if a page is not ready yet, wait and refresh.
echo Smoke test: python scripts\smoke.py
echo.
pause
exit /b 0
