@echo off
setlocal
cd /d %~dp0

rem Keep the isolated audio runtime relative to this application.
set "ANIME_STUDIO_ANALYSIS_HOME=%~dp0audio_analysis"
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort 18081 -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
  start "" http://127.0.0.1:18081
  exit /b 0
)

if not exist .venv\Scripts\python.exe (
  echo [1/3] Creating virtual environment...
  py -3 -m venv .venv
)

echo [2/3] Installing/updating dependencies...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt

where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [ERROR] FFmpeg not found in PATH.
  echo Install FFmpeg on Windows and make sure ffmpeg.exe is available in PATH.
  pause
  exit /b 1
)
where ffprobe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] FFprobe not found in PATH.
  echo FFprobe normally comes with FFmpeg. Add its bin folder to PATH.
  pause
  exit /b 1
)
echo FFmpeg/FFprobe check passed.

echo [3/3] Starting AI Anime Studio at http://127.0.0.1:18081
start "" http://127.0.0.1:18081
.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 18081 --reload

endlocal
