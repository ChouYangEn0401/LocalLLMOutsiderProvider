@echo off
rem Starts llmop for THIS computer only (http://127.0.0.1:8780) and opens the web page.
rem Closing this window stops it.
setlocal
call "%~dp0_env.bat" || exit /b 1
call "%~dp0_first_run.bat" || exit /b 1
title llmop - close this window to stop
"%PY%" -m llmop serve --open %*
if errorlevel 1 pause
