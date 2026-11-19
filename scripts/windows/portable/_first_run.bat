@echo off
rem Called by Start*.bat: creates config.yaml (asks for the admin password) on first use.
rem Optional: set LLMOP_BACKEND_TYPE (ollama, lmstudio, vllm, llamacpp, openai) and
rem LLMOP_MODEL before calling, or answer the questions below.
if exist "%LLMOP_CONFIG%" exit /b 0
echo ============================================================
echo  First start: creating config.yaml
echo ============================================================
set "INIT_ARGS="
set "BTYPE=%LLMOP_BACKEND_TYPE%"
if not defined BTYPE (
  echo Which LLM server do you use?
  echo   1 = Ollama      http://127.0.0.1:11434
  echo   2 = LM Studio   http://127.0.0.1:1234
  echo   3 = other OpenAI-compatible server - edit backends in config.yaml later
  choice /c 123 /m "Your choice"
  if errorlevel 3 set "BTYPE=openai"
  if not errorlevel 3 if errorlevel 2 set "BTYPE=lmstudio"
  if not errorlevel 2 set "BTYPE=ollama"
)
set "INIT_ARGS=--backend-type %BTYPE%"
set "MODEL=%LLMOP_MODEL%"
if not defined MODEL set /p "MODEL=The one model everybody uses, e.g. qwen3.5:9b - Enter = set it later: "
if defined MODEL set "INIT_ARGS=%INIT_ARGS% --model %MODEL%"
echo.
echo  Choose a password for the "admin" account (local console login).
echo  It is NOT shown while you type.
"%PY%" -m llmop init %INIT_ARGS%
if errorlevel 1 (
  echo [X] Setup did not finish. Run Start.bat again to retry.
  pause
  exit /b 1
)
echo.
echo  config.yaml created. Edit it with Notepad to add accounts, tiers or the model.
echo.
exit /b 0
