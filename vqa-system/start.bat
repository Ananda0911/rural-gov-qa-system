@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   乡村智能问答系统启动中...
echo   启动后访问: http://127.0.0.1:8000
echo   关闭本窗口即可停止服务
echo ============================================
venv\Scripts\python.exe manage.py serve
pause
