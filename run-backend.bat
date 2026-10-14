@echo off
rem Starts the LLM Judge backend on http://127.0.0.1:8765 (the extension expects this address).
rem Runs in the project's own environment (.venv), which is created on the first run.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating the project environment in .venv - first run only, this takes a few minutes...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)
set DEEPEVAL_TELEMETRY_OPT_OUT=1
set PYTHONUNBUFFERED=1
rem DeepEval's Synthesizer prints emoji; without UTF-8, Windows' cp1252 codec
rem fails on them when output is redirected (logs, background runs).
set PYTHONUTF8=1
rem An LLM Judge backend left running from an earlier start holds port 8765 and
rem runs old code: stop it. Anything else on the port is left alone.
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess); if ($p.CommandLine -like '*backend.dashboard.app*') { Write-Host 'Stopping the LLM Judge backend that was already running...'; Stop-Process -Id $p.ProcessId -Force; $parent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p.ParentProcessId); if ($parent.CommandLine -like '*backend.dashboard.app*') { Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue } } }"
echo Starting LLM Judge backend on http://127.0.0.1:8765 - please wait...
".venv\Scripts\python.exe" -m uvicorn backend.dashboard.app:app --host 127.0.0.1 --port 8765 || goto :error
goto :eof

:error
echo.
echo Backend failed or stopped. See the messages above.
echo (If it says the port is in use, another program holds port 8765 - close it first.)
pause
exit /b 1
