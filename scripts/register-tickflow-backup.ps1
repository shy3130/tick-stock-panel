[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ProjectRoot = 'D:\A股-v2',
    [string]$BackupRoot = 'D:\A股-v2-backups',
    [string]$TaskName = 'TickFlow Verified Backup'
)

$ErrorActionPreference = 'Stop'

$ResolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Python = Join-Path $ResolvedProject 'backend\.venv\Scripts\python.exe'
$Runner = Join-Path $ResolvedProject 'backend\scripts\tickflow_backup.py'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "TickFlow Python environment not found: $Python"
}
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Backup runner not found: $Runner"
}

$Arguments = '"{0}" backup --project-root "{1}" --backup-root "{2}"' -f `
    $Runner, $ResolvedProject, $BackupRoot
$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Arguments `
    -WorkingDirectory $ResolvedProject
$WeekdayTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At '19:00'
$SundayTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Sunday `
    -At '03:00'
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -WakeToRun
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register verified TickFlow backup schedule')) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger @($WeekdayTrigger, $SundayTrigger) `
        -Settings $Settings `
        -Principal $Principal `
        -Description 'Offline verified TickFlow snapshots; weekdays 19:00 and Sundays 03:00.' `
        -Force | Out-Null
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Description
