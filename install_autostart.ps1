# install_autostart.ps1 — Register NORA to start at Windows logon via Task Scheduler.
#
# Run once from an elevated (Administrator) PowerShell:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\install_autostart.ps1
#
# To remove: Unregister-ScheduledTask -TaskName "NORA_Autostart" -Confirm:$false

$TaskName     = "NORA_Autostart"
$NoraDir      = $PSScriptRoot
$LauncherScript = Join-Path $NoraDir "start_nora.pyw"

# Find pythonw.exe alongside the current python.exe
$PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    Write-Error "python.exe not found in PATH. Activate your virtual environment first."
    exit 1
}
$PythonwExe = Join-Path (Split-Path $PythonExe) "pythonw.exe"
if (-not (Test-Path $PythonwExe)) {
    # Fallback to plain python (will show a brief console flash)
    $PythonwExe = $PythonExe
    Write-Warning "pythonw.exe not found; using python.exe (console window will appear briefly)."
}

Write-Host "Using interpreter : $PythonwExe"
Write-Host "Launcher script   : $LauncherScript"
Write-Host "Working directory : $NoraDir"

$Action = New-ScheduledTaskAction `
    -Execute  "`"$PythonwExe`"" `
    -Argument "`"$LauncherScript`"" `
    -WorkingDirectory $NoraDir

# Trigger: at logon for the current user
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings: no execution time limit, restart on failure up to 3 times
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false

# Run with highest privileges so keyboard hooks and audio work without UAC prompts
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action   $Action `
    -Trigger  $Trigger `
    -Settings $Settings `
    -RunLevel Highest `
    -Force

if ($?) {
    Write-Host ""
    Write-Host "NORA will now start automatically at each Windows logon." -ForegroundColor Green
    Write-Host "To start it right now without rebooting:"
    Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host ""
    Write-Host "To uninstall:"
    Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
} else {
    Write-Error "Task registration failed. Try running as Administrator."
}
