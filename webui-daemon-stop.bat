@echo off
rem 停止 QQMonitor WebUI 后台进程（只停 main.py run，不影响 sync-daemon）
cd /d "%~dp0"
setlocal

powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'main\.py\s+run' }; if ($procs) { $procs | ForEach-Object { Write-Host ('停止 PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host '没有找到运行中的 WebUI 进程' }"

rem 端口仍被占用时兜底：按监听 8088 的 PID 再停一次（处理命令行匹配不到的情况）
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8088" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%P >nul 2>&1
)

ping -n 2 127.0.0.1 >nul
exit /b 0
