#!/usr/bin/env python3
"""IDX Live 장기기억 (memory) — 사람의 단기·장기 기억처럼 한 주의 재료를 '기억 카드'로 응고해 영구 보관한다.

  단기: data.json (5분) · 당일 뉴스/공시/AI 요약                → 매 빌드 daily(data) 가 data/memory/daily/YYYY-MM-DD.json 에 그날의 재료를 누적(원본 url 기준 중복 제거)
  응고: 매주 금요일 17:30 WIB 이후 첫 빌드에서 weekly()          → 그 주 일별 로그를 Claude Code(Max) 가 읽어 카드 JSON 생성 → data/memory/cards.json 에 병합
  장기: cards.json (영구) — weeks(주간 시장 카드) · timeline(종목별 이벤트 카드) · threads(미결 스레드: 후속 확인 일정·상태)
  활용: chat_slice() → data/memory/memory_chat.json (챗봇·사이트용 압축본, publish 가 GitHub 에 올림)

fetch_data.build() 에서: memory_build.daily(data) → memory_build.weekly(claude_fn, log) → data["memory"] = memory_build.chat_slice()
PC 가 금요일 저녁에 꺼져 있으면 다음 켜진 빌드(월요일 아침 등)에서 지난주 분을 만든다. 일별 로그는 120일 보관."""
import json, re, datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent
MEM = ROOT / "data" / "memory"; DAILY = MEM / "daily"
CARDS_P = MEM / "cards.json"; CHAT_P = MEM / "memory_chat.json"; STATE_P = MEM / "state.json"
WIB = dt.timezone(dt.timedelta(hours=7))
CONSOLIDATE_AFTER = (4, 17, 30)          # (weekday=금, 17:30 WIB) 이후 응고
DAILY_KEEP_DAYS = 120
CARD_PER_TICKER = 10                     # 종목별 타임라인 카드 보관 수 (오래된 것부터 정리)
CHAT_PER_TICKER, CHAT_WEEKS, CHAT_THREADS = 6, 4, 20


def _now(): return dt.datetime.now(WIB)
def _load(p, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default
def _save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
def _week_id(d): y, w, _ = d.isocalendar(); return f"{y}-W{w:02d}"
def _short(s, n): s = re.sub(r"\s+", " ", str(s or "")).strip(); return s if len(s) <= n else s[:n - 1] + "…"


# ---------------- 1) 단기 → 일별 로그 ----------------
def daily(data):
    """오늘의 재료를 data/memory/daily/YYYY-MM-DD.json 에 누적. 재료 = AI 요약이 붙은 뉴스·공시(=중요한 것), Catalyst 상위, 지수·수급·매크로 스냅샷"""
    today = (data.get("updated") or "")[:10]
    if not today: return
    p = DAILY / f"{today}.json"
    log = _load(p, {"date": today, "news": {}, "anns": {}, "catalyst": {}, "dividends": {}})
    comp = next((x for x in (data.get("indices") or []) if x.get("code") == "COMPOSITE"), {})
    ix = data.get("index") or {}
    log["index"] = {"px": comp.get("px"), "pct": comp.get("pct"), "prev": comp.get("prev"), "ytd": ix.get("ytd"),
                    "value_idr": ix.get("value_idr"), "adv": ix.get("adv"), "dec": ix.get("dec"),
                    "foreign_net_idr": ix.get("foreign_net_idr"), "foreign_date": ix.get("foreign_date"),
                    "ai": ((data.get("ai") or {}).get("index") or {}).get("ko") or log.get("index", {}).get("ai", "")}
    log["macro"] = {r.get("k"): {"v": r.get("v"), "d": r.get("d"), "ytd": r.get("ytd")} for r in (data.get("macro") or []) if r.get("k")}
    log["sectors"] = [{"name": s.get("name"), "pct": s.get("pct")} for s in (data.get("sectors") or [])]
    for n in (data.get("news") or []) + (data.get("market_news") or []):
        u = n.get("url")
        if not u or not n.get("ai_ko"): continue
        log["news"][u] = {"time": n.get("time"), "src": n.get("src"), "tags": n.get("tags") or [],
                          "t": _short(n.get("t_ko") or n.get("t"), 120), "ai": _short(n.get("ai_ko"), 320)}
    for a in (data.get("announcements") or []):
        u = a.get("url")
        if not u or not a.get("ai_ko") or a.get("date") != today: continue
        log["anns"][u] = {"time": a.get("time"), "t": a.get("t"), "type": a.get("type"),
                          "title": _short(a.get("title_ko") or a.get("title"), 120), "ai": _short(a.get("ai_ko"), 400)}
    for c in (data.get("catalyst") or [])[:10]:
        if c.get("t"): log["catalyst"][c["t"]] = {"ev": c.get("ev_ko"), "hl": _short(c.get("hl"), 120), "score": c.get("score"), "pct": c.get("pct")}
    for t, dv in (data.get("dividends") or {}).items():
        if dv.get("ex") and today <= str(dv.get("ex")) <= (dt.date.fromisoformat(today) + dt.timedelta(days=14)).isoformat():
            log["dividends"][t] = {"dps": dv.get("dps"), "type": dv.get("type"), "ex": dv.get("ex"), "pay": dv.get("pay"), "yld": dv.get("yld")}
    log["updated"] = data.get("updated")
    _save(p, log)
    # 오래된 일별 로그 정리
    try:
        lim = (_now().date() - dt.timedelta(days=DAILY_KEEP_DAYS)).isoformat()
        for f in DAILY.glob("*.json"):
            if f.stem < lim: f.unlink()
    except Exception: pass


# ---------------- 2) 주간 응고 ----------------
def _pending_week():
    """응고해야 할 주(월~금)의 (week_id, [dates]) — 이미 했거나 아직 금요일 17:30 전이면 None"""
    n = _now()
    wd, mins = n.weekday(), n.hour * 60 + n.minute
    fri = n.date() - dt.timedelta(days=wd - 4) if wd >= 4 else n.date() - dt.timedelta(days=wd + 3)   # 이번 주 금요일(지났으면) 또는 지난주 금요일
    if wd == 4 and mins < CONSOLIDATE_AFTER[1] * 60 + CONSOLIDATE_AFTER[2]: fri -= dt.timedelta(days=7)
    wid = _week_id(fri)
    st = _load(STATE_P, {})
    if st.get("last_week") == wid: return None
    days = [(fri - dt.timedelta(days=i)).isoformat() for i in range(4, -1, -1)]
    return wid, days


def _week_material(days):
    """그 주 일별 로그 → Claude 에 줄 압축 텍스트 (중요도 순, 상한 있음)"""
    logs = [l for l in (_load(DAILY / f"{d}.json", None) for d in days) if l]
    if not logs: return None, 0
    out = []
    for l in logs:
        ix = l.get("index") or {}
        out.append(f"## {l['date']}  IHSG {ix.get('px')} ({ix.get('pct')}%) · 거래대금 {round((ix.get('value_idr') or 0)/1e12,1)}조 · 외국인 {round((ix.get('foreign_net_idr') or 0)/1e9)}억(기준 {ix.get('foreign_date')}) · 상승/하락 {ix.get('adv')}/{ix.get('dec')}")
        if ix.get("ai"): out.append(f"지수 요약: {ix['ai']}")
        mac = l.get("macro") or {}
        keys = [k for k in mac if any(x in k for x in ("USD/IDR", "SUN", "10Y", "BI Rate", "WTI", "석탄", "니켈", "CPO"))]
        if keys: out.append("매크로: " + " · ".join(f"{k} {mac[k].get('v')} ({mac[k].get('d')}%)" for k in keys[:8]))
        secs = sorted((s for s in (l.get("sectors") or []) if s.get("pct") is not None), key=lambda s: -abs(s["pct"]))[:4]
        if secs: out.append("업종: " + " · ".join(f"{s['name']} {s['pct']:+.2f}%" for s in secs))
        cat = l.get("catalyst") or {}
        if cat: out.append("Catalyst: " + " · ".join(f"{t}({v.get('ev')}, {v.get('pct')}%) {v.get('hl')}" for t, v in list(cat.items())[:6]))
        anns = list((l.get("anns") or {}).values())[:25]
        for a in anns: out.append(f"[공시 {a.get('t')}] {a.get('title')} — {a.get('ai')}")
        news = list((l.get("news") or {}).values())[:30]
        for nn in news: out.append(f"[뉴스 {','.join(nn.get('tags') or []) or '시장'} · {nn.get('src')}] {nn.get('t')} — {nn.get('ai')}")
        dv = l.get("dividends") or {}
        if dv: out.append("배당 일정: " + " · ".join(f"{t} DPS Rp{v.get('dps')} 배당락 {v.get('ex')}" for t, v in list(dv.items())[:10]))
        out.append("")
    txt = "\n".join(out)
    return txt[:60000], len(logs)


PROMPT = """너는 한국 증권사 인도네시아 리서치의 편집자다. 아래는 IDX Live 가 한 주 동안 모은 재료(지수·수급·매크로·업종·공시 요약·뉴스 요약·배당 일정)다.
이 주의 '기억 카드'를 만들어라. 목적: 몇 달 뒤에도 "그때 무슨 일이 있었고, 후속으로 무엇을 확인해야 하는지" 바로 꺼내 쓰는 것.

출력은 JSON 객체 하나만 (설명·코드블록 금지):
{
 "market": {"summary": "주간 시장 요약 3~4문장(증권사 리포트 문체, 명사형 종결). 지수 주간 흐름과 원인, 외국인 수급, 업종, 매크로(환율·금리·원자재)",
            "ihsg_wk_pct": 숫자 또는 null, "foreign_wk_idr": 숫자(억 IDR) 또는 null,
            "events": ["이 주의 사건 3~6개, 각 1문장, 날짜 포함"]},
 "events": [ {"t": "티커", "date": "YYYY-MM-DD", "type": "배당|유상증자|자사주|M&A·지분|실적|가이던스|경영진|규제·소송|거래정지|계약·사업|기타",
              "summary": "1~2문장, 핵심 수치 포함(금액·비율·일정)", "numbers": "핵심 숫자만 짧게(예: DPS Rp616.7 · 배당락 9/24)",
              "follow_up": "후속으로 확인할 것(없으면 빈 문자열)", "due": "YYYY-MM-DD 또는 빈 문자열", "src": "출처 매체 또는 '공시'"} ],
 "threads_update": [ {"id": "기존 스레드 id", "status": "open|done", "note": "이번 주 진전 1문장"} ],
 "threads_new": [ {"t": "티커", "type": "위 type 중 하나", "question": "확인해야 할 질문 1문장", "due": "YYYY-MM-DD 또는 빈 문자열"} ]
}
규칙: ① 재료에 없는 사실·숫자를 만들지 않는다 ② events 는 주가에 영향을 주는 재료만, 최대 25개, 같은 종목·같은 사안은 1장으로 합친다 ③ 회사명은 로마자 원문, 금액은 Rp5,000억·Rp1.27조 식 ④ 기존 미결 스레드는 이번 주 재료로 해소됐으면 done, 진전만 있으면 open+note, 아무 재료 없으면 생략 ⑤ threads_new 는 후속 확인이 실제로 필요한 것만(최대 10개), 이미 열린 스레드와 중복 금지.
"""


def weekly(claude_fn, log=print):
    """금요일 17:30 이후 첫 빌드에서 지난 주를 응고. claude_fn(prompt) → 텍스트. 반환: 만든 week_id 또는 None"""
    pend = _pending_week()
    if not pend: return None
    wid, days = pend
    material, ndays = _week_material(days)
    st = _load(STATE_P, {})
    if not material or ndays == 0:
        st["last_week"] = wid; st["note"] = "재료 없음"; _save(STATE_P, st); return None   # 로그가 없는 주(도입 전·휴장)는 건너뛴다
    cards = _load(CARDS_P, {"weeks": {}, "timeline": {}, "threads": []})
    open_threads = [t for t in cards.get("threads", []) if t.get("status") != "done"]
    ctx = "\n".join(f"- id={t['id']} {t['t']} [{t.get('type')}] {t.get('question')} (due {t.get('due') or '-'}; 마지막 {t.get('updates', [{}])[-1].get('note', '') if t.get('updates') else ''})" for t in open_threads[:40]) or "(없음)"
    prompt = PROMPT + f"\n[주차] {wid} ({days[0]} ~ {days[-1]})\n\n[기존 미결 스레드]\n{ctx}\n\n[이 주의 재료]\n{material}"
    txt = claude_fn(prompt)
    if not txt: log("주간 기억 응고 실패: Claude 응답 없음"); return None
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    try: j = json.loads(m.group(0) if m else txt)
    except Exception as e: log("주간 기억 응고 실패: JSON 파싱", str(e)[:80]); return None
    if not isinstance(j, dict) or not j.get("market"): log("주간 기억 응고 실패: 형식"); return None
    stamp = _now().strftime("%Y-%m-%d %H:%M")
    mk = j.get("market") or {}
    cards["weeks"][wid] = {"from": days[0], "to": days[-1], "summary": mk.get("summary", ""), "ihsg_wk_pct": mk.get("ihsg_wk_pct"),
                           "foreign_wk_idr": mk.get("foreign_wk_idr"), "events": [str(x) for x in (mk.get("events") or [])][:8], "made": stamp}
    for e in (j.get("events") or [])[:25]:
        t = str(e.get("t") or "").upper().strip()
        if not re.fullmatch(r"[A-Z]{4}", t): continue
        card = {"week": wid, "date": str(e.get("date") or days[-1])[:10], "type": _short(e.get("type"), 20), "summary": _short(e.get("summary"), 300),
                "numbers": _short(e.get("numbers"), 120), "follow_up": _short(e.get("follow_up"), 160), "due": str(e.get("due") or "")[:10], "src": _short(e.get("src"), 30)}
        tl = cards["timeline"].setdefault(t, [])
        if not any(c.get("week") == wid and c.get("type") == card["type"] for c in tl): tl.append(card)
        cards["timeline"][t] = sorted(tl, key=lambda c: c.get("date", ""))[-CARD_PER_TICKER:]
    byid = {t["id"]: t for t in cards.get("threads", [])}
    for u in (j.get("threads_update") or []):
        t = byid.get(str(u.get("id")))
        if not t: continue
        t.setdefault("updates", []).append({"week": wid, "note": _short(u.get("note"), 200)})
        if str(u.get("status")) == "done": t["status"] = "done"; t["closed"] = wid
    nid = max([int(re.sub(r"\D", "", t["id"]) or 0) for t in cards.get("threads", [])] + [0])
    for nw in (j.get("threads_new") or [])[:10]:
        t = str(nw.get("t") or "").upper().strip()
        if not re.fullmatch(r"[A-Z]{4}", t): continue
        nid += 1
        cards.setdefault("threads", []).append({"id": f"th{nid}", "t": t, "type": _short(nw.get("type"), 20), "question": _short(nw.get("question"), 200),
                                                "due": str(nw.get("due") or "")[:10], "opened": wid, "status": "open", "updates": []})
    # 오래된 done 스레드 정리(12주), open 스레드 상한
    keep = []
    for t in cards.get("threads", []):
        if t.get("status") == "done" and t.get("closed") and t["closed"] < _week_id(_now().date() - dt.timedelta(weeks=12)): continue
        keep.append(t)
    cards["threads"] = keep[-200:]
    _save(CARDS_P, cards)
    st["last_week"] = wid; st["made"] = stamp; _save(STATE_P, st)
    log(f"주간 기억 응고 {wid}: 종목 카드 {sum(1 for e in (j.get('events') or []))}장 · 스레드 갱신 {len(j.get('threads_update') or [])} · 신규 {len(j.get('threads_new') or [])}")
    return wid


# ---------------- 3) 장기 → 챗봇·사이트용 압축본 ----------------
def chat_slice():
    """cards.json → 작은 구조 (챗봇 컨텍스트·data.json 에 실림). 종목별 최근 카드 6장, 주간 카드 4주, 미결 스레드 20개"""
    cards = _load(CARDS_P, None)
    if not cards:
        return _load(CHAT_P, None)   # PC 에 카드가 없으면(러너 등) 업로드된 압축본을 그대로 쓴다
    weeks = dict(sorted(cards.get("weeks", {}).items())[-CHAT_WEEKS:])
    tl = {t: v[-CHAT_PER_TICKER:] for t, v in cards.get("timeline", {}).items() if v}
    th = [t for t in cards.get("threads", []) if t.get("status") != "done"]
    th = sorted(th, key=lambda t: (t.get("due") or "9999", t.get("opened") or ""))[:CHAT_THREADS]
    out = {"made": _load(STATE_P, {}).get("made"), "weeks": weeks, "stocks": tl,
           "threads": [{"id": t["id"], "t": t["t"], "type": t.get("type"), "q": t.get("question"), "due": t.get("due"), "opened": t.get("opened"),
                        "last": (t.get("updates") or [{}])[-1].get("note", "") if t.get("updates") else ""} for t in th]}
    _save(CHAT_P, out)
    return out


if __name__ == "__main__":
    import sys
    if "--weekly-now" in sys.argv:      # 점검용: 이번 주 재료로 즉시 응고 (금요일 17:30 규칙 무시)
        st = _load(STATE_P, {}); st.pop("last_week", None); _save(STATE_P, st)
        import subprocess
        def cf(prompt):
            r = subprocess.run(["claude", "-p", "--output-format", "text"], input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            return r.stdout
        print(weekly(cf))
    print(json.dumps(chat_slice(), ensure_ascii=False)[:800])
