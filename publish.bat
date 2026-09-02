@echo off
cd /d "%~dp0"
echo IDX Live - GitHub publish
set PY=py
where py >nul 2>&1
if errorlevel 1 set PY=python
%PY% publish.py %*
echo.
echo exit code %errorlevel%
pause
