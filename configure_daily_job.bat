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
set /p "BACKFILL=Maximum full backfill passes 1-10 (default 3): "
if "%BACKFILL%"=="" set "BACKFILL=3"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$n='%BACKFILL%'; if ($n -notmatch '^[0-9]+$' -or [int]$n -lt 1 -or [int]$n -gt 10) { exit 1 }"
if errorlevel 1 (echo Invalid numeric configuration.& pause& exit /b 1)
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "RUNNER=%~dp0run_market_daily_sync.py"
set "TASKNAME=FundamentalWebApp-DailyMarketSync"
set "TASKCMD=cmd.exe /c ""set DAILY_BACKFILL_PASSES=%BACKFILL% ^& cd /d ""%~dp0"" ^& ""%PYTHON%"" ""%RUNNER%""""
schtasks /Create /F /SC DAILY /TN "%TASKNAME%" /ST "%JOBTIME%" /TR "%TASKCMD%"
if errorlevel 1 (echo Failed to create task. Try running as the same Windows user as the app.& pause& exit /b 1)
echo.
echo Task created: %TASKNAME%
echo Daily time: %JOBTIME%
echo Log file: %~dp0logs\market_daily_sync.log
echo Run now from Task Scheduler to test it. Delete with: schtasks /Delete /F /TN "%TASKNAME%"
pause
