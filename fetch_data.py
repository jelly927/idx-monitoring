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
import json, re, sys, time, html, datetime as dt
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
    def session(self):
        if self.ready: return
        self.s.get(self.BASE + "/id", timeout=30); time.sleep(1)
        self.s.get(self.BASE + "/primary/home/GetIndexList", headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
        self.ready = True
    def get(self, path, **params):
        import os
        proxy = os.environ.get("IDX_PROXY")            # Cloudflare Worker 주소. GitHub 러너 IP가 IDX에 막히면 Worker 경유
        if not proxy: self.session()
        for i in range(3):
            try:
                if proxy:
                    full = requests.Request("GET", self.BASE + path, params=params).prepare().url
                    r = requests.get(proxy.rstrip("/") + "/?url=" + requests.utils.quote(full, safe=""), timeout=40)
                else:
                    r = self.s.get(self.BASE + path, params=params, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=40)
                if r.status_code == 200: return r.json()
                log("IDX", r.status_code, path)
            except Exception as e:
                log("IDX err", path, e)
            time.sleep(2 * (i + 1))
        return None

    # ---- 일별 전종목 요약 (확정된 날은 캐시)
    def stock_summary(self, d, force=False):
        f = CACHE / f"ss_{d:%Y%m%d}.json"
        if f.exists() and not force:
            return json.loads(f.read_text(encoding="utf-8"))
        j = self.get("/primary/TradingSummary/GetStockSummary", date=f"{d:%Y%m%d}", length=9999, start=0)
        rows = (j or {}).get("data") or []
        if rows and (d < now_wib().date() or now_wib().hour >= 17):
            f.write_text(json.dumps(rows), encoding="utf-8")
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
def idx_market():
    d, rows = last_trading_day(idx.stock_summary)
    if not rows: return None
    stocks = [r for r in rows if r.get("Close") and r.get("Previous")]
    adv = sum(1 for r in stocks if r["Change"] > 0); dec = sum(1 for r in stocks if r["Change"] < 0)
    unch = sum(1 for r in stocks if r["Change"] == 0 and r.get("Volume"))
    value = sum(r.get("Value") or 0 for r in rows)
    fbuy = sum(r.get("ForeignBuy") or 0 for r in rows); fsell = sum(r.get("ForeignSell") or 0 for r in rows)
    nonreg = sum(r.get("NonRegularValue") or 0 for r in rows)
    # 20일 평균 대금 (과거 요약은 캐시에서; 첫 실행만 IDX 호출)
    hist = {}; dd = d - dt.timedelta(days=1); n = 0; tries = 0
    while n < 20 and tries < 40:
        f = CACHE / f"ss_{dd:%Y%m%d}.json"
        past = json.loads(f.read_text(encoding="utf-8")) if f.exists() else (idx.stock_summary(dd) if tries < 30 else [])
        if past:
            for r in past: hist.setdefault(r["StockCode"], []).append(r.get("Value") or 0)
            n += 1
        if not f.exists(): time.sleep(0.8)
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
                       "fnet": (r.get("ForeignBuy") or 0) - (r.get("ForeignSell") or 0)})
    return {"date": d.isoformat(), "adv": adv, "dec": dec, "unch": unch, "value_idr": value, "nonreg_idr": nonreg,
            "foreign_buy": fbuy, "foreign_sell": fsell, "foreign_net_idr": fbuy - fsell,
            "foreign_note": f"IDX StockSummary {d:%m/%d} 전체시장 합산 (비정규 Rp{nonreg/1e12:.2f}조 포함)",
            "value": sorted(liquid, key=lambda x: -x["val"])[:10],
            "gainers": sorted(liquid, key=lambda x: -x["pct"])[:10], "losers": sorted(liquid, key=lambda x: x["pct"])[:10],
            "turnover": sorted([x for x in liquid if x["ratio"]], key=lambda x: -x["ratio"])[:10],
            "foreign_top": sorted(liquid, key=lambda x: -x["fnet"])[:5], "foreign_bottom": sorted(liquid, key=lambda x: x["fnet"])[:5],
            "hist_days": n}

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
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("playwright 없음: pip install playwright && playwright install chromium"); return []
    out = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_page(user_agent=UA, timezone_id="Asia/Jakarta")
            for i in range(days):
                d = now_wib().date() + dt.timedelta(days=i)
                pg.goto(f"https://www.saveticker.com/calendar?date={d.isoformat()}", wait_until="networkidle", timeout=45000)
                pg.wait_for_timeout(1500)
                html_ = pg.content()
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
            b.close()
    except Exception as e:
        log("saveticker fail", e)
    return out

INVESTING_CAL = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
def investing_calendar(days=14, countries=("5", "48")):   # 5=US, 48=Indonesia
    """investing.com 경제 캘린더 (비공식 XHR). saveticker 실패 시 대체."""
    if BeautifulSoup is None: return []
    d0 = now_wib().date(); d1 = d0 + dt.timedelta(days=days)
    data = [("country[]", c) for c in countries] + [("importance[]", "1"), ("importance[]", "2"), ("importance[]", "3"),
            ("dateFrom", d0.isoformat()), ("dateTo", d1.isoformat()), ("timeZone", "113"), ("timeFilter", "timeRemain"), ("currentTab", "custom"), ("limit_from", "0")]
    try:
        r = requests.post(INVESTING_CAL, data=data, headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Referer": "https://www.investing.com/economic-calendar/"}, timeout=40)
        soup = BeautifulSoup(r.json().get("data", ""), "lxml"); out = []; cur = None
        for tr in soup.select("tr"):
            if tr.get("id", "").startswith("theDay"):
                cur = tr.get_text(strip=True); continue
            if not tr.get("event_timestamp"): continue
            ts = dt.datetime.strptime(tr["event_timestamp"], "%Y-%m-%d %H:%M:%S")
            flag = tr.select_one(".flagCur span"); cty = (flag.get("title") if flag else "")
            imp = len(tr.select(".sentiment .grayFullBullishIcon"))
            cells = [c.get_text(" ", strip=True) for c in tr.select("td")]
            title = (tr.select_one(".event") or tr).get_text(" ", strip=True)
            act, exp, prev = (tr.select_one(".act") or tr.select_one("td.bold")), tr.select_one(".fore"), tr.select_one(".prev")
            out.append({"date": ts.date().isoformat(), "time": ts.strftime("%H:%M"), "kind": "macro", "country": "ID" if "Indonesia" in cty else "US" if "United States" in cty else cty[:2].upper(),
                        "title": title, "imp": imp or 1, "exp": exp.get_text(strip=True) if exp else None, "prev": prev.get_text(strip=True) if prev else None,
                        "act": act.get_text(strip=True) if act and act.get_text(strip=True) else None, "src": "investing.com"})
        return out
    except Exception as e:
        log("investing fail", e); return []

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
        h = requests.get(BI_LIST, headers={"User-Agent": UA}, timeout=30).text
        urls = re.findall(r'href="([^"]*news-release/Pages/sp_[^"]+\.aspx)"', h)
        cand = [u for u in urls if re.search(r"Indikator Stabilitas", h[max(0, h.find(u) - 300):h.find(u) + 600], re.I)]
        if not cand: return None
        url = cand[0] if cand[0].startswith("http") else "https://www.bi.go.id" + cand[0]
        t = requests.get(url, headers={"User-Agent": UA}, timeout=30).text
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

def kontan_sun10y():
    """Kontan pusatdata 벤치마크 SUN yield 표 — '10 tahun' 행의 첫 숫자."""
    if BeautifulSoup is None: return None
    try:
        h = requests.get("https://pusatdata.kontan.co.id/market/yield_sun_acuan", headers={"User-Agent": UA}, timeout=30).text
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
    sun, sun_src = (bi.get("sun10y"), "BI 보도자료") if bi and bi.get("sun10y") else (None, None)
    if not sun: sun, sun_src = kontan_sun10y(), "Kontan pusatdata"
    if not sun and m.get("sun10y"): sun, sun_src = m["sun10y"], "수기"
    row("국채 10년물", {"px": sun, "prev": None}, inv=True, base=yb.get("SUN10Y"), fmt="{:,.2f}%", note=f'연초 {yb.get("SUN10Y")}%', src=sun_src)
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
            t = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc).astimezone(WIB) if ts else now_wib()
            items.append({"ts": t.isoformat(), "date": t.date().isoformat(), "time": t.strftime("%H:%M") if t.date() == now_wib().date() else t.strftime("%m/%d"),
                          "src": src["name"], "t": title, "tags": tags, "url": e.get("link", "")})
    items.sort(key=lambda x: x["ts"], reverse=True)
    seen, out = set(), []
    for it in items:
        k = it["t"][:60]
        if k in seen: continue
        seen.add(k); out.append(it)
    return out[:max_items]

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

def build():
    m = manual(); yb = CFG["ytd_base"]
    mk = idx_market(); ix = idx_index(); bi = bi_indicators()
    h = yq("^JKSE"); usd = yq("USDIDR=X")
    in_session = 9 <= now_wib().hour < 17 and now_wib().weekday() < 5
    indices = []
    def add_index(code, label, name, live, eod, inv=False, dec=2):
        """장중엔 Yahoo 실시간(지연) 우선 — 전일 종가를 현재가로 보여주지 않는다. 장 마감 후엔 IDX 확정 종가."""
        if live and (in_session or not eod):
            indices.append({"code": code, "label": label, "name": name, "px": round(live["px"], dec), "prev": round(live["prev"], dec), "pct": round((live["px"] / live["prev"] - 1) * 100, 2),
                            "spark": live["spark"], "inv": inv, "asof": f'{live["ts"]} · 15분 지연', "high": live.get("high"), "low": live.get("low")})
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
        index.update({"adv": mk["adv"], "dec": mk["dec"], "unch": mk["unch"], "foreign_net_idr": mk["foreign_net_idr"], "foreign_buy": mk["foreign_buy"],
                      "foreign_sell": mk["foreign_sell"], "foreign_note": mk["foreign_note"], "foreign_date": mk["date"][5:].replace("-", "/"), "nonreg_idr": mk["nonreg_idr"]})
        if not index.get("value_idr"): index["value_idr"] = mk["value_idr"]
    if m.get("index"):                               # 수기값(IDX Daily Statistics PDF 확정치)이 있으면 최우선
        for k, v in m["index"].items():
            if v is not None: index[k] = v
    if bi and bi.get("nonres_week"): index["bi_nonres"] = bi["nonres_week"]["text"]
    corp = idx_corp_calendar() or []
    for c in corp: c["country"] = "ID"
    glob = saveticker_calendar() or investing_calendar()
    seen = set(); macro = []
    for e in macro_calendar() + glob:                    # 수기 항목이 우선, 같은 날짜+제목 앞 12자 중복 제거
        k = (e["date"], re.sub(r"\W", "", e["title"])[:12])
        if k in seen: continue
        seen.add(k); macro.append(e)
    calendar = sorted(macro + corp, key=lambda e: (e["date"], e.get("time") or "—"))
    announcements = idx_announcements_today()
    data = {"mode": "live", "updated": now_wib().strftime("%Y-%m-%d %H:%M"), "delay_min": 0 if ix else 15,
            "fx_basis": m.get("fx_basis", CFG.get("fx_basis")), "idr_per_krw": m.get("idr_per_krw", CFG.get("idr_per_krw")),
            "indices": indices, "index": index,
            "value": mk["value"] if mk else [], "gainers": mk["gainers"] if mk else [], "losers": mk["losers"] if mk else [],
            "turnover": mk["turnover"] if mk else [], "foreign_top": mk["foreign_top"] if mk else [], "foreign_bottom": mk["foreign_bottom"] if mk else [],
            "news": news_block(), "macro": macro_block(bi), "calendar": calendar, "announcements": announcements,
            "sources": {"idx_index": bool(ix), "idx_market": bool(mk), "bi": bi.get("src") if bi else None, "hist_days": mk["hist_days"] if mk else 0, "calendar": "saveticker" if any(e.get("src") == "saveticker" for e in calendar) else "investing.com" if any(e.get("src") == "investing.com" for e in calendar) else "manual"}}
    (ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"data.json | JCI {px} | 상승 {mk['adv'] if mk else '-'} | 외인 {(mk['foreign_net_idr']/1e9) if mk else 0:,.0f}억 | 뉴스 {len(data['news'])} | 일정 {len(calendar)} | 공시 {len(announcements)} | BI {'OK' if bi else 'X'}")

if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop"); sec = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 300
        while True:
            try: build()
            except Exception as e: log("build error", repr(e))
            time.sleep(sec)
    else:
        build()
