@echo off
rem 停止 QQMonitor 自动同步守护进程
cd /d "%~dp0"
setlocal

powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'sync-daemon' }; if ($procs) { $procs | ForEach-Object { Write-Host ('停止 PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host '没有找到运行中的自动同步守护进程' }"

rem 清掉可能残留的锁文件
if exist "data\autosync.daemon.lock" del /q "data\autosync.daemon.lock" 2>nul

ping -n 2 127.0.0.1 >nul
exit /b 0
