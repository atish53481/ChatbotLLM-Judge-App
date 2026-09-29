@echo off
REM One-click start: backend (FastAPI:8202) + frontend (Vite:5173).
REM The backend reads GROQ_API_KEY and CHATBOT_MODEL from .env itself (app.py),
REM so no key is ever passed on a command line, where process lists show it.
cd /d "%~dp0"

if not exist ".env" (
    echo [WARN] .env missing. Copy .env.sample to .env and add your GROQ_API_KEY first.
    pause
    exit /b 1
)

if not exist "..\..\.venv\Scripts\python.exe" (
    echo [WARN] The project environment ..\..\.venv is missing. Run run-backend.bat in the project root once first.
    pause
    exit /b 1
)

if not exist "frontend\node_modules" (
    echo [WARN] frontend\node_modules is missing. Run "npm install" in the frontend folder once first.
    pause
    exit /b 1
)

REM Starting a second copy puts two servers on one port and breaks chat: open the running one instead.
netstat -ano | findstr /r /c:":8202 .*LISTENING" /c:":5173 .*LISTENING" >nul
if not errorlevel 1 (
    echo [INFO] The chatbot is already running ^(port 8202 or 5173 is in use^). Opening it.
    echo        To restart, close the "Backend (8202)" and "Frontend (5173)" windows first.
    start "" "http://localhost:5173"
    timeout /t 5 >nul
    exit /b 0
)

start "Backend (8202)" cmd /k "cd backend && ..\..\..\.venv\Scripts\python -m uvicorn app:app --reload --port 8202"
start "Frontend (5173)" cmd /k "cd frontend && npm run dev"

timeout /t 3 >nul
start "" "http://localhost:5173"
