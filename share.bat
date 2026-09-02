@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IDX Live - make share zip
set PY=py
where py >nul 2>&1
if errorlevel 1 set PY=python
%PY% make_share.py
pause
