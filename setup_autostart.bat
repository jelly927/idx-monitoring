@echo off
rem 윈도우 로그인 시 자동 실행 — 시작프로그램 폴더에 바로가기 생성
cd /d "%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\IDX Live.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo 등록 완료: 다음 로그인부터 IDX Live 가 자동으로 켜집니다 (창은 최소화 상태). 해제는 shell:startup 폴더에서 'IDX Live' 바로가기 삭제.
pause
