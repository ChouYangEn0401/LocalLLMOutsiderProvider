@echo off
rem Shared settings, called by the other .bat files. Do not run directly.
rem Keep every .bat file ASCII-only: cmd.exe reads them in the console code page.
chcp 65001 >nul
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "LLMOP_CONFIG=%ROOT%\config.yaml"
cd /d "%ROOT%"
