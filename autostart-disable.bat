@echo off
rem 取消 QQMonitor 开机自启
rem
rem 用法：
rem   autostart-disable.bat           取消全部
rem   autostart-disable.bat sync      只取消 同步守护
rem   autostart-disable.bat webui     只取消 Web 看板
rem 注意：只是不再自动启动，已在运行的进程要另行停止：
rem   sync-daemon-stop.bat / webui-daemon-stop.bat
cd /d "%~dp0"
setlocal

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SEL=%~1"
if "%SEL%"=="" set "SEL=both"

set "DO_SYNC=0"
set "DO_WEBUI=0"
if /i "%SEL%"=="both" set "DO_SYNC=1"
if /i "%SEL%"=="both" set "DO_WEBUI=1"
if /i "%SEL%"=="all" set "DO_SYNC=1"
if /i "%SEL%"=="all" set "DO_WEBUI=1"
if /i "%SEL%"=="sync" set "DO_SYNC=1"
if /i "%SEL%"=="webui" set "DO_WEBUI=1"
if "%DO_SYNC%%DO_WEBUI%"=="00" (
    echo [错误] 无法识别的选项：%SEL%
    echo        可用：sync / webui / both
    pause
    exit /b 1
)

if "%DO_SYNC%"=="1" call :drop_link QQMonitorAutosync
if "%DO_WEBUI%"=="1" call :drop_link QQMonitorWebui

echo.
echo 已取消相应的开机自启；当前正在运行的进程请用 stop 脚本停止：
echo   sync-daemon-stop.bat / webui-daemon-stop.bat
pause
exit /b 0

:drop_link
if exist "%STARTUP%\%~1.lnk" (
    del /q "%STARTUP%\%~1.lnk"
    echo   已取消: %~1.lnk
) else (
    echo   未开启: %~1.lnk（本来就没有）
)
goto :eof
