#Requires -RunAsAdministrator
<#
Registers the "LLM Judge Backend" scheduled task, so the backend on
http://127.0.0.1:8765 starts when Windows boots and keeps running while you are
logged off. run-backend-service.bat restarts it 30 seconds after any crash.

Run once, in PowerShell opened with "Run as administrator", from this folder:
    powershell -ExecutionPolicy Bypass -File .\install-backend-task.ps1

Windows asks for your account password: a task that runs while you are logged
off needs it (for a Microsoft account, the account password, not the PIN).
Nothing runs while the laptop sleeps, hibernates or is shut down.
Remove the task again with uninstall-backend-task.ps1.
#>
param([string]$TaskName = "LLM Judge Backend")
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$service = Join-Path $root "run-backend-service.bat"
if (-not (Test-Path (Join-Path $root ".venv\Scripts\python.exe"))) {
    throw "The project environment (.venv) is missing: run run-backend.bat once first, then run this again."
}

# A backend already running from this folder (a window, or an older task) would
# keep port 8765; stop it so the task's backend takes over.
. (Join-Path $root "backend-task-common.ps1")
Stop-LlmJudgeBackend

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$service`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$user = "$env:USERDOMAIN\$env:USERNAME"
$credential = Get-Credential -UserName $user -Message "Your Windows password, so the LLM Judge backend can run while you are logged off"
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $credential.UserName -Password $credential.GetNetworkCredential().Password -RunLevel Limited `
    -Description "Keeps the LLM Judge backend (http://127.0.0.1:8765) running, also while logged off. Log: $root\logs\backend.log" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Registered and started '$TaskName'. Checking the backend..."
$up = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 2
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/status" -TimeoutSec 3 | Out-Null
        $up = $true
        break
    } catch { }
}
if ($up) {
    Write-Host "The backend is up on http://127.0.0.1:8765. It will start with Windows and keep running while you are logged off."
} else {
    Write-Host "The task is registered but the backend did not answer within a minute. See $root\logs\backend.log."
}
