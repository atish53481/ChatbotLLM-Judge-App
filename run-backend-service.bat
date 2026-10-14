@echo off
rem Keeps the LLM Judge backend running with no window and no prompts, for the
rem "LLM Judge Backend" scheduled task (see install-backend-task.ps1), which
rem starts it at boot and keeps it alive while you are logged off.
rem
rem It loops: if the backend stops or crashes it is started again 30 seconds
rem later. While something else already serves port 8765 (run-backend.bat in a
rem window, say) it just waits. Output goes to logs\backend.log.
cd /d "%~dp0"
if not exist logs mkdir logs
set DEEPEVAL_TELEMETRY_OPT_OUT=1
set PYTHONUNBUFFERED=1
set PYTHONUTF8=1

:loop
rem Keep one previous log; start a fresh one past about 5 MB.
if exist logs\backend.log for %%F in (logs\backend.log) do if %%~zF GTR 5000000 move /y logs\backend.log logs\backend.old.log >nul
if not exist ".venv\Scripts\python.exe" (
    echo [%date% %time%] .venv is missing: run run-backend.bat once to create it.>> logs\backend.log
    goto wait
)
netstat -ano | findstr /r /c:"127\.0\.0\.1:8765 .*LISTENING" >nul
if not errorlevel 1 goto wait
echo [%date% %time%] Starting the LLM Judge backend.>> logs\backend.log
".venv\Scripts\python.exe" -m uvicorn backend.dashboard.app:app --host 127.0.0.1 --port 8765 >> logs\backend.log 2>&1
echo [%date% %time%] The backend stopped (exit code %errorlevel%); restarting in 30 seconds.>> logs\backend.log

:wait
rem ping waits without needing a console (timeout.exe fails in a scheduled task).
ping -n 31 127.0.0.1 >nul
goto loop
