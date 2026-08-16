@echo off
setlocal enabledelayedexpansion
title Web Resource Crawler

cd /d "%~dp0"
set "LOG=install.log"
set "PIP_OK="

> "%LOG%" echo === resources-reptile install log ===
>> "%LOG%" echo [start] %DATE% %TIME%

rem ============ 1/4 find Python 3.10+ (python or py launcher) ============
set "PYTHON_EXE="
python --version >nul 2>&1
if %errorlevel% equ 0 set "PYTHON_EXE=python"
if not defined PYTHON_EXE (
    py -3 --version >nul 2>&1
    if %errorlevel% equ 0 set "PYTHON_EXE=py -3"
)
if not defined PYTHON_EXE goto :fail_python

for /f "delims=" %%v in ('%PYTHON_EXE% -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "PY_VER=%%v"
echo [1/4] Found Python %PY_VER%
>> "%LOG%" echo [detect] %PYTHON_EXE% %PY_VER%

%PYTHON_EXE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :fail_version

rem ============ 2/4 virtual environment (self-heal if broken) ============
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [2/4] Virtual environment broken, recreating...
        rmdir /s /q ".venv"
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual environment...
    %PYTHON_EXE% -m venv .venv
    if errorlevel 1 goto :fail_venv
)

rem ============ 3/4 dependencies with automatic mirror fallback ============
echo [3/4] Installing dependencies...
if exist ".venv\pip_mirror.txt" (
    set /p LAST_MIRROR=<".venv\pip_mirror.txt"
    if defined LAST_MIRROR call :try_pip "!LAST_MIRROR!" "last working mirror"
)
if not defined PIP_OK call :try_pip "https://pypi.org/simple" "PyPI official"
if not defined PIP_OK call :try_pip "https://pypi.tuna.tsinghua.edu.cn/simple" "Tsinghua mirror"
if not defined PIP_OK call :try_pip "https://mirrors.aliyun.com/pypi/simple/" "Aliyun mirror"
if not defined PIP_OK goto :fail_pip

rem ============ 4/4 VLC player component (optional, non-fatal) ============
echo [4/4] Checking VLC player component...
".venv\Scripts\python.exe" player_vlc.py --ensure
if errorlevel 1 (
    echo Warning: VLC check failed, online preview may be unavailable.
    echo You can still download files normally.
)

echo.
echo All ready. Starting GUI...
start "" ".venv\Scripts\pythonw.exe" gui.py
exit /b 0

rem ---------- helper: try installing dependencies via one index ----------
:try_pip
echo     trying %~2 ...
>> "%LOG%" echo [pip] %~2 (%~1)
".venv\Scripts\python.exe" -m pip install -q -i "%~1" --default-timeout=60 --retries=2 -r requirements.txt >> "%LOG%" 2>&1
if %errorlevel% equ 0 (
    echo     OK - installed via %~2
    > ".venv\pip_mirror.txt" echo %~1
    set "PIP_OK=1"
)
exit /b 0

rem ---------- Python not found ----------
:fail_python
echo.
echo [ERROR] Python 3.10+ was not found on this system.
echo.
echo Fixes:
echo   1. Install from https://www.python.org/downloads/
echo      and check "Add python.exe to PATH" during install.
echo   2. If "python" opens the Microsoft Store, disable its app alias:
echo      Windows Settings - Apps - Advanced app settings - App execution
echo      aliases - "python.exe" / "python3.9"/"python3" - turn OFF.
echo   3. The "py" launcher also works if installed with Python.
echo      Check: py -3 -V
echo.
echo Common solutions also listed in the README "Installation help" section.
pause
exit /b 1

rem ---------- Python too old ----------
:fail_version
echo.
echo [ERROR] Python was found but version %PY_VER% is too old (need 3.10+).
echo.
echo Fixes:
echo   1. Install a newer Python from https://www.python.org/downloads/
echo      and rerun this file (restart the window first).
echo   2. If you have multiple versions, run this file with the newer one, e.g.:
echo      py -3.12 start.bat
echo   3. Check the current version: %PYTHON_EXE% --version
pause
exit /b 1

rem ---------- venv creation failed ----------
:fail_venv
echo.
echo [ERROR] Failed to create the virtual environment.
echo.
echo Fixes:
echo   1. Re-run the Python installer and make sure the install is complete
echo      (select "Repair" if needed).
echo   2. Antivirus programs can block venv creation - allow this folder.
echo   3. Disable Windows "Developer Mode" symlink requirement if it asks.
echo   4. Create it manually:
echo      %PYTHON_EXE% -m venv .venv
pause
exit /b 1

rem ---------- dependency install failed after all mirrors ----------
:fail_pip
echo.
echo [ERROR] Dependency installation failed after trying every mirror.
echo.
echo Possible causes and fixes:
echo   1. No network or firewall blocking PyPI - this script already tried
echo      Tsinghua and Aliyun mirrors automatically.
echo   2. Corporate proxy / VPN - set the system proxy, or set:
echo      HTTP_PROXY and HTTPS_PROXY environment variables, then rerun.
echo   3. Slow network - rerun later, or install manually:
echo      ".venv\Scripts\python.exe" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
echo   4. Corrupted venv - delete the ".venv" folder, then rerun this file.
echo.
echo ---- Last 15 lines of install.log ----
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 15"
echo ---------------------------------------
echo Full log: %LOG%   More help: README "Installation help" section.
pause
exit /b 1