@echo off
cd /d "%~dp0"
".venv\Scripts\uvicorn.exe" main:app --host 127.0.0.1 --port 8000 --reload
