#!/usr/bin/env python3
"""
IDX Live 수집기 v2 — index.html 이 읽는 data.json 을 생성한다.

  python fetch_data.py               1회
  python fetch_data.py --loop 300    300초마다 반복 (장중 권장)

데이터 소스 (전부 공개 · 무료)
  ┌ IDX 공식 (www.idx.co.id/primary/…)  ─ 브라우저 세션 흉내(쿠키+XHR 헤더) 필요
  │  TradingSummary/GetStockSummary?date=YYYYMMDD   전 종목 OHLC·거래대금·외국인 매수/매도·비정규시장 → 급등락·대금급증·외국인 순매수·상승/하락 종목 수
  │  TradingSummary/GetIndexSummary?date=…          COMPOSITE(JCI)·LQ45 등 지수 종가/고저/거래대금
  │  helper/GetIndexChart?indexCode=COMPOSITE&period=1D   장중 스파크라인
  │  Home/GetCalendar?range=m&date=…                기업 일정 (RUPS·Public Expose·Cum/Ex date 등, title=종목코드, Jenis=유형)
  │  DigitalStatistic … LINK_DIVIDEND               배당 공시 (cum/ex/record/payment)
  │  ListedCompany/GetAnnouncement?dateFrom=…       공시 헤드라인 (실적 발표·RUPS 소집 키워드 스크리닝)
  ├ BI 보도자료 "Perkembangan Indikator Stabilitas Nilai Rupiah" (월·금 발표)
  │  루피아 종가(bid)·SBN 10년 yield·DXY·UST 10Y·CDS 5Y·주간 비거주자 순매수(주식/SBN/SRBI)
  ├ Kontan pusatdata yield_sun_acuan (일간 SUN 벤치마크 yield, BI 미발표일 보조)
  ├ Yahoo Finance (yfinance, ~15분 지연)  USDIDR·KRWIDR·Brent·DXY·UST10Y, IDX 실패 시 JCI 대체
  └ 화이트리스트 RSS (config.json) → tickers.json 으로 종목 매칭

원칙: 못 구한 값은 null → 화면에 "확인 필요". 추정 금지. 출처는 항목마다 기록.
"""
import json, re, sys, os, time, html, hashlib, threading, subprocess, queue as _queue, datetime as dt
from pathlib import Path

try:
    import requests, feedparser
except ImportError:
    sys.exit("pip install requests feedparser yfinance pandas beautifulsoup4 lxml")
try:
    import yfinance as yf
except ImportError:
    yf = None
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

ROOT = Path(__file__).parent
CACHE = ROOT / "data" / "cache"; CACHE.mkdir(parents=True, exist_ok=True)
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TICKERS = json.loads((ROOT / "tickers.json").read_text(encoding="utf-8"))
MANUAL_P = ROOT / "data" / "manual.json"
WIB = dt.timezone(dt.timedelta(hours=7))
def now_wib(): return dt.datetime.now(WIB)
def log(*a): print(now_wib().strftime("%H:%M:%S"), *a, flush=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"

# =============================================================== 브라우저 폴백 (Cloudflare 우회)
# IDX 는 Cloudflare 뒤라 python requests 가 403 으로 막힌다. 실제 크로미움으로 열면 통과한다.
# 지속 세션(start() 후 계속 보유)은 Windows 에서 "browser has been closed" 로 끊기므로
# 반드시 with sync_playwright() 블록 안에서 열고 닫는다.
def _pw_call(job, label="", timeout=300):
    """모든 브라우저 작업을 전용 스레드 하나에 몰아서 처리한다.
    - playwright sync API 는 asyncio 루프가 도는 스레드에서 못 쓴다 (yfinance 가 루프를 남긴다)
      → "Playwright Sync API inside the asyncio loop" 오류의 원인
    - playwright 객체는 스레드 간 공유가 안 되므로 세션도 이 스레드에 묶는다"""
    if threading.current_thread() is _PWQ.get("th"):
        return job()                                   # 이미 워커 스레드 안이면 바로 실행
    if _PWQ["q"] is None:
        _PWQ["q"] = _queue.Queue()
        def _loop():
            import asyncio
            try: asyncio.set_event_loop(None)          # 이 스레드에는 루프를 두지 않는다
            except Exception: pass
            while True:
                item = _PWQ["q"].get()
                if item is None: break
                f, box, ev = item
                try: box["v"] = f()
                except Exception as e:
                    box["e"] = e
                    try:
                        import asyncio as _a
                        box["diag"] = f"thread={threading.current_thread().name} loop={_a._get_running_loop()}"
                    except Exception: pass
                finally: ev.set()
        _PWQ["th"] = threading.Thread(target=_loop, daemon=True, name="pw-worker")
        _PWQ["th"].start()
    box, ev = {}, threading.Event()
    _PWQ["q"].put((job, box, ev))
    if not ev.wait(timeout=timeout):
        log("브라우저 시간 초과", label); return None
    if "e" in box:
        log("브라우저 실패", label, str(box["e"])[:120], box.get("diag", "")); return None
    return box.get("v")

def _pw_browser():
    """워커 스레드 안에서만 호출. sync_playwright 인스턴스는 프로세스당 하나만 띄운다.
    (start() 를 여러 번 하면 스레드에 러닝 루프가 남아 다음 호출이 전부 실패한다)"""
    br = _PWQ.get("br")
    if br is not None:
        try:
            br.contexts; return br
        except Exception:
            _pw_shutdown()
    from playwright.sync_api import sync_playwright
    _PWQ["pw"] = sync_playwright().start()
    _PWQ["br"] = _PWQ["pw"].chromium.launch()
    log("브라우저 기동")
    return _PWQ["br"]

def _pw_shutdown():
    """워커 스레드 안에서만 호출."""
    _IDXPW["pg"] = None
    for k, m in (("br", "close"), ("pw", "stop")):
        try:
            o = _PWQ.get(k)
            if o is not None: getattr(o, m)()
        except Exception: pass
    _PWQ["br"] = _PWQ["pw"] = None

def _pw_session(fn, label=""):
    def job():
        try:
            import playwright  # noqa
        except ImportError:
            log("playwright 없음 → 브라우저 폴백 불가 (pip install playwright && playwright install chromium)"); return None
        pg = _pw_browser().new_page(user_agent=UA, locale="id-ID", timezone_id="Asia/Jakarta")
        pg.set_default_timeout(45000)
        try:
            return fn(pg)
        finally:
            try: pg.close()
            except Exception: pass
    return _pw_call(job, label)

def _host(u):
    try: return u.split("/")[2]
    except Exception: return u[:40]

def _wait_cf(pg, tries=20):
    """Cloudflare 대기 화면이 걷힐 때까지."""
    for _ in range(tries):
        try: t = pg.evaluate("() => document.body ? document.body.innerText.slice(0,300) : ''") or ""
        except Exception: t = ""
        if not re.search(r"Just a moment|Checking your browser|Verifying you are human|Tunggu sebentar|verifikasi keamanan|Memverifikasi", t, re.I): return True   # 브라우저 locale 이 id-ID 라 인니어 문구도 뜬다
        pg.wait_for_timeout(1500)
    return False

# IDX 는 호출이 많다(전종목·지수·캘린더·공시·20일 캐시). 매번 브라우저를 새로 띄우면 느리고
# Windows 에서 실행파일 잠금(WinError 32)이 난다 → 빌드 1회당 세션 하나를 열어 재사용하고 끝에 닫는다.
_PWQ = {"q": None, "th": None, "pw": None, "br": None}
_IDXPW = {"pg": None}

def _idx_page(base):
    """워커 스레드 안에서만 호출. IDX 는 호출이 많아 탭 하나를 빌드 내내 재사용한다."""
    pg = _IDXPW.get("pg")
    if pg is not None:
        try:
            pg.evaluate("() => 1"); return pg
        except Exception:
            _IDXPW["pg"] = None
    pg = _pw_browser().new_page(user_agent=UA, locale="id-ID", timezone_id="Asia/Jakarta")
    pg.set_default_timeout(45000)
    pg.goto(base + "/id", wait_until="domcontentloaded", timeout=60000)
    _wait_cf(pg)
    _IDXPW["pg"] = pg
    log("IDX 브라우저 탭 준비 (이후 호출은 이 탭을 재사용)")
    return pg

def _idx_close_now():
    try:
        pg = _IDXPW.get("pg")
        if pg is not None: pg.close()
    except Exception: pass
    _IDXPW["pg"] = None

def idx_browser_close():
    """빌드 종료 시 브라우저를 완전히 내린다."""
    _pw_call(_pw_shutdown, "close", timeout=60)

def browser_text(url, timeout=45000):
    def work(pg):
        pg.goto(url, wait_until="domcontentloaded", timeout=timeout)
        _wait_cf(pg)
        return pg.content()
    return _pw_session(work, _host(url))

def http_text(url, **kw):
    """requests 우선, 막히면 브라우저로 재시도."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30, **kw)
        if r.status_code == 200 and len(r.text) > 200: return r.text
        log("http", r.status_code, _host(url))
    except Exception as e:
        log("http err", _host(url), str(e)[:80])
    return browser_text(url)

# =============================================================== IDX client
class IDX:
    """idx.co.id 는 Cloudflare 뒤에 있어 첫 접속에서 쿠키를 받아야 JSON 엔드포인트가 열린다."""
    BASE = "https://www.idx.co.id"
    def __init__(self):
        try:
            import cloudscraper                     # 403 나오면 pip install cloudscraper
            self.s = cloudscraper.create_scraper()
        except ImportError:
            self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                               "Accept-Language": "en-US,en;q=0.9,id;q=0.8", "Referer": self.BASE + "/"})
        self.ready = False
        self.blocked = False          # 403 한 번 겪으면 이후엔 곧장 브라우저로
    def session(self):
        if self.ready: return
        self.s.get(self.BASE + "/id", timeout=30); time.sleep(1)
        self.s.get(self.BASE + "/primary/home/GetIndexList", headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
        self.ready = True
    def get(self, path, **params):
        import os
        proxy = os.environ.get("IDX_PROXY")            # Cloudflare Worker 주소. GitHub 러너 IP가 IDX에 막히면 Worker 경유
        if not proxy:
            try: self.session()
            except Exception as e: log("IDX 세션 준비 실패", str(e)[:80])
        for i in (range(3) if not self.blocked else []):
            try:
                if proxy:
                    full = requests.Request("GET", self.BASE + path, params=params).prepare().url
                    r = requests.get(proxy.rstrip("/") + "/?url=" + requests.utils.quote(full, safe=""), timeout=40)
                else:
                    r = self.s.get(self.BASE + path, params=params, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=40)
                if r.status_code == 200: return r.json()
                log("IDX", r.status_code, path)
                if r.status_code == 403:
                    self.blocked = True; break        # Cloudflare 차단은 재시도로 안 풀린다 → 바로 브라우저로
            except Exception as e:
                log("IDX err", path, e)
            time.sleep(2 * (i + 1))
        full = requests.Request("GET", self.BASE + path, params=params).prepare().url
        j = self.browser_get(full)
        if j is not None:
            log("IDX 브라우저 경유 OK", path); return j
        log("IDX 최종 실패", path)
        return None

    def browser_get(self, full_url):
        """Cloudflare 를 통과한 페이지 안에서 fetch() 로 JSON 을 받는다. 세션은 빌드 내내 재사용."""
        base = self.BASE
        def job():
            for attempt in (1, 2):
                try:
                    pg = _idx_page(base)
                    return pg.evaluate("""async (u) => {
                        try { const r = await fetch(u, {headers:{'X-Requested-With':'XMLHttpRequest'}});
                              if(!r.ok) return null; return await r.json(); }
                        catch(e) { return null; } }""", full_url)
                except Exception as e:
                    log("IDX 브라우저", ("재시도" if attempt == 1 else "실패"), str(e)[:110])
                    _idx_close_now()
            return None
        return _pw_call(job, "idx.co.id")

    # ---- 일별 전종목 요약 (확정된 날은 캐시)
    def stock_summary(self, d, force=False):
        f = CACHE / f"ss_{d:%Y%m%d}.json"
        if f.exists() and not force:
            return json.loads(f.read_text(encoding="utf-8"))
        j = self.get("/primary/TradingSummary/GetStockSummary", date=f"{d:%Y%m%d}", length=9999, start=0)
        time.sleep(0.6)                                    # IDX 과호출 방지 (실제 네트워크 호출일 때만)
        rows = (j or {}).get("data") or []
        if j is not None and (d < now_wib().date() or (rows and now_wib().hour >= 17)):
            f.write_text(json.dumps(rows), encoding="utf-8")       # 과거 날짜는 빈 결과(주말·휴장)도 캐시
        return rows
    def index_summary(self, d):
        j = self.get("/primary/TradingSummary/GetIndexSummary", lang="id", date=f"{d:%Y%m%d}", start=0, length=9999)
        return (j or {}).get("data") or []
    def index_chart(self, code="COMPOSITE", period="1D", meta=None):
        """1D 차트 종가열. meta(dict) 를 주면 마지막 점의 시각·값을 채운다 (장중 현재 지수)"""
        j = self.get("/primary/helper/GetIndexChart", indexCode=code, period=period)
        pts = [p for p in ((j or {}).get("ChartData") or []) if p.get("Close")]        # 장 시작 전엔 ChartData 가 null
        if meta is not None and pts:
            last = pts[-1]; raw = str(last.get("Date") or last.get("DateTime") or last.get("Time") or "")
            try:
                if re.fullmatch(r"\d{12,13}", raw): ts = dt.datetime.fromtimestamp(int(raw) / 1000, dt.timezone.utc).replace(tzinfo=None)      # IDX 는 WIB 시각을 UTC 인 것처럼 epoch(ms) 로 준다
                elif re.fullmatch(r"\d{9,10}", raw): ts = dt.datetime.fromtimestamp(int(raw), dt.timezone.utc).replace(tzinfo=None)
                else: ts = dt.datetime.fromisoformat(raw[:19]) if raw else None
                meta.update({"px": float(last["Close"]), "date": ts.date().isoformat() if ts else None, "ts": ts.strftime("%H:%M") if ts else None, "raw": raw[:25]})
            except Exception: meta.update({"px": float(last["Close"]), "raw": raw[:25]})
        return [p["Close"] for p in pts]
    def calendar(self, d):
        j = self.get("/primary/Home/GetCalendar", range="m", date=f"{d:%Y%m%d}")
        return (j or {}).get("Results") or []
    def dividends(self, y, m):
        j = self.get("/primary/DigitalStatistic/GetApiDataPaginated", urlName="LINK_DIVIDEND", periodYear=y, periodMonth=m,
                     periodType="monthly", isPrint="False", cumulative="false", pageSize=500, pageNumber=1)
        return (j or {}).get("data") or []
    def announcements(self, d_from, d_to, code=""):
        j = self.get("/primary/ListedCompany/GetAnnouncement", kodeEmiten=code, indexFrom=0, pageSize=1000,
                     dateFrom=f"{d_from:%Y%m%d}", dateTo=f"{d_to:%Y%m%d}", lang="id")
        return (j or {}).get("Replies") or []

idx = IDX()

def last_trading_day(with_data):
    """오늘부터 거꾸로 데이터가 있는 첫 날짜."""
    d = now_wib().date()
    for _ in range(7):
        rows = with_data(d)
        if rows: return d, rows
        d -= dt.timedelta(days=1)
    return None, []

# =============================================================== market block from IDX
def yahoo_intraday(codes, chunk=150):
    """장중 종목 시세 (Yahoo, ~15분 지연). {code: {"px","vol","val"}}. val ≈ 거래량 × (고+저+종)/3."""
    if yf is None or not codes: return {}
    out = {}
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        try:
            df = yf.download([c + ".JK" for c in part], period="1d", interval="1d", group_by="ticker",
                             threads=True, progress=False, auto_adjust=False)
        except Exception as e:
            log("yahoo 장중 랭킹 chunk 실패", str(e)[:80]); continue
        for c in part:
            try:
                d = df[c + ".JK"] if len(part) > 1 else df
                row = d.dropna(subset=["Close"]).iloc[-1]
                px, vol = float(row["Close"]), float(row["Volume"] or 0)
                if not px or px != px or vol <= 0: continue
                vwap = float((row["High"] + row["Low"] + row["Close"]) / 3)
                out[c] = {"px": px, "vol": vol, "val": vol * vwap, "hi": float(row["High"]), "lo": float(row["Low"])}
            except Exception:
                continue
        time.sleep(0.4)
    return out

def yahoo_market(chunk=150):
    """IDX 가 막힌 환경(GitHub 러너 등)용: Yahoo 일봉(약 2개월)만으로 랭킹·전 종목·등락 집계를 만든다.
    외국인·공시는 Yahoo 에 없으므로 None (build 가 PC 분리 파일로 채운다)."""
    if yf is None: return None
    names = all_tickers() or {}; shares = {}
    try:
        part_p = ROOT / "data" / "idx_part.json"
        if part_p.exists():
            for st in json.loads(part_p.read_text(encoding="utf-8")).get("stocks", []):
                names.setdefault(st["t"], [st.get("n") or st["t"]]); shares[st["t"]] = st.get("sh") or 0
    except Exception: pass
    codes = sorted(names)
    if len(codes) < 50: log("yahoo_market: 종목 목록 없음"); return None
    today = now_wib().date(); in_session = today.weekday() < 5 and 9 <= now_wib().hour < 17
    allst = []; adv = dec = unch = 0; value = 0.0; last_date = None
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        try:
            df = yf.download([c + ".JK" for c in part], period="2mo", interval="1d", group_by="ticker", threads=True, progress=False, auto_adjust=False)
        except Exception as e:
            log("yahoo_market chunk 실패", str(e)[:80]); continue
        for c in part:
            try:
                d = (df[c + ".JK"] if len(part) > 1 else df).dropna(subset=["Close"])
                if len(d) < 2: continue
                row, prev = d.iloc[-1], d.iloc[-2]
                px, pc = float(row["Close"]), float(prev["Close"])
                if not px or px != px or not pc: continue
                vol = float(row["Volume"] or 0)
                val = vol * float((row["High"] + row["Low"] + row["Close"]) / 3)
                hist = d.iloc[-21:-1]
                hv = (hist["Volume"] * (hist["High"] + hist["Low"] + hist["Close"]) / 3).dropna()
                avg = float(hv.mean()) if len(hv) >= 5 else None
                ld = d.index[-1].date() if hasattr(d.index[-1], "date") else None
                if ld and (last_date is None or ld > last_date): last_date = ld
                if vol <= 0: continue
                pct = round((px / pc - 1) * 100, 2)
                adv += px > pc; dec += px < pc; unch += px == pc; value += val
                allst.append({"t": c, "n": names.get(c, [c])[0], "px": px, "prev": pc, "pct": pct, "val": val, "vol": vol, "sh": shares.get(c, 0), "mcap": px * shares.get(c, 0),
                              "hi": float(row["High"]), "lo": float(row["Low"]), "ratio": round(val / avg, 1) if avg else None, "fnet": None, "live": 1 if in_session else 0})
            except Exception:
                continue
        time.sleep(0.4)
    if len(allst) < 50: log(f"yahoo_market: 유효 종목 부족 ({len(allst)})"); return None
    minval = CFG.get("min_value_idr", 1e9)
    liquid = [x for x in allst if x["val"] >= minval]
    dstr = (last_date or today).isoformat()
    log(f"yahoo_market {len(allst)}종목 (기준일 {dstr}) · 상승 {adv} 하락 {dec}")
    return {"date": dstr, "adv": adv, "dec": dec, "unch": unch, "value_idr": value, "nonreg_idr": None,
            "foreign_buy": None, "foreign_sell": None, "foreign_net_idr": None, "foreign_note": None,
            "stocks": sorted(allst, key=lambda x: -x["val"]),
            "value": sorted(liquid, key=lambda x: -x["val"])[:10],
            "gainers": sorted(liquid, key=lambda x: -x["pct"])[:10], "losers": sorted(liquid, key=lambda x: x["pct"])[:10],
            "turnover": sorted([x for x in liquid if x["ratio"]], key=lambda x: -x["ratio"])[:10],
            "foreign_top": [], "foreign_bottom": [], "hist_days": 20, "src": "yahoo",
            "rank_date": dstr, "rank_src": f"Yahoo 15분 지연 · {len(allst)}종목" + ("" if in_session else " · 종가"), "rank_asof": now_wib().strftime("%H:%M") if in_session else None}

def idx_market():
    d, rows = last_trading_day(idx.stock_summary)
    if not rows:
        try: return yahoo_market()
        except Exception as e: log("yahoo_market 오류", str(e)[:120]); return None
    today = now_wib().date()
    in_session = (d < today) and today.weekday() < 5 and 9 <= now_wib().hour < 17
    if in_session and CFG.get("intraday_rank", True):
        mk = _market_from_rows(d, rows)                    # 전일 확정치 (외국인·시장 합계)
        prev = {r["StockCode"]: r for r in rows if r.get("Close")}
        universe = [c for c, r in prev.items() if (r.get("Value") or 0) >= CFG.get("intraday_min_prev_value", 1e8)]
        q = yahoo_intraday(universe)
        matched = [c for c in q if prev.get(c) and prev[c].get("Close")]
        moved = sum(1 for c in matched if q[c]["px"] != prev[c]["Close"])
        if len(q) >= 50 and matched and moved / len(matched) < 0.02:   # 개장 직후(15분 지연) 아직 체결 전 → 전일 확정치 유지
            log(f"장중 랭킹: Yahoo 시세가 아직 전일 종가와 동일 ({moved}/{len(matched)} 변동) → 전일 IDX 확정치 표시"); q = {}
        if len(q) >= 50:
            hist = mk.pop("_hist", {})
            liquid = []
            for c, v in q.items():
                r = prev.get(c)
                if not r or not r.get("Close"): continue
                pc = r["Close"]
                if v["val"] < CFG.get("min_value_idr", 1e9): continue
                h = hist.get(c, []); avg = sum(h) / len(h) if len(h) >= 5 else None
                liquid.append({"t": c, "n": (r.get("StockName") or "").replace("Tbk.", "").strip(), "px": v["px"],
                               "pct": round((v["px"] / pc - 1) * 100, 2), "val": v["val"],
                               "ratio": round(v["val"] / avg, 1) if avg else None, "nonreg": 0,
                               "fbuy": None, "fsell": None, "fnet": None})
            for st in mk.get("stocks", []):
                v = q.get(st["t"])
                if not v or not st.get("px"): continue
                pc = st["px"]                                  # 장중 기준가 = 전일(IDX 확정) 종가
                st.update({"px": v["px"], "prev": pc, "pct": round((v["px"] / pc - 1) * 100, 2), "val": v["val"], "vol": v.get("vol"),
                           "hi": v.get("hi"), "lo": v.get("lo"), "live": 1, "mcap": v["px"] * (st.get("sh") or 0)})
                h = hist.get(st["t"], []); avg = sum(h) / len(h) if len(h) >= 5 else None
                st["ratio"] = round(v["val"] / avg, 1) if avg else None
            mk["stocks"] = sorted(mk.get("stocks", []), key=lambda x: -(x["val"] or 0))
            qq = {c: v for c, v in q.items() if c in prev and prev[c].get("Close")}
            adv = sum(1 for c, v in qq.items() if v["px"] > prev[c]["Close"])
            dec = sum(1 for c, v in qq.items() if v["px"] < prev[c]["Close"])
            unch = len(qq) - adv - dec
            mk.update({"rank_date": today.isoformat(), "adv": adv, "dec": dec, "unch": unch,
                       "value_idr_intraday": sum(v["val"] for v in q.values()), "rank_cover": len(q),
                       "rank_src": f"Yahoo 15분 지연 · {len(q)}종목", "rank_asof": now_wib().strftime("%H:%M"),
                       "value": sorted(liquid, key=lambda x: -x["val"])[:10],
                       "gainers": sorted(liquid, key=lambda x: -x["pct"])[:10], "losers": sorted(liquid, key=lambda x: x["pct"])[:10],
                       "turnover": sorted([x for x in liquid if x["ratio"]], key=lambda x: -x["ratio"])[:10]})
            log(f"장중 랭킹 Yahoo {len(q)}/{len(universe)}종목 · 상승 {adv} 하락 {dec}")
            return mk
        log(f"장중 랭킹 Yahoo 실패({len(q)}종목) → 전일 IDX 확정치 표시")
        mk.pop("_hist", None); return mk
    mk = _market_from_rows(d, rows); mk.pop("_hist", None)
    mk.update({"rank_date": d.isoformat(), "rank_src": f"IDX 확정 {d:%m/%d}", "rank_asof": None})
    return mk

def _market_from_rows(d, rows):
    d, rows = last_trading_day(idx.stock_summary)
    if not rows: return None
    stocks = [r for r in rows if r.get("Close") and r.get("Previous")]
    adv = sum(1 for r in stocks if r["Change"] > 0); dec = sum(1 for r in stocks if r["Change"] < 0)
    unch = sum(1 for r in stocks if r["Change"] == 0 and r.get("Volume"))
    value = sum(r.get("Value") or 0 for r in rows)
    # IDX StockSummary 의 ForeignBuy/ForeignSell 은 '체결 주식 수'다. 금액(IDR)으로 쓰려면 체결단가를 곱한다.
    def _upx(r):
        v, vol = (r.get("Value") or 0), (r.get("Volume") or 0)
        return (v / vol) if vol else (r.get("Close") or 0)
    fbuy = sum((r.get("ForeignBuy") or 0) * _upx(r) for r in rows)
    fsell = sum((r.get("ForeignSell") or 0) * _upx(r) for r in rows)
    nonreg = sum(r.get("NonRegularValue") or 0 for r in rows)
    # 20일 평균 대금 (과거 요약은 캐시에서; 첫 실행만 IDX 호출)
    hist = {}; dd = d - dt.timedelta(days=1); n = 0; tries = 0
    while n < 20 and tries < 40:
        f = CACHE / f"ss_{dd:%Y%m%d}.json"
        past = json.loads(f.read_text(encoding="utf-8")) if f.exists() else (idx.stock_summary(dd) if tries < 30 else [])
        if past:
            for r in past: hist.setdefault(r["StockCode"], []).append(r.get("Value") or 0)
            n += 1
        dd -= dt.timedelta(days=1); tries += 1
    minval = CFG.get("min_value_idr", 1e9)
    liquid = []
    for r in stocks:
        if (r.get("Value") or 0) < minval: continue
        h = hist.get(r["StockCode"], [])
        avg = sum(h) / len(h) if len(h) >= 5 else None
        liquid.append({"t": r["StockCode"], "n": (r.get("StockName") or "").replace("Tbk.", "").strip(), "px": r["Close"],
                       "pct": round(r["Change"] / r["Previous"] * 100, 2), "val": r["Value"],
                       "ratio": round(r["Value"] / avg, 1) if avg else None,
                       "nonreg": round((r.get("NonRegularValue") or 0) / r["Value"] * 100) if r["Value"] else 0,
                       "fbuy": (r.get("ForeignBuy") or 0) * _upx(r), "fsell": (r.get("ForeignSell") or 0) * _upx(r),
                       "fnet": ((r.get("ForeignBuy") or 0) - (r.get("ForeignSell") or 0)) * _upx(r)})
    # 종목 검색용 전 종목 (거래 없는 종목 제외)
    allst = []
    for r in stocks:
        if not r.get("Volume"): continue
        h = hist.get(r["StockCode"], []); avg = sum(h) / len(h) if len(h) >= 5 else None
        sh = r.get("ListedShares") or 0
        allst.append({"t": r["StockCode"], "n": (r.get("StockName") or "").replace("Tbk.", "").strip(), "px": r["Close"], "prev": r["Previous"],
                      "sh": sh, "mcap": (r["Close"] or 0) * sh,
                      "pct": round(r["Change"] / r["Previous"] * 100, 2), "val": r["Value"] or 0, "vol": r.get("Volume") or 0,
                      "hi": r.get("High"), "lo": r.get("Low"), "ratio": round((r["Value"] or 0) / avg, 1) if avg else None,
                      "fnet": ((r.get("ForeignBuy") or 0) - (r.get("ForeignSell") or 0)) * _upx(r)})
    return {"date": d.isoformat(), "adv": adv, "dec": dec, "unch": unch, "value_idr": value, "nonreg_idr": nonreg,
            "stocks": sorted(allst, key=lambda x: -x["val"]),
            "foreign_buy": fbuy, "foreign_sell": fsell, "foreign_net_idr": fbuy - fsell,
            "foreign_note": None,
            "value": sorted(liquid, key=lambda x: -x["val"])[:10],
            "gainers": sorted(liquid, key=lambda x: -x["pct"])[:10], "losers": sorted(liquid, key=lambda x: x["pct"])[:10],
            "turnover": sorted([x for x in liquid if x["ratio"]], key=lambda x: -x["ratio"])[:10],
            "foreign_top": sorted(liquid, key=lambda x: -x["fnet"])[:5], "foreign_bottom": sorted(liquid, key=lambda x: x["fnet"])[:5],
            "hist_days": n, "_hist": hist}

def idx_index():
    d, rows = last_trading_day(idx.index_summary)
    if not rows: return None
    def pick(code):
        for r in rows:
            if (r.get("IndexCode") or "").upper() == code: return r
    jci, lq = pick("COMPOSITE"), pick("LQ45")
    if not jci: return None
    prev, px = jci["Previous"], jci["Close"]
    today = now_wib().date()
    m1, m2 = {}, {}
    sp = idx.index_chart("COMPOSITE", "1D", m1); sp_lq = idx.index_chart("LQ45", "1D", m2) if lq else []
    out = {"date": d.isoformat(), "px": px, "prev": prev, "chg": round(px - prev, 2), "pct": round((px / prev - 1) * 100, 2),
           "high": jci["Highest"], "low": jci["Lowest"], "value_idr": jci.get("Value"), "volume": jci.get("Volume"),
           "lq45": {"px": lq["Close"], "prev": lq["Previous"], "pct": round((lq["Close"] / lq["Previous"] - 1) * 100, 2), "spark": sp_lq} if lq else None,
           "spark": sp, "ts": now_wib().strftime("%H:%M") if d == today else None}
    # 장중: 일별 요약(d)이 어제까지라도 1D 차트의 마지막 점이 오늘이면 그것이 현재 지수 — 어제 종가(px)를 prev 로 두고 intraday 로 표시
    if d < today and m1.get("date") == today.isoformat() and m1.get("px"):
        out["intraday"] = {"px": m1["px"], "prev": px, "ts": m1.get("ts") or now_wib().strftime("%H:%M"), "spark": sp, "high": max(sp) if sp else None, "low": min(sp) if sp else None}
        if lq and m2.get("date") == today.isoformat() and m2.get("px"):
            out["intraday_lq45"] = {"px": m2["px"], "prev": lq["Close"], "ts": m2.get("ts") or out["intraday"]["ts"], "spark": sp_lq}
        log(f"IDX 장중 지수 {m1['px']} ({out['intraday']['ts']}, 차트 {len(sp)}점 · {m1.get('raw')})")
    elif d < today and sp: log(f"IDX 1D 차트 마지막 점 날짜 확인 필요: {m1.get('raw')}")
    return out

# =============================================================== corporate calendar from IDX
KW = [("Laporan Keuangan", "실적 공시"), ("RUPS", "주주총회"), ("Public Expose", "기업설명회"), ("Cum Date", "배당부 마감"),
      ("Ex Date", "배당락"), ("Dividen", "배당"), ("Right", "유상증자"), ("Stock Split", "액면분할"), ("Buyback", "자사주"),
      ("Tender Offer", "공개매수"), ("Suspensi", "거래정지"), ("Pencatatan", "상장")]
def kor_type(s):
    for k, v in KW:
        if k.lower() in (s or "").lower(): return v
    return s or "공시"

def probe_ipo():
    """IDX 배당·IPO 엔드포인트 점검 (PC 에서 하루 1회). 결과 샘플을 data/cache/probe_*.json 에 남긴다."""
    if os.environ.get("GITHUB_ACTIONS"): return
    f = CACHE / "probe_done.txt"
    if f.exists() and (time.time() - f.stat().st_mtime) < 86400: return
    today = now_wib().date()
    try:
        dv = idx.dividends(today.year, today.month); log(f"probe 배당 {today:%Y-%m}: {len(dv)}건")
        (CACHE / "probe_dividend.json").write_text(json.dumps(dv[:3], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e: log("probe 배당 오류", e)
    cands = [("/primary/ListedCompany/GetIPO", {}), ("/primary/ListedCompany/GetIpo", {}), ("/primary/Home/GetIpo", {}),
             ("/primary/ListedCompany/GetCompanyProfilesIPO", {}), ("/primary/DigitalStatistic/GetApiDataPaginated", {"urlName": "LINK_IPO", "periodYear": today.year, "periodType": "yearly", "isPrint": "False", "pageSize": 100, "pageNumber": 1}),
             ("/primary/ListedCompany/GetRightIssue", {}), ("/primary/ListedCompany/GetNewListing", {"year": today.year}),
             ("/primary/ListedCompany/GetDividend", {}), ("/primary/Home/GetDividend", {}), ("/primary/ListedCompany/GetCorporateAction", {}),
             ("/primary/ListedCompany/GetIPOList", {}), ("/primary/DigitalStatistic/GetApiDataPaginated", {"urlName": "LINK_DIVIDEND", "periodYear": today.year, "periodMonth": today.month, "periodType": "monthly", "isPrint": "False", "cumulative": "false", "pageSize": 50, "pageNumber": 1}),
             ("/primary/Home/GetCalendar", {"range": "m", "date": f"{today:%Y%m%d}"})]
    res = {}
    for path, params in cands:
        try:
            j = idx.get(path, **params)
            n = len(j) if isinstance(j, list) else len((j or {}).get("data") or (j or {}).get("Results") or (j or {}).get("Replies") or [])
            res[path] = {"type": type(j).__name__, "n": n, "sample": (j[:2] if isinstance(j, list) else j) if j else None}
            log(f"probe {path}: {type(j).__name__} {n}")
        except Exception as e: res[path] = {"err": str(e)[:80]}
    try: (CACHE / "probe_ipo.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str)[:200000], encoding="utf-8")
    except Exception: pass
    f.write_text(now_wib().isoformat(), encoding="utf-8")

def _co(name):
    """회사명 정리: PT · Tbk · (Persero) 제거"""
    n = (name or "").strip()
    for _ in range(2): n = re.sub(r"^PT\.?\s+|\s+Tbk\.?$|\s*\(Persero\)\s*", " ", n, flags=re.I).strip(" .")
    return n

def _corp_title(jenis, desc):
    """IDX 캘린더 정형 항목을 한국어·인니어 제목으로. (제목, 인니어제목)"""
    j = (jenis or "").strip().lower(); dl = desc.lower()
    m = re.search(r"pemberitahuan rups\s*(rencana)?\s*([\d-]+\s+)?(.+)$", desc, flags=re.I)
    if j == "rencana" and m:
        co = _co(m.group(3)); return f"주주총회 · {co} 주주총회 개최 예정", f"RUPS · Pemberitahuan RUPS {co}"
    if j in ("rupo", "rupsu", "rupup"):
        who = {"rupo": ("사채권자집회", "RUPO"), "rupsu": ("수쿠크보유자집회", "RUPSU"), "rupup": ("수익자총회", "RUPUP")}[j]
        act = "소집" if re.search(r"panggilan", dl) else "연기" if re.search(r"penundaan", dl) else "계획" if re.search(r"rencana", dl) else "결과" if re.search(r"hasil", dl) else ""
        obj = re.sub(r"^(rencana|panggilan|penundaan|hasil|pemberitahuan)\s+(dan\s+panggilan\s+)?(rupo|rupsu|rupup)\s*(emisi)?\s*", "", desc, flags=re.I).strip()
        return f"{who[0]} {act} · {obj}".strip(" ·"), f"{who[1]} · {desc}"
    if j in ("tahunan", "insidentil"):
        kind = "연례" if j == "tahunan" else "수시"
        act = "결과" if re.search(r"hasil", dl) else "자료" if re.search(r"materi", dl) else "계획" if re.search(r"rencana", dl) else "취소" if re.search(r"pembatalan", dl) else ""
        co = _co(re.sub(r"^(rencana|hasil|materi)\s+(public expose\s+)?(tahunan|insidentil)\s*(\d{4})?\s*", "", desc, flags=re.I))
        return f"기업설명회 · {co} {kind} Public Expose {act}".strip(), f"Public Expose · {desc}"
    if re.search(r"materi public expose", dl):
        co = _co(re.sub(r"^materi public expose\s+(tahunan|insidentil)?\s*", "", desc, flags=re.I))
        return f"기업설명회 · {co} Public Expose 자료", f"Public Expose · {desc}"
    if re.search(r"pembayaran kupon|pembayaran ijarah", dl):
        rest = re.sub(r"^informasi pembayaran\s+", "", desc, flags=re.I)
        return f"공시 · 이자 지급 — {rest}", f"Keterbukaan · {desc}"
    if re.search(r"pembelian kembali|buyback", dl):
        return "자사주 · 자사주 매입 보고", f"Buyback · {desc}"
    return f"{kor_type(jenis or desc)} · {desc}".strip(" ·"), None

def idx_corp_calendar(days_ahead=14, days_back=1):
    today = now_wib().date(); out = []
    try: probe_ipo()
    except Exception as e: log("probe 오류", e)
    months = (today, (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1))
    for m in months:                                                      # 1) IDX 캘린더
        for e in idx.calendar(m):
            try: d = dt.datetime.fromisoformat(str(e.get("start"))[:19]).date()
            except Exception: continue
            if -days_back <= (d - today).days <= days_ahead:
                desc = (e.get("description") or "").strip(); dl = desc.lower(); kind = "corp"; imp = 2
                if "dividen" in dl:                                            # 배당 일정: cum / ex / 기준일(DPS) / 지급
                    kind = "div"
                    lab = ("배당부 마감(cum)" if " cum " in f" {dl} " else "배당락(ex)" if " ex " in f" {dl} " else "배당 기준일(DPS)" if "dps" in dl else "배당 지급" if "pembayaran" in dl else "배당")
                    m = re.search(r"dividen tunai( interim| final)?", dl); typ = ("중간" if m and "interim" in m.group(0) else "결산" if m and "final" in m.group(0) else "")
                    imp = 3 if lab.startswith(("배당부", "배당락")) else 1
                    co = _co(re.sub(r"^tanggal\s+\w+\s+dividen tunai( interim| final)?\s*", "", desc, flags=re.I))
                    title = f"{lab} · {typ}현금배당" + (f" — {co}" if co else "")
                    title_id = re.sub(r"^tanggal\s+", "", desc, flags=re.I)
                else:
                    title, title_id = _corp_title(e.get("Jenis") or "", desc)
                ev = {"date": d.isoformat(), "t": (e.get("title") or "").strip()[:6], "kind": kind, "imp": imp, "title": title, "src": "IDX 캘린더"}
                if title_id: ev["title_id"] = title_id
                out.append(ev)
    # 신규 상장 (회사 프로필의 상장일 기준, 최근 30일)
    try:
        for code, info in (all_tickers() or {}).items():
            ld = info[3] if len(info) > 3 else None
            if not ld: continue
            d = dt.date.fromisoformat(ld[:10])
            if -30 <= (d - today).days <= days_ahead:
                out.append({"date": d.isoformat(), "t": code, "kind": "ipo", "imp": 2, "title": f"신규 상장 · {info[0]}" + (f" ({info[1]})" if len(info) > 1 and info[1] else ""), "src": "IDX 상장"})
    except Exception as e: log("신규 상장 목록 오류", e)
    for m in months:                                                      # 2) 배당 cum/ex/payment
        for r in idx.dividends(m.year, m.month):
            for k, lab in (("cumDividend", "배당부 마감"), ("exDividend", "배당락"), ("paymentDate", "배당 지급")):
                try: d = dt.datetime.fromisoformat(str(r.get(k))[:10]).date()
                except Exception: continue
                if -days_back <= (d - today).days <= days_ahead:
                    cash = r.get("cashDividend")
                    out.append({"date": d.isoformat(), "t": r.get("code"), "kind": "corp", "imp": 2 if lab == "배당락" else 1,
                                "title": f"{lab} · 현금배당 Rp{cash:,.0f}/주" if cash else lab, "src": "IDX 배당공시"})
    for a in idx.announcements(today - dt.timedelta(days=2), today):     # 3) 공시 헤드라인 (실적·RUPS·Public Expose)
        p = a.get("pengumuman", {}); title = p.get("JudulPengumuman") or ""
        if not any(k.lower() in title.lower() for k, _ in KW[:3]): continue
        try: d = dt.datetime.fromisoformat(str(p.get("TglPengumuman"))[:10]).date()
        except Exception: d = today
        out.append({"date": d.isoformat(), "t": (p.get("Kode_Emiten") or "").strip(), "kind": "corp", "imp": 3 if "Laporan Keuangan" in title else 2,
                    "title": f"{kor_type(title)} · {title[:90]}", "src": "IDX 공시"})
    uni = set(CFG["universe"]); seen = set(); res = []
    per_day = {}
    for o in sorted(out, key=lambda o: (o["date"], o["kind"] not in ("div", "ipo"), o["t"] not in uni, -o["imp"])):
        o["imp"] = 3 if o["t"] in uni and o["imp"] >= 2 else o["imp"]
        k = (o["date"], o["t"], o["title"][:30])
        if k in seen: continue
        if o["kind"] == "corp" and per_day.get(o["date"], 0) >= CFG.get("corp_per_day", 15): continue   # 날짜별 상한 (배당·상장은 제한 없음)
        per_day[o["date"]] = per_day.get(o["date"], 0) + (o["kind"] == "corp")
        seen.add(k); res.append(o)
    return res[:200]

# =============================================================== global / ID calendar
def saveticker_calendar(days=14):
    """saveticker.com/calendar 는 클라이언트 렌더링 → playwright 로 열어 DOM 파싱.
    실패 시 data/cache/saveticker.html 에 원본 저장 (셀렉터 조정용)."""
    out = []
    def _collect(pg):
        for i in range(days):
            d = now_wib().date() + dt.timedelta(days=i)
            try:
                pg.goto(f"https://www.saveticker.com/calendar?date={d.isoformat()}", wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(1800)
                html_ = pg.content()
            except Exception as e:
                log("saveticker", d.isoformat(), str(e)[:60]); continue
            if i == 0: (CACHE / "saveticker.html").write_text(html_, encoding="utf-8")
            # 카드 후보: 시간(HH:MM) + 제목 + (예상: x 이전: y) 패턴을 텍스트에서 추출
            txt = re.sub(r"\s+", " ", re.sub("<[^>]+>", "\n", html_))
            for m in re.finditer(r"(\d{1,2}:\d{2})\s*((?:★|☆|\*){0,3})?\s*([^\n(]{4,80}?)\s*(?:\(예상: ([^ )]+)\s*이전: ([^ )]+)\))?(?:\s*실제: ([^\n ]+))?", txt):
                tm, st, title, exp, prev, act = m.groups()
                title = title.strip()
                if not title or "알림" in title: continue
                imp = st.count("★") if st else 2
                country = "ID" if re.search(r"인도네시아|Indonesia", title) else "US" if re.search(r"美|미국|ISM|JOLTS|연준|Fed|CPI|고용", title) else "GL"
                out.append({"date": d.isoformat(), "time": tm, "kind": "macro", "country": country, "title": title, "imp": imp, "exp": exp, "prev": prev, "act": act, "src": "saveticker"})
        return out
    _pw_session(_collect, "saveticker")
    if not out: log("saveticker 결과 없음 → investing.com 대체")
    return out

INVESTING_CAL = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
INVESTING_CAL_PAGE = "https://www.investing.com/economic-calendar/"

def _week_start():
    d = now_wib().date(); return d - dt.timedelta(days=d.weekday())      # 이번 주 월요일

def _inv_form(days, countries):
    d0 = _week_start(); d1 = now_wib().date() + dt.timedelta(days=days)
    return ([("country[]", c) for c in countries]
            + [("importance[]", "1"), ("importance[]", "2"), ("importance[]", "3"),
               ("dateFrom", d0.isoformat()), ("dateTo", d1.isoformat()), ("timeZone", "113"),
               ("timeFilter", "timeOnly"), ("currentTab", "custom"), ("limit_from", "0")])

def _inv_parse(html_frag, tz_offset=7, keep=("ID", "US")):
    """investing.com 캘린더 HTML(XHR 조각 또는 페이지 표) → 이벤트 리스트. WIB 로 환산."""
    if not html_frag or BeautifulSoup is None: return []
    soup = BeautifulSoup(html_frag, "lxml"); out = []
    shift = dt.timedelta(hours=7 - tz_offset)
    for tr in soup.select("tr"):
        raw = tr.get("event_timestamp") or tr.get("data-event-datetime")
        if not raw: continue
        ts = None
        for f in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try: ts = dt.datetime.strptime(raw.strip(), f); break
            except Exception: pass
        if ts is None: continue
        ts += shift
        flag = tr.select_one(".flagCur span"); cty = (flag.get("title") if flag else "") or ""
        cc = "ID" if "Indonesia" in cty else "US" if "United States" in cty else (cty[:2].upper() or "GL")
        if keep and cc not in keep: continue
        imp = len(tr.select(".sentiment .grayFullBullishIcon"))
        ev = tr.select_one("td.event")
        title = (ev.get_text(" ", strip=True) if ev else "").strip()
        if not title or len(title) < 3: continue
        g = lambda sel: (tr.select_one(sel).get_text(strip=True) if tr.select_one(sel) else None) or None
        act, fore, prev = g("td.act"), g("td.fore"), g("td.prev")
        out.append({"date": ts.date().isoformat(), "time": ts.strftime("%H:%M"), "kind": "macro", "country": cc,
                    "title": title, "imp": imp or 1, "exp": fore, "prev": prev, "act": act, "src": "investing.com"})
    return out

def _tz_offset_from(text):
    m = re.search(r"GMT\s*([+-])\s*(\d{1,2})(?::(\d{2}))?", text or "")
    if not m: return 7
    return (1 if m.group(1) == "+" else -1) * (int(m.group(2)) + int(m.group(3) or 0) / 60)

def investing_calendar(days=14, countries=("5", "48")):
    """investing.com 경제 캘린더 — 이번 주 월요일부터. 실제치(act)는 발표 후 다음 수집에서 자동 반영."""
    form = _inv_form(days, countries)
    try:                                                  # 1) XHR (requests)
        r = requests.post(INVESTING_CAL, data=form, timeout=40,
                          headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Referer": INVESTING_CAL_PAGE})
        if r.status_code == 200:
            # timeZone=113 응답은 WIB(+7) 보다 1시간 빠른 것으로 확인됨(PMI 08:30↔07:30, ISM 22:00↔21:00) → UTC+8 로 환산
            out = _inv_parse((r.json() or {}).get("data", ""), tz_offset=CFG.get("investing_tz_offset", 8))
            out = [e for e in out if e["country"] != "US" or (e.get("imp") or 1) >= CFG.get("calendar_min_imp_us", 2)]
            if out: log(f"캘린더 investing.com {len(out)}건 (http)"); _inv_cache_save(out); return out
        log("investing 캘린더 http", r.status_code, "빈 응답")
    except Exception as e:
        log("investing 캘린더 http 실패", str(e)[:80])

    def work(pg):                                         # 2) 브라우저 — 새 레이아웃(datatable-v2) 표를 직접 읽는다
        pg.goto(INVESTING_CAL_PAGE, wait_until="domcontentloaded", timeout=60000)
        _wait_cf(pg)
        for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept')"):
            try: pg.click(sel, timeout=2000); break
            except Exception: pass
        try:                                              # '이번 주' 탭 (버튼 텍스트로 찾는다)
            pg.click("button:has-text('This Week')", timeout=8000); pg.wait_for_timeout(3500)
        except Exception as e:
            log("investing 'This Week' 클릭 실패", str(e)[:60])
        return pg.evaluate("""() => {
            const out = []; let cur = null;
            for (const tr of document.querySelectorAll('table tbody tr')) {
                const txt = (tr.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!tr.id) { const m = txt.match(/^[A-Z][a-z]+day, ([A-Z][a-z]+ \\d{1,2}, \\d{4})/); if (m) cur = m[1]; continue; }
                const tds = tr.querySelectorAll('td'); if (tds.length < 8) continue;
                const flag = tr.querySelector('[title]');
                const name = tr.querySelector('td:nth-child(4) a div') || tr.querySelector('td:nth-child(4) a');
                out.push({ date: cur, time: tds[1].textContent.trim(), country: flag ? flag.getAttribute('title') : '',
                           title: name ? name.textContent.trim() : '', imp: tr.querySelectorAll('td:nth-child(5) svg.opacity-60').length,
                           act: tds[5].textContent.trim(), fore: tds[6].textContent.trim(), prev: tds[7].textContent.trim() });
            }
            const tz = (document.body.textContent.match(/GMT[+-]\\d{1,2}:\\d{2}/) || [''])[0];
            return { rows: out, tz: tz };
        }""")
    res = _pw_session(work, "investing-calendar") or {}
    out = _inv_rows(res.get("rows") or [], tz_offset=_tz_offset_from(res.get("tz")))
    out = [e for e in out if e["country"] != "US" or (e.get("imp") or 1) >= CFG.get("calendar_min_imp_us", 2)]
    if out:
        log(f"캘린더 investing.com {len(out)}건 (browser, {res.get('tz') or 'tz?'})"); _inv_cache_save(out); return out
    log(f"캘린더 investing.com 실패 (browser, 행 {len(res.get('rows') or [])}) → 마지막 성공분 사용")
    return _inv_cache_load()

INV_CACHE_P = CACHE / "investing_cal.json"
def _inv_cache_save(out):
    """새로 받은 행을 캐시와 합친다 — 부분 응답(예: 브라우저 경로가 당일치만 준 경우)에도 주간 일정이 사라지지 않게.
    같은 (날짜·시각·국가·제목) 은 새 값으로 덮어써 실제치가 갱신되고, 10일 지난 항목은 버린다."""
    try:
        old = []
        try: old = json.loads(INV_CACHE_P.read_text(encoding="utf-8")).get("events") or []
        except Exception: pass
        key = lambda e: (e.get("date"), e.get("time"), e.get("country"), (e.get("title") or "")[:60])
        m = {key(e): e for e in old}
        m.update({key(e): e for e in out})
        lim = (now_wib().date() - dt.timedelta(days=10)).isoformat()
        merged = sorted([e for e in m.values() if (e.get("date") or "") >= lim], key=lambda e: (e.get("date") or "", e.get("time") or ""))
        INV_CACHE_P.write_text(json.dumps({"saved": now_wib().isoformat(), "events": merged}, ensure_ascii=False), encoding="utf-8")
        out[:] = merged
    except Exception as ex: log("캘린더 캐시 저장 실패", ex)
def _inv_cache_load():
    """두 경로 다 실패해도 캘린더가 사라지지 않도록 마지막 성공분을 쓴다 (실제치는 다음 성공 때 갱신)."""
    try:
        j = json.loads(INV_CACHE_P.read_text(encoding="utf-8")); ev = j.get("events") or []
        if ev: log(f"캘린더 캐시 {len(ev)}건 (저장 {j.get('saved','')[:16]})")
        return ev
    except Exception: return []

def _inv_rows(rows, tz_offset=7, keep=("ID", "US")):
    """새 레이아웃(datatable-v2)에서 JS 로 뽑은 행 → 이벤트. 페이지 표시 시간대를 WIB 로 환산."""
    out = []; shift = dt.timedelta(hours=7 - (tz_offset if tz_offset is not None else 7))
    for r in rows:
        try: d0 = dt.datetime.strptime(r.get("date") or "", "%B %d, %Y")
        except Exception: continue
        tm = (r.get("time") or "").strip(); m = re.match(r"^(\d{1,2}):(\d{2})$", tm)
        if m: ts = d0.replace(hour=int(m.group(1)), minute=int(m.group(2))) + shift; time_s = ts.strftime("%H:%M"); date_s = ts.date().isoformat()
        else: time_s = "—"; date_s = d0.date().isoformat()           # All Day / Tentative
        cty = r.get("country") or ""
        cc = "ID" if "Indonesia" in cty else "US" if "United States" in cty else (cty[:2].upper() or "GL")
        if keep and cc not in keep: continue
        title = (r.get("title") or "").strip()
        if len(title) < 3: continue
        g = lambda k: ((r.get(k) or "").strip() or None)
        out.append({"date": date_s, "time": time_s, "kind": "macro", "country": cc, "title": title, "imp": int(r.get("imp") or 1) or 1,
                    "exp": g("fore"), "prev": g("prev"), "act": g("act"), "src": "investing.com"})
    return out

def macro_calendar_auto(days=14):
    """설정에 따라 글로벌 매크로 캘린더 소스를 고른다. 기본은 investing.com."""
    src = (CFG.get("calendar_source") or "investing").lower()
    if src == "saveticker":
        out = saveticker_calendar(days)
        if out: return out
        log("saveticker 실패 → investing.com 으로 대체")
    return investing_calendar(days)

def idx_announcements_today(hours=30):
    """IDX 공시 스트림 (실시간 갱신용). 최근 N시간 공시 전체를 시간순으로."""
    today = now_wib().date(); out = []
    for a in idx.announcements(today - dt.timedelta(days=1), today):
        p = a.get("pengumuman", {}); title = p.get("JudulPengumuman") or ""
        try: ts = dt.datetime.fromisoformat(str(p.get("TglPengumuman"))[:19]).replace(tzinfo=WIB)
        except Exception: continue
        if (now_wib() - ts).total_seconds() > hours * 3600: continue
        att = (a.get("attachments") or [{}])[0].get("FullSavePath")
        out.append({"ts": ts.isoformat(), "date": ts.date().isoformat(), "time": ts.strftime("%H:%M"), "t": (p.get("Kode_Emiten") or "").strip(),
                    "type": p.get("JenisPengumuman") or "", "title": title, "url": ("https://www.idx.co.id" + att) if att and att.startswith("/") else att})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:400]        # 24시간(실제 창 30시간) 내 공시를 모두 싣는다. 400 은 data.json 비대화 방지용 안전장치

# =============================================================== BI press release
BI_LIST = "https://www.bi.go.id/id/publikasi/ruang-media/news-release/"
def bi_indicators():
    """월·금 발표. 최신 'Perkembangan Indikator Stabilitas Nilai Rupiah' 보도자료에서 수치 추출."""
    try:
        h = http_text(BI_LIST)
        if not h: return None
        urls = re.findall(r'href="([^"]*news-release/Pages/sp_[^"]+\.aspx)"', h)
        cand = [u for u in urls if re.search(r"Indikator Stabilitas", h[max(0, h.find(u) - 300):h.find(u) + 600], re.I)]
        if not cand: return None
        url = cand[0] if cand[0].startswith("http") else "https://www.bi.go.id" + cand[0]
        t = http_text(url) or ""
        t = re.sub("<[^>]+>", " ", html.unescape(t)); t = re.sub(r"\s+", " ", t)
        num = lambda s: float(s.replace(".", "").replace(",", ".")) if s else None
        g = lambda rx: (re.search(rx, t, re.I) or [None, None])[1]
        out = {"src": url, "as_of": g(r"Indikator Stabilitas Nilai Rupiah \(([^)]+)\)"),
               "idr_close": num(g(r"Rupiah ditutup pada level \(bid\) Rp([\d\.]+)")),
               "idr_open":  num(g(r"Rupiah dibuka pada level \(bid\) Rp([\d\.]+)")),
               "sun10y":    num(g(r"Yield SBN(?: \(Surat Berharga Negara\))? 10 tahun (?:naik|turun|tetap|stabil)? ?(?:ke|di|pada)? ?([\d,]+)%")),
               "dxy":       num(g(r"DXY\S* (?:menguat|melemah|stabil|tetap)? ?(?:ke|di|pada)? ?level ([\d,]+)")),
               "ust10y":    num(g(r"UST[^%]{0,60}10 tahun (?:naik|turun|tetap|stabil)? ?(?:ke|di|pada)? ?([\d,]+)%")),
               "cds5y":     num(g(r"CDS Indonesia 5 tahun per [^ ]+ [^ ]+ \d{4} sebesar ([\d,]+) bps"))}
        m = re.search(r"Berdasarkan data transaksi ([^,]+), nonresiden tercatat (jual|beli) neto sebesar Rp([\d,]+) triliun(.*?)(?:Selama tahun|$)", t, re.I)
        if m:
            out["nonres_week"] = {"period": m.group(1), "total": (-1 if m.group(2).lower() == "jual" else 1) * num(m.group(3)) * 1e12,
                                  "text": ("nonresiden " + m.group(2) + " neto Rp" + m.group(3) + " triliun" + m.group(4)).strip()[:220]}
        m2 = re.search(r"Selama tahun \d{4}, berdasarkan data setelmen s\.d\. ([^,]+), (.*?)\.", t, re.I)
        if m2: out["nonres_ytd_text"] = m2.group(2)[:220]
        return out
    except Exception as e:
        log("BI fail", e); return None

_IV = {}                                   # 빌드 1회분 investing.com 시세 캐시
INVESTING_QUOTES = {
    "SUN10Y": "https://www.investing.com/rates-bonds/indonesia-10-year-bond-yield",
    "CPO": "https://www.investing.com/commodities/palm-oil",          # Bursa Malaysia FCPO 근월물 (MYR/톤)
}

def investing_quote(url):
    """investing.com 공개 시세 페이지에서 현재가·변동을 읽는다 (실시간, 브라우저 경유).
    반환: {"px": float, "chg": float|None, "pct": float|None, "asof": str|None} · 실패 시 None"""
    def work(pg):
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        _wait_cf(pg)
        for _ in range(6):
            v = pg.evaluate("""() => {
                const q = s => { const e = document.querySelector(s); return e ? e.textContent.trim() : null; };
                return {last: q('[data-test="instrument-price-last"]'),
                        chg:  q('[data-test="instrument-price-change"]'),
                        pct:  q('[data-test="instrument-price-change-percent"]'),
                        time: q('[data-test="trading-time-label"]')};
            }""")
            if v and v.get("last"): return v
            pg.wait_for_timeout(1200)
        return None
    v = _pw_session(work, "investing")
    if not v or not v.get("last"): return None
    num = lambda x: None if x is None else float(re.sub(r"[^0-9.\-]", "", str(x)) or 0)
    try:
        px = float(str(v["last"]).replace(",", ""))
    except Exception:
        return None
    return {"px": px, "chg": num(v.get("chg")), "pct": num(v.get("pct")), "asof": v.get("time")}

def kontan_sun10y():
    """Kontan pusatdata 벤치마크 SUN yield 표 — '10 tahun' 행의 첫 숫자."""
    if BeautifulSoup is None: return None
    try:
        h = http_text("https://pusatdata.kontan.co.id/market/yield_sun_acuan") or ""
        for tr in BeautifulSoup(h, "lxml").select("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(re.search(r"\b10\b.*tahun", c, re.I) for c in cells[:2]):
                nums = [c for c in cells if re.match(r"^\d+[\.,]\d+", c)]
                if nums: return float(nums[0].replace(",", "."))
    except Exception as e:
        log("Kontan yield fail", e)
    return None

# =============================================================== Yahoo
def yspark(sym):
    if yf is None: return []
    try:
        h = yf.Ticker(sym).history(period="1d", interval="5m")
        return [round(float(v), 2) for v in h["Close"].dropna().tolist()]
    except Exception: return []

def ylive(sym):
    """장중 실시간(≈15분 지연) 시세: 1분봉 마지막 값 + 시각. 전일 종가는 prev 로만 쓴다."""
    if yf is None: return None
    try:
        t = yf.Ticker(sym)
        h = t.history(period="2d", interval="1m")
        if h.empty: return None
        c = h["Close"].dropna()
        last_ts = c.index[-1].tz_convert(WIB) if c.index.tz is not None else c.index[-1].tz_localize("UTC").tz_convert(WIB)
        today = now_wib().date()
        todays = c[c.index.tz_convert(WIB).date == today] if c.index.tz is not None else c
        if last_ts.date() != today: return None          # 오늘 틱이 없으면 라이브 아님
        prev = None
        try:
            pc = float(t.fast_info["previousClose"]); prev = pc if pc > 0 else None
        except Exception: pass
        if prev is None:                                  # 폴백: 일봉에서 '오늘보다 앞선 마지막 날' (위치가 아니라 날짜로)
            d = t.history(period="7d", interval="1d")["Close"].dropna()
            di = d.index.tz_convert(WIB) if d.index.tz is not None else d.index
            dd = d[[x.date() < today for x in di]]
            prev = float(dd.iloc[-1]) if len(dd) else float(d.iloc[-1])
        return {"px": float(c.iloc[-1]), "prev": prev, "ts": last_ts.strftime("%H:%M"),
                "spark": [round(float(v), 2) for v in todays.tolist()][-120:], "high": float(todays.max()), "low": float(todays.min())}
    except Exception as e:
        log("ylive fail", sym, e); return None

def ylive_fx(sym):
    """환율용: 15분봉 5일치에서 최근 24시간(WIB)을 스파크로. 오늘 틱이 없어도 마지막 24시간을 보여준다."""
    if yf is None: return None
    try:
        h = yf.Ticker(sym).history(period="5d", interval="15m")["Close"].dropna()
        if len(h) < 4: return None
        idx = h.index.tz_convert(WIB) if h.index.tz is not None else h.index.tz_localize("UTC").tz_convert(WIB)
        last_ts = idx[-1]
        if (now_wib() - last_ts).total_seconds() > 12 * 3600: return None
        d = yf.Ticker(sym).history(period="5d", interval="1d")["Close"].dropna()
        prev = float(d.iloc[-2]) if len(d) >= 2 and d.index[-1].date() == last_ts.date() else float(d.iloc[-1]) if len(d) else float(h.iloc[0])
        recent = h[idx >= last_ts - dt.timedelta(hours=24)]
        return {"px": float(h.iloc[-1]), "prev": prev, "ts": last_ts.strftime("%H:%M"),
                "spark": [round(float(v), 2) for v in recent.tolist()][-120:], "high": float(recent.max()), "low": float(recent.min())}
    except Exception as e:
        log("ylive_fx fail", sym, e); return None

def yq(sym):
    if yf is None: return None
    try:
        c = yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=False)["Close"].dropna()
        if len(c) < 2: return None
        return {"px": float(c.iloc[-1]), "prev": float(c.iloc[-2]), "m1": float(c.iloc[0]), "spark": [round(float(v), 2) for v in c.tolist()]}
    except Exception as e:
        log("yahoo fail", sym, e); return None

MACRO_ID = [  # 시장지표 라벨/주석 → 인니어 (긴 것부터)
    ("USD/IDR (BI bid 종가)", "USD/IDR (penutupan bid BI)"),
    ("USD/IDR (Yahoo 15분 지연)", "USD/IDR (Yahoo, tunda 15 mnt)"),
    ("IDR/KRW (1원당 루피아)", "IDR/KRW (Rupiah per 1 Won)"), ("KRW/IDR (1원당 루피아)", "KRW/IDR (Rupiah per 1 Won)"),
    ("비거주자 주간 순매수", "Net beli nonresiden mingguan"),
    ("국채 10년물", "Obligasi negara 10 tahun"), ("국채 1년물 (단기)", "Obligasi negara 1 tahun (jangka pendek)"),
    ("달러 인덱스 (DXY)", "Indeks dolar (DXY)"), ("금 (US$/oz)", "Emas (US$/oz)"), ("석탄 Newcastle", "Batu bara Newcastle"), ("니켈 LME", "Nikel LME"), ("주석 LME", "Timah LME"),
    ("Bursa Malaysia FCPO 근월물", "FCPO Bursa Malaysia kontrak terdekat"), ("Yahoo 15분 지연", "Yahoo, tunda 15 mnt"), ("6월 2회 +25bp", "Juni 2x +25bp"),
    ("investing.com 실시간", "investing.com real-time"),
    ("BI 보도자료", "Siaran pers BI"),
    ("연초", "Awal tahun"), ("수기", "manual"), ("확인 필요", "perlu konfirmasi"),
    ("6월", "Jun"), ("2회", "2x"),
]
def _id_label(t):
    if not t: return t
    for ko, idn in MACRO_ID: t = t.replace(ko, idn)
    return t

def macro_block(bi):
    yb = CFG["ytd_base"]; out = []; m = manual()
    def row(k, q, inv=False, base=None, fmt="{:,.2f}", inverse_ytd=False, note=None, src=None):
        if not q or q.get("px") is None:
            out.append({"k": k, "v": "확인 필요", "d": None, "ytd": None, "inv": inv, "note": note}); return
        d = round((q["px"] / q["prev"] - 1) * 100, 2) if q.get("prev") else None
        ytd = None
        if base: ytd = round((base / q["px"] - 1) * 100, 2) if inverse_ytd else round((q["px"] / base - 1) * 100, 2)
        out.append({"k": k, "v": fmt.format(q["px"]), "d": d, "ytd": ytd, "inv": inv, "note": " · ".join(x for x in (note, src) if x) or None})
    # 상단 카드에 이미 있는 USD/IDR·국채 10년물은 제외. 순서: IDR/KRW → USD/KRW → UST 10Y → BI Rate → DXY → WTI → Brent → 금 → 석탄 → 니켈 → 주석 → CPO
    usdidr = yq("USDIDR=X") or ylive_fx("USDIDR=X") or ylive("USDIDR=X"); kr = yq("KRWIDR=X")   # 일봉이 비면(러너에서 간헐적) 15분봉·1분봉으로
    row("USD/IDR", usdidr, inv=True, base=yb.get("USDIDR"), fmt="{:,.0f}", note=f'Yahoo 15분 지연 · 연초 {yb.get("USDIDR"):,}')
    row("KRW/IDR (1원당 루피아)", kr, inv=True, base=yb.get("KRWIDR"), note=f'Yahoo 15분 지연 · 연초 {yb.get("KRWIDR")}')
    def irow(key, label, fmt, inv, note):
        v = _IV.get(key)
        if not v: out.append({"k": label, "v": "확인 필요", "d": None, "ytd": None, "inv": inv, "note": note or None}); return
        base = yb.get(key) or v.get("base")
        ytd = round((v["px"] / base - 1) * 100, 2) if base else None
        src = "Yahoo 15분 지연" if str(v.get("asof", "")).startswith("Yahoo") else "investing.com"
        out.append({"k": label, "v": fmt.format(v["px"]), "d": v.get("pct"), "ytd": ytd, "inv": inv,
                    "note": " · ".join(x for x in (note, src, (f'연초 {base:,.4g}' if base and key not in ("TIN", "NICKEL") else f'연초 {base:,.0f}' if base else None)) if x) or None})
    spec = {k: (lab, fmt, inv, note) for k, _, lab, fmt, inv, note in INV_QUOTES}
    irow("UST10Y", *spec["UST10Y"])
    out.append({"k": "BI Rate (7D RR)", "v": f'{m["bi_rate"]:.2f}%' if m.get("bi_rate") else "확인 필요", "d": None, "ytd": None, "note": m.get("bi_note")})
    for key in ("DXY", "WTI", "GOLD", "COAL", "NICKEL", "CPO"): irow(key, *spec[key])
    if bi and bi.get("cds5y"): out.append({"k": "CDS 5Y (bps)", "v": f'{bi["cds5y"]:.2f}', "d": None, "ytd": None, "inv": True, "note": "BI 보도자료"})
    if bi and bi.get("nonres_week"):
        w = bi["nonres_week"]; out.append({"k": f'비거주자 주간 순매수 ({w["period"]})', "v": f'{w["total"]/1e12:+.2f}조', "d": None, "ytd": None, "note": w["text"]})
    for r in out:                                   # 인니어 화면용 라벨
        r["k_ko"] = r["k"]; r["k_id"] = _id_label(r["k"])
        if r.get("note"): r["note_ko"] = r["note"]; r["note_id"] = _id_label(r["note"])
    return out

# =============================================================== news
STOP = {"PT", "TBK", "IDX", "BEI", "OJK", "RUPS", "IPO", "ETF", "SBN", "SUN", "USD", "IDR", "HAJI", "DANA", "BANK", "SAHAM", "EMAS", "ASIA", "CPO", "LNG", "BUMN", "ASEAN", "MSCI", "FTSE", "APBN", "OPEC", "NYSE", "GDP", "CEO", "CFO", "BPS", "AS", "CNBC", "ANTM"}
def all_tickers():
    """IDX 전체 상장 종목 (코드 → 회사명). 하루 1회 캐시. 실패 시 tickers.json 만 사용."""
    f = CACHE / "tickers_all.json"
    if f.exists() and (time.time() - f.stat().st_mtime) < 86400:
        return json.loads(f.read_text(encoding="utf-8"))
    j = idx.get("/primary/ListedCompany/GetCompanyProfiles", start=0, length=9999)
    rows = (j or {}).get("data") or []
    out = {}
    if rows:
        try: (CACHE / "profiles_sample.json").write_text(json.dumps(rows[:3], ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception: pass
    for r in rows:
        code = (r.get("KodeEmiten") or "").strip().upper(); name = (r.get("NamaEmiten") or "").strip()
        if len(code) == 4 and name:
            clean = re.sub(r"^PT\.?\s+|\s+Tbk\.?$|\s*\(Persero\)\s*", " ", name, flags=re.I).strip()
            sec = (r.get("Sektor") or r.get("Sector") or r.get("sektor") or "").strip()
            sub = (r.get("SubSektor") or r.get("SubSector") or r.get("subsektor") or "").strip()
            out[code] = [clean, sec, sub, str(r.get("TanggalPencatatan") or "")[:10]]
    if out:
        f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8"); log("tickers_all", len(out))
        return out
    if f.exists():                                   # IDX 가 막힌 환경 → 오래된 캐시라도 사용
        try: return json.loads(f.read_text(encoding="utf-8"))
        except Exception: pass
    return {}

def build_alias():
    universe = {t: v[:1] for t, v in all_tickers().items()}          # [회사명, 업종, 세부업종] 중 회사명만 별칭으로
    for t, names in TICKERS.items(): universe[t] = list(dict.fromkeys((universe.get(t) or []) + names))
    codes = set(universe) - STOP
    code_rx = re.compile(r"\b([A-Z]{4})\b")
    name_rx = []
    for t, names in universe.items():
        for n in names:
            if len(n) >= 6 and n.upper() not in STOP:      # 짧은 회사명은 오탐이 많아 6자 이상만
                name_rx.append((t, re.compile(r"\b" + re.escape(n) + r"\b", re.I)))
    return codes, code_rx, name_rx
CODES, CODE_RX, NAME_RX = set(), None, []
def screen(text):
    hits = {c for c in CODE_RX.findall(text) if c in CODES} if CODE_RX else set()
    for t, rx in NAME_RX:
        if rx.search(text): hits.add(t)
    return sorted(hits)

def discover_rss(src):
    """rss 가 비어 있으면 홈에서 <link rel=alternate type=application/rss+xml> 탐색 (캐시)."""
    f = CACHE / "rss_map.json"; mp = json.loads(f.read_text()) if f.exists() else {}
    if src["name"] in mp: return mp[src["name"]]
    found = None
    try:
        h = requests.get(src.get("home", src["rss"]), headers={"User-Agent": UA}, timeout=20).text
        m = re.search(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]+href="([^"]+)"', h, re.I) or re.search(r'href="([^"]+)"[^>]+type="application/(?:rss|atom)\+xml"', h, re.I)
        if m:
            found = m.group(1)
            if found.startswith("/"): found = re.match(r"https?://[^/]+", src.get("home", src["rss"])).group(0) + found
    except Exception as e: log("discover fail", src["name"], e)
    mp[src["name"]] = found; f.write_text(json.dumps(mp)); return found

def scrape_home(src, limit=25):
    """RSS 가 전혀 없는 매체: 홈의 기사 링크 텍스트를 헤드라인으로 (제목 25자 이상 anchor)."""
    try:
        h = requests.get(src["home"], headers={"User-Agent": UA}, timeout=20).text
        base = re.match(r"https?://[^/]+", src["home"]).group(0); out = []; seen = set()
        for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>([^<]{25,140})</a>', h):
            url, title = m.group(1), html.unescape(m.group(2)).strip()
            if title in seen: continue
            seen.add(title); out.append({"title": title, "link": url if url.startswith("http") else base + url, "summary": ""})
            if len(out) >= limit: break
        return out
    except Exception as e: log("scrape fail", src["name"], e); return []

# =============================================================== 헤드라인 번역 (인니/영어 → 한국어)
# 원문(t)은 절대 지우지 않는다. 번역문은 t_ko 로 따로 붙이고 화면에서 병기한다 (기계번역이므로 원문 대조 가능해야 함).
TR_CACHE_P = CACHE / "tr_ko.json"          # 외부 엔진(구글·마이메모리) 결과
TR_GOOD_P  = CACHE / "tr_claude.json"      # Claude 번역 — 품질이 좋아 항상 우선한다
def _load_json(p):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}
TR_CACHE = _load_json(TR_CACHE_P)
TR_GOOD = _load_json(TR_GOOD_P)

TR_URL = "https://translate.googleapis.com/translate_a/single"
TR_STATE = {"blocked": False}          # requests 가 막히면 True → 이후는 브라우저 경유

def _tr_parse(j):
    if not (isinstance(j, list) and j and isinstance(j[0], list)): return None
    v = "".join(x[0] for x in j[0] if isinstance(x, list) and x and isinstance(x[0], str)).strip()
    return v or None

def _tr_google(text, sl="auto", tl="ko"):
    """Google 무료 엔드포인트(키 불필요). 사내망에서 막히면 None → 브라우저 경유로 넘어간다."""
    try:
        r = requests.get(TR_URL, params={"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text},
                         headers={"User-Agent": UA}, timeout=12)
        if r.status_code != 200: return None
        return _tr_parse(r.json())
    except Exception:
        return None

def _tr_google_url(text, tl):
    import urllib.parse as _u
    return f"{TR_URL}?client=gtx&sl=auto&tl={tl}&dt=t&q=" + _u.quote(text)

MYMEMORY = "https://api.mymemory.translated.net/get"

def _mm_parse(j):
    try:
        v = (j or {}).get("responseData", {}).get("translatedText")
        if v and "MYMEMORY WARNING" not in v.upper(): return v.strip()
    except Exception: pass
    return None

def _tr_mymemory(text, sl="auto", tl="ko"):
    src = "id" if tl == "ko" else "ko"
    try:
        r = requests.get(MYMEMORY, params={"q": text, "langpair": f"{src}|{tl}"},
                         headers={"User-Agent": UA}, timeout=12)
        if r.status_code != 200: return None
        return _mm_parse(r.json())
    except Exception:
        return None

TR_ENGINES = {"google": _tr_google, "mymemory": _tr_mymemory}

TR_ORIGIN = {"google": "https://translate.googleapis.com/",
             "mymemory": "https://api.mymemory.translated.net/"}

def _tr_path(engine, text, tl):
    import urllib.parse as _u
    if engine == "google":
        return f"/translate_a/single?client=gtx&sl=auto&tl={tl}&dt=t&q=" + _u.quote(text)
    src = "id" if tl == "ko" else "ko"
    return f"/get?q={_u.quote(text)}&langpair=" + _u.quote(src + "|" + tl)

def _tr_browser_batch(pairs, engine="google"):
    """[(원문, 목표언어)] → {(원문, 목표언어): 번역}.
    JSON URL 을 goto 하면 브라우저가 다운로드로 처리하고, page.request 는 지문이 달라 차단된다.
    → 같은 도메인의 렌더 가능한 경로로 이동해 origin 을 맞춘 뒤 상대경로 fetch (same-origin, CORS 없음)."""
    if not pairs: return {}
    parse = {"google": _tr_parse, "mymemory": _mm_parse}[engine]
    def work(pg):
        try:
            pg.goto(TR_ORIGIN[engine], wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log("번역 origin 이동 실패", engine, str(e)[:70])       # 404 라도 origin 은 잡힌다
        out = {}; first_err = None
        for text, tl in pairs:
            try:
                j = pg.evaluate("""async (u) => {
                    try { const r = await fetch(u, {credentials:'omit'});
                          if (!r.ok) return {__err: 'HTTP ' + r.status};
                          return await r.json(); }
                    catch (e) { return {__err: String(e).slice(0,80)}; } }""", _tr_path(engine, text, tl))
            except Exception as e:
                j = {"__err": str(e)[:80]}
            if isinstance(j, dict) and j.get("__err"):
                if first_err is None: first_err = j["__err"]
                continue
            v = parse(j)
            if v: out[(text, tl)] = v
        if not out and first_err: log(f"번역 {engine} 브라우저 응답 오류: {first_err}")
        return out
    return _pw_session(work, "translate:" + engine) or {}

def _tr_cache_reload():
    """다른 프로세스(예약 작업)가 채워 넣은 번역을 반영한다."""
    global TR_CACHE, TR_GOOD
    for key, path, setter in (("m", TR_CACHE_P, "TR_CACHE"), ("c", TR_GOOD_P, "TR_GOOD")):
        try: st = path.stat().st_mtime
        except Exception: continue
        if TR_STATE.get("mtime_" + key) == st: continue
        d = _load_json(path)
        if setter == "TR_CACHE": TR_CACHE = d
        else: TR_GOOD = d
        TR_STATE["mtime_" + key] = st

ID_HINT = re.compile(r"\b(di|ke|dan|yang|naik|turun|saham|laba|persen|pada|dari|untuk|ini|akan|jadi|sebesar|triliun|miliar)\b", re.I)

def _native_lang(t):
    if re.search(r"[가-힣]", t): return "ko"
    if ID_HINT.search(t): return "id"
    return None                                  # 영어 등 → 양쪽 다 번역

def _tr_save():
    try: TR_CACHE_P.write_text(json.dumps(TR_CACHE, ensure_ascii=False), encoding="utf-8")
    except Exception as e: log("번역 캐시 저장 실패", e)

CLAUDE_RULES_KO = """한국어 번역 규칙(증권사 데일리 헤드라인체, 예외 없음): ① 명사형 종결 — 종결어미(~합니다/~했습니다/~됩니다/~하세요/~입니다)와 마침표 금지. 예: "TRIS 중간배당 Rp70억 결정 — 지급 일정", "IHSG 9/2 1부 0.28% 하락 반전 — ADMR·ADRO 약세가 부담". ② 고유명사(회사명·인명·지명)는 로마자 원문 유지(Danantara, Bakrie, Boy Thohir…; Boy→소년 같은 번역 금지). 널리 알려진 지명만 한국어(Indonesia=인도네시아, Jakarta=자카르타). 종목코드·퍼센트 원문 유지. ③ 통화는 Rp 표기, Miliar=억 단위 환산(Rp500 Miliar=Rp5,000억, Rp1,27 Triliun=Rp1.27조), 소수점은 마침표. ④ 용어: Laba=순이익, Pendapatan=매출, Saham=주식, Rekomendasi=투자의견, Kinerja=실적, Emiten=상장사, RUPS(LB)=(임시)주주총회, Buyback=자사주 매입, Dividen=배당, Rights Issue=유상증자, Tender Offer=공개매수, Net Buy/Sell=순매수/순매도, Sesi I=1부, IHSG/JCI=IHSG, Asing=외국인, Komisaris=이사, Direktur Utama=대표. ⑤ 두 절은 " — "로 연결. 영어 원문도 한국어로. ⑥ 경제 캘린더 지표명은 국내 리서치 표기: "ISM Manufacturing PMI (Aug)"→"8월 ISM 제조업 PMI", "Nonfarm Payrolls (Aug)"→"8월 비농업 고용", "Initial Jobless Claims"→"신규 실업수당 청구", "Crude Oil Inventories"→"EIA 원유 재고", "Fed Chair Powell Speaks"→"연준 Powell 의장 연설", "FOMC Member Waller Speaks"→"연준 Waller 이사 연설"; (Aug) 같은 월은 앞으로 "8월 …", (MoM)/(YoY)/(QoQ)는 유지. ⑦ "실적 공시 · " 같은 한국어 접두어가 있으면 유지하고 뒤의 인니어만 번역."""
CLAUDE_RULES_ID = """Terjemahkan ke bahasa Indonesia gaya judul berita ekonomi (Kontan/Bisnis): ringkas, tanpa titik di akhir, nama perusahaan/orang/kode saham tetap, angka & persen tetap. Untuk judul kalender ekonomi AS tambahkan "AS" bila perlu dan bulan dalam bahasa Indonesia (Agu, Jul). Awalan Korea seperti "실적 공시 · " diganti "Laporan Keuangan · ", "주주총회 · "→"RUPS · ", "기업설명회 · "→"Public Expose · ", "공시 · "→"Keterbukaan · "."""

def _secret(name):
    import os
    v = os.environ.get(name.upper())
    if v: return v.strip()
    try: return (json.loads((ROOT / "secrets.json").read_text(encoding="utf-8-sig")).get(name) or "").strip() or None
    except Exception: return None

def _claude_cli():
    """PC 에 설치된 Claude Code(claude 명령, Max/Pro 구독으로 로그인) 경로. 없으면 None."""
    import shutil
    for name in ("claude", "claude.cmd", "claude.exe"):
        w = shutil.which(name)
        if w: return w
    cands = [Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude", Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
             Path.home() / "AppData" / "Local" / "Programs" / "claude" / "claude.exe", Path.home() / ".claude" / "bin" / "claude.exe", Path.home() / ".claude" / "local" / "claude.exe"]
    for c in cands:
        if c.exists(): return str(c)
    if not TR_STATE.get("cli_logged"):
        TR_STATE["cli_logged"] = True
        log("Claude Code 미발견 — 확인 경로:", "; ".join(str(c) for c in cands[:4]))
    return None

def _claude_complete(prompt, key, model):
    """프롬프트 → 응답 텍스트. 1) API 키가 있으면 Anthropic API  2) 없으면 Claude Code CLI(구독)  3) 둘 다 없으면 None."""
    if key:
        r = requests.post("https://api.anthropic.com/v1/messages", json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]}, timeout=90,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        if r.status_code != 200: log(f"Claude API 번역 실패 {r.status_code}: {r.text[:120]}"); return None
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    cli = _claude_cli()
    if not cli: return None
    import subprocess
    try:
        r = subprocess.run([cli, "-p", "--output-format", "text"], input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, cwd=str(ROOT))
    except Exception as e:
        log("Claude Code 번역 실행 오류", str(e)[:100]); return None
    if r.returncode != 0: log(f"Claude Code 번역 실패 (exit {r.returncode}): {(r.stderr or r.stdout)[:120]}"); return None
    return (r.stdout or "").strip()

def _tr_claude_api(pairs):
    """pairs: [(원문, 목표언어)] → {(원문, 목표언어): 번역}. Anthropic API 키(secrets.json) 또는 PC 의 Claude Code 로 번역. 결과는 tr_claude.json 에 영구 저장."""
    key = _secret("anthropic_api_key")
    if not pairs or not (key or _claude_cli()): return {}
    cfg = CFG.get("translate") or {}
    model = cfg.get("claude_model", "claude-sonnet-4-5")
    out = {}
    for tl in ("ko", "id"):
        texts = [t for t, l in pairs if l == tl]
        if not texts: continue
        rules = CLAUDE_RULES_KO if tl == "ko" else CLAUDE_RULES_ID
        for i in range(0, len(texts), 40):
            chunk = texts[i:i + 40]
            prompt = (rules + "\n\n아래 JSON 배열의 각 제목을 같은 순서로 번역해, 번역문만 담은 JSON 문자열 배열 하나로만 답하라(설명·코드블록 금지).\n" if tl == "ko" else
                      rules + "\n\nTerjemahkan setiap judul dalam array JSON berikut dengan urutan yang sama; jawab HANYA dengan satu array JSON berisi string terjemahan (tanpa penjelasan/code block).\n")
            try:
                txt = _claude_complete(prompt + json.dumps(chunk, ensure_ascii=False), key, model)
                if txt is None: return out
                txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
                m = re.search(r"\[.*\]", txt, re.S); txt = m.group(0) if m else txt
                arr = json.loads(txt)
                if not isinstance(arr, list) or len(arr) != len(chunk): log(f"Claude API 번역 응답 형식 불일치 ({len(chunk)}→{len(arr) if isinstance(arr, list) else '?'})"); continue
                for t, v in zip(chunk, arr):
                    if isinstance(v, str) and v.strip(): out[(t, tl)] = v.strip()
            except Exception as e:
                log("Claude API 번역 오류", str(e)[:100]); return out
    if out:
        try:
            cur = _load_json(TR_GOOD_P) or {}
            for (t, tl), v in out.items(): cur[hashlib.md5((t + "|" + tl).encode("utf-8")).hexdigest()] = v
            TR_GOOD_P.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
            TR_GOOD.update(cur)
            log(f"Claude 번역 {len(out)}건 ({'API' if key else 'Claude Code'}) → tr_claude.json (총 {len(cur)})")
        except Exception as e: log("tr_claude.json 저장 실패", e)
    return out

def translate_field(items, field="t", langs=("ko", "id"), budget=None):
    """items 의 field 를 언어별로 번역해 field_ko / field_id 를 붙인다.
    엔진을 순서대로 시도하고(requests → 브라우저), 실패한 건은 원문을 유지한다."""
    cfg = CFG.get("translate") or {}
    if not cfg.get("enabled", True): return items
    _tr_cache_reload()                              # 예약 작업이 채워 넣은 번역도 반영
    eng_cfg = cfg.get("engines")
    if eng_cfg is None: eng_cfg = [cfg.get("engine", "google"), "mymemory"]
    engines = [e for e in eng_cfg if e in TR_ENGINES]          # engines: [] 이면 외부 기계번역은 안 쓰되 캐시(Claude 번역)는 반드시 적용
    budget = cfg.get("max_per_run", 60) if budget is None else budget

    claude_ok = bool(_secret("anthropic_api_key") or _claude_cli())
    need = []                                        # (item, 대상필드, 원문, 목표언어, 캐시키)
    for it in items:
        src = (it.get(field) or "").strip()
        if not src: continue
        native = _native_lang(src)
        for tl in langs:
            tgt = f"{field}_{tl}"
            if it.get(tgt): continue
            if tl == native: it[tgt] = src; continue
            key = hashlib.md5((src + "|" + tl).encode("utf-8")).hexdigest()
            if key in TR_GOOD: it[tgt] = TR_GOOD[key]; continue      # Claude 번역 우선
            if key in TR_CACHE:
                it[tgt] = TR_CACHE[key]                                # 기계번역 캐시 — Claude 가 가능하면 뒤에서 품질 업그레이드 대상에 포함
                if not claude_ok: continue
            need.append((it, tgt, src, tl, key))
    if need and (_secret("anthropic_api_key") or _claude_cli()):   # Claude (API 키 또는 PC 의 Claude Code) — 품질 우선, 결과는 영구 캐시
        got = _tr_claude_api(list({(src, tl) for _, _, src, tl, _ in need[:cfg.get("claude_max_per_run", 120)]}))
        if got:
            rest = []
            for it, tgt, src, tl, key in need:
                v = got.get((src, tl))
                if v: it[tgt] = v
                else: rest.append((it, tgt, src, tl, key))
            need = rest
    need = [n for n in need if n[4] not in TR_CACHE]  # 기계번역 캐시가 이미 있는 건은 엔진 재호출 불필요
    if not need or not engines: return items         # 캐시 적용은 위에서 끝남. 엔진이 없으면 미번역분은 원문 유지
    need = need[:budget]                             # 남은 건 다음 실행에서 이어서
    ok = 0; used = []
    remaining = need

    for eng in engines:
        if not remaining: break
        fn = TR_ENGINES[eng]
        flag = "blocked_" + eng
        if not TR_STATE.get(flag):                   # 1) requests 경로 — 첫 건으로 가능 여부 판정
            it, tgt, src, tl, key = remaining[0]
            v = fn(src, "auto", tl)
            if v is None:
                TR_STATE[flag] = True
                log(f"번역 {eng}: requests 차단 → 브라우저 경유")
            else:
                TR_CACHE[key] = v; it[tgt] = v; ok += 1
                for it, tgt, src, tl, key in remaining[1:]:
                    v = fn(src, "auto", tl)
                    if v: TR_CACHE[key] = v; it[tgt] = v; ok += 1
                    time.sleep(0.12)
                used.append(eng + "(http)")
                remaining = [n for n in remaining if not n[0].get(n[1])]
                continue
        got = _tr_browser_batch([(n[2], n[3]) for n in remaining], eng)   # 2) 브라우저 경로
        if got:
            for it, tgt, src, tl, key in remaining:
                v = got.get((src, tl))
                if v: TR_CACHE[key] = v; it[tgt] = v; ok += 1
            used.append(eng + "(browser)")
        remaining = [n for n in remaining if not n[0].get(n[1])]

    _tr_save()
    tag = " · ".join(used) if used else "전부 실패"
    log(f"번역[{field}] {ok}/{len(need)}건 · {tag}" + ("" if not remaining else f" · 미번역 {len(remaining)}건은 원문 유지"))
    return items

def translate_news(items):
    return translate_field(items, "t")

SEEN_P = CACHE / "news_seen.json"; _SEEN = None
def _seen_load():
    global _SEEN
    if _SEEN is None:
        try: _SEEN = json.loads(SEEN_P.read_text(encoding="utf-8"))
        except Exception: _SEEN = {}
    return _SEEN
def _seen_get(k):
    v = _seen_load().get(k)
    try: return dt.datetime.fromisoformat(v) if v else None
    except Exception: return None
def _seen_put(k, t):
    m = _seen_load()
    if k in m: return
    m[k] = t.isoformat()
    if len(m) > 3000:  # 오래된 항목 정리
        for kk in sorted(m, key=m.get)[: len(m) - 2000]: m.pop(kk, None)
    try: SEEN_P.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    except Exception as ex: log("news_seen save fail", ex)

def news_block(max_items=None):
    global CODES, CODE_RX, NAME_RX
    CODES, CODE_RX, NAME_RX = build_alias()
    max_items = max_items or CFG.get("news_max_items", 80); items = []; market = []; diag = {}
    for src in CFG["whitelist"]:
        entries = []; dg = diag.setdefault(src["name"], {"feed": 0, "stock": 0, "market": 0, "drop": 0, "via": "rss"})
        for url in [u for u in (src.get("rss"), None) if u is not None]:
            try: entries = feedparser.parse(url, request_headers={"User-Agent": UA}).entries
            except Exception as e: log("rss fail", src["name"], e)
        if not entries:
            alt = discover_rss(src)
            if alt and alt != src.get("rss"):
                try: entries = feedparser.parse(alt, request_headers={"User-Agent": UA}).entries
                except Exception: pass
        if not entries:
            entries = scrape_home(src); log("rss empty → home scrape", src["name"], len(entries)); dg["via"] = "home"
        dg["feed"] = len(entries)
        for e in entries[:40]:
            title = html.unescape(e.get("title", "")).strip()
            summ = re.sub("<[^>]+>", " ", html.unescape(e.get("summary", "")))
            tags = screen(title + " " + summ)
            _div_from_text(title + ". " + summ, tags, e.get("link", ""), src["name"])
            is_market = not tags
            if is_market and not _market_news_ok(title, summ, e.get("link", "")): dg["drop"] += 1; continue     # 티커 없는 기사 중 경제·정책·금융·증시 섹션만 시장 뉴스로
            if is_market and not (e.get("published_parsed") or e.get("updated_parsed")) and not _entry_image(e): dg["drop"] += 1; continue   # 홈 스크랩(시각·사진 없음)은 시장 뉴스에서 제외
            dg["market" if is_market else "stock"] += 1
            ts = e.get("published_parsed") or e.get("updated_parsed")
            est = not ts
            # 발행시각이 없는 항목(홈 스크랩·날짜 없는 피드)은 "처음 본 시각"을 기억해 매 빌드마다 최신으로 올라오지 않게 한다
            if ts: t = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc).astimezone(WIB)
            else:
                k = e.get("link") or title
                t = _seen_get(k) or now_wib(); _seen_put(k, t)
            raw = str(e.get("published") or e.get("updated") or "")
            # 시간대 표기가 없는 피드(예: "Wed, 02 Sep 2026 11:34:02")는 feedparser 가 UTC 로 간주해 WIB 보다 7시간 미래가 된다
            # → 표기가 없거나 결과가 미래이면 현지(WIB) 시각으로 되돌린다
            has_tz = bool(re.search(r"(Z|[+-]\d{2}:?\d{2}|GMT|UTC|WIB|WITA|WIT)\s*$", raw.strip()))
            if ts and (not has_tz or t > now_wib() + dt.timedelta(minutes=10)):
                t2 = t - dt.timedelta(hours=7)
                if t2 <= now_wib() + dt.timedelta(minutes=10): t = t2
            if t > now_wib(): t = now_wib()
            it = {"ts": t.isoformat(), "date": t.date().isoformat(), "time": t.strftime("%H:%M") if t.date() == now_wib().date() else t.strftime("%m/%d"),
                  "src": src["name"], "t": title, "tags": tags, "url": e.get("link", "")}
            if est: it["t_est"] = True
            img = _entry_image(e)
            if img: it["img"] = img
            (market if is_market else items).append(it)
    items.sort(key=lambda x: x["ts"], reverse=True); market.sort(key=lambda x: x["ts"], reverse=True)
    try:                                              # 매체별 연결 상태 진단 (피드 건수 · 종목/시장 채택 · 제외) → 로그 + 캐시
        (CACHE / "news_srcs.json").write_text(json.dumps({"saved": now_wib().isoformat(), "srcs": diag}, ensure_ascii=False, indent=1), encoding="utf-8")
        log("뉴스 매체 진단 " + " | ".join(f'{k} {v["feed"]}/{v["stock"]}/{v["market"]}/{v["drop"]}{"h" if v["via"]=="home" else ""}' for k, v in diag.items()))
    except Exception as ex: log("뉴스 진단 저장 실패", ex)
    seen, out = set(), []
    for it in items:
        k = it["t"][:60]
        if k in seen: continue
        seen.add(k); out.append(it)
    seen2, out2 = set(), []
    for it in market:
        k = it["t"][:60]
        if k in seen2: continue
        seen2.add(k); out2.append(it)
    MARKET_NEWS[:] = translate_news(out2[:CFG.get("market_news_max", 30)])
    return translate_news(out[:max_items])

MARKET_NEWS = []
# 뉴스 제목·요약에서 주당 배당금 추출 (예: "dividen interim Rp30 per saham", "Rp169,15 miliar ... Rp30 per lembar saham")
NEWS_DIVS = {}
DIV_RX = re.compile(r"(?:dividen|dps)[^.]{0,160}?rp\s?([\d.,]+)\s*(?:per|/|tiap|setiap)\s*(?:lembar\s+|unit\s+)?saham|rp\s?([\d.,]+)\s*(?:per|/|tiap|setiap)\s*(?:lembar\s+)?saham[^.]{0,100}?dividen", re.I)
def _id_num(raw):
    """인니식 숫자: 1.000,5 → 1000.5 · 17,01 → 17.01 · 1.000 → 1000 · 30 → 30"""
    raw = raw.strip().rstrip(".,")
    try:
        if "." in raw and "," in raw: return float(raw.replace(".", "").replace(",", "."))
        if "," in raw:
            a, b = raw.rsplit(",", 1); return float(raw.replace(",", "")) if len(b) == 3 else float(a.replace(",", "") + "." + b)
        if "." in raw:
            a, b = raw.rsplit(".", 1); return float(raw.replace(".", "")) if len(b) == 3 else float(raw)
        return float(raw)
    except Exception: return None
def _div_from_text(text, tags, url="", src=""):
    """단일 종목 기사 + 배당 언급 + 'Rp N per saham' 이 있으면 NEWS_DIVS[티커] 에 기록 (먼저 본 것 유지)"""
    if len(tags) != 1 or "dividen" not in (text or "").lower() or tags[0] in NEWS_DIVS: return
    m = DIV_RX.search(text)
    if not m: return
    v = _id_num(m.group(1) or m.group(2) or "")
    if v and 0 < v < 100000: NEWS_DIVS[tags[0]] = {"dps": v, "src": src, "url": url}
MARKET_RX = re.compile(r"\b(ihsg|jci|bei\b|bursa|ojk|bank indonesia|bi rate|bi-rate|suku bunga|rupiah|inflasi|deflasi|the fed|fomc|wall street|nasdaq|s&p|dow jones|net buy|net sell|asing (beli|jual|masuk|keluar)|obligasi|sbn\b|sun\b|yield|treasury|brent|harga minyak|batu ?bara|coal|nikel|nickel|cpo\b|sawit|harga emas|gold price|danantara|msci|ftse|resesi|pasar saham|pasar modal|stock market|ekonomi (ri|indonesia|global|as|china)|pdb\b|gdp\b|neraca (dagang|perdagangan)|cadangan devisa|tarif (trump|impor|as)|trade war|perang dagang|dividen|ipo\b|right issue|rights issue)\b", re.I)
# 시장 뉴스 섹션 필터: URL 의 섹션(경제·금융·증시·산업)으로 1차 판별, 비경제 섹션(사회·정치·스포츠·연예 등)은 키워드가 맞아도 제외
SEC_OK = re.compile(r"[/.](market|market-news|saham|bursa|bursa-dan-valas|ekonomi|economy|ekonomi-bisnis|berita-ekonomi-bisnis|finansial|finance|keuangan|bisnis|business|investasi|moneter|makro|perbankan|industri|energi|komoditas|pasar-modal|emiten|korporasi|migas|mineral|properti|infrastruktur|money|kontan)(?=[/.\-?]|$)", re.I)
SEC_SOFT = re.compile(r"[/.](politik|nasional|medcom-nasional|internasional|news)(?=[/.\-?]|$)", re.I)   # 정치·국내·국제면: 정책·경제 키워드가 있으면 채택
POLICY_RX = re.compile(r"\b(kebijakan|pemerintah|presiden|prabowo|menteri|menkeu|purbaya|kabinet|dpr|apbn|anggaran|pajak|bea|cukai|subsidi|stimulus|regulasi|perpres|peraturan|ojk|bank indonesia|\bbi\b|bps|kemenkeu|kemendag|kementerian|ekspor|impor|tarif|investasi|danantara|bumn|utang|defisit|inflasi|pertumbuhan ekonomi|pdb|umkm|upah|umr|ump|ketenagakerjaan|harga (bbm|pangan|beras)|the fed|fomc|trump|tarif)\b", re.I)
SEC_NO = re.compile(r"[/.](video|foto|humaniora|nusantara|megapolitan|hukum|hukum-kriminal|kriminal|olahraga|sport|sports|bola|sepakbola|lifestyle|gaya-hidup|hiburan|entertainment|selebriti|seleb|showbiz|teknologi|tekno|inet|travel|wisata|kuliner|food|kesehatan|health|edukasi|pendidikan|opini|kolom|foto|weekend|hype|inspirasi|regional|daerah|jateng|jatim|jabar|sumut|sulsel|bali|otomotif|oto|wolipop|haibunda|sepakbola|liga|piala)(?=[/.\-?]|$)", re.I)
NONMKT_RX = re.compile(r"\b(orangutan|orang utan|satwa|hewan|gajah|harimau|komodo|badak|penyu|banjir|gempa|erupsi|tsunami|longsor|kebakaran|sepak ?bola|timnas|liga|piala|artis|selebriti|film|drama|konser|kriminal|pembunuhan|narkoba|polisi|kecelakaan|virus|covid|cuaca|resep|kuliner|wisata|pernikahan|viral|horoskop|zodiak|ramalan|sinopsis|jadwal (sholat|shalat|imsak)|doa|khutbah)\b", re.I)
def _market_news_ok(title, summ="", link=""):
    """티커가 없는 기사 중 시장 뉴스로 채택할지: 비경제 섹션·비경제 소재는 제외, 경제 섹션이거나 시장·거시 키워드가 있으면 채택"""
    if NONMKT_RX.search(title or ""): return False
    path = "/" + re.sub(r"^https?://", "", link or "")     # 호스트 첫 토큰(nasional.kompas.com 등)도 섹션으로 판별되게
    if SEC_NO.search(path) and not SEC_OK.search(path): return False
    if SEC_OK.search(path): return True
    if SEC_SOFT.search(path):                          # 정치·국내·국제면은 제목에 정책/경제 키워드가 있을 때만 (요약은 보지 않음)
        return bool(POLICY_RX.search(title or "") or MARKET_RX.search(title or ""))
    return bool(MARKET_RX.search(title)) or bool(MARKET_RX.search((summ or "")[:200]))

def _entry_image(e):
    """RSS 항목의 썸네일 URL (media:thumbnail / media:content / enclosure / summary 안의 <img>)"""
    try:
        for m in (e.get("media_thumbnail") or []):
            u = m.get("url")
            if u: return u
        for m in (e.get("media_content") or []):
            u = m.get("url")
            if u and (m.get("medium") in (None, "image") or re.search(r"\.(jpe?g|png|webp)", u, re.I)): return u
        for l in (e.get("enclosures") or e.get("links") or []):
            if str(l.get("type", "")).startswith("image") and l.get("href"): return l["href"]
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', e.get("summary", "") or "")
        if m: return m.group(1)
        if e.get("img"): return e["img"]
    except Exception: pass
    return None

# =============================================================== manual / build
def manual():
    if MANUAL_P.exists():
        try: return json.loads(MANUAL_P.read_text(encoding="utf-8"))
        except Exception as e: log("manual.json parse fail", e)
    return {}

def macro_calendar():
    today = now_wib().date(); out = []
    for c in manual().get("macrocal", []):
        try: d = dt.date.fromisoformat(c["date"])
        except Exception: continue
        if -3 <= (d - today).days <= 45:
            out.append({"date": c["date"], "time": c.get("time", "—"), "kind": "macro", "country": c.get("country", "ID"), "title": c.get("title") or c.get("x", ""), "imp": c.get("imp", 2),
                        "exp": c.get("exp"), "prev": c.get("prev"), "act": c.get("act"), "src": c.get("src") or "manual"})
    return out

EV_TAGS = [  # 캘린더 제목 → 지표 태그 (한/영/인니). 같은 (날짜·시각·국가)에서 태그가 같을 때만 같은 지표로 본다
    ("core", r"\bcore\b|근원|inti"), ("cpi", r"\bcpi\b|inflation|inflasi|물가|인플레"),
    ("pmi", r"\bpmi\b"), ("mfg", r"manufactur|제조업|manufaktur"), ("svc", r"services|non-manufacturing|서비스|jasa|composite"),
    ("ism", r"\bism\b"), ("jolts", r"jolts|구인"), ("nfp", r"nonfarm|payroll|비농업"), ("unemp", r"unemployment|실업|pengangguran"),
    ("trade", r"trade balance|무역수지|neraca"), ("retail", r"retail|소매|ritel"), ("gdp", r"\bgdp\b|성장률|pdb"),
    ("rate", r"rate decision|기준금리|bi rate|bi-rate|7-day|fomc|suku bunga|금리 결정"), ("fx", r"reserves|외환보유|cadangan devisa"),
    ("conf", r"confidence|심리|keyakinan|sentiment"), ("claims", r"jobless|claims|실업수당"), ("ppi", r"\bppi\b|생산자물가"),
    ("auction", r"auction|입찰|lelang"), ("oil", r"crude|oil|원유|minyak"), ("speech", r"speaks|speech|연설|testif"),
]
def _ev_tags(title):
    t = (title or "").lower(); return frozenset(k for k, rx in EV_TAGS if re.search(rx, t))

GLOBAL_IDX = [("^GSPC", "S&P 500", "S&P500"), ("^IXIC", "나스닥", "Nasdaq"), ("^DJI", "다우", "Dow Jones"),
              ("^KS11", "코스피", "KOSPI"), ("^N225", "니케이 225", "Nikkei 225"), ("^HSI", "항셍", "Hang Seng")]   # 6개 = 3열×2행 고정
def global_indices():
    """주요 해외 지수: 현재가·전일종가는 Yahoo fast_info(신뢰도 높음), 5분봉은 스파크와 장중 여부 판정용."""
    if yf is None: return []
    out = []
    for sym, ko, en in GLOBAL_IDX:
        try:
            t = yf.Ticker(sym); fi = t.fast_info
            px, prev = float(fi["lastPrice"]), float(fi["previousClose"])
            if not (px > 0 and prev > 0): continue
            spark = []; live = False; ts = ""
            try:
                h = t.history(period="1d", interval="5m")["Close"].dropna()
                if len(h):
                    ht = h.index[-1]; ht = (ht.tz_convert(WIB) if ht.tzinfo else ht.tz_localize("UTC").tz_convert(WIB))
                    live = (now_wib() - ht).total_seconds() < 3 * 3600
                    hl = h.index[-1] if h.index[-1].tzinfo else ht                   # 종가 날짜는 현지(거래소) 기준 — 미국 9/3 마감이 WIB 로는 9/4 새벽
                    ts = ht.strftime("%H:%M") if live else f"{hl.date():%m/%d} 종가"
                    if live: px = float(h.iloc[-1])
                    spark = [round(float(v), 2) for v in h.tolist()][-80:]
            except Exception: pass
            if not spark:
                try:
                    d = t.history(period="7d", interval="1d", auto_adjust=False)["Close"].dropna(); spark = [round(float(v), 2) for v in d.tolist()]
                    if not ts and len(d): ld = d.index[-1]; ts = f"{(ld.tz_convert(WIB) if ld.tzinfo else ld).date():%m/%d} 종가"
                except Exception: pass
            out.append({"sym": sym, "name": ko, "name_id": en, "px": round(px, 2), "prev": round(prev, 2), "pct": round((px / prev - 1) * 100, 2), "asof": ts or "종가", "live": live, "spark": spark})
        except Exception as e:
            log("global idx fail", sym, str(e)[:60])
    return out

SECTOR_KO = {"Energy": "에너지", "Energi": "에너지", "Basic Materials": "소재", "Barang Baku": "소재", "Industrials": "산업재", "Perindustrian": "산업재",
             "Consumer Non-Cyclicals": "필수소비재", "Barang Konsumen Primer": "필수소비재", "Consumer Cyclicals": "경기소비재", "Barang Konsumen Non-Primer": "경기소비재",
             "Healthcare": "헬스케어", "Kesehatan": "헬스케어", "Financials": "금융", "Keuangan": "금융", "Properties & Real Estate": "부동산", "Properti & Real Estat": "부동산",
             "Technology": "기술", "Teknologi": "기술", "Infrastructures": "인프라", "Infrastruktur": "인프라", "Transportation & Logistic": "운송·물류", "Transportasi & Logistik": "운송·물류"}
SECTOR_ID = {"에너지": "Energi", "소재": "Barang Baku", "산업재": "Perindustrian", "필수소비재": "Konsumen Primer", "경기소비재": "Konsumen Non-Primer", "헬스케어": "Kesehatan",
             "금융": "Keuangan", "부동산": "Properti", "기술": "Teknologi", "인프라": "Infrastruktur", "운송·물류": "Transportasi"}
def sector_block(stocks):
    """업종별 집계: 종목 수·상승/하락·대금가중 평균 등락·거래대금·대표 종목. 업종은 IDX 회사 프로필(IDX-IC) 기준."""
    names = all_tickers() or {}
    agg = {}
    for st in stocks or []:
        info = names.get(st["t"]) or []
        sec = info[1] if len(info) > 1 and info[1] else ""
        if not sec: continue
        ko = SECTOR_KO.get(sec, sec)
        a = agg.setdefault(ko, {"name": ko, "name_id": SECTOR_ID.get(ko, sec), "n": 0, "adv": 0, "dec": 0, "val": 0.0, "wsum": 0.0, "mcap": 0.0, "top": []})
        pct = st.get("pct") or 0; val = st.get("val") or 0; mc = st.get("mcap") or 0
        a["n"] += 1; a["adv"] += pct > 0; a["dec"] += pct < 0; a["val"] += val; a["mcap"] += mc
        a["wsum"] += pct * (mc if mc else val)
        a["top"].append((mc if mc else val, st["t"], pct))
    out = []
    for a in agg.values():
        w = a["mcap"] if a["mcap"] else a["val"]
        a["pct"] = round(a["wsum"] / w, 2) if w else 0.0
        a["top"] = [{"t": t, "pct": p} for _, t, p in sorted(a["top"], reverse=True)[:10]]
        a.pop("wsum", None); out.append(a)
    return sorted(out, key=lambda x: -x["val"])

PUBLISHED_IN_BUILD = False
def _auto_publish():
    """PC 에서 수집이 끝나면 변경 파일(idx_part·번역 캐시 등)을 GitHub 로 올린다 (publish.py, 토큰은 secrets.json).
    run.py 를 재시작하지 않아도 동작하도록 build() 끝에서 직접 호출한다. GitHub 러너에서는 실행하지 않는다."""
    global PUBLISHED_IN_BUILD
    PUBLISHED_IN_BUILD = False
    if os.environ.get("GITHUB_ACTIONS") or not CFG.get("auto_push") or not (ROOT / "secrets.json").exists(): return
    try:
        import subprocess
        args = [sys.executable, str(ROOT / "publish.py"), "--quiet"] + (["--data"] if CFG.get("auto_push_data") else [])
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        r = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=env)
        out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        log("auto_push:", out[-1][:140] if out else f"exit {r.returncode}")
        PUBLISHED_IN_BUILD = True
    except Exception as e:
        log("auto_push 오류", str(e)[:120])

# ---------------- 배당 금액 (investing.com 배당 캘린더 · 인도네시아) + 국채 10년물 일별 이력 ----------------
INV_DIV_SVC = "https://www.investing.com/dividends-calendar/Service/getCalendarFilteredData"
INV_DIV_PAGE = "https://www.investing.com/dividends-calendar/"
DIV_P = CACHE / "dividends.json"
def _div_parse(frag):
    """XHR 조각(<tr>…) → {code: {dps, type, ex, pay, yld, name}}"""
    out = {}
    if not frag or BeautifulSoup is None: return out
    soup = BeautifulSoup("<table>" + frag + "</table>", "lxml")
    for tr in soup.select("tr"):
        if tr.has_attr("tablesorterdivider"): continue
        tds = tr.select("td")
        if len(tds) < 7: continue
        a = tds[1].select_one("a"); code = (a.get_text(strip=True) if a else "").upper()
        if not code: continue
        def d8(s):
            try: return dt.datetime.strptime(s.strip(), "%b %d, %Y").date().isoformat()
            except Exception: return None
        try: dps = float(tds[3].get_text(strip=True).replace(",", ""))
        except Exception: dps = None
        sp = tds[4].select_one("span[title]")
        out[code] = {"dps": dps, "type": (sp.get("title") if sp else "") or "", "ex": d8(tds[2].get_text()), "pay": d8(tds[5].get_text()),
                     "yld": tds[6].get_text(strip=True) or None, "name": (tds[1].get("title") or "").strip()}
    return out

def investing_dividends(days_back=20, days_ahead=30):
    """인도네시아(country 48) 배당: 배당락일·주당 배당금·지급일·수익률. 6시간 캐시, 실패 시 마지막 성공분"""
    try: cache = json.loads(DIV_P.read_text(encoding="utf-8"))
    except Exception: cache = {}
    try:
        if cache.get("saved") and (now_wib() - dt.datetime.fromisoformat(cache["saved"])).total_seconds() < 6 * 3600: return cache.get("items") or {}
    except Exception: pass
    d0 = (now_wib().date() - dt.timedelta(days=days_back)).isoformat(); d1 = (now_wib().date() + dt.timedelta(days=days_ahead)).isoformat()
    form = [("country[]", "48"), ("dateFrom", d0), ("dateTo", d1), ("currentTab", "custom"), ("limit_from", "0")]
    items = {}
    try:
        r = requests.post(INV_DIV_SVC, data=form, timeout=40, headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Referer": INV_DIV_PAGE})
        if r.status_code == 200: items = _div_parse((r.json() or {}).get("data", ""))
    except Exception as e: log("배당 investing http 실패", str(e)[:80])
    if not items:
        def work(pg):
            pg.goto(INV_DIV_PAGE, wait_until="domcontentloaded", timeout=60000); _wait_cf(pg)
            return pg.evaluate("""async ([d0, d1]) => {
                const fd = new URLSearchParams(); fd.append('country[]', '48'); fd.append('dateFrom', d0); fd.append('dateTo', d1); fd.append('currentTab', 'custom'); fd.append('limit_from', '0');
                const r = await fetch('/dividends-calendar/Service/getCalendarFilteredData', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'}, body: fd.toString()});
                const j = await r.json(); return j.data || ''; }""", [d0, d1])
        try: items = _div_parse(_pw_session(work, "investing-dividends") or "")
        except Exception as e: log("배당 investing browser 실패", str(e)[:80])
    if items:
        old = cache.get("items") or {}; old.update(items)                # 지난 항목은 유지(30일)
        lim = (now_wib().date() - dt.timedelta(days=30)).isoformat()
        old = {k: v for k, v in old.items() if (v.get("ex") or "9999") >= lim}
        try: DIV_P.write_text(json.dumps({"saved": now_wib().isoformat(), "items": old}, ensure_ascii=False), encoding="utf-8")
        except Exception: pass
        log(f"배당 investing.com {len(items)}건 (캐시 {len(old)})"); return old
    log("배당 investing.com 실패 → 캐시 사용"); return cache.get("items") or {}

def attach_dividends(corp, divs):
    """배당 캘린더 항목(kind=div)에 주당 배당금·지급일·수익률을 붙이고 제목에 Rp/주 를 넣는다"""
    n = 0
    for e in corp:
        if e.get("kind") != "div": continue
        v = divs.get(e.get("t") or "")
        if not v or not v.get("dps"): continue
        e["dps"] = v["dps"]; e["pay"] = v.get("pay"); e["yld"] = v.get("yld"); n += 1
        amt = f"Rp{v['dps']:,.2f}".rstrip("0").rstrip(".") if v["dps"] < 10 else f"Rp{v['dps']:,.0f}"
        if "/주" not in e["title"]: e["title"] = e["title"].replace("현금배당", f"현금배당 {amt}/주", 1)
        if e.get("title_id") and "/saham" not in e["title_id"]: e["title_id"] = e["title_id"] + f" — {amt}/saham"
    return n

# ---------------- investing.com 일괄 시세 (브라우저 1세션 안에서 fetch — 페이지별 로딩 없음) + 연초 종가(YTD 기준) ----------------
INV_QUOTES = [   # key, path, 표시명, 포맷, inv(상승=빨강), 단위 메모
    ("SUN10Y", "/rates-bonds/indonesia-10-year-bond-yield", "국채 10년물", "{:,.3f}%", True, ""),
    ("SUN1Y", "/rates-bonds/indonesia-1-year-bond-yield", "국채 1년물 (단기)", "{:,.3f}%", True, ""),
    ("UST10Y", "/rates-bonds/u.s.-10-year-bond-yield", "UST 10Y", "{:,.3f}%", True, ""),
    ("DXY", "/indices/usdollar", "달러 인덱스 (DXY)", "{:,.2f}", False, ""),
    ("WTI", "/commodities/crude-oil", "WTI (US$/bbl)", "{:,.2f}", False, ""),
    ("BRENT", "/commodities/brent-oil", "Brent (US$/bbl)", "{:,.2f}", False, ""),
    ("GOLD", "/commodities/gold", "금 (US$/oz)", "{:,.1f}", False, ""),
    ("COAL", "/commodities/newcastle-coal-futures", "석탄 Newcastle (US$/t)", "{:,.2f}", False, ""),
    ("NICKEL", "/commodities/nickel", "니켈 LME (US$/t)", "{:,.0f}", False, ""),
    ("TIN", "/commodities/tin", "주석 LME (US$/t)", "{:,.0f}", False, ""),
    ("CPO", "/commodities/palm-oil", "CPO (MYR/t)", "{:,.0f}", False, "Bursa Malaysia FCPO 근월물"),
]
INV_YAHOO = {"UST10Y": "^TNX", "DXY": "DX-Y.NYB", "WTI": "CL=F", "GOLD": "GC=F"}   # Yahoo 15분 지연으로 대체 가능한 항목
INV_ROTATE = ["COAL", "NICKEL", "CPO"]                                              # investing 전용 — 빌드마다 1개씩 순환(세션당 진입 3회 넘으면 Cloudflare 검증에 걸림)
INV_Q_P = CACHE / "inv_quotes.json"
def y_base(sym):
    """연초(2025-12-31 이하 마지막 거래일) 종가 — YTD 기준"""
    if yf is None: return None, None
    try:
        c = yf.Ticker(sym).history(start="2025-12-15", end="2026-01-06", interval="1d", auto_adjust=False)["Close"].dropna()
        c = c[c.index.strftime("%Y-%m-%d") <= "2025-12-31"]
        return (float(c.iloc[-1]), c.index[-1].strftime("%Y-%m-%d")) if len(c) else (None, None)
    except Exception as e:
        log("yahoo base fail", sym, e); return None, None
def _inv_num(s):
    try: return float(str(s).replace(",", "").replace("%", "").replace("(", "").replace(")", "").replace("+", "").strip())
    except Exception: return None
def investing_batch(ttl_min=3):
    """국채 10년물(카드용)은 매 빌드, investing 전용 원자재·단기채는 빌드마다 1개씩 순환 갱신(약 35분 주기), 나머지는 Yahoo.
    연초 종가(YTD 기준)는 1회만 받아 캐시. 반환 {key: {"px","chg","pct","asof","base","base_date","ts"}}"""
    try: c = json.loads(INV_Q_P.read_text(encoding="utf-8"))
    except Exception: c = {}
    q = c.get("quotes") or {}
    for k, sym in INV_YAHOO.items():                     # Yahoo 항목
        v = yq(sym)
        try:                                              # 선물(금·원유)은 1개월 일봉의 '전일' 이 월물 교체로 어긋날 수 있어 fast_info 의 전일 종가를 우선 사용
            fi = yf.Ticker(sym).fast_info; lp, pc = float(fi["lastPrice"]), float(fi["previousClose"])
            if lp > 0 and pc > 0: v = dict(v or {}, px=lp, prev=pc)
        except Exception: pass
        if v:
            prev = q.get(k) or {}
            if not prev.get("base"): prev["base"], prev["base_date"] = y_base(sym)
            q[k] = {"px": v["px"], "chg": round(v["px"] - v["prev"], 4), "pct": round((v["px"] / v["prev"] - 1) * 100, 2), "asof": "Yahoo 15분 지연",
                    "base": prev.get("base"), "base_date": prev.get("base_date"), "ts": now_wib().isoformat()}
    try:
        fresh = c.get("saved") and (now_wib() - dt.datetime.fromisoformat(c["saved"])).total_seconds() < ttl_min * 60
    except Exception: fresh = False
    if fresh and q.get("SUN10Y"): return q
    oldest = min(INV_ROTATE, key=lambda k: (q.get(k) or {}).get("ts") or "")
    keys = ["SUN10Y", oldest]
    need_base = [k for k in keys if not (q.get(k) or {}).get("base")]
    urls = {k: u for k, u, *_ in INV_QUOTES}
    def work_one(k):
      def work(pg):
        """페이지 안 fetch() 는 Cloudflare 가 403 으로 막고, 한 탭에서 연속 진입도 검증에 걸린다 → 항목마다 새 탭으로 진입"""
        out = {}
        for _once in (1,):
            u = urls[k]
            try:
                pg.goto("https://www.investing.com" + u, wait_until="domcontentloaded", timeout=45000); _wait_cf(pg)
                o = None
                for _ in range(6):
                    o = pg.evaluate("""() => {
                        const q = s => { const e = document.querySelector(s); return e ? e.textContent.trim() : null; };
                        const m = document.documentElement.innerHTML.match(/"instrument_id":"?(\\d+)/);
                        return { last: q('[data-test="instrument-price-last"]'), chg: q('[data-test="instrument-price-change"]'),
                                 pct: q('[data-test="instrument-price-change-percent"]'), time: q('[data-test="trading-time-label"]'), id: m ? m[1] : null,
                                 snip: document.title + ' | ' + (document.body ? document.body.innerText.replace(/\s+/g, ' ').slice(0, 160) : '') }; }""")
                    if o and o.get("last"): break
                    pg.wait_for_timeout(1500)
                pg.wait_for_timeout(2500)                 # 연속 진입 시 속도 제한 완화
                if o and o.get("last") and k in need_base and o.get("id"):
                    try:
                        o.update(pg.evaluate("""async (id) => {
                            const r = await fetch(`https://api.investing.com/api/financialdata/historical/${id}?start-date=2025-12-15&end-date=2026-01-03&time-frame=Daily&add-missing-rows=false`, { headers: { 'domain-id': 'www' } });
                            const j = await r.json();
                            const rows = (j.data || []).map(x => [x.rowDateTimestamp.slice(0, 10), parseFloat(x.last_closeRaw)]).filter(x => x[0] <= '2025-12-31').sort();
                            return rows.length ? { base: rows[rows.length - 1][1], base_date: rows[rows.length - 1][0] } : {}; }""", o["id"]) or {})
                    except Exception as e: o["base_err"] = str(e)[:80]
                out[k] = o or {}
            except Exception as e: out[k] = {"err": str(e)[:120]}
        return out
      return work
    res = {}
    for k in keys:
        try: res.update(_pw_session(work_one(k), f"investing-{k}") or {})
        except Exception as e: log("investing 시세 실패", k, str(e)[:100])
    got = 0
    for k in keys:
        o = (res or {}).get(k) or {}
        px = _inv_num(o.get("last"))
        if px is None:
            log(f"investing {k} 실패: {str(o.get('err') or o)[:160]}"); continue
        prev = q.get(k) or {}
        q[k] = {"px": px, "chg": _inv_num(o.get("chg")), "pct": _inv_num(o.get("pct")), "asof": o.get("time") or "",
                "base": o.get("base") or prev.get("base"), "base_date": o.get("base_date") or prev.get("base_date"), "id": o.get("id") or prev.get("id"), "ts": now_wib().isoformat()}
        got += 1
    try: INV_Q_P.write_text(json.dumps({"saved": now_wib().isoformat(), "quotes": q}, ensure_ascii=False), encoding="utf-8")
    except Exception: pass
    log(f"investing.com {got}/{len(keys)} ({'·'.join(keys)}) · 보유 " + " ".join(f'{k} {q[k]["px"]:g}' for k, *_ in INV_QUOTES if k in q))
    return q

SUN_DAILY_P = CACHE / "sun10y_daily.json"
INV_SUN_HIST = "https://www.investing.com/rates-bonds/indonesia-10-year-bond-yield-historical-data"
def sun10y_daily():
    """investing.com 국채 10년물 일별 종가(최근 약 1개월) → [[date, px], …] 오름차순. 1시간 캐시"""
    try: c = json.loads(SUN_DAILY_P.read_text(encoding="utf-8"))
    except Exception: c = {}
    try:
        if c.get("saved") and (now_wib() - dt.datetime.fromisoformat(c["saved"])).total_seconds() < 3600 and c.get("rows"): return c["rows"]
    except Exception: pass
    def work(pg):
        pg.goto(INV_SUN_HIST, wait_until="domcontentloaded", timeout=60000); _wait_cf(pg)
        try: pg.wait_for_selector("table tbody tr", timeout=15000)
        except Exception: pass
        return pg.evaluate("""() => [...document.querySelectorAll('table')[0]?.querySelectorAll('tbody tr') || []].map(tr => [...tr.querySelectorAll('td')].slice(0, 2).map(td => td.textContent.trim()))""")
    rows = []
    try:
        for d, p in (_pw_session(work, "sun10y-hist") or []):
            try: rows.append([dt.datetime.strptime(d, "%b %d, %Y").date().isoformat(), float(p.replace(",", ""))])
            except Exception: pass
    except Exception as e: log("국채 이력 investing 실패", str(e)[:80])
    rows.sort()
    if rows:
        try: SUN_DAILY_P.write_text(json.dumps({"saved": now_wib().isoformat(), "rows": rows}), encoding="utf-8")
        except Exception: pass
        log(f"국채10Y 일별 {len(rows)}일 ({rows[0][0]}~{rows[-1][0]})"); return rows
    return c.get("rows") or []

SUN_HIST_P = CACHE / "sun10y_hist.json"
def sun10y_card(iv):
    """상단 4번째 지수 카드 — 국채 10년물(investing.com 실시간). 스파크는 빌드마다 쌓는 자체 이력(최근 7일, PC 가 저장·업로드)"""
    if not iv or iv.get("px") is None: return None
    px = float(iv["px"]); prev = round(px - iv["chg"], 4) if iv.get("chg") is not None else None
    try: hist = json.loads(SUN_HIST_P.read_text(encoding="utf-8"))
    except Exception: hist = []
    ts = now_wib().replace(second=0, microsecond=0).isoformat()
    # investing.com 이 간헐적으로 엉뚱한 값을 한 번 주는 경우(예: 7.229 → 7.102 → 7.229) — 직전 확정값과 30bp 이상 차이 나면
    # '미확인'([ts, px, 1]) 으로만 적고 카드에는 직전 확정값을 유지, 다음 빌드에서 같은 값이 다시 오면 확정한다
    conf = [h for h in hist if len(h) < 3]; last = conf[-1][1] if conf else None
    if last is not None and abs(px - last) > 0.3:
        pend = [h for h in hist if len(h) >= 3]
        if pend and abs(pend[-1][1] - px) <= 0.05: hist = [h[:2] for h in hist]; hist.append([ts, px])
        else:
            hist.append([ts, px, 1]); log(f"국채10Y 급변 보류 {last} → {px} (다음 빌드 확인)")
            px = last; prev = conf[-2][1] if len(conf) > 1 else px
    elif not hist or hist[-1][1] != px or hist[-1][0] < (now_wib() - dt.timedelta(minutes=30)).isoformat(): hist.append([ts, px])   # 값이 같아도 30분마다 1점 (추세선 유지)
    lim = (now_wib() - dt.timedelta(days=7)).isoformat()
    hist = [h for h in hist if h[0] >= lim][-400:]
    if not os.environ.get("GITHUB_ACTIONS"):
        try: SUN_HIST_P.write_text(json.dumps(hist), encoding="utf-8")
        except Exception as e: log("국채 이력 저장 실패", e)
    spark = [h[1] for h in hist if len(h) < 3]; span = "intraday"
    daily = sun10y_daily()
    if len(daily) >= 3:                                   # 1W: 최근 5거래일 일별 종가 + 현재가
        wk = [r[1] for r in daily[-6:]]
        if daily[-1][0] == now_wib().date().isoformat(): wk[-1] = px
        else: wk.append(px)
        spark = wk[-6:]; span = "1W"
        if prev is None or iv.get("chg") is None: prev = daily[-2][1] if daily[-1][0] == now_wib().date().isoformat() else daily[-1][1]
    if prev is None: prev = px
    return {"code": "SUN10Y", "label": "SUN 10Y", "name": "국채 10년물", "px": round(px, 3), "prev": round(prev, 3), "pct": round((px - prev) * 100, 1),   # pct = bp 변화
            "spark": spark if len(spark) > 1 else [], "span": span, "inv": True, "asof": ("investing.com " + re.sub(r"^(\d{2}:\d{2}):\d{2}$", r"\1", str(iv.get("asof") or ""))).strip(), "unit": "bp"}

# ---------------- KISI 뉴스 (kisi.co.id/blog/edukasi — 공개 API, 본문 안에 base64 사진) ----------------
KISI_API = "https://api-compro.kisi.co.id/api/v1/kisiNews/list"
KISI_P = CACHE / "kisi_news.json"; _PIL_TRIED = False
def _kisi_thumb(b64):
    """본문에 박힌 base64 원본 사진(수백 KB)을 320px JPEG 썸네일(≈10KB) data URI 로 축소. Pillow 없으면 None"""
    global _PIL_TRIED
    try:
        try: from PIL import Image
        except ImportError:                                   # PC 에 Pillow 가 없으면 1회 자동 설치
            if _PIL_TRIED: return None
            _PIL_TRIED = True
            subprocess.call([sys.executable, "-m", "pip", "install", "-q", "pillow"], timeout=180)
            from PIL import Image
        import base64, io
        im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        w, h = im.size
        if w > 320: im = im.resize((320, max(1, round(h * 320 / w))), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=62, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log("KISI 썸네일 실패", repr(e)[:80]); return None

def kisi_news(max_items=None):
    """KISI 블로그(Edukasi) 최신 글: 제목·시각·링크·썸네일·언급 종목. API 실패 시 캐시 목록 사용"""
    global CODES, CODE_RX, NAME_RX
    if not CODE_RX: CODES, CODE_RX, NAME_RX = build_alias()
    n = max_items or CFG.get("kisi_news_max", 10)
    try: cache = json.loads(KISI_P.read_text(encoding="utf-8"))
    except Exception: cache = {}
    rows = None
    try:
        r = requests.get(KISI_API, params={"limit": n, "offset": 0, "active": 1},
                         headers={"User-Agent": UA, "Origin": "https://kisi.co.id", "Referer": "https://kisi.co.id/"}, timeout=60)
        rows = (r.json() or {}).get("data") or []
    except Exception as e: log("KISI 뉴스 실패", repr(e)[:120])
    today = now_wib().date().isoformat(); out = []
    if rows is None:                                  # 오프라인 → 캐시로 목록 복원
        rows = [{"news_id": k, **v, "_cached": True} for k, v in cache.items()]
    for a in rows:
        nid = str(a.get("news_id") or ""); title = html.unescape(str(a.get("news_title") or a.get("t") or "")).strip()
        if not nid or not title: continue
        c = cache.get(nid) or {}
        content = a.get("news_content") or ""
        img = c.get("img")
        if not img and content:
            m = re.search(r'<img[^>]+src=["\']data:image/[a-z]+;base64,([^"\']+)', content)
            if m: img = _kisi_thumb(m.group(1))
        text = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html.unescape(content))).strip() if content else c.get("text", "")
        d = str(a.get("news_date") or c.get("date") or "")[:10]; tm = str(a.get("news_time") or c.get("tm") or "")[:5]
        try: ts = dt.datetime.fromisoformat(f"{d}T{tm or '00:00'}:00").replace(tzinfo=WIB)
        except Exception: continue
        tags = c.get("tags") if c.get("tags") is not None else screen(title + " " + text[:400])
        _div_from_text(title + ". " + (text or c.get("text") or ""), tags, "", "KISI")
        it = {"id": nid, "ts": ts.isoformat(), "date": d, "time": tm if d == today else ts.strftime("%m/%d"), "src": "KISI", "t": title, "tags": tags,
              "url": f"https://kisi.co.id/blog/edukasi/{requests.utils.quote(title)}/{nid}"}
        if img: it["img"] = img
        if text: it["sum"] = text[:220]
        out.append(it)
        cache[nid] = {"t": title, "date": d, "tm": tm, "tags": tags, "img": img, "text": text[:1500]}
    out.sort(key=lambda x: x["ts"], reverse=True); out = out[:n]
    try:                                              # 캐시는 최근 60건만 유지
        keep = sorted(cache.items(), key=lambda kv: (kv[1].get("date") or "", kv[1].get("tm") or ""), reverse=True)[:60]
        KISI_P.write_text(json.dumps(dict(keep), ensure_ascii=False), encoding="utf-8")
    except Exception as e: log("KISI 캐시 저장 실패", e)
    log(f"KISI 뉴스 {len(out)}건 (사진 {sum(1 for x in out if x.get('img'))})")
    return translate_news(out)

# ---------------- AI 요약 (Google Gemini · PC 에서만 생성, 캐시를 GitHub 로 올려 러너·사이트가 그대로 씀) ----------------
GEMINI_MODEL = "gemini-2.5-flash-lite"                      # 기본값 — 계정에서 못 쓰면 models 목록에서 flash 계열을 자동 선택
_GEM = {"model": None}
def _gemini_model(key):
    """사용 가능한 모델 자동 선택: generateContent 지원 + 'flash' 계열, 최신 버전·lite 우선. 결과는 캐시(data/cache/gemini_model.txt)"""
    if _GEM["model"]: return _GEM["model"]
    fp = CACHE / "gemini_model.txt"
    try:
        if fp.exists() and (time.time() - fp.stat().st_mtime) < 7 * 86400: _GEM["model"] = fp.read_text().strip() or None
    except Exception: pass
    if _GEM["model"]: return _GEM["model"]
    try:
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key={key}", timeout=30); ms = (r.json() or {}).get("models") or []
        cand = [m["name"].split("/")[-1] for m in ms if "generateContent" in (m.get("supportedGenerationMethods") or []) and "flash" in m["name"] and not re.search(r"image|tts|audio|live|preview|exp|thinking|native", m["name"])]
        def ver(n):
            v = re.search(r"(\d+(?:\.\d+)?)", n); return float(v.group(1)) if v else 0
        cand.sort(key=lambda n: ("lite" in n, ver(n)), reverse=True)   # lite 우선(빠르고 무료 한도 큼), 그 안에서 최신
        if not cand: cand = [m["name"].split("/")[-1] for m in ms if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        _GEM["model"] = cand[0] if cand else GEMINI_MODEL
        log(f"Gemini 모델 선택: {_GEM['model']} (후보 {', '.join(cand[:6])})")
        try: fp.write_text(_GEM["model"])
        except Exception: pass
    except Exception as e:
        log("Gemini 모델 목록 실패", str(e)[:80]); _GEM["model"] = GEMINI_MODEL
    return _GEM["model"]
AI_ANN_P = CACHE / "ann_ai.json"; AI_STK_P = CACHE / "stock_ai.json"
def _gemini(prompt, max_tokens=1500, temperature=0.2, search=False):
    """Gemini generateContent (REST). 실패 시 None. 404(모델 없음)면 모델을 다시 고른다.
    search=True 면 구글 검색 그라운딩을 붙인다 — 모델이 거부(400)하면 자동으로 빼고 재시도한다."""
    key = _secret("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not key: return None
    if _GEM.get("fail", 0) >= 2: return None                 # 이번 빌드에서 연속 2회 실패(타임아웃·503)면 나머지는 건너뛴다 — 빌드 지연 방지
    model = _gemini_model(key)
    try:
        gc = {"temperature": temperature, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}
        if not _GEM.get("nothink"): gc["thinkingConfig"] = {"thinkingBudget": 0}          # 속도 우선(추론 비활성). 모델이 거부하면 빼고 재시도
        use_search = bool(search) and not _GEM.get("nosearch")
        def _body():
            b = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": dict(gc)}
            if use_search:
                b["tools"] = [{"google_search": {}}]
                b["generationConfig"].pop("responseMimeType", None)   # 그라운딩과 JSON 강제는 함께 못 쓴다
            return b
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        r = requests.post(url, json=_body(), timeout=90 if use_search else 60)
        if r.status_code == 400 and use_search:                                  # 모델이 검색 도구를 거부 → 빼고 재시도(이후 계속 제외)
            _GEM["nosearch"] = True; use_search = False
            r = requests.post(url, json=_body(), timeout=60)
        if r.status_code == 400 and not _GEM.get("nothink"):                      # 모델이 thinkingConfig 를 거부 → 빼고 재시도(이후 계속 제외)
            _GEM["nothink"] = True; gc.pop("thinkingConfig", None)
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}", json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gc}, timeout=60)
        if r.status_code == 400 and not _GEM.get("nojson"):                       # JSON 응답 모드도 거부 → 일반 텍스트로(파싱은 _json_loads_loose 가 처리)
            _GEM["nojson"] = True; gc.pop("responseMimeType", None)
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}", json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gc}, timeout=60)
        if r.status_code == 404 and _GEM["model"]:      # 모델 퇴역 → 캐시 지우고 다음 호출에서 재선택
            _GEM["model"] = None
            try: (CACHE / "gemini_model.txt").unlink()
            except Exception: pass
        if r.status_code != 200:
            log("Gemini", r.status_code, re.sub(r"\s+", " ", r.text)[:300], "| gc:", ",".join(gc.keys())); _GEM["fail"] = _GEM.get("fail", 0) + (1 if r.status_code in (429, 500, 503) else 0); return None
        j = r.json(); txt = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])
        _GEM["fail"] = 0
        return txt
    except Exception as e:
        log("Gemini 오류", str(e)[:120]); _GEM["fail"] = _GEM.get("fail", 0) + 1; return None
def _json_loads_loose(txt):
    if not txt: return None
    try: return json.loads(txt)
    except Exception: pass
    m = re.search(r"[\[{].*[\]}]", txt, re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def _pdf_text(url, max_pages=3, max_chars=6000):
    """IDX 공시 PDF → 텍스트 (pypdf). 스캔본이면 빈 문자열"""
    try:
        try: from pypdf import PdfReader
        except ImportError:
            subprocess.call([sys.executable, "-m", "pip", "install", "-q", "pypdf"], timeout=180); from pypdf import PdfReader
        import io, base64
        raw = b""
        try:
            r = idx.s.get(url, timeout=40, headers={"User-Agent": UA, "Referer": "https://www.idx.co.id/"}) if hasattr(idx, "s") else requests.get(url, timeout=40, headers={"User-Agent": UA})
            if r.status_code == 200 and r.content[:5].startswith(b"%PDF"): raw = r.content
        except Exception: pass
        if not raw:                                       # Cloudflare 에 막히면 IDX 브라우저 탭 안에서 fetch → base64
            def job():
                pg = _idx_page(idx.BASE)
                return pg.evaluate("""async (u) => { try { const r = await fetch(u); if (!r.ok) return null; const b = new Uint8Array(await r.arrayBuffer());
                    if (b.length > 6000000) return null; let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); return btoa(s); } catch (e) { return null; } }""", url)
            b64 = _pw_call(job, "idx-pdf")
            if b64: raw = base64.b64decode(b64)
        if not raw or len(raw) > 6_000_000 or not raw[:5].startswith(b"%PDF"): return ""
        rd = PdfReader(io.BytesIO(raw)); out = []
        for pg in rd.pages[:max_pages]:
            try: out.append(pg.extract_text() or "")
            except Exception: pass
        return re.sub(r"[ \t]+", " ", "\n".join(out)).strip()[:max_chars]
    except Exception as e:
        log("PDF 텍스트 실패", str(e)[:80]); return ""

def ai_announcements(anns, per_build=6):
    """공시 PDF 본문을 2문장으로 요약(한/인니) → a['ai_ko'], a['ai_id']. 캐시(url 기준) · 빌드당 최대 per_build 건 신규 처리"""
    try: cache = json.loads(AI_ANN_P.read_text(encoding="utf-8"))
    except Exception: cache = {}
    can = bool(_secret("gemini_api_key") or os.environ.get("GEMINI_API_KEY")) and not os.environ.get("GITHUB_ACTIONS")
    todo = [a for a in anns if a.get("url") and a["url"] not in cache]
    done = 0
    for a in (todo[:per_build] if can else []):
        text = _pdf_text(a["url"])
        if len(text) < 200:
            cache[a["url"]] = {"ko": "", "id": "", "note": "scan", "ts": now_wib().isoformat()}; continue   # 스캔본·본문 없음 → 요약 불가로 기록(재시도 안 함)
        prompt = ("다음은 인도네시아 증권거래소(IDX) 공시 원문 일부다. 투자자 관점에서 핵심만 요약하라.\n"
                  "[필수 포함 항목] 공시 유형이 아래에 해당하면 그 수치와 일정을 반드시 요약에 넣는다. 원문에 없으면 '미기재'로 적는다.\n"
                  "  · 자사주 매입(pembelian kembali saham/buyback): 매입 한도 금액, 매입 주식수, 발행주식 대비 비율, 매입 기간(시작~종료일), 자금 출처\n"
                  "  · 배당(dividen): 주당 배당금(DPS), 총액, 배당락일(cum/ex date), 지급일\n"
                  "  · 유상증자·제3자배정(rights issue/HMETD/private placement): 발행 주식수, 발행가, 조달 금액, 지분 희석률, 일정\n"
                  "  · 지분 매각·인수(divestasi/akuisisi): 대상 회사, 지분율, 거래 금액, 매도·매수 주체, 완료 예정일\n"
                  "출력은 JSON 하나: {\"ko\": \"한국어 2~3문장, 증권사 리포트 문체(명사형 종결), 금액·비율·날짜 등 숫자 포함\", \"id\": \"Bahasa Indonesia 2-3 kalimat\", \"tags\": [\"핵심 키워드 최대 3개(한국어)\"]}\n"
                  "숫자는 한국식 표기(천 단위 콤마, 소수점은 마침표: 58.31%, 3,190,144,498주, Rp1,250억)로 바꾸고 회사명은 원문 그대로. 원문에 없는 내용은 쓰지 말 것. 한국어 문장에 인니어·스페인어 단어를 섞지 말 것.\n\n"
                  f"[종목] {a.get('t')}  [제목] {a.get('title')}\n[원문]\n{text}")
        j = _json_loads_loose(_gemini(prompt, 1100))
        if not j or not j.get("ko"): continue
        cache[a["url"]] = {"ko": str(j.get("ko", ""))[:600], "id": str(j.get("id", ""))[:600], "tags": [str(x)[:20] for x in (j.get("tags") or [])][:3], "ts": now_wib().isoformat()}; done += 1
    if done or (can and todo):
        keep = sorted(cache.items(), key=lambda kv: kv[1].get("ts", ""), reverse=True)[:400]
        try: AI_ANN_P.write_text(json.dumps(dict(keep), ensure_ascii=False), encoding="utf-8")
        except Exception: pass
        log(f"공시 AI 요약 {done}건 (대기 {max(0, len(todo) - per_build)}) · 캐시 {len(keep)}")
    for a in anns:
        c = cache.get(a.get("url") or "")
        if c and c.get("ko"): a["ai_ko"] = c["ko"]; a["ai_id"] = c["id"]; a["ai_tags"] = c.get("tags") or []
    return anns

# ── Catalyst — 오늘 주가에 영향을 줄 재료가 있는 종목 ────────────────────────
# 점수 = 뉴스영향도 0.50 + 시가총액 0.30 + 거래대금 0.20 (각 0~100)
# 룰 기반이라 왜 이 종목이 위에 왔는지 항상 되짚어볼 수 있다 (AI 호출 없음 = 할당량 소모 없음)
CAT_EVENTS = [
    (95, "지분매각·경영권", "Divestasi & pengendali",
     r"jual saham|penjualan saham|melepas saham|divestasi|lepas kepemilikan|pengendali|pengambilalihan|caplok|tender offer|sell .{0,20}shares"),
    (90, "법적 리스크", "Risiko hukum",
     r"wanprestasi|digugat|menggugat|gugatan|pailit|PKPU|disuspensi|suspensi saham|delisting|sanksi|denda|tersangka|penyidikan"),
    (80, "자본구조", "Aksi korporasi",
     r"rights issue|HMETD|private placement|penambahan modal|buyback|pembelian kembali saham|stock split|reverse stock|konversi saham"),
    (70, "실적·배당", "Kinerja & dividen",
     r"dividen|laba bersih|rugi bersih|kinerja keuangan|pendapatan naik|pendapatan turun|revisi target"),
    (55, "영업·계약", "Operasional",
     r"kontrak|ekspansi|pabrik baru|proyek|kerja sama|MoU|akuisisi aset|IPO anak"),
    (40, "경영진 변동", "Manajemen",
     r"direksi|komisaris|RUPS|pembebastugasan|mengundurkan diri|penunjukan direktur"),
]
CAT_EVENTS = [(w, ko, idn, re.compile(rx, re.I)) for w, ko, idn, rx in CAT_EVENTS]
CAT_W = {"news": 0.40, "size": 0.30, "surge": 0.30}   # 뉴스 40 · 시가총액 30 · 거래대금 급증 30
CAT_MCAP = (11.0, 15.0)      # log10(IDR) 정규화 구간 — Rp1,000억 ~ Rp1,000조
CAT_SURGE = (1.0, 3.0)       # 20일 평균 대비 거래대금 배수: 1배=0점, 3배 이상=100점

def _cat_norm(v, lo, hi):
    import math
    if not v or v <= 0: return 0.0
    x = (math.log10(v) - lo) / (hi - lo)
    return max(0.0, min(1.0, x)) * 100

def catalyst_block(data, top=10):
    today = (data.get("updated") or "")[:10]
    stocks = {r.get("t"): r for r in (data.get("stocks") or []) if r.get("t")}
    divs = data.get("dividends") or {}
    cand = {}    # ticker → dict

    def bump(t, w, ko, idn, src, hl=None, url=None, outlet=None):
        if t not in stocks: return
        c = cand.setdefault(t, {"w": 0, "ko": "", "id": "", "outlets": set(), "ann": False, "hl": "", "url": ""})
        if w > c["w"]:
            c["w"], c["ko"], c["id"] = w, ko, idn
            if hl: c["hl"], c["url"] = hl, url or ""
        if outlet: c["outlets"].add(outlet)
        if src == "ann": c["ann"] = True
        if not c["hl"] and hl: c["hl"], c["url"] = hl, url or ""

    # 1) 종목 뉴스
    #    한 기사에 티커가 여러 개 달릴 때(본문 언급까지 태깅됨) 그 재료를 전 종목에 나눠주면
    #    엉뚱한 종목이 상위로 올라온다. 제목에 티커나 회사명이 직접 등장하는 종목만 인정한다.
    for n in (data.get("news") or []):
        head = f"{n.get('t_id') or ''} {n.get('t') or ''} {n.get('t_ko') or ''}"
        tags = n.get("tags") or []
        for w, ko, idn, rx in CAT_EVENTS:
            if not rx.search(head): continue
            for t in tags:
                if len(tags) > 1:
                    nm = (stocks.get(t, {}).get("n") or "").split()
                    key = " ".join(nm[:2]).lower()
                    if t not in head and not (key and key in head.lower()): continue
                bump(t, w, ko, idn, "news", n.get("t_ko") or n.get("t"), n.get("url"), n.get("src"))
            break
    # 2) 공시
    for a in (data.get("announcements") or []):
        txt = f"{a.get('title') or ''} {a.get('title_ko') or ''}"
        for w, ko, idn, rx in CAT_EVENTS:
            if rx.search(txt):
                bump(a.get("t"), w, ko, idn, "ann", a.get("ai_ko") or a.get("title_ko") or a.get("title"), a.get("url"))
                break
    # 3) 배당락 당일 — 뉴스가 없어도 주가에 기계적으로 영향
    for t, dv in divs.items():
        if dv.get("ex") == today:
            bump(t, 75, "배당락", "Ex-dividend", "div",
                 f"배당락일 · DPS Rp{dv.get('dps')} ({dv.get('type') or ''}, 수익률 {dv.get('yld') or '—'})", "")

    out = []
    for t, c in cand.items():
        o = stocks[t]
        news = c["w"] + (15 if len(c["outlets"]) >= 3 else 8 if len(c["outlets"]) == 2 else 0) + (10 if c["ann"] else 0)
        news = min(100.0, news)
        size = _cat_norm(o.get("mcap"), *CAT_MCAP)
        ratio = o.get("ratio") or 0
        surge = max(0.0, min(1.0, (ratio - CAT_SURGE[0]) / (CAT_SURGE[1] - CAT_SURGE[0]))) * 100 if ratio else 0.0
        score = CAT_W["news"] * news + CAT_W["size"] * size + CAT_W["surge"] * surge
        out.append({"t": t, "n": o.get("n"), "px": o.get("px"), "pct": o.get("pct"), "val": o.get("val"),
                    "mcap": o.get("mcap"), "ratio": ratio, "ev_ko": c["ko"], "ev_id": c["id"],
                    "hl": (c["hl"] or "")[:160], "url": c["url"],
                    "score": round(score, 1), "s_news": round(news), "s_size": round(size), "s_surge": round(surge),
                    "srcs": len(c["outlets"])})
    out.sort(key=lambda x: -x["score"])
    return out[:top]

AI_IDX_P = CACHE / "index_ai.json"
IDX_HIST_P = CACHE / "index_hist.json"
def _index_hist(data, keep=30):
    """IHSG 일별 종가를 쌓아 최근 며칠 흐름을 만든다 — '며칠 올랐으니 차익실현' 판단의 근거.
    빌드마다 오늘 종가를 갱신하고, 전일 종가(index.prev)도 비어 있으면 채운다."""
    try: h = json.loads(IDX_HIST_P.read_text(encoding="utf-8"))
    except Exception: h = {}
    comp = next((x for x in (data.get("indices") or []) if x.get("code") == "COMPOSITE"), {})
    today = (data.get("updated") or "")[:10]
    if comp.get("px") and today: h[today] = round(float(comp["px"]), 2)
    prev_close = comp.get("prev")
    if prev_close:
        past = sorted(d for d in h if d < today)
        if not past or abs(h[past[-1]] - float(prev_close)) > 0.01:
            d1 = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat() if today else None
            if d1 and d1 not in h: h[d1] = round(float(prev_close), 2)
    h = dict(sorted(h.items())[-keep:])
    try: IDX_HIST_P.write_text(json.dumps(h, ensure_ascii=False), encoding="utf-8")
    except Exception: pass
    ds = sorted(h)
    out = []
    for i in range(1, len(ds)):
        a, b = h[ds[i - 1]], h[ds[i]]
        if a: out.append({"날짜": ds[i], "종가": b, "등락%": round((b / a - 1) * 100, 2)})
    return out[-6:]

def _recent_ret(codes, days=6):
    """ss_YYYYMMDD.json(IDX 일별 요약)에서 종목별 최근 N거래일 누적 등락률을 뽑는다.
    '며칠 올랐으니 차익실현' 같은 판단의 근거로 쓴다. 파일이 없으면 빈 dict."""
    import glob
    fs = sorted(glob.glob(str(CACHE / "ss_*.json")))[-days:]
    series = {}
    for f in fs:
        try: rows = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception: continue
        if not isinstance(rows, list): continue
        for r in rows:
            c = r.get("StockCode")
            if c in codes:
                cl = r.get("Close")
                if cl: series.setdefault(c, []).append(float(cl))
    out = {}
    for c, v in series.items():
        if len(v) >= 2 and v[0]:
            out[c] = round((v[-1] / v[0] - 1) * 100, 2)
    return out


def ai_index(data, ttl_min=30):
    """지수가 왜 움직였나 — 한 줄. 지수를 끌어내린(올린) 대형주를 먼저 특정하고, 그 원인을
    뉴스·최근 며칠 주가 흐름·해외 변수에서 찾는다. 지수 등락 자체를 원인으로 쓰는 동어반복은 금지."""
    try: cache = json.loads(AI_IDX_P.read_text(encoding="utf-8"))
    except Exception: cache = {}
    now = now_wib()
    ts = cache.get("ts")
    if ts:
        try:
            if (now - dt.datetime.fromisoformat(ts)).total_seconds() < ttl_min * 60 and cache.get("ko"):
                return {"ko": cache["ko"], "id": cache.get("id", ""), "ts": ts}
        except Exception: pass
    can = bool(_secret("gemini_api_key") or os.environ.get("GEMINI_API_KEY")) and not os.environ.get("GITHUB_ACTIONS")
    if not can:
        return {"ko": cache.get("ko", ""), "id": cache.get("id", ""), "ts": ts} if cache.get("ko") else {}

    i = data.get("index") or {}
    comp = next((x for x in (data.get("indices") or []) if x.get("code") == "COMPOSITE"), {})
    stocks = [r for r in (data.get("stocks") or []) if r.get("mcap") and r.get("pct") is not None]
    # 지수 기여도 근사 = 시가총액 × 등락률 (IHSG 는 시총가중. 유동주식 조정은 반영 못 하므로 근사치)
    for r in stocks: r["_c"] = (r.get("mcap") or 0) * (r.get("pct") or 0)
    stocks.sort(key=lambda r: r["_c"])
    def brief(rows):
        return [{"t": r.get("t"), "n": r.get("n"), "등락%": r.get("pct"),
                 "시총_조IDR": round((r.get("mcap") or 0) / 1e12, 1)} for r in rows]
    drag, lift = brief(stocks[:8]), brief(list(reversed(stocks[-5:])))
    rets = _recent_ret({r["t"] for r in drag + lift})
    for r in drag + lift: r["최근5일누적%"] = rets.get(r["t"])

    ctx = {
        "IHSG": {"현재": comp.get("px"), "전일종가": comp.get("prev"), "전일대비%": comp.get("pct"),
                  "장중고": i.get("high"), "장중저": i.get("low"), "YTD%": i.get("ytd"), "1개월%": i.get("m1")},
        "거래대금_IDR": i.get("value_idr"), "상승_하락_보합": [i.get("adv"), i.get("dec"), i.get("unch")],
        "외국인수급": {"기준일": i.get("foreign_date"), "오늘날짜": (data.get("updated") or "")[:10],
                       "순매수_IDR": i.get("foreign_net_idr"),
                       "주의": "기준일이 오늘이 아니면 전일 확정치이므로 오늘 등락의 원인으로 쓰지 말 것. "
                               "순매수/순매도 방향을 값의 부호로 반드시 확인할 것(음수=순매도)."},
        "지수_최근흐름": _index_hist(data),
        "지수를_끌어내린_종목": drag, "지수를_끌어올린_종목": lift,
        "업종": [{"명": x.get("name"), "등락%": x.get("pct")} for x in (data.get("sectors") or [])],
        "해외지수": [{"명": g.get("name"), "등락%": g.get("pct")} for g in (data.get("global") or [])],
        "환율_금리": [{"항목": m.get("k"), "값": m.get("v"), "전일대비": m.get("d")} for m in (data.get("macro") or [])[:5]],
        "시장뉴스": [n.get("t_id") or n.get("t") for n in (data.get("market_news") or [])[:14]],
        "종목뉴스": [f"{','.join(n.get('tags') or [])}: {n.get('t_id') or n.get('t')}" for n in (data.get("news") or [])[:20]],
    }
    prompt = (
        "오늘 인도네시아 증시(IHSG)가 왜 이렇게 움직였는지 한 문장으로 쓴다.\n\n"
        "[반드시 먼저 할 것] 구글 검색을 사용한다. 아래 같은 질의로 오늘자 기사를 찾아 원인을 확인한다.\n"
        "  · \"IHSG hari ini turun kenapa\"  · \"IHSG melemah <오늘 날짜>\"  · \"오늘 IHSG 하락 이유\"\n"
        "  Bloomberg Technoz·Kontan·CNBC Indonesia·Bisnis 기사에 그날의 원인이 정리돼 있다. 검색 결과가 1순위 근거다.\n\n"
        "[검색으로도 안 잡히면] 아래 데이터에서 찾는다.\n"
        "  1. 지수를_끌어내린_종목 / 끌어올린_종목 과 업종 등락으로 어느 업종·종목이 지수를 움직였는지 특정한다.\n"
        "  2. 그 종목들이 왜 움직였는지를 (a) 종목뉴스·시장뉴스의 사건, "
        "(b) 지수_최근흐름·최근5일누적% — 전일 급등했거나 며칠 올랐다면 차익실현 물량 출회, "
        "(c) 해외지수·환율·금리 등 외부 변수 에서 찾는다.\n\n"
        "[금지]\n"
        "  · 지수 등락을 그 자체의 원인으로 쓰는 동어반복. '하락 종목 우위에 기인하여 하락', "
        "'대다수 업종 하락으로 지수 하락', '매도 우위로 약세 마감' 은 전부 결과를 다시 말한 것이라 틀린 문장이다.\n"
        "  · 외국인수급.기준일 이 오늘날짜와 다른데도 그 값을 오늘 원인으로 쓰는 것.\n  · 순매수/순매도를 반대로 쓰는 것. 값이 음수면 순매도다.\n"
        "  · 데이터에 없는 수치를 지어내는 것.\n\n"
        "[좋은 예]\n"
        "  · \"전일 1.09% 급등에 따른 차익실현 매물과 미국 8월 고용지표 발표 대기 관망세로 IHSG 0.47% 하락.\"\n"
        "  · \"전일 급등한 은행주(BBRI·BBCA·BMRI) 차익실현 물량 출회로 IHSG 0.41% 하락.\"\n"
        "  · \"니켈 가격 반등에 따른 소재주 강세로 IHSG 0.35% 상승.\"\n\n"
        "[문체] 증권사 리포트체(명사형 종결), 100자 이내, 숫자는 천 단위 콤마. "
        "추정형('~로 보인다') 금지, 근거 기반 표현('~ 영향', '~에 따른', '~ 대기') 사용. 종목은 티커로 표기.\n"
        "출력은 JSON 하나만: {\"ko\": \"한국어 한 문장\", \"id\": \"Bahasa Indonesia satu kalimat\"}\n\n"
        + json.dumps(ctx, ensure_ascii=False))
    j = _json_loads_loose(_gemini(prompt, 700, search=True))
    if not j or not j.get("ko"):
        return {"ko": cache.get("ko", ""), "id": cache.get("id", ""), "ts": ts} if cache.get("ko") else {}
    out = {"ko": str(j.get("ko", ""))[:200], "id": str(j.get("id", ""))[:250], "ts": now.isoformat()}
    try: AI_IDX_P.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception: pass
    log("지수 AI 요약 갱신:", out["ko"][:70])
    return out

def ai_stocks(data, batch=10, ttl_min=60, per_build=40):
    """종목별 '왜 움직였나' — 거래대금 상위 100 + 업종 대표 종목. 뉴스·공시·배당·수급·거래대금을 근거로 한/인니 요약. 1시간 캐시, 배치 호출"""
    try: cache = json.loads(AI_STK_P.read_text(encoding="utf-8"))
    except Exception: cache = {}
    stocks = {s["t"]: s for s in (data.get("stocks") or [])}
    targets = [s["t"] for s in sorted(data.get("stocks") or [], key=lambda s: -(s.get("val") or 0))[:100]]
    for sec in data.get("sectors") or []:
        for o in (sec.get("top") or [])[:10]:
            if o["t"] not in targets: targets.append(o["t"])
    can = bool(_secret("gemini_api_key") or os.environ.get("GEMINI_API_KEY")) and not os.environ.get("GITHUB_ACTIONS")
    now = now_wib()
    def stale(t):
        c = cache.get(t)
        if not c: return True
        try: age = (now - dt.datetime.fromisoformat(c["ts"])).total_seconds() / 60
        except Exception: return True
        pct = (stocks.get(t) or {}).get("pct")
        return age > ttl_min or (pct is not None and c.get("pct") is not None and abs(pct - c["pct"]) >= 1.5)
    todo = [t for t in targets if stale(t)][:per_build] if can else []
    news = data.get("news") or []; anns = data.get("announcements") or []; divs = data.get("dividends") or {}
    jci = next((x for x in data.get("indices") or [] if x["code"] == "COMPOSITE"), {}) or {}
    sec_of = {}
    for sec in data.get("sectors") or []:
        for o in sec.get("top") or []: sec_of[o["t"]] = sec
    def ctx(t):
        s = stocks.get(t) or {}; sec = sec_of.get(t) or {}
        nl = [f'- {n.get("time")} {n.get("src")}: {n.get("t")}' for n in news if t in (n.get("tags") or [])][:6]
        al = [f'- {a.get("time")} {a.get("title")}' + (f' → {a["ai_ko"]}' if a.get("ai_ko") else "") for a in anns if a.get("t") == t][:5]
        dv = divs.get(t)
        lines = [f'[{t}] {s.get("n","")} · 현재가 Rp{s.get("px")} ({s.get("pct")}%) · 거래대금 Rp{(s.get("val") or 0)/1e9:,.0f}억 (20일 평균 대비 {s.get("ratio")}배) · 외국인 순매수 {(s.get("fnet") or 0)/1e9:+,.0f}억 · 장중 고/저 {s.get("hi")}/{s.get("lo")}',
                 f'  시장: JCI {jci.get("pct")}% · 업종 {sec.get("name","?")} {(sec.get("mcap_pct") if sec.get("mcap_pct") is not None else sec.get("pct"))}%']
        if dv and dv.get("dps"): lines.append(f'  배당: Rp{dv["dps"]}/주 · 배당락 {dv.get("ex")} · 지급 {dv.get("pay")}')
        lines.append("  오늘 뉴스:\n" + ("\n".join(nl) if nl else "  (없음)"))
        lines.append("  오늘 공시:\n" + ("\n".join(al) if al else "  (없음)"))
        return "\n".join(lines)
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        prompt = ("아래는 인도네시아 증시(IDX) 종목별 오늘 데이터다. 각 종목이 오늘 왜 오르고/내리고 있는지 근거를 연결해 요약하라.\n"
                  "규칙: 데이터에 있는 뉴스·공시·배당·수급·거래대금·업종/시장 흐름만 근거로 쓴다. 근거가 없거나 시장·업종 흐름과 비슷한 수준이면 conf 를 \"low\" 로 하고 억지 이유를 만들지 않는다. 숫자는 한국식 표기(Rp1,250억·3.85%·2.1배), 금액 단위는 억/조 루피아.\n"
                  "출력은 JSON 배열: [{\"t\": 티커, \"ko\": \"한국어 2문장, 증권사 시황 문체(명사형 종결), 숫자 포함\", \"id\": \"Bahasa Indonesia 2 kalimat\", \"tags\": [\"키워드 최대 3개(한국어)\"], \"conf\": \"high|low\"}]\n\n"
                  + "\n\n".join(ctx(t) for t in chunk))
        j = _json_loads_loose(_gemini(prompt, 3000))
        if not isinstance(j, list): log("종목 AI 요약 응답 파싱 실패"); continue
        for o in j:
            t = str(o.get("t", "")).upper()
            if t not in chunk: continue
            cache[t] = {"ko": str(o.get("ko", ""))[:500], "id": str(o.get("id", ""))[:500], "tags": [str(x)[:20] for x in (o.get("tags") or [])][:3], "conf": "low" if str(o.get("conf", "")).lower() == "low" else "high",
                        "pct": (stocks.get(t) or {}).get("pct"), "ts": now.isoformat(), "hhmm": now.strftime("%H:%M")}; done += 1
    if done:
        try: AI_STK_P.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception: pass
    if can: log(f"종목 AI 요약 {done}건 (대상 {len(targets)} · 갱신 필요 {len(todo)})")
    lim = (now - dt.timedelta(hours=26)).isoformat()
    return {t: c for t, c in cache.items() if c.get("ts", "") >= lim}

def _thin(a, n=120):
    a = a or []; return a[::max(1, len(a) // n)]

def housekeeping():
    """캐시 비대화 방지: 30일 지난 일별 요약(ss_*.json)·프로브 파일 삭제, news_seen 14일, stock_ai 2일"""
    try:
        cut = (now_wib().date() - dt.timedelta(days=30)).strftime("%Y%m%d"); n = 0
        for f in CACHE.glob("ss_*.json"):
            if f.stem[3:] < cut:
                try: f.unlink(); n += 1
                except Exception: pass
        try:
            m = json.loads(SEEN_P.read_text(encoding="utf-8")); lim = (now_wib() - dt.timedelta(days=14)).isoformat()
            m2 = {k: v for k, v in m.items() if str(v) >= lim}
            if len(m2) < len(m): SEEN_P.write_text(json.dumps(m2, ensure_ascii=False), encoding="utf-8")
        except Exception: pass
        try:
            c = json.loads(AI_STK_P.read_text(encoding="utf-8")); lim = (now_wib() - dt.timedelta(days=2)).isoformat()
            c2 = {k: v for k, v in c.items() if v.get("ts", "") >= lim}
            if len(c2) < len(c): AI_STK_P.write_text(json.dumps(c2, ensure_ascii=False), encoding="utf-8")
        except Exception: pass
        if n: log(f"캐시 정리: 일별 요약 {n}개 삭제")
    except Exception as e: log("캐시 정리 오류", e)

def build():
    m = manual(); yb = CFG["ytd_base"]
    mk = idx_market(); ix = idx_index(); bi = bi_indicators()
    IDX_PART = ROOT / "data" / "idx_part.json"; idx_from_pc = None
    yahoo_mode = bool(mk and mk.get("src") == "yahoo")
    if (not mk or yahoo_mode) and IDX_PART.exists():  # IDX 가 막힌 환경(GitHub 러너): PC 가 올린 IDX 분리 파일을 먼저 읽는다
        try: idx_from_pc = json.loads(IDX_PART.read_text(encoding="utf-8"))
        except Exception as e: log("idx_part 읽기 실패", e); idx_from_pc = None
        pix = (idx_from_pc or {}).get("ix")
        if pix and (not ix or (pix.get("date") or "") > (ix.get("date") or "") or (pix.get("intraday") and not ix.get("intraday"))):   # 러너 IDX 지수가 오래됐거나 장중 스냅샷이 없으면 PC 분 사용
            if ix: log(f"IDX 지수 러너 응답 {ix.get('date')} < PC {pix.get('date')} → PC 수집분 사용")
            ix = pix
    # investing.com 은 yfinance 보다 먼저 — yfinance 가 스레드에 asyncio 루프를 남기면 브라우저 기동이 막힌다
    _IV.update(investing_batch())                     # 국채 10년물 카드 + 시장 지표(환율·금리·원자재) 한 번에
    h = yq("^JKSE"); usd = yq("USDIDR=X"); krw = yq("KRW=X")
    in_session = 9 <= now_wib().hour < 17 and now_wib().weekday() < 5
    indices = []
    def add_index(code, label, name, live, eod, inv=False, dec=2):
        """장중엔 Yahoo 실시간(지연) 우선 — 전일 종가를 현재가로 보여주지 않는다. 장 마감 후엔 IDX 확정 종가."""
        today_iso = now_wib().date().isoformat()
        if live and eod and eod.get("date") and eod["date"] < today_iso and eod.get("px"):
            live = dict(live, prev=eod["px"])             # IDX 가 확정한 직전 거래일 종가를 전일종가로 (Yahoo 는 하루 밀리는 경우가 있음)
        if eod and eod.get("date") == today_iso and eod.get("px") and eod.get("ts") and (not live or str(live.get("ts", "")) < eod["ts"]):
            # 장중 IDX 스냅샷(PC 수집)이 Yahoo 1분봉보다 최신 — Yahoo 는 점심 휴장 뒤 틱이 늦게 붙는 경우가 있다 → IDX 값을 현재가로
            live = {"px": eod["px"], "prev": eod["prev"], "ts": eod["ts"], "spark": eod.get("spark") or (live or {}).get("spark") or [],
                    "high": eod.get("high"), "low": eod.get("low"), "src": "idx"}
        if live and (in_session or not eod):
            indices.append({"code": code, "label": label, "name": name, "px": round(live["px"], dec), "prev": round(live["prev"], dec), "pct": round((live["px"] / live["prev"] - 1) * 100, 2),
                            "spark": _thin(live["spark"], 240), "inv": inv, "asof": live["ts"], "high": live.get("high"), "low": live.get("low"), "src": live.get("src", "yahoo")})
        elif eod:
            indices.append({"code": code, "label": label, "name": name, "px": round(eod["px"], dec), "prev": round(eod["prev"], dec), "pct": round((eod["px"] / eod["prev"] - 1) * 100, 2),
                            "spark": _thin(eod.get("spark"), 240), "inv": inv, "asof": eod.get("asof", "종가")})
    jl, ll, ul = ylive("^JKSE"), ylive("^JKLQ45"), ylive("USDIDR=X")
    if not ul: ul = ylive_fx("USDIDR=X")             # 환율은 24시간 거래 — 1분봉이 비면 15분봉 최근 24시간으로
    glob_idx = global_indices()                       # yfinance 호출은 브라우저 작업(번역·캘린더) 전에 모아서
    today_iso = now_wib().date().isoformat()
    ixi = (ix or {}).get("intraday"); ixl = (ix or {}).get("intraday_lq45")
    add_index("COMPOSITE", "IHSG", "자카르타 종합", jl,
              ({"px": ixi["px"], "prev": ixi["prev"], "spark": ixi["spark"], "date": today_iso, "ts": ixi["ts"], "high": ixi.get("high"), "low": ixi.get("low"), "asof": f'IDX {ixi["ts"]}'} if ixi else
               {"px": ix["px"], "prev": ix["prev"], "spark": ix["spark"], "date": ix.get("date"), "ts": ix.get("ts"), "high": ix.get("high"), "low": ix.get("low"), "asof": f'IDX {ix["date"][5:].replace("-", "/")} 종가'}) if ix else None)
    add_index("LQ45", "LQ45", "대형 45종목", ll,
              ({"px": ixl["px"], "prev": ixl["prev"], "spark": ixl["spark"], "date": today_iso, "ts": ixl["ts"], "asof": f'IDX {ixl["ts"]}'} if ixl else
               {"px": ix["lq45"]["px"], "prev": ix["lq45"]["prev"], "spark": ix["lq45"]["spark"], "date": ix.get("date"), "ts": ix.get("ts"), "asof": "IDX 종가"}) if ix and ix.get("lq45") else None)
    add_index("USDIDR", "USD/IDR", "달러/루피아", ul, {"px": usd["px"], "prev": usd["prev"], "asof": "Yahoo", "spark": usd.get("spark") or []} if usd else None, inv=True, dec=0)
    sc = sun10y_card(_IV.get("SUN10Y"))
    if sc: indices.append(sc)                         # 4번째 카드: 국채 10년물 (외국인 순매수 카드는 시장 현황 아래 '외국인 수급'으로 통합)
    jci_card = next((x for x in indices if x["code"] == "COMPOSITE"), None)
    px = jci_card["px"] if jci_card else None
    index = {"session": (jci_card["asof"] if jci_card else "—"),
             "high": (jci_card.get("high") if jci_card and jci_card.get("high") else (ix["high"] if ix else None)), "low": (jci_card.get("low") if jci_card and jci_card.get("low") else (ix["low"] if ix else None)),
             "prev": jci_card["prev"] if jci_card else None,
             "ytd": round((px / yb["JCI"] - 1) * 100, 2) if px and yb.get("JCI") else None, "m1": round((px / h["m1"] - 1) * 100, 2) if px and h else None,
             "value_idr": ix.get("value_idr") if ix else None, "volume": ix.get("volume") if ix else None}
    if mk:
        index.update({"rank_src": mk.get("rank_src"), "rank_asof": mk.get("rank_asof"), "rank_date": mk.get("rank_date")})
        index.update({"adv": mk["adv"], "dec": mk["dec"], "unch": mk["unch"], "foreign_net_idr": mk["foreign_net_idr"], "foreign_buy": mk["foreign_buy"],
                      "foreign_sell": mk["foreign_sell"], "foreign_note": mk["foreign_note"], "foreign_date": None if yahoo_mode else mk["date"][5:].replace("-", "/"), "nonreg_idr": mk["nonreg_idr"]})
        if not index.get("value_idr"): index["value_idr"] = mk["value_idr"]
    if m.get("index"):                               # 수기값(IDX Daily Statistics PDF 확정치)이 있으면 최우선
        for k, v in m["index"].items():
            if v is not None: index[k] = v
    if bi and bi.get("nonres_week"): index["bi_nonres"] = bi["nonres_week"]["text"]
    news_items = news_block(); kisi_items = kisi_news()   # 뉴스를 먼저 — 기사 속 배당 금액(NEWS_DIVS)을 캘린더에 쓴다
    corp = idx_corp_calendar() or []
    if not corp and idx_from_pc and idx_from_pc.get("corp_cal"):          # IDX 차단 환경: PC 가 올린 기업·배당 캘린더 사용
        corp = idx_from_pc["corp_cal"]
    try:
        DIVS = investing_dividends()
        for t, v in NEWS_DIVS.items():                     # investing.com 에 없는 종목은 화이트리스트·KISI 기사에서 보충
            if t not in DIVS or not DIVS[t].get("dps"): DIVS[t] = {"dps": v["dps"], "type": "", "ex": None, "pay": None, "yld": None, "src": v["src"], "url": v.get("url")}
        log(f"배당 금액 부착 {attach_dividends(corp, DIVS)}건 (기사 보충 {len(NEWS_DIVS)}: {', '.join(NEWS_DIVS)})")
    except Exception as e: log("배당 금액 오류", e); DIVS = {}
    for c in corp: c["country"] = "ID"
    glob = macro_calendar_auto()
    # 수기 항목이 우선하되, 수기 exp/prev/act 가 비어 있으면 자동 수집값으로 채운다
    # (예: ID CPI 를 미리 적어두고 act 를 비워두면 발표 후 자동으로 실제치가 들어온다)
    seen = {}; macro = []
    for e in macro_calendar() + glob:
        k = (e["date"], re.sub(r"\W", "", e["title"])[:12])
        tags = _ev_tags(e["title"])
        k2 = (e["date"], e.get("time"), e.get("country"), tags) if (e.get("time") not in (None, "", "—") and tags) else None
        if k2 and k2 in seen: k = k2                      # 같은 시각 + 같은 지표 태그일 때만 수기·investing 병합
        if k in seen:
            base = seen[k]
            for f in ("exp", "prev", "act"):
                if base.get(f) in (None, "", "—") and e.get(f) not in (None, "", "—"):
                    base[f] = e[f]
                    if f == "act": base["act_src"] = e.get("src")
            continue
        seen[k] = e
        if k2: seen[k2] = e
        macro.append(e)
    calendar = translate_field(sorted(macro + corp, key=lambda e: (e["date"], e.get("time") or "—")), "title")
    announcements = translate_field(idx_announcements_today(), "title")
    # ---- IDX 분리 파일: PC(IDX 접근 가능)가 data/idx_part.json 을 쓰고 GitHub 로 올리면,
    #      IDX 가 막힌 GitHub 러너는 그 파일을 읽어 랭킹·종목·외국인·공시를 채운다 (러너는 지수·뉴스·캘린더만 직접 수집)
    if mk and not yahoo_mode:
        part = {"saved": now_wib().strftime("%Y-%m-%d %H:%M"), "index": {k: index.get(k) for k in ("rank_src", "rank_asof", "rank_date", "adv", "dec", "unch", "foreign_net_idr", "foreign_buy", "foreign_sell", "foreign_note", "foreign_date", "nonreg_idr", "value_idr", "volume")},
                "value": mk["value"], "gainers": mk["gainers"], "losers": mk["losers"], "turnover": mk["turnover"], "foreign_top": mk["foreign_top"], "foreign_bottom": mk["foreign_bottom"],
                "stocks": mk.get("stocks", []), "announcements": announcements, "hist_days": mk["hist_days"], "corp_cal": corp,
                "ix": ({k: v for k, v in ix.items() if k not in ("spark", "lq45", "intraday", "intraday_lq45")} | {"spark": _thin(ix.get("spark")), "lq45": ({k: v for k, v in ix["lq45"].items() if k != "spark"} | {"spark": _thin(ix["lq45"].get("spark"))}) if ix.get("lq45") else None}
                       | ({"intraday": ix["intraday"] | {"spark": _thin(ix["intraday"].get("spark"))}} if ix.get("intraday") else {})
                       | ({"intraday_lq45": ix["intraday_lq45"] | {"spark": _thin(ix["intraday_lq45"].get("spark"))}} if ix.get("intraday_lq45") else {})) if ix else None}
        try: IDX_PART.write_text(json.dumps(part, ensure_ascii=False), encoding="utf-8")
        except Exception as e: log("idx_part 저장 실패", e)
    elif idx_from_pc:
        try:
            pi = idx_from_pc.get("index") or {}
            for k, v in pi.items():
                if v is not None and index.get(k) is None: index[k] = v
            tag = f' · 외국인·공시 PC {idx_from_pc.get("saved", "")[5:]}' if yahoo_mode else f' · PC {idx_from_pc.get("saved", "")[-5:]}'
            index["rank_src"] = (index.get("rank_src") or pi.get("rank_src") or "IDX") + tag
            if not announcements: announcements = idx_from_pc.get("announcements") or []
            if yahoo_mode:                               # Yahoo 랭킹 + PC 의 외국인 순매수(종목별) 결합
                fn = {st["t"]: st.get("fnet") for st in idx_from_pc.get("stocks", [])}
                for st in mk.get("stocks", []): st["fnet"] = fn.get(st["t"])
                mk["foreign_top"] = idx_from_pc.get("foreign_top") or []; mk["foreign_bottom"] = idx_from_pc.get("foreign_bottom") or []
            log(f'IDX 차단 → {"Yahoo 랭킹 + " if yahoo_mode else ""}PC 수집분 사용 (idx_part.json {idx_from_pc.get("saved")})')
        except Exception as e: log("idx_part 읽기 실패", e); idx_from_pc = None
    P = lambda k: (mk[k] if mk else (idx_from_pc or {}).get(k) or [])
    # 원화 환산율: 실시간(USD/IDR ÷ USD/KRW, Yahoo). 실패 시 config 기본값
    usd_px = (ul or {}).get("px") or (usd or {}).get("px"); krw_px = (krw or {}).get("px")
    if usd_px and krw_px:
        idr_per_krw = round(usd_px / krw_px, 4)
        fx_ts = (ul or {}).get("ts") or now_wib().strftime("%H:%M")
        fx_basis = f"{idr_per_krw:.2f} IDR/KRW · USD/IDR {usd_px:,.0f} · USD/KRW {krw_px:,.0f} · {fx_ts} Yahoo"
    else:
        idr_per_krw = CFG.get("idr_per_krw"); fx_basis = CFG.get("fx_basis")
    data = {"mode": "live", "updated": now_wib().strftime("%Y-%m-%d %H:%M"), "delay_min": 0 if ix else 15,
            "fx_basis": fx_basis, "idr_per_krw": idr_per_krw,
            "indices": indices, "index": index,
            "value": P("value"), "gainers": P("gainers"), "losers": P("losers"),
            "turnover": P("turnover"), "foreign_top": P("foreign_top"), "foreign_bottom": P("foreign_bottom"),
            "stocks": P("stocks"),
            "sectors": sector_block(P("stocks")), "global": glob_idx, "dividends": DIVS,
            "news": news_items, "market_news": MARKET_NEWS, "kisi_news": kisi_items, "macro": macro_block(bi), "calendar": calendar, "announcements": announcements,
            "sources": {"idx_index": bool(ix), "idx_market": bool(mk), "idx_from_pc": (idx_from_pc or {}).get("saved"), "bi": bi.get("src") if bi else None, "hist_days": mk["hist_days"] if mk else (idx_from_pc or {}).get("hist_days", 0), "calendar": "saveticker" if any(e.get("src") == "saveticker" for e in calendar) else "investing.com" if any(e.get("src") == "investing.com" for e in calendar) else "manual"}}
    try: housekeeping()
    except Exception: pass
    _GEM["fail"] = 0
    try: ai_announcements(data["announcements"])
    except Exception as e: log("공시 AI 요약 오류", repr(e)[:120])
    try: data["ai"] = {"stocks": ai_stocks(data), "model": _GEM.get("model") or GEMINI_MODEL}
    except Exception as e: log("종목 AI 요약 오류", repr(e)[:120]); data["ai"] = {"stocks": {}}
    try: data["ai"]["index"] = ai_index(data)
    except Exception as e: log("지수 AI 요약 오류", repr(e)[:120])
    try:
        data["mcap"] = [{"t": r.get("t"), "n": r.get("n"), "px": r.get("px"), "pct": r.get("pct"),
                         "val": r.get("val"), "ratio": r.get("ratio"), "fnet": r.get("fnet"), "mcap": r.get("mcap")}
                        for r in sorted([x for x in (data.get("stocks") or []) if x.get("mcap")],
                                        key=lambda x: -(x.get("mcap") or 0))[:10]]
    except Exception as e: log("시가총액 랭킹 오류", repr(e)[:120]); data["mcap"] = []
    try:
        data["catalyst"] = catalyst_block(data)
        log(f"Catalyst {len(data['catalyst'])}종목" + (f" · 1위 {data['catalyst'][0]['t']} {data['catalyst'][0]['score']}점" if data["catalyst"] else ""))
    except Exception as e: log("Catalyst 오류", repr(e)[:120]); data["catalyst"] = []
    (ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # data.js: index.html 을 파일(file://)로 직접 열어도 마지막 수집 데이터가 보이도록 (fetch 는 file:// 에서 막힘)
    try: (ROOT / "data.js").write_text("window.__IDX_DATA=" + json.dumps(data, ensure_ascii=False) + ";", encoding="utf-8")
    except Exception as e: log("data.js 저장 실패", e)
    idx_browser_close()
    _auto_publish()
    log(f"data.json | JCI {px} | 상승 {index.get('adv', '-')} | 외인 {(index.get('foreign_net_idr') or 0)/1e9:,.0f}억 | 뉴스 {len(data['news'])} | 일정 {len(calendar)} | 공시 {len(announcements)} | BI {'OK' if bi else 'X'}")

if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop"); sec = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 300
        while True:
            try: build()
            except Exception as e: log("build error", repr(e))
            time.sleep(sec)
    else:
        build()
