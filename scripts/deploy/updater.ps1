<#
.SYNOPSIS
    MRS 助手自动替换脚本（由计划任务每 5 分钟调用一次）。

.DESCRIPTION
    决策 4.A（全自动）落地：
      1. 监视 inbox 目录里的 MRS助手.exe
      2. 校验大小与 PE 头，不合格就退到 rejected 目录并通知一次
      3. 任务正在跑（run-state.json: running 且 pid 存活）就等下一轮
      4. 备份 exe / dist / config.json
      5. 关闭 GUI（onefile 是父子两个进程，必须整树杀）-> 替换 -> 重启
      6. 等待 20 秒做存活检测；起不来就整份回退并重启旧版本
      7. 结果推送到 mypanel，成功时删除 inbox 里的 exe

.NOTES
    - 不备份 node.exe 与 node_modules（各约 90MB / 56MB，部署时从不替换）。
    - 计划任务只应在 VM 内注册；脚本不含任何硬编码凭据。
#>
[CmdletBinding()]
param(
    [string]$AppDir = 'C:\Tools\MRS助手',
    [string]$InboxDir = 'C:\Tools\mrs-inbox',
    [string]$BackupRoot = 'C:\Tools\mrs-backup',
    [string]$ControlDir = 'C:\Tools\mrs-control',
    [string]$ExeName = 'MRS助手.exe',
    [string]$NotifyUrl = 'http://my.luqion.cn/notify/push',
    [string]$NotifyFallbackUrl = 'http://192.168.1.170:18050/notify/push',
    [int]$MinSizeMB = 15,
    [int]$StartupGraceSeconds = 20,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$LogPath = Join-Path $ControlDir 'update.log'
$ProcName = [IO.Path]::GetFileNameWithoutExtension($ExeName)
$TargetExe = Join-Path $AppDir $ExeName
$InboxExe = Join-Path $InboxDir $ExeName
$RejectDir = Join-Path $InboxDir 'rejected'
$StatePath = Join-Path $AppDir 'run-state.json'

function Write-Log {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 } catch { }
    Write-Host $line
}

function Send-Notice {
    param([string]$Message)
    $json = @{ message = $Message } | ConvertTo-Json -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    foreach ($url in @($NotifyUrl, $NotifyFallbackUrl)) {
        if ([string]::IsNullOrWhiteSpace($url)) { continue }
        try {
            Invoke-RestMethod -Uri $url -Method Post -ContentType 'application/json; charset=utf-8' `
                -Body $bytes -TimeoutSec 15 | Out-Null
            return $true
        } catch {
            Write-Log ("通知失败（{0}）：{1}" -f $url, $_.Exception.Message)
        }
    }
    return $false
}

function Test-PeHeader {
    param([string]$Path)
    # 只读前两个字节，避免把 20MB 整个读进内存
    try {
        $fs = [IO.File]::OpenRead($Path)
        try {
            $buf = New-Object byte[] 2
            $read = $fs.Read($buf, 0, 2)
            return ($read -eq 2 -and $buf[0] -eq 0x4D -and $buf[1] -eq 0x5A)   # 'MZ'
        } finally { $fs.Dispose() }
    } catch {
        return $false
    }
}

function Get-AppProcess {
    # 中文进程名，用 -Name 直接匹配；返回可能是父子两个进程
    return @(Get-Process -Name $ProcName -ErrorAction SilentlyContinue)
}

function Start-App {
    <#
      启动 GUI。必须重定向标准输出/错误：若父进程（计划任务/管道宿主）持有控制台句柄，
      不重定向时 Start-Process 可能挂住不返回。
    #>
    param([string]$ExePath, [string]$WorkDir)
    $out = Join-Path $ControlDir 'launch-stdout.log'
    $err = Join-Path $ControlDir 'launch-stderr.log'
    try {
        Start-Process -FilePath $ExePath -WorkingDirectory $WorkDir `
            -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
    } catch {
        # 重定向失败时退回普通启动（VM 计划任务里没有管道宿主，通常没问题）
        Write-Log ("带重定向启动失败，改用普通启动：{0}" -f $_.Exception.Message)
        Start-Process -FilePath $ExePath -WorkingDirectory $WorkDir | Out-Null
    }
}

function Stop-AppTree {
    foreach ($p in (Get-AppProcess)) {
        try {
            & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
        } catch {
            Write-Log ("结束进程 {0} 失败：{1}" -f $p.Id, $_.Exception.Message)
        }
    }
    Start-Sleep -Seconds 2
    $left = Get-AppProcess
    if ($left.Count -gt 0) {
        Write-Log ("警告：仍有 {0} 个进程未退出" -f $left.Count)
    }
}

function Test-TaskRunning {
    # run-state.json 由 GUI 写入（决策 1.B / B1）。文件不存在视为空闲。
    if (-not (Test-Path -LiteralPath $StatePath)) { return $false }
    try {
        $st = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($st.state -ne 'running') { return $false }
        if (-not $st.pid) { return $false }
        $alive = Get-Process -Id ([int]$st.pid) -ErrorAction SilentlyContinue
        return ($null -ne $alive)
    } catch {
        # 状态文件读不出来时保守处理：当作任务在跑，等下一轮
        Write-Log ("run-state.json 解析失败，按运行中处理：{0}" -f $_.Exception.Message)
        return $true
    }
}

# ------------------------------------------------------------------ 主流程

if (-not (Test-Path -LiteralPath $InboxExe)) {
    return   # 没有待更新文件：静默退出，避免每 5 分钟写日志
}

Write-Log ("发现待更新文件：{0}" -f $InboxExe)

# 1) 校验
$item = Get-Item -LiteralPath $InboxExe
$sizeMB = [math]::Round($item.Length / 1MB, 1)
if ($sizeMB -lt $MinSizeMB) {
    New-Item -ItemType Directory -Path $RejectDir -Force | Out-Null
    $moved = Join-Path $RejectDir ('{0}_{1}' -f (Get-Date -Format 'yyyyMMdd_HHmmss'), $ExeName)
    Move-Item -LiteralPath $InboxExe -Destination $moved -Force
    $msg = "# MRS 更新被拒绝`n`n文件太小（$sizeMB MB，要求 ≥ $MinSizeMB MB），已移到 rejected 目录：`n$moved"
    Write-Log ("拒绝：大小 {0}MB 不达标" -f $sizeMB)
    Send-Notice $msg | Out-Null
    exit 1
}

if (-not (Test-PeHeader $InboxExe)) {
    New-Item -ItemType Directory -Path $RejectDir -Force | Out-Null
    $moved = Join-Path $RejectDir ('{0}_{1}' -f (Get-Date -Format 'yyyyMMdd_HHmmss'), $ExeName)
    Move-Item -LiteralPath $InboxExe -Destination $moved -Force
    $msg = "# MRS 更新被拒绝`n`n不是有效的 Windows 可执行文件（PE 头缺失），已移到：`n$moved"
    Write-Log '拒绝：PE 头校验失败'
    Send-Notice $msg | Out-Null
    exit 1
}

if (-not (Test-Path -LiteralPath $TargetExe)) {
    Write-Log ("目标不存在：{0}（AppDir 是否配置正确？）" -f $TargetExe)
    exit 1
}

# 2) 任务在跑就等下一轮（GUI 常驻不代表有任务在跑）
if (Test-TaskRunning) {
    Write-Log '任务正在运行，本轮跳过，等下一轮'
    exit 0
}

if ($DryRun) {
    Write-Log ("DryRun：校验与空闲检查均通过，将替换 {0}（未实际执行）" -f $TargetExe)
    exit 0
}

# 3) 备份（只备份会被替换的部分）
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$backupDir = Join-Path $BackupRoot $stamp
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
Copy-Item -LiteralPath $TargetExe -Destination $backupDir -Force
foreach ($sub in @('dist', 'scripts')) {
    $path = Join-Path $AppDir $sub
    if (Test-Path -LiteralPath $path) {
        Copy-Item -LiteralPath $path -Destination $backupDir -Recurse -Force
    }
}
foreach ($file in @('config.json', 'config.example.json')) {
    $path = Join-Path $AppDir $file
    if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $backupDir -Force }
}
Write-Log ("已备份到 {0}" -f $backupDir)

# 4) 关闭 GUI -> 替换 -> 重启
Stop-AppTree
Copy-Item -LiteralPath $InboxExe -Destination $TargetExe -Force
Write-Log '已替换可执行文件'

Start-App -ExePath $TargetExe -WorkDir $AppDir
Start-Sleep -Seconds $StartupGraceSeconds

# 5) 存活检测
$alive = Get-AppProcess
if ($alive.Count -gt 0) {
    Remove-Item -LiteralPath $InboxExe -Force -ErrorAction SilentlyContinue
    Write-Log '更新成功，进程存活'
    $msg = "# MRS 助手更新成功`n`n新版本已启动（进程 {0} 个）。`n备份保留在：``$backupDir``" -f $alive.Count
    Send-Notice $msg | Out-Null
    exit 0
}

# 6) 起不来 -> 回退
Write-Log '新版本未能存活，开始回退'
Stop-AppTree
Copy-Item -LiteralPath (Join-Path $backupDir $ExeName) -Destination $TargetExe -Force
foreach ($sub in @('dist', 'scripts')) {
    $src = Join-Path $backupDir $sub
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination $AppDir -Recurse -Force
    }
}
Start-App -ExePath $TargetExe -WorkDir $AppDir
Start-Sleep -Seconds 8
$restored = Get-AppProcess

if ($restored.Count -gt 0) {
    $msg = "# MRS 助手更新失败，已回退`n`n新版本启动不了，已还原旧版本并重启。`n备份：``$backupDir``"
    Write-Log '回退成功，旧版本已启动'
    Send-Notice $msg | Out-Null
    exit 1
}

$msg = "# MRS 助手更新失败，回退也没起来`n`n需要人工处理。`n备份目录：``$backupDir``"
Write-Log '回退后仍无法启动，需要人工介入'
Send-Notice $msg | Out-Null
exit 2