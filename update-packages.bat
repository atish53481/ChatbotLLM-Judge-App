@echo off
rem Brings .venv up to the newest versions requirements.txt allows (DeepEval,
rem FastAPI, the OpenAI client, ...). Called by run-backend.bat and
rem run-backend-service.bat on every start; set JUDGE_AUTO_UPDATE=0 to skip it.
rem
rem Safe by design: the installed set is saved first, the smoke tests run after
rem the upgrade, and if the upgrade or the tests fail the saved set is put back.
rem With no network the upgrade fails and the current versions are kept.
rem It never stops the start: it always exits 0.
cd /d "%~dp0"
if "%JUDGE_AUTO_UPDATE%"=="0" exit /b 0
if exist .env findstr /r /b /c:"JUDGE_AUTO_UPDATE *= *0" .env >nul && exit /b 0
if not exist ".venv\Scripts\python.exe" exit /b 0
rem A backend still serving 8765 locks package files: update on the next start.
netstat -ano | findstr /r /c:"127\.0\.0\.1:8765 .*LISTENING" >nul
if not errorlevel 1 (
    echo A backend is already running: skipping the package update.
    exit /b 0
)
set PY=.venv\Scripts\python.exe
set LAST_GOOD=.venv\last-good-packages.txt

echo Checking for package updates (set JUDGE_AUTO_UPDATE=0 to skip)...
"%PY%" -m pip freeze --exclude-editable > "%LAST_GOOD%" 2>nul || exit /b 0
"%PY%" -m pip install --quiet --disable-pip-version-check --upgrade --upgrade-strategy eager -r requirements.txt
if errorlevel 1 goto rollback

rem Nothing changed: done. Otherwise make sure the app still loads and its wiring works.
"%PY%" -m pip freeze --exclude-editable > "%LAST_GOOD%.new" 2>nul
fc /c "%LAST_GOOD%" "%LAST_GOOD%.new" >nul
if not errorlevel 1 (
    echo Packages are up to date.
    del "%LAST_GOOD%.new" >nul 2>&1
    exit /b 0
)
del "%LAST_GOOD%.new" >nul 2>&1
"%PY%" -m pytest -m smoke -q -p no:cacheprovider >nul 2>&1
if errorlevel 1 goto rollback
echo Packages updated:
"%PY%" -m pip show deepeval fastapi openai 2>nul | findstr /b /c:"Name:" /c:"Version:"
exit /b 0

:rollback
echo Package update failed or broke the smoke tests: restoring the previous versions.
"%PY%" -m pip install --quiet --disable-pip-version-check -r "%LAST_GOOD%"
exit /b 0
