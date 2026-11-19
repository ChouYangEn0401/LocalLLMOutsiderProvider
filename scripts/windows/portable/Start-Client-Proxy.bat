@echo off
rem Encrypted mode, on the USER's computer: a local OpenAI-compatible proxy at
rem http://127.0.0.1:8788/v1 that encrypts every request for the pinned worker.
rem Point your program (e.g. SYNOX: local / other) at http://127.0.0.1:8788
rem
rem First time:  1. this file prints your signing public_key and sig_key_id; paste the
rem                 public_key when you create an encrypted key on the account website
rem              2. create data\llmop_home\client\config.json (template is printed
rem                 below): gateway_url = https://<api-host>/<secret>, api_key,
rem                 pinned_workers = the worker's public_key (from the worker window)
setlocal
call "%~dp0_env.bat" || exit /b 1
"%PY%" -m llmop client keygen
if errorlevel 1 goto :fail
echo.
title llmop client proxy 127.0.0.1:8788 - close this window to stop
"%PY%" -m llmop client proxy
if errorlevel 1 goto :fail
exit /b 0
:fail
pause
exit /b 1
