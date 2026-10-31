@echo off
rem QQMonitor 自动同步守护进程（不开 WebUI 也定时同步群消息）
rem 双击启动后无窗口常驻后台；想停止可运行 sync-daemon-stop.bat
cd /d "%~dp0"
setlocal

rem ↓ 下面几行是 pythonw.exe 的探测顺序。换过 conda 环境名/装在其他盘时，
rem   把你的路径写在第一条 set "PYW=..." 里即可，后面几行只是兜底。
rem 直接用 QQMonitor 环境里的 pythonw.exe 启动（不走 conda run：
rem conda.exe 是控制台进程，双击后黑窗口一关，控制台关闭事件会连带终止整条进程树）
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

rem 已在运行则直接退出（守护进程内部也有锁，这里是给用户一个明确提示）
powershell -NoProfile -Command "$pids = Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'sync-daemon' } | Select-Object -ExpandProperty ProcessId; if ($pids) { exit 0 } else { exit 1 }" 2>nul
if not errorlevel 1 (
    echo [提示] 自动同步守护进程已在运行，无需重复启动。
    ping -n 2 127.0.0.1 >nul
    exit /b 0
)

echo 正在后台启动自动同步守护进程（无窗口常驻）...
rem pythonw 是 GUI 进程、不附加控制台，黑窗口关闭后依然存活
start "" "%PYW%" -X utf8 main.py sync-daemon

echo 已启动。日志见 data\qqmonitor.log
echo 停止方法：运行 sync-daemon-stop.bat
ping -n 3 127.0.0.1 >nul
exit /b 0
