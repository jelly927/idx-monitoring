@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IDX Live - make share zip
if not exist data.js (
  echo [ERROR] data.js not found. Run start.bat first and wait for one collection cycle.
  pause & exit /b 1
)
set OUT=IDX_Live_share.zip
if exist "%OUT%" del "%OUT%"
powershell -NoProfile -Command "Compress-Archive -Path 'index.html','data.js','data.json','README.md' -DestinationPath '%OUT%' -Force"
echo.
echo Created %OUT%  (open index.html inside after extracting - no Python needed, shows the latest collected data)
echo For live auto-refresh, send the GitHub Pages link instead.
pause
