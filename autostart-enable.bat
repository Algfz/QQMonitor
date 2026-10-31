@echo off
rem QQMonitor 开机自启设置（可以只启其中一个）
rem
rem 用法：
rem   autostart-enable.bat           双击运行，按菜单选择
rem   autostart-enable.bat both      两个都自启（同步守护 + Web 看板）
rem   autostart-enable.bat sync      只自启 同步守护
rem   autostart-enable.bat webui     只自启 Web 看板
rem 取消自启：autostart-disable.bat
cd /d "%~dp0"
setlocal

rem 直接用 QQMonitor 环境里的 pythonw.exe（不走 conda run：
rem conda.exe 是控制台进程，会连带弹出黑窗口）
set "PYW=%USERPROFILE%\.conda\envs\QQMonitor\pythonw.exe"
if not exist "%PYW%" set "PYW=%USERPROFILE%\.conda\envs\QQMoniter\pythonw.exe"
if not exist "%PYW%" set "PYW=%ProgramData%\anaconda3\envs\QQMonitor\pythonw.exe"
if not exist "%PYW%" set "PYW=%ProgramData%\anaconda3\envs\QQMoniter\pythonw.exe"
if not exist "%PYW%" for /f "delims=" %%P in ('where pythonw 2^>nul') do set "PYW=%%P"
if not exist "%PYW%" (
    echo [错误] 未找到 pythonw.exe：%PYW%
    echo        请确认 conda 环境 QQMonitor 已创建。
    pause
    exit /b 1
)

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SEL=%~1"
if "%SEL%"=="" goto ask
goto parse

:ask
echo.
echo   要让哪些项目「开机自启」？（选中的自启，未选中的会被取消）
echo     [1] 只启 同步守护 —— 后台定时同步群消息
echo     [2] 只启 Web 看板 —— 局域网可访问，手机也能开
echo     [3] 两个都启（推荐）
echo.
set /p "SEL=输入 1 / 2 / 3 后回车（直接回车 = 3）: "
if "%SEL%"=="" set "SEL=3"

:parse
set "DO_SYNC=0"
set "DO_WEBUI=0"
if "%SEL%"=="1" set "DO_SYNC=1"
if "%SEL%"=="2" set "DO_WEBUI=1"
if "%SEL%"=="3" set "DO_SYNC=1"
if "%SEL%"=="3" set "DO_WEBUI=1"
if /i "%SEL%"=="sync" set "DO_SYNC=1"
if /i "%SEL%"=="webui" set "DO_WEBUI=1"
if /i "%SEL%"=="both" set "DO_SYNC=1"
if /i "%SEL%"=="both" set "DO_WEBUI=1"
if /i "%SEL%"=="all" set "DO_SYNC=1"
if /i "%SEL%"=="all" set "DO_WEBUI=1"
if "%DO_SYNC%%DO_WEBUI%"=="00" (
    echo [错误] 无法识别的选项：%SEL%
    echo        可用：1 / 2 / 3 / sync / webui / both
    pause
    exit /b 1
)

rem 目标状态语义：选中的建快捷方式，未选中的删掉
if "%DO_SYNC%"=="1" call :make_link QQMonitorAutosync sync-daemon
if "%DO_SYNC%"=="0" call :drop_link QQMonitorAutosync
if "%DO_WEBUI%"=="1" call :make_link QQMonitorWebui run
if "%DO_WEBUI%"=="0" call :drop_link QQMonitorWebui

echo.
echo 当前开机自启设置：
if "%DO_SYNC%"=="1" echo   [开] 同步守护（QQMonitorAutosync.lnk）
if "%DO_SYNC%"=="0" echo   [关] 同步守护
if "%DO_WEBUI%"=="1" echo   [开] Web 看板（QQMonitorWebui.lnk）
if "%DO_WEBUI%"=="0" echo   [关] Web 看板
echo.
echo  下次开机按上述设置自动在后台运行（无窗口）。
echo  取消全部自启：autostart-disable.bat
echo  停止当前进程：sync-daemon-stop.bat / webui-daemon-stop.bat
pause
exit /b 0

:make_link
rem %1=快捷方式名（不含扩展名） %2=main.py 的子命令
rem WindowStyle=7 配合 pythonw 实现完全无窗口
powershell -NoProfile -ExecutionPolicy Bypass -Command "$sh=New-Object -ComObject WScript.Shell; $d=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'; $l=$sh.CreateShortcut((Join-Path $d '%~1.lnk')); $l.TargetPath='%PYW%'; $l.Arguments='-X utf8 %CD%\main.py %~2'; $l.WorkingDirectory='%CD%'; $l.WindowStyle=7; $l.Save(); Write-Host '  [开] %~1.lnk'"
goto :eof

:drop_link
if exist "%STARTUP%\%~1.lnk" (
    del /q "%STARTUP%\%~1.lnk"
    echo   [关] %~1.lnk（已移除）
)
goto :eof
