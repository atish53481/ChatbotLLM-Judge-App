<#
Puts an "LLM Judge" shortcut on the desktop: double-click it to start the
backend (if needed) and open the web app. Run once from this folder:
    powershell -ExecutionPolicy Bypass -File .\create-desktop-shortcut.ps1
#>
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$path = Join-Path $desktop "LLM Judge.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = Join-Path $root "start-llm-judge.bat"
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = (Join-Path $root "ChatbotExtension\icons\icon.ico") + ",0"
$shortcut.Description = "Start LLM Judge and open it in the browser"
$shortcut.WindowStyle = 7  # minimized: the backend's console stays out of the way
$shortcut.Save()
Write-Host "Created $path"
