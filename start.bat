@echo off
setlocal
title Web Resource Crawler

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/3] Checking dependencies...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Dependency install failed, trying system Python...
    goto :sysrun
)

echo [3/4] Checking VLC player component...
".venv\Scripts\python.exe" player_vlc.py --ensure
echo [4/4] Starting GUI...
start "" ".venv\Scripts\pythonw.exe" gui.py
exit /b 0

:sysrun
echo Starting with system Python...
start "" pythonw gui.py
exit /b 0

:fail
echo.
echo Failed to start: Python not found. Please install Python 3.10+ and add it to PATH.
pause
exit /b 1