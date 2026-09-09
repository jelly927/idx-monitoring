#!/usr/bin/env python3
"""Morning Brief 자동 배포 — '데일리 시황' 폴더의 최신 브리프(PPTX/PDF)를 briefs/latest_ko.pdf · latest_id.pdf 로 만든다.
publish.py 가 매 빌드마다 호출 → 새 파일이 생겼을 때만 변환(PowerPoint COM, 없으면 LibreOffice)하고 GitHub 에 올린다.

config.json:
  "brief_dirs_ko": ["C:\\Users\\csd04\\Desktop\\데일리 시황"]   ← KO 브리프 폴더 (여러 개 가능)
  "brief_dirs_id": ["C:\\Users\\csd04\\Downloads"]              ← ID 브리프 폴더 (다운로드 폴더의 kisi_morning_brief_YYYYMMDD.pptx)
  같은 날짜 파일이 여러 개면(_1, (1) 등) 마지막으로 저장된 것을 쓴다.
파일명 규칙(둘 다 날짜가 이름에 있어야 함):
  KO: 260909_데일리시황_KISI.pptx / .pdf      (YYMMDD_데일리시황… 또는 YYMMDD 데일리 시황…)
  ID: kisi_morning_brief_20260909.pptx / .pdf  (kisi_morning_brief[_ID]_YYYYMMDD…)
수동 대체: idx-live 루트 또는 briefs 폴더에 latest_ko.pdf / latest_id.pdf 를 직접 넣으면 그 파일이 더 새로울 때 그대로 쓴다.
상태: data/cache/brief_state.json (어느 원본을 변환했는지 기록 → 같은 파일은 다시 변환하지 않음)"""
import sys, re, json, shutil, subprocess, tempfile, datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
STATE = ROOT / "data" / "cache" / "brief_state.json"
MAX_AGE_DAYS = 7          # 이보다 오래된 원본은 무시 (옛 파일로 최신본을 덮지 않음)
MIN_SETTLE_SEC = 60       # 저장 직후 파일은 잠깐 기다림 (저장 중 변환 방지)

KO_RX = re.compile(r"^(\d{6})[ _]?데일리[ _]?시황.*\.(pptx|pdf)$", re.I)
ID_RX = re.compile(r"^kisi_morning_brief(?:_ID)?_?(\d{8})\D.*\.(pptx|pdf)$|^kisi_morning_brief(?:_ID)?_?(\d{8})\.(pptx|pdf)$", re.I)

def _date(m, lang):
    g = [x for x in m.groups() if x and x.isdigit()][0]
    try: return dt.datetime.strptime(g, "%y%m%d" if len(g) == 6 else "%Y%m%d").date()
    except ValueError: return None

def _dirs(lang):
    """언어별 검색 폴더: config brief_dirs_ko / brief_dirs_id (없으면 brief_dirs) + idx-live 루트·briefs/"""
    try: cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception: cfg = {}
    ds = [Path(d) for d in (cfg.get(f"brief_dirs_{lang}") or cfg.get("brief_dirs") or []) if d]
    ds += [ROOT, ROOT / "briefs"]
    return [d for d in ds if d.is_dir()]

def _candidates(lang):
    rx = KO_RX if lang == "ko" else ID_RX
    today = dt.date.today(); out = []
    for d in _dirs(lang):
        try: names = list(d.iterdir())
        except Exception: continue
        for p in names:
            if not p.is_file() or p.name.startswith("~$"): continue
            m = rx.match(p.name)
            if not m: continue
            day = _date(m, lang)
            if not day or (today - day).days > MAX_AGE_DAYS: continue
            out.append((day, p.stat().st_mtime, p))
    out.sort(reverse=True)
    return out

def _load_state():
    try: return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception: return {}

def _save_state(st):
    try: STATE.parent.mkdir(parents=True, exist_ok=True); STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception: pass

# ---------------- PPTX → PDF ----------------
LAST_ERR = ""
def _ppt_com(src: Path, dst: Path) -> bool:
    """PowerPoint COM (PowerShell 경유, 추가 패키지 불필요). 사용자가 PowerPoint 를 열어 둔 상태여도 그 문서는 건드리지 않고,
    다른 프레젠테이션이 열려 있으면 PowerPoint 를 종료하지 않는다."""
    if sys.platform != "win32": return False
    ps = f'''
$ErrorActionPreference = "Stop"
$app = New-Object -ComObject PowerPoint.Application
$pres = $app.Presentations.Open("{src}", $true, $true, $false)
$pres.SaveCopyAs("{dst}", 32)
$pres.Close()
if ($app.Presentations.Count -eq 0) {{ $app.Quit() }}
"OK"'''
    try:
        import base64
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True, text=True, timeout=180)
        if "OK" in (r.stdout or "") and dst.exists() and dst.stat().st_size > 10_000: return True
        global LAST_ERR
        raw = (r.stderr or "") + " " + (r.stdout or "")
        errs = re.findall(r'<S S="Error">(.*?)</S>', raw, re.S)                     # PowerShell CLIXML 에서 오류 문장만 추출
        txt = " ".join(errs) if errs else raw
        txt = txt.replace("_x000D__x000A_", " ").replace("&quot;", '"').replace("&apos;", "'").replace("&gt;", ">").replace("&lt;", "<")
        LAST_ERR = " ".join(txt.split())[:600] or f"exit {r.returncode}"
        print("  브리프 변환(PowerPoint) 실패:", LAST_ERR[:120], flush=True)
    except Exception as e:
        LAST_ERR = str(e)[:300]
        print("  브리프 변환(PowerPoint) 오류:", LAST_ERR[:120], flush=True)
    return False

def _soffice(src: Path, dst: Path) -> bool:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        for c in (r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
            if Path(c).exists(): exe = c; break
    if not exe: return False
    try:
        subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", str(dst.parent), str(src)], capture_output=True, timeout=240)
        made = dst.parent / (src.stem + ".pdf")
        if made.exists() and made.stat().st_size > 10_000:
            if made != dst: made.replace(dst)
            return True
    except Exception as e:
        print("  브리프 변환(LibreOffice) 오류:", str(e)[:120], flush=True)
    return False

def convert(src: Path, dst: Path) -> bool:
    """원본을 임시 폴더에 복사한 뒤 변환 (열려 있는 원본 파일 보호). 성공 시 dst 에 PDF."""
    tmpd = Path(tempfile.mkdtemp(prefix="brief_"))
    try:
        tsrc = tmpd / re.sub(r"[^\w.\-]", "_", src.name); shutil.copy2(src, tsrc)
        if src.suffix.lower() == ".pdf":
            shutil.copy2(tsrc, dst); return True
        tdst = tmpd / "out.pdf"
        ok = _ppt_com(tsrc, tdst) or _soffice(tsrc, tdst)
        if ok: shutil.copy2(tdst, dst)
        return ok
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

# ---------------- 메인 ----------------
def sync(quiet=False) -> str:
    """briefs/latest_ko.pdf · latest_id.pdf 갱신. 반환: 로그용 짧은 메모 (예: 'KO 09/09 변환')"""
    bd = ROOT / "briefs"; bd.mkdir(exist_ok=True)
    st = _load_state(); notes = []
    now = dt.datetime.now().timestamp()
    for lang in ("ko", "id"):
        dst = bd / f"latest_{lang}.pdf"
        cands = _candidates(lang)
        if cands:
            day, mtime, src = cands[0]
            key = f"{src}|{int(mtime)}"
            cur = st.get(lang, {})
            if key != cur.get("key") and now - mtime >= MIN_SETTLE_SEC:
                if cur.get("date") and str(day) < cur["date"]:
                    pass                                                   # 기록된 것보다 옛 날짜 → 무시
                elif convert(src, dst):
                    st[lang] = {"key": key, "date": str(day), "src": src.name, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
                    notes.append(f"{lang.upper()} {day:%m/%d} ← {src.name}")
                    if not quiet: print("  브리프 변환:", lang, src.name, flush=True)
                else:
                    st[lang] = dict(cur, fail_key=key, fail_msg=LAST_ERR, fail_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # 수동 대체: 루트에 직접 놓인 latest_*.pdf 가 더 새로우면 그대로 사용
        manual = ROOT / dst.name
        if manual.exists() and (not dst.exists() or manual.stat().st_mtime > dst.stat().st_mtime + 1):
            try:
                if not dst.exists() or manual.read_bytes() != dst.read_bytes():
                    shutil.copy2(manual, dst); notes.append(f"{lang.upper()} 수동본"); st[lang] = {"key": "manual|" + str(int(manual.stat().st_mtime)), "date": st.get(lang, {}).get("date", ""), "src": manual.name, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
            except Exception as e:
                print("  브리프 복사 실패:", dst.name, e, flush=True)
    _save_state(st)
    return " · ".join(notes)

if __name__ == "__main__":
    n = sync()
    print("brief_sync:", n or "변경 없음")
    for lang in ("ko", "id"):
        p = ROOT / "briefs" / f"latest_{lang}.pdf"
        if p.exists(): print(f"  {p.name}: {p.stat().st_size:,} bytes · {dt.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}")
    print("  상태:", json.dumps(_load_state(), ensure_ascii=False))
