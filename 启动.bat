@echo off
chcp 65001 >nul
title 知识库 AI

echo ===============================================
echo            正在启动  知识库 AI
echo ===============================================
echo.

REM ---- 1) 确认 Ollama 是否已运行;没起才后台拉起(不杀进程、不绑定盘符) ----
ollama list >nul 2>&1
if errorlevel 1 (
    echo Ollama 还没启动,正在后台拉起,请稍候...
    start "Ollama 服务 - 请勿关闭此窗口" /min cmd /c ollama serve
    timeout /t 6 >nul
) else (
    echo Ollama 已在运行。
)
echo.

REM ---- 2) 启动知识库后端 ----
cd /d "%~dp0backend"
echo 正在检查并安装依赖(首次会下载,稍慢,请耐心等待,别关窗口)...
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo 浏览器即将打开 http://localhost:8000  首次启动稍慢,请耐心等待。
start "" http://localhost:8000
python -m uvicorn app:app --host 0.0.0.0 --port 8000
pause
