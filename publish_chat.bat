@echo off
chcp 949 >nul
cd /d "%~dp0"
title IDX Live - chatbot publish
echo [1/2] chat_context.json 생성
set PY=py
where py >nul 2>&1
if errorlevel 1 set PY=python
%PY% make_chat_context.py
if errorlevel 1 goto err
echo.
echo [2/2] GitHub 업로드
%PY% publish.py --chat
if errorlevel 1 goto err
echo.
echo 완료. 실패 0 이면 정상입니다.
goto end
:err
echo.
echo *** 오류 발생 - 위 메시지를 확인하세요 ***
:end
echo.
pause
