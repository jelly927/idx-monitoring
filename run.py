#!/usr/bin/env python3
"""
한 번에 실행: 수집기(5분 반복) + 화면 서버(8080) + 브라우저 자동 열기.
    python run.py            (창 하나만 켜두면 됨. 닫으면 멈춤)
    python run.py --port 8090 --interval 300
모든 출력은 run_log.txt 에도 함께 기록된다 — 창이 닫혀도 원인을 볼 수 있다.
"""
import sys, threading, time, webbrowser, subprocess, os, http.server, socketserver, functools, traceback, datetime
from pathlib import Path
ROOT = Path(__file__).parent
os.chdir(ROOT)

# ---------- 콘솔 인코딩: 한글/기호가 cp949 에서 깨지거나 죽지 않도록 ----------
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

# ---------- 로그: 화면 + run_log.txt 동시 기록 ----------
class Tee:
    def __init__(self, stream, fh): self.stream, self.fh = stream, fh
    def write(self, s):
        try: self.stream.write(s)
        except Exception: pass
        try: self.fh.write(s); self.fh.flush()
        except Exception: pass
    def flush(self):
        for x in (self.stream, self.fh):
            try: x.flush()
            except Exception: pass
LOG = open(ROOT / "run_log.txt", "a", encoding="utf-8", errors="replace")
sys.stdout = Tee(sys.__stdout__, LOG); sys.stderr = Tee(sys.__stderr__, LOG)
print("\n" + "=" * 70)
print("IDX Live 시작", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("python :", sys.version.split()[0], "|", sys.executable)
print("폴더   :", ROOT)
print("=" * 70, flush=True)

port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
interval = int(sys.argv[sys.argv.index("--interval") + 1]) if "--interval" in sys.argv else 300

def ensure_deps():
    """requirements 미설치·playwright 브라우저 미설치면 자동으로 설치 (처음 한 번만 시간이 걸림)."""
    try:
        import requests, feedparser, yfinance, bs4, lxml  # noqa
    except ImportError as e:
        print("패키지 설치 중…", e, flush=True)
        subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")])
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:
        print("캘린더/IDX 우회용 브라우저(chromium) 설치 중… (처음 한 번, 1~3분)", repr(e)[:120], flush=True)
        subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])

def fetch_loop():
    """fetch_data.py / config.json 이 바뀌면 자동으로 다시 불러온다 — 창을 재시작할 필요가 없다."""
    import importlib
    watch = [ROOT / "fetch_data.py", ROOT / "config.json", ROOT / "tickers.json"]
    stamp = lambda: tuple(f.stat().st_mtime if f.exists() else 0 for f in watch)
    try:
        ensure_deps()
        import fetch_data
        last = stamp()
    except Exception:
        print("수집기 로딩 실패:"); traceback.print_exc(); return
    while True:
        try:
            cur = stamp()
            if cur != last:
                print(">>> 코드/설정 변경 감지 → 수집기를 다시 불러옵니다", flush=True)
                importlib.reload(fetch_data); last = cur
            fetch_data.build()
            auto_push()
        except Exception:
            print("build error:"); traceback.print_exc()
        finally:
            try: fetch_data.idx_browser_close()
            except Exception: pass
        time.sleep(interval)

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def end_headers(self):
        self.send_header("Cache-Control", "no-store"); super().end_headers()

def find_git():
    """PATH → GitHub Desktop 내장 git → Program Files 순으로 git.exe 를 찾는다."""
    import shutil, glob
    g = shutil.which("git")
    if g: return g
    la = os.environ.get("LOCALAPPDATA", "")
    cands = glob.glob(os.path.join(la, "GitHubDesktop", "app-*", "resources", "app", "git", "cmd", "git.exe"))
    cands += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
              os.path.join(la, "Programs", "Git", "cmd", "git.exe")]
    for c in sorted(cands, reverse=True):
        if os.path.exists(c): return c
    return None

def auto_push():
    """config.json 의 auto_push 가 true 면 수집 결과(data.json, data.js)를 GitHub 로 올린다.
    → GitHub Pages 주소만 공유하면 받는 사람은 파이썬 없이 항상 최신 화면을 본다."""
    try:
        import json as _j
        cfg = _j.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        if not cfg.get("auto_push"): return
        try:
            import fetch_data as _fd
            if getattr(_fd, "PUBLISHED_IN_BUILD", False): return     # build() 안에서 이미 올림
        except Exception: pass
        if (ROOT / "secrets.json").exists():   # git 없이 GitHub API 로 올림 (publish.py) — 토큰은 secrets.json 에 본인이 저장
            args = [sys.executable, str(ROOT / "publish.py"), "--quiet"] + (["--data"] if cfg.get("auto_push_data") else [])
            r = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=300)
            out = (r.stdout + r.stderr).strip().splitlines()
            print("auto_push:", out[-1][:160] if out else f"exit {r.returncode}", flush=True); return
        if not (ROOT / ".git").exists():
            print("auto_push: .git 폴더가 없어 건너뜀 (git init 필요)"); return
        git = find_git()
        if not git: print("auto_push: git 을 찾지 못해 건너뜀 (Git for Windows 또는 GitHub Desktop 설치 필요)"); return
        q = lambda *a: subprocess.run([git, *a], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        files = ["data/cache/tr_claude.json", "data/manual.json", "config.json", "tickers.json"]
        if cfg.get("auto_push_data"): files += ["data.json", "data.js"]   # GitHub 러너가 수집을 못 할 때만 PC 가 data 도 올린다
        q("add", *files)
        c = q("commit", "-q", "-m", ("data " if cfg.get("auto_push_data") else "translations ") + datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + " [skip ci]")
        if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr): return
        p = q("push", "-q", "origin", "main")
        if p.returncode == 0: print("auto_push: GitHub 반영 완료", flush=True)
        else:
            q("pull", "--rebase", "-q", "origin", "main"); p = q("push", "-q", "origin", "main")
            print("auto_push:", "GitHub 반영 완료" if p.returncode == 0 else ("실패 " + (p.stderr or "")[-160:]), flush=True)
    except Exception as e:
        print("auto_push 오류:", repr(e)[:120], flush=True)

def already_running(port):
    """같은 포트에 이미 IDX Live 가 떠 있으면 True. 창을 두 개 띄우면 data.json 과 브라우저가 충돌한다."""
    import socket
    s = socket.socket(); s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", port)); return True
    except Exception:
        return False
    finally:
        s.close()

if __name__ == "__main__":
    if already_running(port):
        print(f"\n[중단] 이미 IDX Live 가 켜져 있습니다 (포트 {port}).")
        print(f"       기존 창을 그대로 쓰시거나, 그 창을 닫은 뒤 다시 실행하세요.")
        print(f"       화면: http://localhost:{port}/\n")
        try: webbrowser.open(f"http://localhost:{port}/")
        except Exception: pass
        input("엔터를 누르면 닫힙니다... ")
        sys.exit(0)
    try:
        threading.Thread(target=fetch_loop, daemon=True).start()
        socketserver.TCPServer.allow_reuse_address = False
        with socketserver.TCPServer(("", port), functools.partial(Quiet, directory=str(ROOT))) as httpd:
            url = f"http://localhost:{port}/"
            print(f"\n>>> 화면 주소: {url}")
            print(f">>> 이 창을 닫으면 멈춥니다. 수집은 {interval}초마다.\n", flush=True)
            threading.Timer(2, lambda: webbrowser.open(url)).start()
            try: httpd.serve_forever()
            except KeyboardInterrupt: print("\n사용자 중단(Ctrl+C)")
    except OSError as e:
        print(f"\n[오류] 포트 {port} 를 열 수 없습니다: {e}")
        print(f"       이미 IDX Live 가 켜져 있을 수 있습니다 → 먼저 http://localhost:{port}/ 를 열어보세요.")
        print(f"       그래도 안 되면: python run.py --port 8090")
    except Exception:
        print("\n[오류] 예기치 못한 종료:"); traceback.print_exc()
    finally:
        print("\n종료됨. 이 내용은 run_log.txt 에도 저장되었습니다.", flush=True)
