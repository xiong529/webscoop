@echo off
setlocal
title Web Resource Crawler

cd /d "%~dp0"

rem ---- 1/4 Check Python version (3.10+) ----
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :fail

rem ---- 2/4 Create virtual environment (first run only) ----
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

rem ---- 3/4 Install dependencies ----
echo [3/4] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency install failed. Please check your network / pip mirror,
    echo then run this file again.
    pause
    exit /b 1
)

rem ---- 4/4 VLC player component (optional, non-fatal) ----
echo [4/4] Checking VLC player component...
".venv\Scripts\python.exe" player_vlc.py --ensure
if errorlevel 1 (
    echo Warning: VLC check failed, online preview may be unavailable.
    echo You can still download files normally.
)

echo Starting GUI...
start "" ".venv\Scripts\pythonw.exe" gui.py
exit /b 0

:fail
echo.
echo Failed to start: Python 3.10+ not found or virtualenv creation failed.
echo Please install Python 3.10+ from https://www.python.org/ and add it to PATH,
echo then run this file again.
pause
exit /b 1