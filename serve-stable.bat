@echo off
cd /d "%~dp0"
echo.
echo [FrameOS] 稳定模式：无 --reload，不会因 data/app.db 写入而重启进程。
echo   本机: http://127.0.0.1:8000/app/
echo   探活: http://127.0.0.1:8000/api/health
echo.
".venv\Scripts\uvicorn.exe" main:app --host 0.0.0.0 --port 8000
