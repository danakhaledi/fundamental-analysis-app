@echo off
setlocal
cd /d "%~dp0"
title Fundamental Analysis Web App

REM Detect Python 3.11+; support both the Python Launcher and python.exe.
set "BASEPY="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 --version >nul 2>nul
    if not errorlevel 1 set "BASEPY=py -3.11"
)
if not defined BASEPY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
        if not errorlevel 1 set "BASEPY=python"
    )
)
if not defined BASEPY goto :python_error

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating Python virtual environment...
    %BASEPY% -m venv .venv
    if errorlevel 1 goto :venv_error
)
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" goto :venv_error

echo.
echo [2/4] Installing requirements from official PyPI...
"%PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt --index-url https://pypi.org/simple
if not errorlevel 1 goto :installed

echo.
echo Official PyPI failed. Trying Aliyun mirror...
"%PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt --index-url https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 goto :installed

echo.
echo ERROR: Installation failed from both package sources.
echo Check network, proxy, firewall, and the errors above.
pause
exit /b 1

:installed
echo.
echo [3/4] Checking configuration...
if not exist .env copy .env.example .env >nul
if not exist .env (
    echo ERROR: Could not create .env from .env.example.
    pause
    exit /b 1
)
echo.
echo [4/4] Starting application at http://127.0.0.1:8000
"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 (
    echo.
    echo Application stopped with an error. Review messages above.
    pause
    exit /b 1
)
exit /b 0

:python_error
echo ERROR: Python 3.11 or newer was not found.
echo Install Python 3.11+ and enable Add python.exe to PATH.
pause
exit /b 1

:venv_error
echo ERROR: Could not create or locate .venv.
echo Check the Python installation and folder permissions.
pause
exit /b 1
