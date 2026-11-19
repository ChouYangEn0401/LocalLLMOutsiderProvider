@echo off
rem Starts the whole llmop node (see listeners / tunnels / roles in config.yaml):
rem   local console + key-less /v1  http://127.0.0.1:8781  (this computer only)
rem   account website               http://127.0.0.1:8780  (tunnel "ui")
rem   API link                      http://127.0.0.1:8782/<secret>/v1  (tunnel "api")
rem Closing this window stops it. Extra options are passed on, e.g.
rem   Start.bat --open          also open the local console
rem   Start.bat --no-tunnel     never start the tunnels
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
