@echo off
REM One-click video translation: paste a YouTube link, pick a language,
REM get back a dubbed video whose voice stays in sync with the picture.
REM   1) finds a usable Python 3.10+ (or installs it automatically the first time)
REM   2) builds the app's own environment + installs deps on first run
REM   3) asks you a few questions and produces dubbed.mp4 (or dubbed.mp3)
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
    echo Please CLOSE this window and double-click dub.cmd again.
    echo.
    pause
    exit /b 1
  )
)

REM ---------- first-time setup ----------
if not exist .venv (
  echo First run: setting up ^(this takes a minute^)...
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

REM ---------- ask what to make ----------
echo.
echo ================= Video translation =================
set "URL="
set /p "URL=Paste the YouTube link and press Enter:  "
if "!URL!"=="" ( echo No link given. & pause & exit /b 1 )

echo.
echo  Language codes:  hi=Hindi  es=Spanish  fr=French  de=German
echo                   ar=Arabic  zh-CN=Chinese  ja=Japanese  en=English
set "LANG=hi"
set /p "LANG=Language code [default hi]:  "

echo.
echo  What kind of footage is it?
echo    1^) Talking head  ^(a person speaking on camera^)
echo    2^) No faces      ^(POV / gameplay / screen recording^)
echo    3^) Audio only    ^(just the dubbed .mp3, no video^)
set "MODE=2"
set /p "MODE=Choose 1, 2 or 3 [default 2]:  "

set "FLAGS=--broll --download --out dubbed.mp4"
if "!MODE!"=="1" set "FLAGS=--faces --download --out dubbed.mp4"
if "!MODE!"=="3" set "FLAGS=--out dubbed.mp3"

echo.
echo Working... downloading, translating, voicing and syncing. Please wait.
echo.
python dub.py "!URL!" --lang !LANG! !FLAGS!

echo.
echo Finished. Look for dubbed.mp4 ^(or dubbed.mp3^) in this folder:
echo   %~dp0
echo.
pause
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
