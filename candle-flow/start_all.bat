@echo off
chcp 65001 >nul
echo ========================================
echo   Candle Flow 蜡烛图交易系统 - 启动
echo ========================================
echo.

cd /d "%~dp0"

if not exist "backend\venv\Scripts\activate.bat" (
    echo [提示] 尚未安装，正在运行 install_windows.bat ...
    call install_windows.bat
)

echo 启动后端服务 (http://localhost:8000) ...
start "CandleFlow-Backend" cmd /k "cd /d %~dp0backend && call venv\Scripts\activate.bat && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

timeout /t 3 /nobreak >nul

echo 启动前端服务 (http://localhost:5173) ...
start "CandleFlow-Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

timeout /t 2 /nobreak >nul
echo.
echo 系统已启动:
echo   前端: http://localhost:5173
echo   后端: http://localhost:8000
echo   API文档: http://localhost:8000/docs
echo.
start http://localhost:5173
