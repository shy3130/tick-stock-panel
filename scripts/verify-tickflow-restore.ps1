[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Snapshot,
    [string]$ProjectRoot = 'D:\A股-v2',
    [string]$RestoreRoot = 'D:\A股-v2-restore-test',
    [ValidateRange(1024, 65535)]
    [int]$Port = 3028
)

$ErrorActionPreference = 'Stop'
$ResolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
$ResolvedSnapshot = (Resolve-Path -LiteralPath $Snapshot).Path
$Python = Join-Path $ResolvedProject 'backend\.venv\Scripts\python.exe'
$Runner = Join-Path $ResolvedProject 'backend\scripts\tickflow_backup.py'
$ProductionData = Join-Path $ResolvedProject 'data'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "TickFlow Python environment not found: $Python"
}

New-Item -ItemType Directory -Path $RestoreRoot -Force | Out-Null
$ResolvedRestoreRoot = (Resolve-Path -LiteralPath $RestoreRoot).Path.TrimEnd('\')
$RunId = '{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), ([guid]::NewGuid().ToString('N').Substring(0, 8))
$RestoreDir = Join-Path $ResolvedRestoreRoot $RunId
$ContainerName = "TickFlow_Backup_Verify_$($RunId.Replace('-', '_'))"
$Succeeded = $false

try {
    & $Python $Runner verify $ResolvedSnapshot
    if ($LASTEXITCODE -ne 0) { throw 'Snapshot manifest verification failed.' }

    & $Python $Runner restore-test $ResolvedSnapshot $RestoreDir --production-data $ProductionData
    if ($LASTEXITCODE -ne 0) { throw 'Isolated restore copy failed.' }

    $Forbidden = Get-ChildItem -LiteralPath $RestoreDir -Recurse -File | Where-Object {
        $_.Name -eq 'auth.json' -or
        $_.Name -like 'auth.json.*' -or
        $_.Name -eq 'secrets.json' -or
        $_.Name -like '.env*'
    }
    if ($Forbidden) {
        throw "Restore contains forbidden credential files: $($Forbidden.FullName -join ', ')"
    }

    $Image = docker inspect TickFlow_Stock_Panel --format '{{.Config.Image}}'
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Image)) {
        throw 'Cannot determine the production TickFlow image.'
    }

    docker run --detach `
        --name $ContainerName `
        --publish "127.0.0.1:$Port`:3018" `
        --env DATA_DIR=/app/data `
        --mount "type=bind,source=$RestoreDir,target=/app/data" `
        $Image | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Restore verification container failed to start.' }

    $Deadline = (Get-Date).AddMinutes(3)
    $Healthy = $false
    while ((Get-Date) -lt $Deadline) {
        try {
            $Response = Invoke-WebRequest `
                -Uri "http://127.0.0.1:$Port/health" `
                -UseBasicParsing `
                -TimeoutSec 3
            if ($Response.StatusCode -eq 200) {
                $Healthy = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $Healthy) {
        throw 'Restored TickFlow instance did not become healthy.'
    }

    $Succeeded = $true
    [pscustomobject]@{
        Snapshot = $ResolvedSnapshot
        RestoreHealth = 'healthy'
        Port = $Port
        CredentialsPresent = $false
    }
}
finally {
    docker rm --force $ContainerName 2>$null | Out-Null
    if ($Succeeded -and (Test-Path -LiteralPath $RestoreDir)) {
        $ResolvedRestoreDir = (Resolve-Path -LiteralPath $RestoreDir).Path
        if ([IO.Path]::GetDirectoryName($ResolvedRestoreDir) -ne $ResolvedRestoreRoot) {
            throw "Refusing to remove restore directory outside expected root: $ResolvedRestoreDir"
        }
        Remove-Item -LiteralPath $RestoreDir -Recurse -Force
    }
}
