@echo off
chcp 65001 >nul
echo ========================================
echo   Candle Flow 蜡烛图交易系统 - 安装
echo ========================================
echo.

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Node.js，请先安装 Node.js 18+
    pause
    exit /b 1
)

echo [1/4] 创建 Python 虚拟环境...
if not exist "backend\venv" (
    python -m venv backend\venv
)

echo [2/4] 安装后端依赖...
call backend\venv\Scripts\activate.bat
pip install -r backend\requirements.txt -q
if errorlevel 1 (
    echo [错误] 后端依赖安装失败
    pause
    exit /b 1
)

echo [3/4] 初始化数据库...
if not exist "backend\data" mkdir backend\data
cd backend
python -c "from app.database import init_db; init_db(); print('Database initialized')"
python scripts\init_kline.py 000001.SZ
cd ..

echo [4/4] 安装前端依赖...
cd frontend
call npm install
if errorlevel 1 (
    echo [错误] 前端依赖安装失败
    cd ..
    pause
    exit /b 1
)
cd ..

echo.
echo ========================================
echo   安装完成！
echo   运行 start_all.bat 启动系统
echo ========================================
pause
