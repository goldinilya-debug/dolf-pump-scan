# Registers a Windows Task Scheduler task that starts start_bot.bat automatically.
# Run in PowerShell from the executor folder:
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1            (start at your logon)
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -AtStartup (start at boot, even before logon; run PowerShell as Administrator)
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Remove    (delete the task)
param([switch]$AtStartup, [switch]$Remove, [string]$TaskName = "BingX Executor")

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $here "start_bot.bat"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed."
    exit 0
}
if (-not (Test-Path $bat)) { Write-Error "start_bot.bat not found in $here"; exit 1 }

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $here
$user = "$env:USERDOMAIN\$env:USERNAME"
if ($AtStartup) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT1M"   # give the network a minute after boot
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "BingX S06 executor (start_bot.bat)" -Force | Out-Null
Write-Host "Task '$TaskName' registered. Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
