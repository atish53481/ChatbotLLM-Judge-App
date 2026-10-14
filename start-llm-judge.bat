@echo off
rem One-click start: opens the LLM Judge web app (http://127.0.0.1:8765/) in the
rem browser, starting the backend first when it is not running yet.
rem Close this window to stop the backend it started.
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

call "%~dp0run-backend.bat"
