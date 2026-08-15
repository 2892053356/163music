@echo off
chcp 65001 >nul
title 网易云音乐 Telegram Bot
cd /d "%~dp0"

echo ========================================
echo   网易云音乐 Telegram Bot
echo ========================================
echo.

REM 检查依赖
python -c "import telegram" 2>nul
if errorlevel 1 (
    echo [1/2] 正在安装依赖...
    python -m pip install -r requirements.txt
    echo.
)

echo [2/2] 启动 Bot...
echo.
python bot.py

pause
