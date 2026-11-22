<#
.SYNOPSIS
    在 VM 内注册 MRS 自动替换的计划任务（每 N 分钟跑一次 updater.ps1）。

.DESCRIPTION
    决策 6.4：用 Windows 计划任务轮询 mrs-inbox。
    需要在 VM 里以管理员身份执行一次。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\Tools\mrs-control\install-task.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'MRS-Updater',
    [string]$ControlDir = 'C:\Tools\mrs-control',
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = 'Stop'

$script = Join-Path $ControlDir 'updater.ps1'
if (-not (Test-Path -LiteralPath $script)) {
    throw "找不到 $script，请先把 updater.ps1 放进 $ControlDir"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $script)

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# 机器必须开着才会跑；不在电池/空闲上做额外限制
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest -Force | Out-Null

Write-Host ("已注册计划任务 {0}，每 {1} 分钟检查一次 {2}" -f $TaskName, $IntervalMinutes, (Join-Path 'C:\Tools\mrs-inbox' 'MRS助手.exe'))
Write-Host ("查看状态：Get-ScheduledTask -TaskName {0}" -f $TaskName)
Write-Host ("立即试跑：Start-ScheduledTask -TaskName {0}" -f $TaskName)