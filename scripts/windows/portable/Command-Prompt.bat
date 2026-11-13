@echo off
rem Opens a console where "llmop ..." commands work (llmop usage, llmop keys, ...).
call "%~dp0_env.bat" || exit /b 1
doskey llmop="%PY%" -m llmop $*
title llmop console
echo Type commands like:  llmop usage    llmop keys    llmop add-user NAME    llmop --help
cmd /k
