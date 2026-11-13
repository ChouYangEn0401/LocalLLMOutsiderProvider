@echo off
rem Starts llmop plus a Cloudflare quick tunnel: a public https://....trycloudflare.com
rem address (changes every start; shown on the web page and in data\public_url.txt).
rem Needs cloudflared once:  winget install --id Cloudflare.cloudflared
setlocal
call "%~dp0_env.bat" || exit /b 1

set "CLOUDFLARED="
if exist "%ROOT%\cloudflared.exe" set "CLOUDFLARED=%ROOT%\cloudflared.exe"
if not defined CLOUDFLARED for /f "delims=" %%P in ('where cloudflared 2^>nul') do if not defined CLOUDFLARED set "CLOUDFLARED=%%P"
if not defined CLOUDFLARED if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not defined CLOUDFLARED if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles%\cloudflared\cloudflared.exe"
if not defined CLOUDFLARED goto :no_cloudflared

call "%~dp0_first_run.bat" || exit /b 1
title llmop + Cloudflare tunnel - close this window to stop
echo cloudflared: %CLOUDFLARED%
echo The public address appears below in a few seconds.
"%PY%" -m llmop serve --tunnel --open %*
if errorlevel 1 pause
exit /b 0

:no_cloudflared
echo [!] cloudflared is not installed. It creates the public https address.
choice /c YN /m "Install it now with winget"
if errorlevel 2 goto :manual
winget install --id Cloudflare.cloudflared
echo.
echo Close this window and double-click Start-With-Tunnel.bat again.
pause
exit /b 0
:manual
echo Install it later with:  winget install --id Cloudflare.cloudflared
echo (or put cloudflared.exe next to this file). Start.bat works without it.
pause
exit /b 1
