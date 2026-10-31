@echo off
rem QQMonitor WebUI 一键启动（双击即可；关闭窗口即停止）
cd /d "%~dp0"
setlocal
set PYTHONUTF8=1
title QQMonitor WebUI

rem ↓ python.exe 的探测顺序：优先仓库自带的 .venv，其次常见 conda 环境，最后 PATH。
rem   你的环境在别处，就把第一行 set "PY=" 改成自己的路径。
set "PY=%~dp0.venv\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.conda\envs\QQMonitor\python.exe"
if not exist "%PY%" set "PY=%ProgramData%\anaconda3\envs\QQMonitor\python.exe"
if not exist "%PY%" for /f "delims=" %%P in ('where python 2^>nul') do set "PY=%%P"
if not exist "%PY%" (
    echo [错误] 没找到 Python 解释器。
    echo        请先运行 setup.cmd 建好环境，或把本文件开头的 set "PY=..." 改成自己的路径。
    pause
    exit /b 1
)

rem 已经在运行就直接开浏览器
netstat -ano 2>nul | findstr ":8088" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [提示] WebUI 已在运行（8088 正在监听），直接打开浏览器。
    start "" http://127.0.0.1:8088
    ping -n 2 127.0.0.1 >nul
    exit /b 0
)

echo ============================================
echo   QQMonitor WebUI
echo   3 秒后自动打开浏览器，关掉本窗口即停止
echo ============================================
echo.
start "" /b powershell -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8088'"

"%PY%" -X utf8 main.py run

echo.
echo WebUI 已停止。
pause
