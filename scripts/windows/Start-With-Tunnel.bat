@echo off
rem Starts llmop plus a Cloudflare quick tunnel: a public https://....trycloudflare.com
rem address, no router setup, no domain needed. The address changes every start;
rem it is printed below, shown on the web page, and saved in data\public_url.txt.
rem Closing this window stops both. Needs cloudflared, installed once with:
rem   winget install --id Cloudflare.cloudflared
rem If cloudflared lives elsewhere, set tunnel.cloudflared in config.yaml, set
rem tunnel.enabled: true and use Start.bat instead.
setlocal
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] llmop is not installed yet. Double-click Setup.bat first.
  pause
  exit /b 1
)

set "CLOUDFLARED="
for /f "delims=" %%P in ('where cloudflared 2^>nul') do if not defined CLOUDFLARED set "CLOUDFLARED=%%P"
if not defined CLOUDFLARED if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not defined CLOUDFLARED if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles%\cloudflared\cloudflared.exe"
if not defined CLOUDFLARED goto :no_cloudflared

:run
title llmop + Cloudflare tunnel - close this window to stop
echo cloudflared: %CLOUDFLARED%
echo The public address appears below in a few seconds.
echo.
"%VENV_PY%" -m llmop serve --tunnel %*
if errorlevel 1 pause
exit /b 0

:no_cloudflared
echo [X] cloudflared was not found. It is needed for the public https address.
echo     Install it once with:
echo.
echo       winget install --id Cloudflare.cloudflared
echo.
where winget >nul 2>nul || goto :manual
choice /c YN /m "Install it now with winget"
if errorlevel 2 goto :manual
winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :manual
if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not defined CLOUDFLARED if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles%\cloudflared\cloudflared.exe"
if defined CLOUDFLARED goto :run
echo [OK] Installed. Close this window and double-click Start-With-Tunnel.bat again.
pause
exit /b 0

:manual
echo     Or download it from
echo     https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
echo     then close this window and double-click Start-With-Tunnel.bat again.
pause
exit /b 1
