@echo off
rem Gives the API link a new secret path. The old link stops working at once,
rem API keys stay valid; users see the new link on the account website.
rem   Rotate-API-Link.bat                 new secret path only (instant)
rem   Rotate-API-Link.bat --change-host   also a new host name (restarts only the
rem                                       API tunnel of the running node)
rem   Rotate-API-Link.bat --grace 600     the old link keeps working 10 more minutes
setlocal
call "%~dp0_env.bat" || exit /b 1
if not exist "%LLMOP_CONFIG%" (
  echo [X] config.yaml does not exist yet. Double-click Start.bat first.
  pause
  exit /b 1
)
"%PY%" -m llmop tunnel rotate-api %*
pause
