@echo off
cd /d "%~dp0"
echo.
echo [FrameOS] 监听 0.0.0.0:8000 — 局域网内其它设备可访问
echo   本机浏览器: http://127.0.0.1:8000/app/
echo   局域网用户: http://192.168.31.15:8000/app/   （本机运行 ipconfig 查看 IPv4）
echo.
echo 若其它电脑打不开，请在 Windows「防火墙」里允许入站 TCP 8000，或以管理员运行:
echo   netsh advfirewall firewall add rule name="FrameOS 8000" dir=in action=allow protocol=TCP localport=8000
echo.
".venv\Scripts\uvicorn.exe" main:app --host 0.0.0.0 --port 8000 --reload
