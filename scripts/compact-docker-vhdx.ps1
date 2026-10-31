#Requires -Version 5.1
<#
.SYNOPSIS
    回收 Docker Desktop（WSL2 后端）虚拟磁盘占用的空间。

.DESCRIPTION
    背景：Docker Desktop 的数据盘 docker_data.vhdx 只会增长，删除容器/数据后
    空间不自动归还。典型现象：文件 400 GB，实际只用 13 GB。

    为什么「diskpart compact + wsl --set-sparse」那套没用：
      1) wsl --manage docker-desktop --set-sparse 只作用于发行版根盘
         (C:\Users\<user>\AppData\Local\Docker\wsl\main\ext4.vhdx，通常 <1 GB)，
         不是那个 400 GB 的数据盘；
      2) 数据盘不是稀疏文件，guest 里的 fstrim/TRIM 会被直接丢弃；
      3) diskpart compact vdisk 只能回收「内容为零」的块，而 ext4 释放的块
         里仍是旧数据，对 compact 来说依然算「已分配」。

    唯一可靠的办法是「重建数据盘」：
      备份卷 → 停 Docker → 删除 vhdx → 注销发行版 → 重启 Docker（自动重建）
      → 恢复卷 → 起服务 → 校验行数。

.PARAMETER Vhdx
    Docker 数据盘路径，默认 D:\DockerData\docker_data.vhdx。

.PARAMETER BackupDir
    卷备份目录，默认 D:\docker-backup（脚本不会自动删除，确认后再手动删）。

.PARAMETER ComposeDir
    docker-compose.yml 所在目录，默认 D:\FinBridge。

.PARAMETER QuestDbVolume
    QuestDB 数据卷名，默认 finbridge_questdb_data。

.PARAMETER VenvVolume
    mcp-app 的 Python 虚拟环境卷，默认 finbridge_mcp_venv。
    备份它可让容器启动时跳过 uv sync（省 5–15 分钟）。

.PARAMETER DryRun
    只做检查并打印计划，不停止/删除任何东西；不需要管理员权限。

.PARAMETER Force
    跳过交互确认。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\compact-docker-vhdx.ps1 -DryRun

.EXAMPLE
    # 管理员 PowerShell
    powershell -ExecutionPolicy Bypass -File .\scripts\compact-docker-vhdx.ps1
#>
[CmdletBinding()]
param(
    [string]$Vhdx = "D:\DockerData\docker_data.vhdx",
    [string]$BackupDir = "D:\docker-backup",
    [string]$ComposeDir = "D:\FinBridge",
    [string]$QuestDbVolume = "finbridge_questdb_data",
    [string]$VenvVolume = "finbridge_mcp_venv",
    [string]$QuestDbRest = "http://127.0.0.1:9000",
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Write-Ok([string]$text) { Write-Host "    [OK] $text" -ForegroundColor Green }
function Write-Warn2([string]$text) { Write-Host "    [!] $text" -ForegroundColor Yellow }
function Write-Fail([string]$text) { Write-Host "    [X] $text" -ForegroundColor Red }

function Get-VhdxAllocatedGb([string]$path) {
    if (-not (Test-Path $path)) { return 0 }
    if (-not ("SparseProbe" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class SparseProbe {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint GetCompressedFileSize(string lpFileName, out uint lpFileSizeHigh);
}
"@
    }
    $hi = 0
    $lo = [SparseProbe]::GetCompressedFileSize($path, [ref]$hi)
    return [math]::Round((([double]$hi * 4294967296) + $lo) / 1GB, 2)
}

function Invoke-Docker([string[]]$arguments) {
    $out = & docker @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($arguments -join ' ') 失败：`n$($out -join "`n")"
    }
    return $out
}

function Test-FileUnlocked([string]$path) {
    try {
        $s = [System.IO.File]::Open($path, 'Open', 'ReadWrite', 'None')
        $s.Close()
        return $true
    } catch {
        return $false
    }
}

function Get-QuestDbCount([string]$sql) {
    try {
        $r = Invoke-RestMethod -Uri "$QuestDbRest/exec?query=$([uri]::EscapeDataString($sql))" -TimeoutSec 30
        return [int64]$r.dataset[0][0]
    } catch {
        return -1
    }
}

function Wait-DockerReady([int]$timeoutSec = 300) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        & docker ps *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Backup-Volume([string]$vol, [string]$backupDir, [string]$stamp) {
    $file = Join-Path $backupDir "$vol-$stamp.tgz"
    $leaf = Split-Path $file -Leaf
    $out = & docker run --rm -v "${vol}:/data" -v "${backupDir}:/b" alpine sh -c "tar czf /b/$leaf -C /data . && echo BACKUP_OK" 2>&1
    if ($LASTEXITCODE -ne 0 -or ($out -join "`n") -notmatch "BACKUP_OK") {
        throw "备份卷 $vol 失败：`n$($out -join "`n")"
    }
    $entries = & docker run --rm -v "${backupDir}:/b" alpine sh -c "tar tzf /b/$leaf | wc -l" 2>&1
    $n = [int]($entries | Select-Object -Last 1)
    if ($n -lt 10) { throw "备份 $file 内容异常（$n 个条目）" }
    Write-Ok ("{0} -> {1}（{2:N2} GB，{3} 个条目）" -f $vol, $file, ((Get-Item $file).Length / 1GB), $n)
    return $file
}

# ------------------------------------------------------------------ 0. 预检
Write-Step "0. 预检"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$composeFile = Join-Path $ComposeDir "docker-compose.yml"
if (-not (Test-Path $composeFile)) { throw "找不到 compose 文件：$composeFile" }

$beforeAlloc = Get-VhdxAllocatedGb $Vhdx
$beforeFree = [math]::Round((Get-PSDrive D).Free / 1GB, 1)

Write-Host ("    数据盘      : {0}" -f $Vhdx)
Write-Host ("    当前占用    : {0} GB" -f $beforeAlloc)
Write-Host ("    D 盘可用    : {0} GB" -f $beforeFree)
Write-Host ("    备份目录    : {0}" -f $BackupDir)
Write-Host ("    compose     : {0}" -f $composeFile)

if (-not $isAdmin -and -not $DryRun) {
    throw "需要管理员权限：请以管理员身份打开 PowerShell 后重跑。"
}

$volumes = & docker volume ls --format "{{.Name}}" 2>&1
$hasDbVol = $volumes -contains $QuestDbVolume
$hasVenvVol = $volumes -contains $VenvVolume
Write-Host ("    卷 {0} : {1}" -f $QuestDbVolume, $(if ($hasDbVol) { "存在" } else { "不存在（跳过备份）" }))
Write-Host ("    卷 {0} : {1}" -f $VenvVolume, $(if ($hasVenvVol) { "存在" } else { "不存在（跳过备份）" }))

$preCount = Get-QuestDbCount "select count() from ohlcv_1d"
if ($preCount -ge 0) { Write-Host ("    当前 ohlcv_1d 行数: {0:N0}" -f $preCount) } else { Write-Warn2 "QuestDB 未响应，行数基线记为 -1" }

if ($DryRun) {
    Write-Step "DryRun：以下动作会被执行（本次不执行）"
    @(
        "1) docker compose -f $composeFile stop",
        "2) 备份卷 $QuestDbVolume / $VenvVolume -> $BackupDir",
        "3) docker compose down，退出 Docker Desktop，wsl --shutdown",
        "4) 删除 $Vhdx（当前 $beforeAlloc GB）",
        "5) wsl --unregister docker-desktop",
        "6) 启动 Docker Desktop，等待引擎就绪（会重建一个约 1 GB 的数据盘）",
        "7) 重建卷并恢复备份",
        "8) docker compose up -d，等待 healthy",
        "9) 校验 ohlcv_1d 行数是否等于 $preCount"
    ) | ForEach-Object { Write-Host "    $_" }
    Write-Host ""
    Write-Host ("    预计回收: {0} GB" -f [math]::Round($beforeAlloc - 1.5, 1)) -ForegroundColor Green
    return
}

if (-not $Force) {
    Write-Host ""
    $answer = Read-Host "即将停止 Docker、删除数据盘并重建。输入 YES 继续"
    if ($answer -ne "YES") { Write-Warn2 "已取消"; return }
}

# ------------------------------------------------------------ 1. 停止容器
Write-Step "1. 停止容器（保留卷）"
Invoke-Docker @("compose", "-f", $composeFile, "stop") | Out-Null
Write-Ok "容器已停止"

# -------------------------------------------------------------- 2. 备份卷
Write-Step "2. 备份卷"
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

$backups = [ordered]@{}
if ($hasDbVol) { $backups[$QuestDbVolume] = Backup-Volume $QuestDbVolume $BackupDir $stamp }
if ($hasVenvVol) { $backups[$VenvVolume] = Backup-Volume $VenvVolume $BackupDir $stamp }

# ------------------------------------------------------- 3. 停 Docker / WSL
Write-Step "3. 停止 Docker Desktop 与 WSL"
Invoke-Docker @("compose", "-f", $composeFile, "down") | Out-Null
Get-Process -Name "Docker Desktop", "com.docker.backend", "com.docker.build", "docker-agent" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
& wsl --shutdown *> $null
Start-Sleep -Seconds 10

$unlocked = $false
for ($i = 0; $i -lt 12; $i++) {
    if (Test-FileUnlocked $Vhdx) { $unlocked = $true; break }
    Write-Warn2 "数据盘仍被占用，等待 5s（$($i + 1)/12）"
    Start-Sleep -Seconds 5
}
if (-not $unlocked) { throw "数据盘仍被占用：请手动退出 Docker Desktop（含托盘）后重试。" }
Write-Ok "数据盘已释放"

# ------------------------------------------------------------ 4. 删除数据盘
Write-Step "4. 删除旧数据盘"
$sizeCheck = (Get-Item $Vhdx).Length / 1GB
if ($sizeCheck -lt 1) { throw ("数据盘只有 {0:N2} GB，疑似异常，终止。" -f $sizeCheck) }
Remove-Item $Vhdx -Force
Write-Ok ("已删除，D 盘可用 {0:N1} GB" -f ((Get-PSDrive D).Free / 1GB))

& wsl --unregister docker-desktop 2>&1 | Out-Null
Write-Ok "已注销 docker-desktop 发行版"

# ------------------------------------------------------ 5. 重启 Docker Desktop
Write-Step "5. 启动 Docker Desktop（重建数据盘）"
$exe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
if (-not (Test-Path $exe)) { throw "找不到 Docker Desktop：$exe" }
Start-Process $exe
if (-not (Wait-DockerReady 300)) { throw "Docker 引擎 300s 未就绪，请手动检查 Docker Desktop。" }
Write-Ok ("Docker 引擎就绪，新数据盘 {0:N2} GB" -f (Get-VhdxAllocatedGb $Vhdx))

# -------------------------------------------------------------- 6. 恢复卷
Write-Step "6. 恢复卷"
foreach ($item in $backups.GetEnumerator()) {
    $vol = $item.Key
    $file = $item.Value
    $leaf = Split-Path $file -Leaf
    & docker volume create $vol *> $null
    $out = & docker run --rm -v "${vol}:/data" -v "${BackupDir}:/b" alpine sh -c "tar xzf /b/$leaf -C /data && echo RESTORE_OK" 2>&1
    if ($LASTEXITCODE -ne 0 -or ($out -join "`n") -notmatch "RESTORE_OK") {
        throw "恢复卷 $vol 失败。备份仍在：$file"
    }
    Write-Ok "$vol 已恢复"
}

# -------------------------------------------------------------- 7. 起服务
Write-Step "7. 启动服务"
Invoke-Docker @("compose", "-f", $composeFile, "up", "-d") | Out-Null

$healthy = $false
for ($i = 0; $i -lt 36; $i++) {
    Start-Sleep -Seconds 5
    $state = (& docker inspect -f "{{.State.Health.Status}}" mcp-app 2>&1) -join ""
    if ($state -match "healthy") { $healthy = $true; break }
}
if ($healthy) { Write-Ok "mcp-app healthy" } else { Write-Warn2 "mcp-app 未在 180s 内 healthy，请检查 docker logs mcp-app" }

# -------------------------------------------------------------- 8. 校验
Write-Step "8. 校验"
$postCount = Get-QuestDbCount "select count() from ohlcv_1d"
$afterAlloc = Get-VhdxAllocatedGb $Vhdx
$afterFree = [math]::Round((Get-PSDrive D).Free / 1GB, 1)

Write-Host ("    ohlcv_1d 行数 : {0:N0} -> {1:N0}" -f $preCount, $postCount)
Write-Host ("    数据盘        : {0} GB -> {1} GB" -f $beforeAlloc, $afterAlloc)
Write-Host ("    D 盘可用      : {0} GB -> {1} GB" -f $beforeFree, $afterFree)

if ($preCount -ge 0 -and $postCount -ne $preCount) {
    Write-Fail "行数不一致！备份保留在 $BackupDir，请勿删除。"
    exit 1
}
Write-Ok "行数一致"

Write-Host ""
Write-Host "完成。确认服务正常后，可删除备份目录：$BackupDir" -ForegroundColor Green
