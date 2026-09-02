@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IDX Live - push to GitHub
where git >nul 2>&1
if errorlevel 1 goto nogit
git add -A
git commit -m "update %date% %time%"
git push origin main
echo.
echo ============================================================
echo  Done. The site updates in 1-2 minutes:
echo  https://jelly927.github.io/idx-live/
echo  (First time only: a GitHub login window will open.)
echo ============================================================
goto end
:nogit
echo [ERROR] git not found. Install Git for Windows: https://git-scm.com/download/win
:end
pause
