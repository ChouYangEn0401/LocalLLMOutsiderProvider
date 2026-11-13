@echo off
rem Called by Start*.bat: creates config.yaml (asks for the admin password) on first use.
if exist "%LLMOP_CONFIG%" exit /b 0
echo ============================================================
echo  First start: creating config.yaml
echo  Choose a password for the "admin" account (web login).
echo  It is NOT shown while you type.
echo ============================================================
"%PY%" -m llmop init
if errorlevel 1 (
  echo [X] Setup did not finish. Run Start.bat again to retry.
  pause
  exit /b 1
)
echo.
echo  config.yaml created. Edit it with Notepad to add accounts or change quotas.
echo.
exit /b 0
