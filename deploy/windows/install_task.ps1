<#
Registers a Scheduled Task that starts the agent supervisor at logon and again
every 5 minutes if it is not running, so a reboot, a crash or a killed window
never leaves the inbox unanswered.

    powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
    powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1 -Uninstall
#>
param([switch]$Uninstall)

$name = "UARB Agent"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$cmd = Join-Path $root "deploy\windows\run_agent.cmd"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" | Where-Object { $_.CommandLine -like "*run_agent.cmd*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "removed $name"
    exit 0
}

# The supervisor exits only when killed, so 'start if not running' is safe: a second
# instance is refused by Task Scheduler's IgnoreNew policy.
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$cmd`"" -WorkingDirectory $root
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -Hidden -WakeToRun
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger @($logon, $every5) -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "registered and started '$name' (logs in $root\logs)"
