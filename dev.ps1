# tickflow-stock-panel - one-shot launcher for backend + frontend (Windows / PowerShell)
#
# Usage:
#   .\dev.ps1
#   .\dev.ps1 -BackendPort 8000 -FrontendPort 5173
#   .\dev.ps1 -NoReload          # 内测/长时间运行: 不启用 uvicorn --reload
#   .\dev.ps1 -Force             # 已有实例时先结束它再启动
#   $env:BACKEND_PORT='8000'; .\dev.ps1
#
# 进程输出直接重定向到 data\logs\*.log, 启动器只做有界 tail, 内存占用恒定。
# 停止请用 Ctrl-C (会结束整棵进程树); 直接点窗口 X 不会走到清理逻辑。
#
# If you see "running scripts is disabled":
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

[CmdletBinding()]
param(
    [int]$BackendPort  = 0,
    [int]$FrontendPort = 0,
    # 检测到已有后端实例时强制结束它再启动。默认拒绝启动, 避免多实例叠加。
    [switch]$Force,
    # 不启用 uvicorn --reload。内测/长时间运行时建议开启: 改文件不再触发
    # 进程重启, 也就不会反复做 1GB+ 的 enriched 全量预热。
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'

$Root        = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir  = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$EnvFile     = Join-Path $Root '.env'

# Read only launcher-owned keys. Do not execute .env as PowerShell code.
function Read-DotEnvValue($Path, $Name) {
    if (-not (Test-Path $Path)) { return $null }
    $escaped = [Regex]::Escape($Name)
    foreach ($line in Get-Content $Path) {
        if ($line -match "^\s*$escaped\s*=\s*(.*?)\s*$") {
            $value = $Matches[1].Trim()
            $value = ($value -replace '\s+#.*$', '').Trim()
            if ($value.Length -ge 2 -and
                (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                 ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                return $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

$DotEnvHost = Read-DotEnvValue $EnvFile 'HOST'
$DotEnvPort = Read-DotEnvValue $EnvFile 'PORT'
$BindAddress = if ($env:HOST) { $env:HOST } elseif ($DotEnvHost) { $DotEnvHost } else { '0.0.0.0' }
$DisplayHost = if ($BindAddress -in @('0.0.0.0', '::')) { 'localhost' } else { $BindAddress }

# Port precedence: CLI arg > BACKEND_PORT env > PORT env > .env PORT > default
if ($BackendPort -le 0) {
    if ($env:BACKEND_PORT) { $BackendPort = [int]$env:BACKEND_PORT }
    elseif ($env:PORT)     { $BackendPort = [int]$env:PORT }
    elseif ($DotEnvPort)   { $BackendPort = [int]$DotEnvPort }
    else                   { $BackendPort = 3018 }
}
if ($FrontendPort -le 0) { $FrontendPort = if ($env:FRONTEND_PORT) { [int]$env:FRONTEND_PORT } else { 3011 } }

# 单实例守卫: 重复运行 dev.ps1 会各自拉起一套后端, 每个实例常驻 1GB+;
# 靠端口检测杀不干净 (--reload 的父进程持有 socket 并会重生子进程), 因此改为
# 启动前显式识别已有实例: 默认拒绝启动, -Force 时才整树结束。
# 只匹配本仓库的 venv 解释器, 避免误伤其他项目里同名模块 app.main:app 的进程。
function Stop-ExistingInstance {
    $venvPython = Join-Path $BackendDir '.venv\Scripts\python.exe'
    $existing = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -and
            $_.CommandLine -like '*uvicorn*app.main:app*' -and
            ($_.CommandLine -like "*$venvPython*" -or
             ($_.CommandLine -like '*.venv\Scripts\python*' -and
              $_.ExecutablePath -like "$BackendDir*"))
        })
    if ($existing.Count -eq 0) { return $true }
    $ids = @($existing | ForEach-Object { $_.ProcessId })
    if (-not $Force) {
        Log-Warn "检测到已有后端实例 (PID: $($ids -join ', ')), 本次启动取消"
        Log-Warn '如需重启, 重新运行并加 -Force, 或先执行:'
        Log-Warn "  Stop-Process -Id $($ids -join ',') -Force"
        return $false
    }
    Log-Warn "结束已有后端实例 (PID: $($ids -join ', '))"
    foreach ($p in $ids) {
        $null = & cmd /c "taskkill /F /T /PID $p 2>nul"
        try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}
    }
    Start-Sleep -Milliseconds 500
    return $true
}

# Force UTF-8 console output so child process logs aren't garbled
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
} catch {}

function Log-Info($m) { Write-Host "[dev] $m" -ForegroundColor DarkGray }
function Log-Ok  ($m) { Write-Host "[dev] $m" -ForegroundColor Green }
function Log-Warn($m) { Write-Host "[dev] $m" -ForegroundColor Yellow }
function Log-Err ($m) { Write-Host "[dev] $m" -ForegroundColor Red }

# ===== 1. Dependency check =====
function Require-Cmd($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Log-Err "$cmd not found"
        Write-Host "       install via: $hint"
        exit 1
    }
}

Require-Cmd 'uv'   'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   OR   winget install --id=astral-sh.uv'
Require-Cmd 'pnpm' 'npm i -g pnpm   OR   corepack enable; corepack prepare pnpm@9 --activate'

# ===== 2. Port check - kill anything listening on the target ports =====
function Free-Port($name, $port) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if (-not $conns) { return }
    $pids = @($conns.OwningProcess | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    if ($pids.Count -eq 0) { return }

    # Filter to PIDs that still exist as running processes.
    # A zombie TCP endpoint can linger after the process is already dead.
    $alive = @($pids | Where-Object {
        try { [System.Diagnostics.Process]::GetProcessById($_) | Out-Null; $true }
        catch { $false }
    })

    if ($alive.Count -eq 0) {
        # All processes are dead but kernel still holds the socket (zombie endpoint).
        # On Windows this can linger for minutes, but uvicorn/vite can still bind
        # via SO_REUSEADDR — no point waiting, just proceed.
        Log-Warn "port ${port} (${name}) - zombie socket (processes gone), starting anyway"
        return
    }

    Log-Warn "port $port ($name) is in use, killing PID: $($alive -join ', ')"
    # Use taskkill /F /T to kill the entire process tree (parent + children),
    # not just the parent. Stop-Process only kills one process, leaving child
    # processes (e.g. uvicorn spawned by uv) as orphans holding the socket.
    foreach ($p in $alive) {
        # Suppress stderr properly for Windows PowerShell (5.x)
        $null = & cmd /c "taskkill /F /T /PID $p 2>nul"
        # Fallback: if taskkill failed, try Stop-Process
        try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}
    }

    # Wait up to 5 seconds for the kernel to release the TCP endpoint
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 500
        $still = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        if (-not $still) {
            Log-Ok "port $port freed"
            return
        }
    }

    # Port still stuck — process might be dead with zombie socket
    $anyAlive = $still | Where-Object {
        try { [System.Diagnostics.Process]::GetProcessById($_.OwningProcess) | Out-Null; $true }
        catch { $false }
    }
    if ($anyAlive.Count -eq 0) {
        Log-Warn "port ${port} - processes gone but socket lingers, starting anyway"
    } else {
        Log-Err "port ${port} still in use by live process(es). Inspect: Get-NetTCPConnection -LocalPort ${port}"
        exit 1
    }
}

if (-not (Stop-ExistingInstance)) { exit 1 }

Free-Port 'backend'  $BackendPort
Free-Port 'frontend' $FrontendPort

# ===== 3. Dependency install =====
# Match Docker's whitespace-separated BACKEND_EXTRAS behavior so old CPUs can
# select Polars' rtcompat runtime before the backend starts.
$BackendExtras = $env:BACKEND_EXTRAS
if (-not (Test-Path Env:BACKEND_EXTRAS)) {
    $BackendExtras = Read-DotEnvValue $EnvFile 'BACKEND_EXTRAS'
}

$BackendExtraArgs = @()
if (-not [string]::IsNullOrWhiteSpace($BackendExtras)) {
    foreach ($extra in ($BackendExtras -split '\s+' | Where-Object { $_ })) {
        $BackendExtraArgs += '--extra', $extra
    }
}

if (-not (Test-Path (Join-Path $BackendDir '.venv')) -or $BackendExtraArgs.Count) {
    if ($BackendExtraArgs.Count) {
        Log-Info "syncing Python deps with extras: $BackendExtras"
    } else {
        Log-Info 'first run - installing Python deps (1-2 min)...'
    }
    Push-Location $BackendDir
    try { & uv sync --frozen @BackendExtraArgs } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Log-Err 'uv sync failed'; exit 1 }
    Log-Ok 'backend deps installed'
}

if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
    Log-Info 'first run - installing Node deps...'
    Push-Location $FrontendDir
    try { & pnpm install } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Log-Err 'pnpm install failed'; exit 1 }
    Log-Ok 'frontend deps installed'
}

# ===== 4. Banner (ASCII so it renders on any codepage) =====
Write-Host ''
Write-Host '+----------------------------------------------+' -ForegroundColor Blue
Write-Host '|  tickflow-stock-panel                        |' -ForegroundColor Blue
Write-Host '|                                              |' -ForegroundColor Blue
Write-Host "|  backend   http://${DisplayHost}:$BackendPort"  -ForegroundColor Blue
Write-Host "|  frontend  http://${DisplayHost}:$FrontendPort" -ForegroundColor Blue
Write-Host '|                                              |' -ForegroundColor Blue
Write-Host '|  Ctrl-C closes both                          |' -ForegroundColor Blue
Write-Host '+----------------------------------------------+' -ForegroundColor Blue
Write-Host ''

# ===== 5. Launch processes =====
# 输出由操作系统直接重定向到日志文件, PowerShell 只持有 PID, 不参与数据搬运。
#
# 旧实现用 Start-Job + 每 150ms Receive-Job 轮询: Start-Job 会把每一行原生输出
# 包装成带 job/runspace 元数据的 PSObject 存进 job 输出集合。当生产快于消费
# (控制台滚动缓冲、窗口最小化、RDP 断开、日志音量突增) 时对象持续堆积且不及时
# 释放 —— 实测开盘跑到中午, 启动器单个 powershell.exe 可占十几 GB。改为文件
# 重定向后, 启动器内存与日志总量无关; 控制台只做「读新增字节」的有界 tail。
$LogDir = Join-Path $Root 'data\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$BackendOutLog  = Join-Path $LogDir 'backend.out.log'
$BackendErrLog  = Join-Path $LogDir 'backend.err.log'
$FrontendOutLog = Join-Path $LogDir 'frontend.out.log'
$FrontendErrLog = Join-Path $LogDir 'frontend.err.log'

# 每次启动重置本会话日志 (后端自身的 data/backend.log 已有轮转, 这里只放转发副本)
foreach ($f in @($BackendOutLog, $BackendErrLog, $FrontendOutLog, $FrontendErrLog)) {
    Remove-Item $f -Force -ErrorAction SilentlyContinue
}

$env:PYTHONUNBUFFERED = '1'
# Start-Process 的 -ArgumentList 按空格拼接, 含空格的路径必须自带引号
function Quote-Arg($value) {
    if ($value -match '[\s"]') { return '"' + ($value -replace '"', '\"') + '"' }
    return $value
}

$BackendArgs = @('-m', 'uvicorn', 'app.main:app')
if (Test-Path $EnvFile) { $BackendArgs += @('--env-file', (Quote-Arg $EnvFile)) }
if (-not $NoReload) { $BackendArgs += '--reload' }
$BackendArgs += @('--host', $BindAddress, '--port', [string]$BackendPort)

$env:BACKEND_HOST = $BindAddress
$env:BACKEND_PORT = [string]$BackendPort
$pnpmExe = (Get-Command 'pnpm.cmd' -ErrorAction SilentlyContinue).Source
if (-not $pnpmExe) { $pnpmExe = (Get-Command 'pnpm' -ErrorAction SilentlyContinue).Source }
if (-not $pnpmExe) {
    Log-Err 'pnpm not found'
    exit 1
}

# ===== 6. Cleanup =====
$script:cleaning = $false
function Cleanup-All {
    if ($script:cleaning) { return }
    $script:cleaning = $true
    Write-Host ''
    Log-Info 'shutting down...'

    # /T 结束整棵树 (uvicorn --reload 的父进程 + server 子进程 / pnpm + vite + esbuild)
    foreach ($proc in @($backendProc, $frontendProc)) {
        if ($proc -and -not $proc.HasExited) {
            $null = & cmd /c "taskkill /F /T /PID $($proc.Id) 2>nul"
        }
    }
    if ($script:tails) {
        foreach ($state in @($script:tails.Values)) {
            if ($state -and $state.Reader) {
                try { $state.Reader.Dispose() } catch {}
            }
        }
    }
    Log-Ok "bye (logs: $LogDir)"
}

# ===== 7. Bounded log tail =====
# 每个日志文件一个 StreamReader, 每轮只消费「上次之后新增的内容」。
# 内存占用 = StreamReader 解码缓冲 + 一行待拼接文本, 与日志总量无关。
# StreamReader 负责 UTF-8 解码, 多字节字符跨读边界也不会被截断成乱码。
$script:tails = @{}
foreach ($p in @($BackendOutLog, $BackendErrLog, $FrontendOutLog, $FrontendErrLog)) {
    $script:tails[$p] = @{ Reader = $null; LastLength = 0L; Pending = '' }
}
$MaxPending = 64KB

function Open-LogTail($path) {
    $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)
    return New-Object System.IO.StreamReader(
        $fs, (New-Object System.Text.UTF8Encoding($false)))
}

function Read-NewLogLines($path, $prefix, $color) {
    $state = $script:tails[$path]
    if (-not $state) { return }
    try {
        if ($null -eq $state.Reader) {
            if (-not (Test-Path $path)) { return }
            $state.Reader = Open-LogTail $path
        } elseif ($state.Reader.BaseStream.Length -lt $state.LastLength) {
            # 文件被重置/替换 → 从头重新跟随
            $state.Reader.Dispose()
            $state.Reader = Open-LogTail $path
            $state.Pending = ''
        }
        $state.LastLength = $state.Reader.BaseStream.Length

        # ReadToEnd: 从当前位置读到末尾, 只取新增部分
        $chunk = $state.Reader.ReadToEnd()
        if ([string]::IsNullOrEmpty($chunk)) { return }

        $text = $state.Pending + $chunk
        $complete = $text.EndsWith("`n")
        $lines = $text -split "`n"
        if ($complete) {
            $state.Pending = ''
            if ($lines.Count -gt 0) { $lines = $lines[0..($lines.Count - 2)] }
        } else {
            # 末尾没有换行 = 这行可能还没写完, 留到下一轮
            $state.Pending = $lines[-1]
            $lines = if ($lines.Count -gt 1) { $lines[0..($lines.Count - 2)] } else { @() }
            if ($state.Pending.Length -gt $MaxPending) {
                Write-Host $prefix -NoNewline -ForegroundColor $color
                Write-Host ($state.Pending.Substring(0, $MaxPending) + ' ...[truncated]')
                $state.Pending = ''
            }
        }
        foreach ($line in $lines) {
            Write-Host $prefix -NoNewline -ForegroundColor $color
            Write-Host $line.TrimEnd("`r")
        }
    } catch {
        # 日志读取失败绝不能拖垮启动器
    }
}

# ===== 8. Launch + main loop =====
# 启动与主循环同处一个 try/finally: 任一 Start-Process 失败或 Ctrl-C 都会走
# Cleanup-All, 不留下半套进程。
$prevCtrlC = [Console]::TreatControlCAsInput
try {
    $backendProc = Start-Process `
        -FilePath (Join-Path $BackendDir '.venv\Scripts\python.exe') `
        -ArgumentList $BackendArgs `
        -WorkingDirectory $BackendDir `
        -RedirectStandardOutput $BackendOutLog `
        -RedirectStandardError  $BackendErrLog `
        -NoNewWindow -PassThru

    # .cmd 不是可执行文件: 关闭 shell 执行 (重定向输出时必须) 后要走 cmd.exe /c。
    # 整条命令作为一个参数交给 cmd 解析, 避免 Start-Process 自行拼接时加错引号。
    $cmdExe = if ($env:ComSpec) { $env:ComSpec } else { 'cmd.exe' }
    $frontendCmd = '"{0}" dev --host {1} --port {2}' -f $pnpmExe, $BindAddress, $FrontendPort
    $frontendProc = Start-Process `
        -FilePath $cmdExe `
        -ArgumentList @('/c', $frontendCmd) `
        -WorkingDirectory $FrontendDir `
        -RedirectStandardOutput $FrontendOutLog `
        -RedirectStandardError  $FrontendErrLog `
        -NoNewWindow -PassThru

    # Treat Ctrl-C as input so try/finally is guaranteed to run.
    [Console]::TreatControlCAsInput = $true

    while ($true) {
        if ([Console]::KeyAvailable) {
            $key = [Console]::ReadKey($true)
            if (($key.Modifiers -band [ConsoleModifiers]::Control) -and $key.Key -eq 'C') {
                break
            }
        }

        Read-NewLogLines $BackendOutLog  '[backend ] ' Blue
        Read-NewLogLines $BackendErrLog  '[backend ] ' Blue
        Read-NewLogLines $FrontendOutLog '[frontend] ' Green
        Read-NewLogLines $FrontendErrLog '[frontend] ' Green

        if ($backendProc.HasExited -or $frontendProc.HasExited) {
            Log-Warn 'one of the processes exited; closing the other...'
            break
        }

        Start-Sleep -Milliseconds 300
    }
}
finally {
    [Console]::TreatControlCAsInput = $prevCtrlC
    Cleanup-All
}
