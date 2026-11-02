param(
    [string]$TaskName = "TodayBrief",
    [string]$ProjectDir = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$Time = "07:00"
)

$ErrorActionPreference = "Stop"

# Create a user-level daily task with schtasks, then add WakeToRun and
# StartWhenAvailable. This path works without elevation on this machine;
# Register-ScheduledTask with an AtStartup trigger required admin.
$action = "cmd /c cd /d `"$ProjectDir`" && .venv\Scripts\today-brief.exe"

try {
    schtasks /create /tn $TaskName /tr $action /sc daily /st $Time /f | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /create failed with exit code $LASTEXITCODE"
    }

    $settings = New-ScheduledTaskSettingsSet `
        -WakeToRun `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null

    Write-Host "Registered $TaskName at $Time with WakeToRun + StartWhenAvailable."
    Write-Host "Run now: schtasks /run /tn $TaskName"
    Write-Host "Status: Get-ScheduledTaskInfo -TaskName $TaskName"
}
catch {
    Write-Error "Registration failed: $($_.Exception.Message)"
    Write-Host "If this is an access-denied error, open PowerShell as Administrator and rerun:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit 1
}
