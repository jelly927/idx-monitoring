#!/usr/bin/env python3
"""공유용 zip 생성 — 파이썬 없이 index.html 만 열면 마지막 수집 데이터가 보이는 묶음."""
import zipfile, time, sys
from pathlib import Path
ROOT = Path(__file__).parent
OUT = ROOT / "IDX_Live_share.zip"
FILES = ["index.html", "data.js", "data.json", "README.md"]
if not (ROOT / "data.js").exists():
    sys.exit("[ERROR] data.js 가 없습니다. start.bat 을 실행해 한 번 수집한 뒤 다시 하세요.")
if OUT.exists(): OUT.unlink()
skipped = []
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for name in FILES:
        p = ROOT / name
        if not p.exists(): continue
        for i in range(3):
            try:
                z.writestr(name, p.read_bytes()); break
            except PermissionError:
                if i == 2: skipped.append(name)
                else: time.sleep(2)
print(f"생성: {OUT.name} ({OUT.stat().st_size//1024} KB)")
if skipped: print("건너뜀(잠김):", ", ".join(skipped), "— index.html/data.js 만 있으면 화면은 정상")
print("받는 사람: 압축 풀고 index.html 더블클릭 (파이썬 불필요). 실시간 갱신은 GitHub Pages 링크로.")
