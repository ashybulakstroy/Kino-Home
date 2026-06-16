@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
for /f "tokens=2 delims==" %%a in ('findstr "SERVER_PORT" .env') do set PORT=%%a
if "%PORT%"=="" set PORT=8765
if not exist logs mkdir logs
start "LocaL-Kino" cmd /c "python -u stream_server.py 1>>logs\server.out.log 2>>logs\server.err.log"
timeout /t 3 /nobreak >nul
rem start http://localhost:%PORT%
