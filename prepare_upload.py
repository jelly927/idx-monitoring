#!/usr/bin/env python3
"""GitHub 웹 업로드용 폴더(github_upload/)를 만든다 — Git 설치 없이 브라우저로 드래그해 올리기 위함.
포함: 코드·설정·워크플로·번역 캐시·전일 IDX 요약 캐시. 제외: 로그, 백업, zip, 임시 파일."""
import shutil, glob, os, webbrowser
from pathlib import Path
ROOT = Path(__file__).parent; OUT = ROOT / "github_upload"
FILES = ["index.html", "fetch_data.py", "run.py", "selftest.py", "config.json", "tickers.json", "requirements.txt",
         "README.md", "DEPLOY.md", ".gitignore", "start.bat", "push.bat", "share.bat", "make_share.py",
         "setup_autostart.bat", "prepare_upload.py", "upload.bat", "data.json", "data.js",
         "data/manual.json", "data/cache/tr_claude.json", "data/cache/rss_map.json", "data/cache/investing_cal.json",
         ".github/workflows/update.yml", "worker/worker.js"]
FILES += sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "data" / "cache").glob("ss_*.json"))[-25:]
if OUT.exists(): shutil.rmtree(OUT)
n = 0
for rel in FILES:
    src = ROOT / rel
    if not src.exists(): continue
    dst = OUT / rel; dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst); n += 1
print(f"github_upload/ 에 {n}개 파일 준비 완료")
print("1) 열리는 GitHub 업로드 페이지에 github_upload 폴더 안의 내용물 전체(Ctrl+A)를 드래그")
print("2) 아래 'Commit changes' 클릭")
print("   .github 폴더가 안 올라가면: Add file > Create new file > 이름 .github/workflows/update.yml > 내용 붙여넣기")
try:
    os.startfile(str(OUT))
    webbrowser.open("https://github.com/jelly927/idx-live/upload/main")
except Exception: pass
