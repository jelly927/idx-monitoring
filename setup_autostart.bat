@echo off
rem Register IDX Live (start.bat) to run at Windows logon (creates a shortcut in the Startup folder). ASCII only - cmd cannot parse UTF-8 Korean.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_autostart.ps1" "%~dp0"
echo.
echo Done. IDX Live will start automatically at Windows logon (minimized window).
echo To remove: delete "IDX Live.lnk" in the shell:startup folder.
pause
