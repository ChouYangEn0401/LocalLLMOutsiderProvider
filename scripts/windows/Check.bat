@echo off
rem Checks config.yaml, lists the accounts and tests the connection to the LLM server(s).
setlocal
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] llmop is not installed yet. Double-click Setup.bat first.
  pause
  exit /b 1
)
"%VENV_PY%" -m llmop check %*
echo.
pause
