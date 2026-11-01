@echo off
chcp 65001 >nul
title 乡村政务智能问答系统 - 一键启动
cd /d %~dp0vqa-system

if not exist venv\Scripts\python.exe (
    echo ============================================
    echo   [错误] 未找到虚拟环境 venv
    echo   请先按 README.md 安装依赖：
    echo     python -m venv venv
    echo     venv\Scripts\pip install -r requirements.txt
    echo ============================================
    pause
    exit /b 1
)

echo ============================================
echo   乡村政务智能问答系统启动中...
echo   浏览器将自动打开: http://127.0.0.1:8000
echo   关闭本窗口即可停止服务
echo ============================================

rem 延迟 3 秒后自动打开浏览器（服务启动需要一点时间）
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000"

venv\Scripts\python.exe manage.py serve
pause
