@echo off
rem Starts llmop on this computer only: http://127.0.0.1:8780
rem Closing this window stops it. Extra options are passed on, e.g.
rem   Start.bat --open          also open the web page
rem   Start.bat --port 8781     use another port
setlocal
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] llmop is not installed yet. Double-click Setup.bat first.
  pause
  exit /b 1
)
title llmop - close this window to stop
"%VENV_PY%" -m llmop serve %*
if errorlevel 1 pause
