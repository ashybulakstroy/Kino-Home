@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
for /f "tokens=2 delims==" %%a in ('findstr "SERVER_PORT" .env') do set PORT=%%a
if "%PORT%"=="" set PORT=8765
start "LocaL-Kino" python stream_server.py
timeout /t 3 /nobreak >nul
start http://localhost:%PORT%
