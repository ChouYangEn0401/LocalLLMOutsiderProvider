@echo off
rem Shared settings for the portable (unzip-and-run) edition. Do not run directly.
rem ASCII only: cmd.exe reads .bat files in the console code page.
chcp 65001 >nul
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
set "PY=%ROOT%\python\python.exe"
set "LLMOP_CONFIG=%ROOT%\config.yaml"
cd /d "%ROOT%"
if not exist "%PY%" (
  echo [X] python\python.exe is missing. Unzip the whole folder again.
  pause
  exit /b 1
)
exit /b 0
