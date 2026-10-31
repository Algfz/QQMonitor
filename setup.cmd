@echo off
rem ===================================================================
rem  QQMonitor 一键安装：找 Python 3.11 -> 建 .venv -> 装依赖 -> 生成配置
rem  用法：双击本文件；或在本目录执行  setup.cmd --cpu / --models all
rem ===================================================================
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3.11 "bootstrap.py" %*
  if not errorlevel 1 goto :done
  py -3 "bootstrap.py" %*
  if not errorlevel 1 goto :done
  goto :failed
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "bootstrap.py" %*
  if not errorlevel 1 goto :done
  goto :failed
)

echo [错误] 没找到 Python。请先安装 Python 3.11：
echo     winget install -e --id Python.Python.3.11
echo   或从 https://www.python.org/downloads/ 下载安装包
pause
exit /b 2

:failed
pause
exit /b 1

:done
echo.
echo 安装完成。双击 webui.bat 启动看板。
pause
exit /b 0
