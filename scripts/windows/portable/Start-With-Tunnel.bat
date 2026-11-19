@echo off
rem Starts the whole llmop node plus TWO Cloudflare quick tunnels:
rem   tunnel "ui"  -> account website  https://....trycloudflare.com
rem   tunnel "api" -> API link         https://....trycloudflare.com/<secret>/v1
rem Quick-tunnel addresses change at every start; both are shown in the local
rem console and in this window. Rotate-API-Link.bat gives a new API link at any time.
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
title llmop node + 2 Cloudflare tunnels - close this window to stop
echo cloudflared: %CLOUDFLARED%
echo The public addresses appear below in a few seconds.
"%PY%" -m llmop serve --tunnel --open %*
if errorlevel 1 pause
exit /b 0

:no_cloudflared
echo [!] cloudflared is not installed. It creates the public https addresses.
choice /c YN /m "Install it now with winget"
if errorlevel 2 goto :manual
winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
echo.
echo Close this window and double-click Start-With-Tunnel.bat again.
pause
exit /b 0
:manual
echo Install it later with:  winget install --id Cloudflare.cloudflared
echo (or put cloudflared.exe next to this file). Start.bat works without it.
pause
exit /b 1
