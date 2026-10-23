@echo off
rem One-click start: opens the LLM Judge web app (http://127.0.0.1:8765/) in the
rem browser, starting the backend first when it is not running yet.
rem Close this window to stop the backend it started. Its output is also saved
rem under logs\ (see below).
cd /d "%~dp0"
set APP_URL=http://127.0.0.1:8765/

rem Already running (another window, or the scheduled task): just open the app.
powershell -NoProfile -Command "try { Invoke-WebRequest '%APP_URL%app/' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    start "" "%APP_URL%"
    exit /b 0
)

rem Opens the browser once the backend answers; the first run builds .venv,
rem which takes a few minutes, so it waits up to 15 minutes.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 900; $i++) { try { Invoke-WebRequest '%APP_URL%app/' -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process '%APP_URL%'; exit } catch { Start-Sleep -Seconds 1 } }"

rem Everything the backend prints (chatbot/judge traffic, errors) shows here and is
rem also appended to logs\llm-judge-YYYY-MM-DD.log.
if not exist logs mkdir logs
call "%~dp0run-backend.bat" 2>&1 | powershell -NoProfile -Command "$log = 'logs\llm-judge-' + (Get-Date -Format 'yyyy-MM-dd') + '.log'; Write-Host ('Log file: ' + (Join-Path (Get-Location) $log)); $input | ForEach-Object { Write-Host $_; Add-Content -LiteralPath $log -Value $_ -Encoding UTF8 }"
