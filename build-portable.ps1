<#
build-portable.ps1 - build the zero-install Windows download.

What it makes: build\dubsync\ (and a zip of it) containing everything the tool
needs - its own Python, every dependency including Whisper's native ones, and
ffmpeg. The person who downloads it extracts the zip and double-clicks one file;
there is nothing to install and no PATH to fix.

Why an embeddable Python rather than PyInstaller: yt-dlp and edge-tts both break
under PyInstaller's frozen imports. The embeddable distribution is just a folder
of DLLs, so everything behaves exactly as it does in a normal install.

The `_pth` gotcha: the embeddable Python ignores the usual path setup. Its
`python312._pth` must list `..` (so the app's .py files, which sit one level
above \runtime, are importable) and `import site` (so pip's site-packages is
picked up). Miss either and the tool starts and then fails on the first import.

Run:  powershell -ExecutionPolicy Bypass -File build-portable.ps1
#>
[CmdletBinding()]
param(
    [string]$PyVersion = "3.12.10",     # match what the tool is developed against
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$Root  = $PSScriptRoot
$Build = Join-Path $Root "build"
$Cache = Join-Path $Build "cache"       # downloads live here so rebuilds are fast
$App   = Join-Path $Build "dubsync"
$Rt    = Join-Path $App "runtime"
$PyTag = "python" + ($PyVersion -split '\.')[0] + ($PyVersion -split '\.')[1]   # python312

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

# --- 0. clean slate (keep the download cache) --------------------------------
Step "Preparing $App"
if (Test-Path $App) { Remove-Item $App -Recurse -Force }
New-Item -ItemType Directory -Force -Path $App, $Cache | Out-Null

# --- 1. the runtime: embeddable Python ---------------------------------------
Step "Fetching embeddable Python $PyVersion"
$embedZip = Join-Path $Cache "python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    Invoke-WebRequest -UseBasicParsing -OutFile $embedZip `
        "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
}
Expand-Archive -Path $embedZip -DestinationPath $Rt -Force

# The path file: '..' makes the app importable, 'import site' switches pip on.
Step "Writing $PyTag._pth"
@"
$PyTag.zip
.
..
Lib\site-packages
import site
"@ | Set-Content -Path (Join-Path $Rt "$PyTag._pth") -Encoding ascii

# --- 2. pip + every dependency (including Whisper's native ones) -------------
Step "Installing pip"
$getPip = Join-Path $Cache "get-pip.py"
if (-not (Test-Path $getPip)) {
    Invoke-WebRequest -UseBasicParsing -OutFile $getPip "https://bootstrap.pypa.io/get-pip.py"
}
& "$Rt\python.exe" $getPip --no-warn-script-location --no-cache-dir | Out-Null

Step "Installing dependencies (faster-whisper pulls ctranslate2 / av / onnxruntime - this is the big one)"
& "$Rt\python.exe" -m pip install --no-warn-script-location --no-cache-dir `
    -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# --- 3. the app itself --------------------------------------------------------
Step "Copying the app"
foreach ($f in @("app.py", "dub.py", "hinglish.py", "transcribe.py",
                 "requirements.txt", "README.md", "THIRD-PARTY-LICENSES.md")) {
    Copy-Item (Join-Path $Root $f) $App
}
foreach ($opt in @("LICENSE", "LICENSE.txt")) {            # MIT text, if it's here
    if (Test-Path (Join-Path $Root $opt)) { Copy-Item (Join-Path $Root $opt) $App }
}
Copy-Item (Join-Path $Root "licenses") $App -Recurse

# --- 4. ffmpeg ----------------------------------------------------------------
# Only ffmpeg + ffprobe: ffplay is another ~30 MB and the tool never calls it.
Step "Copying ffmpeg"
$ffSrc = $env:FFMPEG_DIR
if (-not $ffSrc -or -not (Test-Path (Join-Path $ffSrc "ffmpeg.exe"))) {
    foreach ($c in @((Join-Path $Root "ffmpeg\bin"), "D:\Toolkit\ffmpeg\bin")) {
        if (Test-Path (Join-Path $c "ffmpeg.exe")) { $ffSrc = $c; break }
    }
}
if (-not $ffSrc) { throw "ffmpeg.exe not found - set FFMPEG_DIR to the folder holding it." }
$ffDst = Join-Path $App "ffmpeg\bin"
New-Item -ItemType Directory -Force -Path $ffDst | Out-Null
Copy-Item (Join-Path $ffSrc "ffmpeg.exe") $ffDst
Copy-Item (Join-Path $ffSrc "ffprobe.exe") $ffDst

# --- 5. launchers -------------------------------------------------------------
Step "Writing launchers"
@'
@echo off
REM Starts dubsync and opens it in your browser. Everything it needs is in this
REM folder (its own Python in \runtime, ffmpeg in \ffmpeg) - nothing to install.
cd /d "%~dp0"
echo Starting dubsync... your browser will open at http://127.0.0.1:5000
echo Keep this window open while you use it. Close it (or press Ctrl+C) to stop.
"%~dp0runtime\python.exe" "%~dp0app.py"
if errorlevel 1 pause
'@ | Set-Content -Path (Join-Path $App "Start dubsync.cmd") -Encoding ascii

@'
@echo off
REM The same dubber without the browser: it asks the questions in this window.
cd /d "%~dp0"
"%~dp0runtime\python.exe" "%~dp0dub.py"
pause
'@ | Set-Content -Path (Join-Path $App "Dub a video (console).cmd") -Encoding ascii

@'
dubsync - start here
====================

WHAT THIS DOES
  Dubs a YouTube video into another language, keeping the new voice in time
  with the picture. It listens to the video itself (Whisper) to get an
  accurate script, so it works even on videos with bad captions or none.

TO USE IT
  1. Double-click   "Start dubsync.cmd"
  2. Your browser opens. Paste a YouTube link, pick a language, press Start.
  3. Wait - a dub takes minutes, and the page shows you where it's up to.
  4. Download the result. (Copies also land in the "dubs" folder here.)

  The same page has a "Text -> speech" tab: paste any script, pick a voice,
  get an .mp3.

GOOD TO KNOW
  - Nothing to install. This folder contains its own Python (\runtime) and
    ffmpeg (\ffmpeg).
  - Needs internet: it downloads the video, and on the FIRST dub it also
    downloads the Whisper speech model (about half a gigabyte, once).
  - Windows 64-bit only.
  - The first dub on a PC without a good GPU runs on the CPU. It works; it's
    just slower. A 5-minute video can take 10+ minutes.
  - HINGLISH (Hindi dubs that sound like a real YouTuber) is optional and
    needs one extra thing: install Ollama from ollama.com, then run
    "ollama pull gemma3:4b" once. Without it, Hindi dubs still work - they
    just sound more formal.
  - If Windows SmartScreen warns about the .cmd file, click "More info" then
    "Run anyway". A .cmd is a plain text file - open it in Notepad and read it.

WHAT IT SENDS OUT
  The video download (YouTube), the text to translate (Google Translate) and
  the text to speak (Microsoft Edge voices). Nothing else leaves your PC, and
  Hinglish runs entirely on your own machine.

Source, licences and the full manual: see README.md and
THIRD-PARTY-LICENSES.md in this folder.
'@ | Set-Content -Path (Join-Path $App "START HERE.txt") -Encoding ascii

# --- 6. trim ------------------------------------------------------------------
# pip and the wheel caches are build-time only; shipping them wastes ~40 MB.
Step "Trimming build-only files"
foreach ($junk in @("$Rt\Lib\site-packages\pip", "$Rt\Lib\site-packages\pip-*.dist-info",
                    "$Rt\Lib\site-packages\setuptools", "$Rt\Lib\site-packages\setuptools-*.dist-info",
                    "$Rt\Lib\site-packages\pkg_resources", "$Rt\Scripts")) {
    Get-Item $junk -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
}
Get-ChildItem $App -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# --- 6b. attribution -----------------------------------------------------------
# Regenerated from the runtime we just built (after trimming, so build-only
# packages don't appear), then refreshed inside the package. Attribution that is
# generated from what actually ships can't silently go stale when a dep is added.
Step "Regenerating third-party attribution"
& "$Rt\python.exe" (Join-Path $Root "gen-licenses.py") "$Rt\Lib\site-packages"
if ($LASTEXITCODE -ne 0) { throw "gen-licenses.py failed" }
Copy-Item (Join-Path $Root "THIRD-PARTY-LICENSES.md") $App -Force
Remove-Item (Join-Path $App "licenses") -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $Root "licenses") $App -Recurse

$sizeMB = [math]::Round(((Get-ChildItem $App -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "   folder size: $sizeMB MB"

# --- 7. zip -------------------------------------------------------------------
if (-not $SkipZip) {
    Step "Zipping"
    $zip = Join-Path $Build "dubsync-portable-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $App -DestinationPath $zip -CompressionLevel Optimal
    $zipMB = [math]::Round(((Get-Item $zip).Length / 1MB), 1)
    Write-Host "`nBuilt $zip  ($zipMB MB)" -ForegroundColor Green
} else {
    Write-Host "`nBuilt $App (zip skipped)" -ForegroundColor Green
}
