# 压缩 Docker Desktop 的虚拟磁盘（回收 vhdx 空转空间）
#
# 背景：WSL2 的 docker_data.vhdx 只增不减，删数据后空间不自动归还。
#       典型现象：文件 200+ GB，实际只用了 10 GB。
#
# 用法：
#   1. 以【管理员身份】打开 PowerShell
#   2. 执行：  powershell -ExecutionPolicy Bypass -File D:\FinBridge\scripts\compact-docker-vhdx.ps1
#
# 影响：会停止 Docker Desktop / QuestDB / MCP 服务，预计 10~20 分钟。
#       数据在命名卷里，不会丢；压缩完重启 Docker Desktop 即可。

param(
    [string]$Vhdx = "D:\DockerData\docker_data.vhdx",
    [switch]$SkipSparse
)

$ErrorActionPreference = "Stop"

function Get-SizeGB([string]$path) {
    if (-not (Test-Path $path)) { return 0 }
    return [math]::Round((Get-Item $path).Length / 1GB, 2)
}

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path $Vhdx)) {
    throw "找不到虚拟磁盘：$Vhdx"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "需要管理员权限：请右键 PowerShell → 以管理员身份运行，然后重跑本脚本。"
}

$before = Get-SizeGB $Vhdx
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " 目标文件: $Vhdx"
Write-Host " 压缩前大小: $before GB"
Write-Host "==============================================" -ForegroundColor Cyan

# ---------------------------------------------------------------- 停 Docker
Write-Host "`n[1/5] 停止 Docker Desktop 与 WSL..." -ForegroundColor Yellow
$docker = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if ($docker) {
    Write-Host "  正在退出 Docker Desktop（等待 60 秒）..."
    $docker | ForEach-Object { $_.CloseMainWindow() | Out-Null }
    Start-Sleep -Seconds 15
    Get-Process "Docker Desktop" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

wsl --shutdown
Start-Sleep -Seconds 8

$vmmem = Get-Process "vmmem" -ErrorAction SilentlyContinue
if ($vmmem) {
    Write-Host "  WSL 虚拟机仍在运行，等待其退出..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}

# 占用检查：文件被锁说明容器还在跑
try {
    $stream = [System.IO.File]::Open($Vhdx, 'Open', 'ReadWrite', 'None')
    $stream.Close()
    Write-Host "  ✓ 虚拟磁盘已释放，可以压缩" -ForegroundColor Green
} catch {
    throw "虚拟磁盘仍被占用，请手动完全退出 Docker Desktop（含托盘图标）后重试。"
}

# ---------------------------------------------------------------- 压缩
Write-Host "`n[2/5] 用 diskpart 压缩虚拟磁盘（可能需要几分钟）..." -ForegroundColor Yellow
$dpScript = @"
select vdisk file="$Vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@
$tmp = Join-Path $env:TEMP "compact_vdisk.txt"
$dpScript | Out-File -FilePath $tmp -Encoding ascii

$out = & diskpart /s $tmp 2>&1
$out | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
Remove-Item $tmp -Force

$after = Get-SizeGB $Vhdx
$freed = [math]::Round($before - $after, 2)

Write-Host "`n[3/5] 压缩结果" -ForegroundColor Yellow
Write-Host ("  压缩前: {0} GB" -f $before)
Write-Host ("  压缩后: {0} GB" -f $after)
Write-Host ("  回收:   {0} GB" -f $freed) -ForegroundColor Green

if ($freed -lt 1) {
    Write-Host "  ⚠ 几乎没有回收，可能 vhdx 已经很紧凑，或 diskpart 失败（看上面输出）" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 开启稀疏
if (-not $SkipSparse) {
    Write-Host "`n[4/5] 开启稀疏磁盘（以后自动回收）..." -ForegroundColor Yellow
    try {
        wsl --manage docker-desktop --set-sparse true 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        Write-Host "  ✓ 已开启（若报错可忽略，不影响已回收的空间）" -ForegroundColor Green
    } catch {
        Write-Host "  ⚠ 当前 WSL 版本不支持 --set-sparse，跳过" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- 收尾
Write-Host "`n[5/5] 完成，请手动启动 Docker Desktop" -ForegroundColor Yellow
Write-Host "  启动后验证：" -ForegroundColor Cyan
Write-Host "    docker ps                        # 应看到 questdb-server / mcp-app"
Write-Host "    docker exec questdb-server du -sh /var/lib/questdb/db"
Write-Host ""
Write-Host "  D 盘可用空间已增加约 $freed GB" -ForegroundColor Green
