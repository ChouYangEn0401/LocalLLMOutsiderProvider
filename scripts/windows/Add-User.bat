@echo off
rem Adds an account to config.yaml (your comments in the file are kept).
rem A running llmop picks it up by itself; no restart needed.
rem With arguments they go straight to "llmop add-user", e.g.
rem   Add-User.bat bob --tokens-per-day 50000 --generate
setlocal
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] llmop is not installed yet. Double-click Setup.bat first.
  pause
  exit /b 1
)
if not "%~1"=="" (
  "%VENV_PY%" -m llmop add-user %*
  goto :done
)

echo Add an account. Usernames may use letters, digits and . _ @ -
set "NEWUSER="
set /p "NEWUSER=Username: "
if not defined NEWUSER goto :done
set "TPD="
set /p "TPD=Tokens per day - press Enter for the default in config.yaml, 0 = unlimited: "
set "EXTRA="
if defined TPD set "EXTRA=--tokens-per-day %TPD%"
choice /c YN /m "Generate a random password for them - N = you type one"
if errorlevel 2 (
  "%VENV_PY%" -m llmop add-user "%NEWUSER%" %EXTRA%
) else (
  "%VENV_PY%" -m llmop add-user "%NEWUSER%" %EXTRA% --generate
)

:done
echo.
pause
