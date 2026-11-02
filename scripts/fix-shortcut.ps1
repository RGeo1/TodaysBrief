$ErrorActionPreference = "Stop"

$desktop = [Environment]::GetFolderPath("Desktop")
$root = (Resolve-Path "$PSScriptRoot\..").Path
$target = Join-Path $root "dist\TodayBrief.exe"
$workdir = $root
$icon = Join-Path $root "assets\today_brief.ico"

if (-not (Test-Path $target)) {
    throw "Standalone exe not found: $target. Run scripts\build-app.ps1 first."
}

$cnName = ([char]0x4ECA) + ([char]0x65E5) + ([char]0x5E55) + ([char]0x50DA) + ".lnk"
$names = @($cnName, "TodayBrief.lnk")

$ws = New-Object -ComObject WScript.Shell
foreach ($name in $names) {
    $link = Join-Path $desktop $name
    $s = $ws.CreateShortcut($link)
    $s.TargetPath = $target
    $s.WorkingDirectory = $workdir
    $s.IconLocation = "$icon,0"
    $s.Description = "TodayBrief - Calendar Daily Brief"
    $s.WindowStyle = 1
    $s.Save()

    $check = $ws.CreateShortcut($link)
    Write-Host "LINK=$link"
    Write-Host "  TARGET=$($check.TargetPath)"
    Write-Host "  TARGET_EXISTS=$(Test-Path $check.TargetPath)"
    Write-Host "  WORKDIR=$($check.WorkingDirectory)"
    Write-Host "  ICON=$($check.IconLocation)"
}
