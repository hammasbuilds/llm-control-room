@echo off
rem Starts LLM Control Room and opens the browser. Close this window to stop the server.
set VIRTUAL_ENV=
cd /d "%~dp0"
title LLM Control Room
uv run llm-control-room %*
if errorlevel 1 pause
