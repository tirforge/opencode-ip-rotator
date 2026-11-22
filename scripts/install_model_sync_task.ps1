# Register a Windows Scheduled Task that refreshes the `oracle` provider's
# model list and effort variants from the official OpenCode catalog every
# six hours.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_model_sync_task.ps1

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $repo "scripts\sync_oracle_models.py"
$logDir = Join-Path $repo "data\logs"
$log = Join-Path $logDir "model_sync.log"

if (-not (Test-Path $python)) { throw "Python venv not found: $python" }
if (-not (Test-Path $script)) { throw "Sync script not found: $script" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$stamp = 'echo [%DATE% %TIME%] --- model sync --- >> "{0}" & ' -f $log
$argument = '/c ' + $stamp + '"' + $python + '" "' + $script + '" >> "' + $log + '" 2>&1'

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Hours 6) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "OpenCode Oracle Model Sync" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Sync oracle provider models + reasoning efforts from the official OpenCode catalog every 6 hours." `
    -Force | Out-Null

Write-Host "Registered task 'OpenCode Oracle Model Sync' (every 6 hours)."
Write-Host "Log: $log"
Write-Host "Run now: Start-ScheduledTask -TaskName 'OpenCode Oracle Model Sync'"
