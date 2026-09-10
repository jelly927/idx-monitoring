@echo off
rem Register IDX Live autostart v2: Startup shortcut + scheduled task (logon + 10-min watchdog) + no-sleep power plan. ASCII only.
cd /d
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_autostart.ps1"
echo.
echo To remove: delete "IDX Live.lnk" in shell:startup and the "IDX Live" task in Task Scheduler.
pause
