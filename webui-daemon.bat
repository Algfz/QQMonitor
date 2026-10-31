@echo off
rem QQMonitor WebUI 后台静默启动（无窗口常驻，同局域网的电脑/手机可访问）
rem 双击本文件即可；停止请运行 webui-daemon-stop.bat
cd /d "%~dp0"
setlocal

rem 直接用 QQMonitor 环境里的 pythonw.exe 启动（走 conda run 的话，
rem 控制台窗口一关会连带终止整条进程树）
rem   优先用仓库自带的 .venv（环境就在项目里，整个文件夹拷走就能跑）
set "PYW=%~dp0.venv\pythonw.exe"
if not exist "%PYW%" set "PYW=%USERPROFILE%\.conda\envs\QQMonitor\pythonw.exe"

if not exist "%PYW%" set "PYW=%USERPROFILE%\.conda\envs\QQMoniter\pythonw.exe"
if not exist "%PYW%" set "PYW=%ProgramData%\anaconda3\envs\QQMonitor\pythonw.exe"
if not exist "%PYW%" set "PYW=%ProgramData%\anaconda3\envs\QQMoniter\pythonw.exe"
if not exist "%PYW%" for /f "delims=" %%P in ('where pythonw 2^>nul') do set "PYW=%%P"
if not exist "%PYW%" (
    echo [错误] 未找到 pythonw.exe：%PYW%
    echo        请确认 conda 环境 QQMonitor 已创建：
    echo        conda create -n QQMonitor python=3.11 -y
    echo        conda run -n QQMonitor pip install -r requirements.txt
    pause
    exit /b 1
)

rem 端口已在监听则视为已运行
netstat -ano 2>nul | findstr ":8088" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [提示] WebUI 已在运行（端口 8088 正在监听），无需重复启动。
    ping -n 2 127.0.0.1 >nul
    exit /b 0
)

echo 正在后台启动 WebUI（无窗口常驻）...
start "" "%PYW%" -X utf8 main.py run

echo.
echo 已启动。日志见 data\qqmonitor.log（开头会打印局域网访问地址）。
echo 停止方法：运行 webui-daemon-stop.bat
ping -n 3 127.0.0.1 >nul
exit /b 0
