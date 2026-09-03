#!/usr/bin/env python3
"""Git 설치 없이 GitHub 저장소에 변경된 파일만 올린다 (GitHub REST API, requests 만 사용).
토큰: secrets.json {"github_token": "github_pat_..."} (gitignore 됨) 또는 환경변수 GITHUB_TOKEN.
사용: python publish.py            → 코드·설정·번역 캐시 등 (data.json 제외, GitHub 러너가 작성)
      python publish.py --data     → data.json / data.js 도 함께 (러너가 IDX 를 못 읽을 때)
      python publish.py --all      → 위 전부 + 전일 IDX 요약 캐시 (ss_*.json)"""
import sys, json, base64, hashlib, time
from pathlib import Path
import requests

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
REPO = CFG.get("github_repo", "jelly927/idx-monitoring"); BRANCH = CFG.get("github_branch", "main")
API = "https://api.github.com"

CODE = ["index.html", "fetch_data.py", "run.py", "selftest.py", "publish.py", "config.json", "tickers.json", "requirements.txt",
        "README.md", "DEPLOY.md", ".gitignore", "start.bat", "push.bat", "share.bat", "make_share.py", "setup_autostart.bat",
        "prepare_upload.py", "upload.bat", "publish.bat", ".github/workflows/update.yml", "worker/worker.js",
        "data/manual.json", "data/idx_part.json", "data/cache/tr_claude.json", "data/cache/rss_map.json", "data/cache/tickers_all.json", "data/cache/kisi_news.json"]
DATA = ["data.json", "data.js", "data/cache/investing_cal.json", "data/cache/news_seen.json"]

def token():
    import os
    t = os.environ.get("GITHUB_TOKEN")
    p = ROOT / "secrets.json"
    if not t and p.exists():
        try: t = json.loads(p.read_text(encoding="utf-8")).get("github_token")
        except Exception as e: sys.exit(f"secrets.json 파싱 실패: {e}")
    if not t:
        sys.exit("토큰 없음: secrets.json 에 {\"github_token\": \"github_pat_...\"} 저장 (README '자동 배포' 참고)")
    return t.strip()

def blob_sha(b: bytes) -> str:
    h = hashlib.sha1(); h.update(f"blob {len(b)}\0".encode()); h.update(b); return h.hexdigest()

def main(argv):
    files = list(CODE)
    if "--data" in argv or "--all" in argv: files += DATA
    if "--all" in argv: files += sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "data" / "cache").glob("ss_*.json"))[-25:]
    quiet = "--quiet" in argv
    s = requests.Session(); s.headers.update({"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    r = s.get(f"{API}/repos/{REPO}/git/trees/{BRANCH}", params={"recursive": "1"}, timeout=30)
    if r.status_code == 401: sys.exit("토큰이 거부됨(401): 토큰 재발급 필요 (Contents: Read and write 권한)")
    if r.status_code == 404: sys.exit(f"저장소를 찾지 못함(404): {REPO} — 토큰에 이 저장소 접근 권한이 있는지 확인")
    r.raise_for_status()
    remote = {t["path"]: t["sha"] for t in r.json().get("tree", []) if t["type"] == "blob"}
    up = skip = fail = 0
    for rel in files:
        p = ROOT / rel
        if not p.exists(): continue
        b = p.read_bytes()
        if remote.get(rel) == blob_sha(b): skip += 1; continue
        # idx_part.json(PC 의 IDX 수집분)만 [skip ci] 없이 올려 GitHub 러너가 바로 병합·배포하게 한다. 나머지는 러너를 깨우지 않음
        tag = "" if rel == "data/idx_part.json" else " [skip ci]"
        body = {"message": f"{rel} {time.strftime('%Y-%m-%d %H:%M')}{tag}", "content": base64.b64encode(b).decode(), "branch": BRANCH}
        if rel in remote: body["sha"] = remote[rel]
        for attempt in range(3):
            pr = s.put(f"{API}/repos/{REPO}/contents/{rel}", json=body, timeout=60)
            if pr.status_code in (200, 201): up += 1; print("  올림:", rel, flush=True); break
            if pr.status_code == 409 or pr.status_code == 422 and "sha" in pr.text:   # 그 사이 원격이 바뀜 → sha 갱신 후 재시도
                g = s.get(f"{API}/repos/{REPO}/contents/{rel}", params={"ref": BRANCH}, timeout=30)
                if g.ok: body["sha"] = g.json().get("sha")
                time.sleep(1.5); continue
            print("  실패:", rel, pr.status_code, pr.text[:120]); fail += 1; break
        else:
            print("  실패(재시도 초과):", rel); fail += 1
    msg = f"publish: 올림 {up} · 동일 {skip} · 실패 {fail}  →  https://github.com/{REPO}"
    print(msg, flush=True)
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
