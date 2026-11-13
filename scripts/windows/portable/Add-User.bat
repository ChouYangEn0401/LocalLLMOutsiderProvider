@echo off
rem Adds a web/API account to config.yaml. Example: Add-User.bat friend1
setlocal
call "%~dp0_env.bat" || exit /b 1
set "NAME=%~1"
if "%NAME%"=="" set /p "NAME=New username: "
if "%NAME%"=="" exit /b 1
"%PY%" -m llmop add-user "%NAME%" %2 %3 %4 %5 %6 %7 %8 %9
pause
