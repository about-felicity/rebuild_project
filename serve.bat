@echo off
REM 仅本机可连。要让局域网访问请用 serve-lan.bat（监听 0.0.0.0）
cd /d "%~dp0"
".venv\Scripts\uvicorn.exe" main:app --host 127.0.0.1 --port 8000 --reload
