@echo off
rem Starts the whole llmop node and opens the local console http://127.0.0.1:8781/
rem   local console + key-less /v1   http://127.0.0.1:8781  (this computer only)
rem   account website                http://127.0.0.1:8780  (public via tunnel "ui")
rem   API link                       http://127.0.0.1:8782/<secret>/v1  (tunnel "api")
rem Tunnels start only if tunnels.ui / tunnels.api have enabled: true in config.yaml;
rem Start-With-Tunnel.bat always starts both. Closing this window stops everything.
setlocal
call "%~dp0_env.bat" || exit /b 1
call "%~dp0_first_run.bat" || exit /b 1
title llmop node - close this window to stop
"%PY%" -m llmop serve --open %*
if errorlevel 1 pause
