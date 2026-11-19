@echo off
rem Encrypted mode, on a computer that runs ONLY the worker (next to the LLM server).
rem If the worker runs on the same computer as the node, do not use this: put
rem   roles: [gateway, worker]
rem in config.yaml and use Start.bat (the node starts the worker itself).
rem
rem First time:  1. this file creates the worker keys and data\llmop_home\worker\config.json
rem              2. edit that config.json: gateway_url = wss://<api-host>/<secret>/worker/ws
rem                 and backends = your local LLM server (127.0.0.1 only)
rem              3. in the node's local console create a worker and copy its token, then
rem                 Start-Worker.bat --reg-token llmop_reg_...
rem Later:       double-click. Compare the key_id / public_key printed below with
rem              what the client pins (pinned_workers).
setlocal
call "%~dp0_env.bat" || exit /b 1
if not exist "%LLMOP_HOME%\worker\keys.json" (
  "%PY%" -m llmop worker init
  if errorlevel 1 goto :fail
  echo.
  echo Now edit %LLMOP_HOME%\worker\config.json, then run this file again
  echo with:  Start-Worker.bat --reg-token llmop_reg_...
  start "" notepad "%LLMOP_HOME%\worker\config.json"
  pause
  exit /b 0
)
title llmop worker - close this window to stop
"%PY%" -m llmop worker run %*
if errorlevel 1 goto :fail
exit /b 0
:fail
pause
exit /b 1
