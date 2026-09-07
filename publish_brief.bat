@echo off
cd /d "%~dp0"
echo IDX Live - Morning Brief PDF publish (briefs/latest_ko.pdf, latest_id.pdf)
set PY=py
where py >nul 2>&1
if errorlevel 1 set PY=python
%PY% publish.py --brief
echo.
echo exit code %errorlevel%
pause
