@echo off
rem Shared settings for the portable (unzip-and-run) edition. Do not run directly.
rem ASCII only: cmd.exe reads .bat files in the console code page.
rem Everything stays in this folder: config.yaml, data\ (database) and
rem data\llmop_home\ (worker and client PRIVATE KEYS). Never share the data folder.
chcp 65001 >nul
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
set "PY=%ROOT%\python\python.exe"
set "LLMOP_CONFIG=%ROOT%\config.yaml"
set "LLMOP_HOME=%ROOT%\data\llmop_home"
rem cloudflared.exe placed next to this file is found first
set "PATH=%ROOT%;%PATH%"
cd /d "%ROOT%"
if not exist "%PY%" (
  echo [X] python\python.exe is missing. Unzip the whole folder again.
  pause
  exit /b 1
)
exit /b 0
