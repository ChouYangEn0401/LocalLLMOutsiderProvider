@echo off
rem Checks config.yaml and whether Ollama / vLLM can be reached.
setlocal
call "%~dp0_env.bat" || exit /b 1
"%PY%" -m llmop check
echo.
"%PY%" -m llmop usage
pause
