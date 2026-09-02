@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IDX Live

if not exist "%~dp0run.py" goto nozip
echo %~dp0 | findstr /i "\Temp\" >nul && goto nozip

set PY=py
where py >nul 2>&1
if errorlevel 1 set PY=python
where %PY% >nul 2>&1
if errorlevel 1 goto nopy
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 goto stub

echo Launcher: %PY%
echo.
%PY% run.py
goto done

:nozip
echo [ERROR] run.py not found next to start.bat.
echo         You are probably running start.bat from INSIDE the zip file.
echo         Right-click the zip -> "Extract All..." first, then run start.bat from the extracted folder.
goto done

:nopy
echo [ERROR] Python not found.
echo         Install Python 3.11+ from https://www.python.org/downloads/
echo         and tick "Add python.exe to PATH" during install. Then run start.bat again.
goto done

:stub
echo [ERROR] "python" here is the Microsoft Store placeholder, not real Python.
echo         Install Python from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo         or disable the Store alias: Settings ^> Apps ^> Advanced app settings ^> App execution aliases.
goto done

:done
echo.
echo ============================================================
echo  Stopped. Check the messages above and run_log.txt
echo ============================================================
pause
