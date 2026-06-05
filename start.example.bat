@echo off
cd /d "%~dp0"
set ODDS_API_KEY=你的_The_Odds_API_金鑰

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":18011" ^| findstr LISTENING') do (
  echo 關閉舊程序 PID %%a ...
  taskkill /F /PID %%a >nul 2>&1
)

python -m uvicorn web_api:app --host 127.0.0.1 --port 18011
pause
