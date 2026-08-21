# 163Music Bot 一键启动脚本（Cloudflare Tunnel版）
# 功能：启动 cloudflared -> 获取公网地址 -> 更新 .env -> 启动 bot

$ErrorActionPreference = "Stop"
$workDir = "C:\Users\Administrator\Desktop\163music_webhook"
Set-Location $workDir

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  163Music Bot - 一键启动（Cloudflare Tunnel）" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# 检查 cloudflared.exe
if (-not (Test-Path "cloudflared.exe")) {
    Write-Host "[错误] 未找到 cloudflared.exe" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}

# [1/3] 启动 Cloudflare Tunnel
Write-Host "[1/3] 启动 Cloudflare Tunnel（端口 8080）..." -ForegroundColor Yellow
$tunnelLog = "cf_tunnel.log"
if (Test-Path $tunnelLog) { Remove-Item $tunnelLog -Force }
# 同时重定向 stdout 和 stderr
$proc = Start-Process -FilePath ".\cloudflared.exe" -ArgumentList "tunnel --url http://localhost:8080 --no-autoupdate" -WindowStyle Normal -PassThru -RedirectStandardOutput $tunnelLog -RedirectStandardError "cf_tunnel_err.log"

# [2/3] 等待并获取公网地址（最多重试30次，每次2秒）
Write-Host "[2/3] 等待 Cloudflare Tunnel 获取公网地址..." -ForegroundColor Yellow
$cfUrl = $null
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 2
    # 同时检查 stdout 和 stderr 日志
    $allLogs = @()
    if (Test-Path $tunnelLog) { $allLogs += Get-Content $tunnelLog -ErrorAction SilentlyContinue }
    if (Test-Path "cf_tunnel_err.log") { $allLogs += Get-Content "cf_tunnel_err.log" -ErrorAction SilentlyContinue }
    $match = $allLogs | Select-String -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
    if ($match) {
        $cfUrl = $match.Matches[0].Value
        Write-Host "  成功获取公网地址（第 $i 次尝试）" -ForegroundColor Green
        break
    }
    Write-Host "  第 $i 次尝试: 等待中..." -ForegroundColor Gray
}

if (-not $cfUrl) {
    Write-Host ""
    Write-Host "[警告] 无法自动获取 Cloudflare Tunnel 地址" -ForegroundColor Red
    Write-Host ""
    Write-Host "请查看 cloudflared 窗口中的地址" -ForegroundColor Yellow
    Write-Host "将 https://xxxx.trycloudflare.com 填入 .env 的 WEBHOOK_URL"
    Write-Host "然后运行 start_bot.bat"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

Write-Host "[3/3] Cloudflare Tunnel 地址: $cfUrl" -ForegroundColor Green
Write-Host ""

# 更新 .env 文件中的 WEBHOOK_URL
$envFile = ".env"
$content = Get-Content $envFile -Raw -Encoding UTF8
$content = $content -replace 'WEBHOOK_URL=.*', "WEBHOOK_URL=$cfUrl"
Set-Content $envFile -Value $content -Encoding UTF8 -NoNewline
Write-Host "WEBHOOK_URL 已更新为: $cfUrl" -ForegroundColor Green
Write-Host ""

# 检查并安装 rich（美化日志输出，失败不影响启动）
Write-Host "检查依赖..." -ForegroundColor Yellow
try {
    $richCheck = & py -c "import rich; print('ok')" 2>$null
    if ($richCheck -eq "ok") {
        Write-Host "  rich 已安装" -ForegroundColor Green
    } else {
        Write-Host "  安装 rich 库（美化日志）..." -ForegroundColor Yellow
        & py -m pip install rich --quiet 2>$null | Out-Null
        Write-Host "  rich 安装完成" -ForegroundColor Green
    }
} catch {
    Write-Host "  rich 检查跳过（不影响 Bot 运行）" -ForegroundColor Gray
}
Write-Host ""

# 启动 bot
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Bot 启动中..." -ForegroundColor Cyan
Write-Host "  Cloudflare Tunnel窗口不要关闭！" -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

py bot_v6.2.py

Write-Host ""
Write-Host "Bot 已停止" -ForegroundColor Yellow
Read-Host "按回车键退出"
