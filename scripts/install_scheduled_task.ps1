# Registers an hourly Windows Scheduled Task that runs the FTC submitter.
#
# Task behaviour:
#   * Triggers every 1 hour, indefinitely, starting 5 minutes from now.
#   * Runs whether the user is logged in or not, in the background
#     (no visible window). Playwright launches Chrome headless from the
#     submitter itself.
#   * If a run is missed (laptop asleep, off, etc.) the task will fire
#     as soon as it can.
#   * If a run is still going when the next trigger hits, the new one is
#     skipped (don't double-submit).
#
# Re-running this script overwrites the existing task with the new
# settings, so it's safe to tweak and re-install.

[CmdletBinding()]
param(
    [string]$TaskName = "FTCReportAutomation-HourlySubmit",
    [string]$RepoRoot = "C:\Users\hello\Documents\FTCReportAutomation",
    [int]$IntervalMinutes = 60
)

$ErrorActionPreference = "Stop"

$batPath = Join-Path $RepoRoot "scripts\run_submit.bat"
if (-not (Test-Path $batPath)) {
    throw "Could not find $batPath. Did you pull the latest code?"
}

Write-Host "Installing scheduled task '$TaskName' ..."
Write-Host "  Repo:       $RepoRoot"
Write-Host "  Bat script: $batPath"
Write-Host "  Cadence:    every $IntervalMinutes minutes"

# Cmd-line invocation: hide window, run the .bat
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$batPath`"" `
    -WorkingDirectory $RepoRoot

# First trigger 5 min from now; then repeat every $IntervalMinutes forever
$startTime = (Get-Date).AddMinutes(5)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startTime `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Run under the current logged-in user so the saved Playwright session
# (storage_state.json under the user profile dir) is accessible.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Existing task found; unregistering first."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Hourly retry of FTC do-not-call submissions for any voicemails that are queued but not yet filed. Logs append to logs/auto_submit.log." | Out-Null

Write-Host ""
Write-Host "Task installed."
Write-Host "  First run:  $startTime"
Write-Host "  Then every: $IntervalMinutes minutes"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Start-ScheduledTask -TaskName $TaskName    # run it now"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
Write-Host "  Get-Content $RepoRoot\logs\auto_submit.log -Tail 80 -Wait"
