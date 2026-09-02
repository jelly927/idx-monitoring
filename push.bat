@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IDX Live - push to GitHub
set GIT=git
where git >nul 2>&1
if not errorlevel 1 goto gitok
for /d %%D in ("%LOCALAPPDATA%\GitHubDesktop\app-*") do if exist "%%D\resources\app\git\cmd\git.exe" set "GIT=%%D\resources\app\git\cmd\git.exe"
if exist "%ProgramFiles%\Git\cmd\git.exe" set "GIT=%ProgramFiles%\Git\cmd\git.exe"
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GIT=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
if "%GIT%"=="git" goto nogit
:gitok

rem data.json / data.js are owned by the GitHub Actions runner - do not push them from this PC
"%GIT%" add -A -- . ":!data.json" ":!data.js" ":!data/cache/investing_cal.json" ":!run_log.txt"
"%GIT%" commit -m "update %date% %time% [skip ci]"

set /a TRY=0
:retry
set /a TRY+=1
"%GIT%" pull --rebase --autostash origin main
"%GIT%" push origin main
if not errorlevel 1 goto ok
if %TRY% GEQ 3 goto fail
echo Retry %TRY%/3 in 5 seconds...
timeout /t 5 /nobreak >nul
goto retry

:ok
echo.
echo ============================================================
echo  Done. The site updates in 1-2 minutes:
echo  https://jelly927.github.io/idx-live/
echo ============================================================
goto end
:fail
echo.
echo [ERROR] push failed 3 times. Copy the messages above and send them.
goto end
:nogit
echo [ERROR] git not found (PATH, GitHub Desktop, Program Files all checked).
echo         Option 1: winget install --id Git.Git -e --source winget   (accept the admin prompt)
echo         Option 2: install GitHub Desktop (no admin needed) https://desktop.github.com  then run push.bat again
:end
pause
