@echo off
rem Live view of what the judge is doing: each question sent to the chatbot and its
rem reply, each judge call (model, time, tokens, output), metric start/finish, and
rem errors / rate limits. Reads today's logs\traffic-YYYY-MM-DD.log, which the
rem backend writes however it was started (start-llm-judge.bat, run-backend.bat,
rem service). Close this window (or Ctrl+C) to stop watching.
cd /d "%~dp0"
title LLM Judge - live traffic
powershell -NoProfile -Command "$log = 'logs\traffic-' + (Get-Date -Format 'yyyy-MM-dd') + '.log'; while (-not (Test-Path $log)) { Write-Host ('Waiting for ' + $log + ' - start the backend first'); Start-Sleep -Seconds 5 }; Write-Host ('Watching ' + $log + ' (recent traffic, then live)') -ForegroundColor Cyan; Get-Content -LiteralPath $log -Wait -Tail 400 -Encoding UTF8 | Where-Object { $_ -match 'llm_judge\.traffic|ERROR|WARNING|Traceback|429' } | ForEach-Object { $line = $_ -replace '^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ \w+ llm_judge\.traffic: ', '$1  '; if ($line -match 'ERROR|Traceback|429|failed') { Write-Host $line -ForegroundColor Red } elseif ($line -match '<- judge') { Write-Host $line -ForegroundColor Green } elseif ($line -match '-> judge') { Write-Host $line -ForegroundColor DarkGreen } elseif ($line -match 'chatbot') { Write-Host $line -ForegroundColor Yellow } else { Write-Host $line } }"
