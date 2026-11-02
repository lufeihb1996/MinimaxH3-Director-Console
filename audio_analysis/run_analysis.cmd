@echo off
setlocal
if "%~1"=="" (
 echo Drag an audio file onto this script to analyze it.
 pause
 exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0analyze_local.py" "%~1" --output "%~dp0results\%~n1"
pause
