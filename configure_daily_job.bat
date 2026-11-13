@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Configure Daily Market Sync Job
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found. Run run_windows.bat first.
  pause
  exit /b 1
)
echo.
echo Creates or updates a Windows Task Scheduler task. The PC must be on at run time.
set /p "JOBTIME=Daily start time HH:MM (default 07:00): "
if "%JOBTIME%"=="" set "JOBTIME=07:00"
echo %JOBTIME%| findstr /r "^[0-2][0-9]:[0-5][0-9]$" >nul
if errorlevel 1 (echo Invalid time. Use HH:MM, e.g. 07:00.& pause& exit /b 1)
set /p "RESOLVE=Symbol resolution batch size 1-800 (default 500): "
if "%RESOLVE%"=="" set "RESOLVE=500"
set /p "ROUNDS=Maximum symbol resolution rounds 1-100 (default 20): "
if "%ROUNDS%"=="" set "ROUNDS=20"
set /p "BACKFILL=Maximum full backfill passes 1-10 (default 3): "
if "%BACKFILL%"=="" set "BACKFILL=3"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$v=@('%RESOLVE%','%ROUNDS%','%BACKFILL%'); if (($v[0] -notmatch '^\d+$') -or [int]$v[0] -lt 1 -or [int]$v[0] -gt 800 -or ($v[1] -notmatch '^\d+$') -or [int]$v[1] -lt 1 -or [int]$v[1] -gt 100 -or ($v[2] -notmatch '^\d+$') -or [int]$v[2] -lt 1 -or [int]$v[2] -gt 10) { exit 1 }"
if errorlevel 1 (echo Invalid numeric configuration.& pause& exit /b 1)
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "RUNNER=%~dp0run_market_daily_sync.py"
set "TASKNAME=FundamentalWebApp-DailyMarketSync"
set "TASKCMD=cmd.exe /c ""set DAILY_SYMBOL_RESOLUTION_BATCH=%RESOLVE% ^& set DAILY_SYMBOL_RESOLUTION_ROUNDS=%ROUNDS% ^& set DAILY_BACKFILL_PASSES=%BACKFILL% ^& cd /d ""%~dp0"" ^& ""%PYTHON%"" ""%RUNNER%""""
schtasks /Create /F /SC DAILY /TN "%TASKNAME%" /ST "%JOBTIME%" /TR "%TASKCMD%"
if errorlevel 1 (echo Failed to create task. Try running as the same Windows user as the app.& pause& exit /b 1)
echo.
echo Task created: %TASKNAME%
echo Daily time: %JOBTIME%
echo Log file: %~dp0logs\market_daily_sync.log
echo Run now from Task Scheduler to test it. Delete with: schtasks /Delete /F /TN "%TASKNAME%"
pause
