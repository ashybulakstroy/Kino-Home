@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
for /f "tokens=2 delims==" %%a in ('findstr "SERVER_PORT" .env') do set PORT=%%a
if "%PORT%"=="" set PORT=8765
if not exist logs mkdir logs
start "Kino Gallery" cmd /c "python -u stream_server.py 1>>logs\server.out.log 2>>logs\server.err.log"
timeout /t 3 /nobreak >nul
echo Kino Gallery: http://localhost:%PORT%
echo Logs: logs\server.out.log and logs\server.err.log
powershell -NoProfile -NoExit -Command "Get-Content -Path 'logs\server.out.log','logs\server.err.log' -Tail 80 -Wait"
rem start http://localhost:%PORT%
