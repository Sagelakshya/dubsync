@echo off
REM One-click launcher.
REM   1) finds a usable Python 3.10+ (or installs it automatically the first time)
REM   2) builds the app's own environment + installs deps on first run
REM   3) starts the app
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

call :detect
if not defined PYEXE (
  echo.
  echo Python 3.10+ was not found on this PC. Trying to install it automatically...
  echo This is a one-time step and may take a few minutes.
  echo.
  where winget >nul 2>nul
  if !errorlevel!==0 (
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  ) else (
    echo Could not auto-install ^(winget is unavailable on this PC^).
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo During setup, TICK "Add Python to PATH". Then run this file again.
    echo.
    pause
    exit /b 1
  )
  call :detect
  if not defined PYEXE (
    echo.
    echo Python was installed, but this window can't see it yet.
    echo Please CLOSE this window and double-click run.cmd again.
    echo.
    pause
    exit /b 1
  )
)

echo Using Python: "%PYEXE%" %PYARGS%

REM ---------- first-time app setup ----------
if not exist .venv (
  echo First run: setting up the app ^(this takes a minute^)...
  "%PYEXE%" %PYARGS% -m venv .venv
  if not exist .venv (
    echo.
    echo Setup failed: could not create the environment. See messages above.
    pause
    exit /b 1
  )
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

echo.
echo  App is starting. Open http://127.0.0.1:5000 in your browser.
echo  (Keep this window open. Press Ctrl+C here to stop.)
echo.
python app.py
goto :eof


REM ===================== helpers =====================
:detect
REM Sets PYEXE (the executable, may contain spaces) and PYARGS (extra args).
set "PYEXE="
set "PYARGS="

REM 1) Windows "py" launcher -- ignores the Microsoft Store stub, most reliable
where py >nul 2>nul
if !errorlevel!==0 (
  py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
  if !errorlevel!==0 (
    set "PYEXE=py" & set "PYARGS=-3" & goto :eof
  )
)

REM 2) "python" on PATH -- rejects the Store stub and versions below 3.10
python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if !errorlevel!==0 (
  set "PYEXE=python" & goto :eof
)

REM 3) A per-user install (winget / python.org) not yet on this session's PATH
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
  if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
)
goto :eof
