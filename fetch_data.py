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
import json, re, sys, time, html, hashlib, threading, queue as _queue, datetime as dt
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

def _wait_cf(pg, tries=10):
    """Cloudflare 대기 화면이 걷힐 때까지."""
    for _ in range(tries):
        try: t = pg.evaluate("() => document.body ? document.body.innerText.slice(0,300) : ''") or ""
        except Exception: t = ""
        if not re.search(r"Just a moment|Checking your browser|Verifying you are human", t): return True
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
    def index_chart(self, code="COMPOSITE", period="1D"):
        j = self.get("/primary/helper/GetIndexChart", indexCode=code, period=period)
        return [p["Close"] for p in (j or {}).get("ChartData", []) if p.get("Close")]
    def calendar(self, d):
        j = self.get("/primary/Home/GetCalendar", range="m", date=f"{d:%Y%m%d}")
        return (j or {}).get("Results") or []
    def dividends(self, y, m):
        j = self.get("/primary/DigitalStatistic/GetApiDataPaginated", urlName="LINK_DIVIDEND", periodYear=y, periodMonth=m,
                     periodType="monthly", isPrint="False", cumulative="false", pageSize=500, pageNumber=1)
        return (j or {}).get("data") or []
    def announcements(self, d_from, d_to, code=""):
        j = self.get("/primary/ListedCompany/GetAnnouncement", kodeEmiten=code, indexFrom=0, pageSize=500,
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

def idx_market():
    d, rows = last_trading_day(idx.stock_summary)
    if not rows: return None
    today = now_wib().date()
    in_session = (d < today) and today.weekday() < 5 and 9 <= now_wib().hour < 17
    if in_session and CFG.get("intraday_rank", True):
        mk = _market_from_rows(d, rows)                    # 전일 확정치 (외국인·시장 합계)
        prev = {r["StockCode"]: r for r in rows if r.get("Close")}
        universe = [c for c, r in prev.items() if (r.get("Value") or 0) >= CFG.get("intraday_min_prev_value", 1e8)]
        q = yahoo_intraday(universe)
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
                           "hi": v.get("hi"), "lo": v.get("lo"), "live": 1})
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
        allst.append({"t": r["StockCode"], "n": (r.get("StockName") or "").replace("Tbk.", "").strip(), "px": r["Close"], "prev": r["Previous"],
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
    return {"date": d.isoformat(), "px": px, "prev": prev, "chg": round(px - prev, 2), "pct": round((px / prev - 1) * 100, 2),
            "high": jci["Highest"], "low": jci["Lowest"], "value_idr": jci.get("Value"), "volume": jci.get("Volume"),
            "lq45": {"px": lq["Close"], "prev": lq["Previous"], "pct": round((lq["Close"] / lq["Previous"] - 1) * 100, 2), "spark": idx.index_chart("LQ45", "1D")} if lq else None,
            "spark": idx.index_chart("COMPOSITE", "1D")}

# =============================================================== corporate calendar from IDX
KW = [("Laporan Keuangan", "실적 공시"), ("RUPS", "주주총회"), ("Public Expose", "기업설명회"), ("Cum Date", "배당부 마감"),
      ("Ex Date", "배당락"), ("Dividen", "배당"), ("Right", "유상증자"), ("Stock Split", "액면분할"), ("Buyback", "자사주"),
      ("Tender Offer", "공개매수"), ("Suspensi", "거래정지"), ("Pencatatan", "상장")]
def kor_type(s):
    for k, v in KW:
        if k.lower() in (s or "").lower(): return v
    return s or "공시"

def idx_corp_calendar(days_ahead=14, days_back=1):
    today = now_wib().date(); out = []
    months = (today, (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1))
    for m in months:                                                      # 1) IDX 캘린더
        for e in idx.calendar(m):
            try: d = dt.datetime.fromisoformat(str(e.get("start"))[:19]).date()
            except Exception: continue
            if -days_back <= (d - today).days <= days_ahead:
                out.append({"date": d.isoformat(), "t": (e.get("title") or "").strip()[:6], "kind": "corp", "imp": 2,
                            "title": f'{kor_type(e.get("Jenis"))} · {e.get("description") or ""}'.strip(" ·"), "src": "IDX 캘린더"})
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
    for o in sorted(out, key=lambda o: (o["date"], o["t"] not in uni)):
        o["imp"] = 3 if o["t"] in uni and o["imp"] >= 2 else o["imp"]
        k = (o["date"], o["t"], o["title"][:30])
        if k in seen: continue
        seen.add(k); res.append(o)
    return res[:40]

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
    return out[:80]

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
        d = t.history(period="5d", interval="1d")["Close"].dropna()
        prev = float(d.iloc[-2]) if len(d) >= 2 and last_ts.date() == today else float(d.iloc[-1])
        if last_ts.date() != today: return None          # 오늘 틱이 없으면 라이브 아님
        return {"px": float(c.iloc[-1]), "prev": prev, "ts": last_ts.strftime("%H:%M"),
                "spark": [round(float(v), 2) for v in todays.tolist()][-120:], "high": float(todays.max()), "low": float(todays.min())}
    except Exception as e:
        log("ylive fail", sym, e); return None

def yq(sym):
    if yf is None: return None
    try:
        c = yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=False)["Close"].dropna()
        if len(c) < 2: return None
        return {"px": float(c.iloc[-1]), "prev": float(c.iloc[-2]), "m1": float(c.iloc[0])}
    except Exception as e:
        log("yahoo fail", sym, e); return None

MACRO_ID = [  # 시장지표 라벨/주석 → 인니어 (긴 것부터)
    ("USD/IDR (BI bid 종가)", "USD/IDR (penutupan bid BI)"),
    ("USD/IDR (Yahoo 15분 지연)", "USD/IDR (Yahoo, tunda 15 mnt)"),
    ("IDR/KRW (1원당 루피아)", "IDR/KRW (Rupiah per 1 Won)"),
    ("비거주자 주간 순매수", "Net beli nonresiden mingguan"),
    ("국채 10년물", "Obligasi negara 10 tahun"),
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
    usdidr = yq("USDIDR=X")
    if bi and bi.get("idr_close"):
        row("USD/IDR (BI bid 종가)", {"px": bi["idr_close"], "prev": usdidr["prev"] if usdidr else None}, inv=True, base=yb.get("USDIDR"), fmt="{:,.0f}", inverse_ytd=True, note=f'BI {bi.get("as_of")}')
    row("USD/IDR (Yahoo 15분 지연)", usdidr, inv=True, base=yb.get("USDIDR"), fmt="{:,.0f}", inverse_ytd=True)
    row("IDR/KRW (1원당 루피아)", yq("KRWIDR=X"), inv=True, base=yb.get("KRWIDR"), inverse_ytd=True, note=f'연초 {yb.get("KRWIDR")}')
    # 국채 10년물 — investing.com 실시간이 1순위, 실패 시 BI 보도자료 → Kontan → 수기
    sun = sun_prev = sun_src = None
    iv = _IV.get("SUN10Y")
    if iv:
        sun, sun_src = iv["px"], "investing.com 실시간"
        if iv.get("chg") is not None: sun_prev = round(iv["px"] - iv["chg"], 4)
    if sun is None and bi and bi.get("sun10y"): sun, sun_src = bi["sun10y"], "BI 보도자료"
    if sun is None: sun, sun_src = kontan_sun10y(), "Kontan pusatdata"
    if sun is None and m.get("sun10y"): sun, sun_src = m["sun10y"], "수기"
    row("국채 10년물", {"px": sun, "prev": sun_prev}, inv=True, base=yb.get("SUN10Y"), fmt="{:,.3f}%", note=f'연초 {yb.get("SUN10Y")}%', src=sun_src)
    out.append({"k": "BI Rate", "v": f'{m["bi_rate"]:.2f}%' if m.get("bi_rate") else "확인 필요", "d": None, "ytd": None, "note": m.get("bi_note")})
    row("Brent (US$/bbl)", yq("BZ=F"), base=yb.get("Brent"))
    dxy = yq("DX-Y.NYB")
    if bi and bi.get("dxy"): row("DXY", {"px": bi["dxy"], "prev": dxy["prev"] if dxy else None}, note=f'BI {bi.get("as_of")}')
    else: row("DXY", dxy)
    ust = yq("^TNX")
    if bi and bi.get("ust10y"): row("UST 10Y", {"px": bi["ust10y"], "prev": ust["prev"] if ust else None}, inv=True, fmt="{:,.3f}%", note="BI")
    else: row("UST 10Y", ust, inv=True, fmt="{:,.2f}%")
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
    for r in rows:
        code = (r.get("KodeEmiten") or "").strip().upper(); name = (r.get("NamaEmiten") or "").strip()
        if len(code) == 4 and name:
            clean = re.sub(r"^PT\.?\s+|\s+Tbk\.?$|\s*\(Persero\)\s*", " ", name, flags=re.I).strip()
            out[code] = [clean]
    if out:
        f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8"); log("tickers_all", len(out))
        return out
    return {}

def build_alias():
    universe = dict(all_tickers()); 
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

def _tr_claude_api(pairs):
    """pairs: [(원문, 목표언어)] → {(원문, 목표언어): 번역}. Anthropic API (secrets.json 의 anthropic_api_key). 결과는 tr_claude.json 에 영구 저장."""
    key = _secret("anthropic_api_key")
    if not key or not pairs: return {}
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
            body = {"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt + json.dumps(chunk, ensure_ascii=False)}]}
            try:
                r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=90,
                                  headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
                if r.status_code != 200:
                    log(f"Claude API 번역 실패 {r.status_code}: {r.text[:120]}"); return out
                txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
                txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
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
            log(f"Claude API 번역 {len(out)}건 → tr_claude.json (총 {len(cur)})")
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
            if key in TR_CACHE: it[tgt] = TR_CACHE[key]; continue
            need.append((it, tgt, src, tl, key))
    if need and _secret("anthropic_api_key"):        # Claude API (키가 있을 때만) — 품질 우선, 결과는 영구 캐시
        got = _tr_claude_api(list({(src, tl) for _, _, src, tl, _ in need[:cfg.get("claude_max_per_run", 120)]}))
        if got:
            rest = []
            for it, tgt, src, tl, key in need:
                v = got.get((src, tl))
                if v: it[tgt] = v
                else: rest.append((it, tgt, src, tl, key))
            need = rest
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
    max_items = max_items or CFG.get("news_max_items", 80); items = []
    for src in CFG["whitelist"]:
        entries = []
        for url in [u for u in (src.get("rss"), None) if u is not None]:
            try: entries = feedparser.parse(url, request_headers={"User-Agent": UA}).entries
            except Exception as e: log("rss fail", src["name"], e)
        if not entries:
            alt = discover_rss(src)
            if alt and alt != src.get("rss"):
                try: entries = feedparser.parse(alt, request_headers={"User-Agent": UA}).entries
                except Exception: pass
        if not entries:
            entries = scrape_home(src); log("rss empty → home scrape", src["name"], len(entries))
        for e in entries[:40]:
            title = html.unescape(e.get("title", "")).strip()
            summ = re.sub("<[^>]+>", " ", html.unescape(e.get("summary", "")))
            tags = screen(title + " " + summ)
            if CFG.get("news_only_with_ticker", True) and not tags: continue
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
            items.append(it)
    items.sort(key=lambda x: x["ts"], reverse=True)
    seen, out = set(), []
    for it in items:
        k = it["t"][:60]
        if k in seen: continue
        seen.add(k); out.append(it)
    return translate_news(out[:max_items])

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

def build():
    m = manual(); yb = CFG["ytd_base"]
    mk = idx_market(); ix = idx_index(); bi = bi_indicators()
    # investing.com 은 yfinance 보다 먼저 — yfinance 가 스레드에 asyncio 루프를 남기면 브라우저 기동이 막힌다
    _IV["SUN10Y"] = investing_quote(INVESTING_QUOTES["SUN10Y"])
    log("국채10Y investing.com", (f'{_IV["SUN10Y"]["px"]}%' if _IV.get("SUN10Y") else "실패"))
    h = yq("^JKSE"); usd = yq("USDIDR=X")
    in_session = 9 <= now_wib().hour < 17 and now_wib().weekday() < 5
    indices = []
    def add_index(code, label, name, live, eod, inv=False, dec=2):
        """장중엔 Yahoo 실시간(지연) 우선 — 전일 종가를 현재가로 보여주지 않는다. 장 마감 후엔 IDX 확정 종가."""
        if live and (in_session or not eod):
            indices.append({"code": code, "label": label, "name": name, "px": round(live["px"], dec), "prev": round(live["prev"], dec), "pct": round((live["px"] / live["prev"] - 1) * 100, 2),
                            "spark": live["spark"], "inv": inv, "asof": live["ts"], "high": live.get("high"), "low": live.get("low")})
        elif eod:
            indices.append({"code": code, "label": label, "name": name, "px": round(eod["px"], dec), "prev": round(eod["prev"], dec), "pct": round((eod["px"] / eod["prev"] - 1) * 100, 2),
                            "spark": eod.get("spark") or [], "inv": inv, "asof": eod.get("asof", "종가")})
    jl, ll, ul = ylive("^JKSE"), ylive("^JKLQ45"), ylive("USDIDR=X")
    add_index("COMPOSITE", "IHSG", "자카르타 종합", jl, {"px": ix["px"], "prev": ix["prev"], "spark": ix["spark"], "asof": f'IDX {ix["date"][5:].replace("-", "/")} 종가'} if ix else None)
    add_index("LQ45", "LQ45", "대형 45종목", ll, {"px": ix["lq45"]["px"], "prev": ix["lq45"]["prev"], "spark": ix["lq45"]["spark"], "asof": "IDX 종가"} if ix and ix["lq45"] else None)
    add_index("USDIDR", "USD/IDR", "달러/루피아", ul, {"px": usd["px"], "prev": usd["prev"], "asof": "Yahoo"} if usd else None, inv=True, dec=0)
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
                      "foreign_sell": mk["foreign_sell"], "foreign_note": mk["foreign_note"], "foreign_date": mk["date"][5:].replace("-", "/"), "nonreg_idr": mk["nonreg_idr"]})
        if not index.get("value_idr"): index["value_idr"] = mk["value_idr"]
    if m.get("index"):                               # 수기값(IDX Daily Statistics PDF 확정치)이 있으면 최우선
        for k, v in m["index"].items():
            if v is not None: index[k] = v
    if bi and bi.get("nonres_week"): index["bi_nonres"] = bi["nonres_week"]["text"]
    corp = idx_corp_calendar() or []
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
    IDX_PART = ROOT / "data" / "idx_part.json"; idx_from_pc = None
    if mk:
        part = {"saved": now_wib().strftime("%Y-%m-%d %H:%M"), "index": {k: index.get(k) for k in ("rank_src", "rank_asof", "rank_date", "adv", "dec", "unch", "foreign_net_idr", "foreign_buy", "foreign_sell", "foreign_note", "foreign_date", "nonreg_idr", "value_idr", "volume")},
                "value": mk["value"], "gainers": mk["gainers"], "losers": mk["losers"], "turnover": mk["turnover"], "foreign_top": mk["foreign_top"], "foreign_bottom": mk["foreign_bottom"],
                "stocks": mk.get("stocks", []), "announcements": announcements, "hist_days": mk["hist_days"],
                "ix": ({k: v for k, v in ix.items() if k not in ("spark", "lq45")} | {"spark": (ix.get("spark") or [])[::max(1, len(ix.get("spark") or []) // 120)], "lq45": ({k: v for k, v in ix["lq45"].items() if k != "spark"} | {"spark": (ix["lq45"].get("spark") or [])[::max(1, len(ix["lq45"].get("spark") or []) // 120)]}) if ix.get("lq45") else None}) if ix else None}
        try: IDX_PART.write_text(json.dumps(part, ensure_ascii=False), encoding="utf-8")
        except Exception as e: log("idx_part 저장 실패", e)
    elif IDX_PART.exists():
        try:
            idx_from_pc = json.loads(IDX_PART.read_text(encoding="utf-8"))
            pi = idx_from_pc.get("index") or {}
            for k, v in pi.items():
                if v is not None and index.get(k) is None: index[k] = v
            index["rank_src"] = (pi.get("rank_src") or "IDX") + f' · PC {idx_from_pc.get("saved", "")[-5:]}'
            if not ix and idx_from_pc.get("ix"): ix = idx_from_pc["ix"]
            if not announcements: announcements = idx_from_pc.get("announcements") or []
            log(f'IDX 차단 → PC 수집분 사용 (idx_part.json {idx_from_pc.get("saved")})')
        except Exception as e: log("idx_part 읽기 실패", e); idx_from_pc = None
    P = lambda k: (mk[k] if mk else (idx_from_pc or {}).get(k) or [])
    data = {"mode": "live", "updated": now_wib().strftime("%Y-%m-%d %H:%M"), "delay_min": 0 if ix else 15,
            "fx_basis": m.get("fx_basis", CFG.get("fx_basis")), "idr_per_krw": m.get("idr_per_krw", CFG.get("idr_per_krw")),
            "indices": indices, "index": index,
            "value": P("value"), "gainers": P("gainers"), "losers": P("losers"),
            "turnover": P("turnover"), "foreign_top": P("foreign_top"), "foreign_bottom": P("foreign_bottom"),
            "stocks": P("stocks"),
            "news": news_block(), "macro": macro_block(bi), "calendar": calendar, "announcements": announcements,
            "sources": {"idx_index": bool(ix), "idx_market": bool(mk), "idx_from_pc": (idx_from_pc or {}).get("saved"), "bi": bi.get("src") if bi else None, "hist_days": mk["hist_days"] if mk else (idx_from_pc or {}).get("hist_days", 0), "calendar": "saveticker" if any(e.get("src") == "saveticker" for e in calendar) else "investing.com" if any(e.get("src") == "investing.com" for e in calendar) else "manual"}}
    (ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # data.js: index.html 을 파일(file://)로 직접 열어도 마지막 수집 데이터가 보이도록 (fetch 는 file:// 에서 막힘)
    try: (ROOT / "data.js").write_text("window.__IDX_DATA=" + json.dumps(data, ensure_ascii=False) + ";", encoding="utf-8")
    except Exception as e: log("data.js 저장 실패", e)
    idx_browser_close()
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
