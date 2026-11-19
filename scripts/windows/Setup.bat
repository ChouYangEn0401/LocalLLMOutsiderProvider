@echo off
rem First-time setup: finds Python 3.10+, creates .venv, installs llmop and
rem creates config.yaml (asks for the admin password). Safe to run again.
setlocal
call "%~dp0_env.bat"
title llmop setup
echo === LocalLLMOutsiderProvider - llmop setup ===
echo.

set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [X] Python 3.10 or newer was not found.
  echo     Download it from https://www.python.org/downloads/windows/
  echo     On the first installer screen tick "Add python.exe to PATH",
  echo     then double-click Setup.bat again.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo [OK] found %%V

if not exist "%VENV_PY%" (
  echo ... creating a private Python environment in .venv
  %PY% -m venv "%ROOT%\.venv" || goto :fail
)
echo ... installing llmop and the packages it needs - the first time takes 1-2 minutes
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip

if exist "%ROOT%\pyproject.toml" if exist "%ROOT%\src\llmop" goto :install_src
set "WHEEL="
for %%W in ("%ROOT%\dist\*.whl") do set "WHEEL=%%~fW"
if defined WHEEL goto :install_wheel
echo [X] Nothing to install: no source code - pyproject.toml - and no dist\*.whl found in
echo     %ROOT%
goto :fail

:install_src
"%VENV_PY%" -m pip install --disable-pip-version-check -q -e "%ROOT%[secure]" || goto :fail
goto :installed

:install_wheel
echo ... installing %WHEEL%
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade "%WHEEL%[secure]" || goto :fail

:installed
"%VENV_PY%" -m llmop --version || goto :fail

if exist "%ROOT%\config.yaml" (
  echo [OK] config.yaml already exists - kept as it is.
) else (
  echo.
  echo Now choose the password for the web login "admin" - at least 8 characters.
  echo Nothing is shown while you type; that is normal.
  "%VENV_PY%" -m llmop init || goto :fail
)

echo.
echo === Checking the configuration and your LLM server ===
"%VENV_PY%" -m llmop check
echo.
echo Setup finished. Next:
echo   Start.bat              start llmop for this computer only
echo   Start-With-Tunnel.bat  start llmop with a public https address
echo   Add-User.bat           add an account for a friend
echo   Check.bat              check the settings and the LLM server again
pause
exit /b 0

:fail
echo.
echo [X] Setup failed. Please send a screenshot of the messages above to whoever helps you.
pause
exit /b 1
