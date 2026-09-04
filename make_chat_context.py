#!/usr/bin/env python3
"""data.json -> data/cache/chat_context.json
챗봇(/api/chat)과 외부 크롤러(/api/feed)가 읽는 경량 컨텍스트 파일을 만든다.
data.json 은 1.4MB 라 프롬프트에 통째로 넣을 수 없으므로 답변에 필요한 필드만 추린다.
fetch_data.py 는 건드리지 않는다 (실행 순서: fetch_data.py -> make_chat_context.py).
사용: python make_chat_context.py
"""
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "data.json"
OUT = ROOT / "data" / "cache" / "chat_context.json"

NEWS_N, MKT_N, ANN_N, CAL_DAYS = 45, 20, 30, 8
RANKS = ["value", "gainers", "losers", "turnover", "foreign_top", "foreign_bottom"]
RANK_FIELDS = ["t", "n", "px", "pct", "val", "fnet"]
STOCK_FIELDS = ["n", "px", "pct", "val", "fnet", "mcap"]


def num(v, nd=2):
    """숫자만 반올림해서 돌려준다. 값이 없으면 None (0 으로 채우지 않는다)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return round(v, nd) if isinstance(v, float) else v
    return None


def pick(row, fields):
    return [num(row.get(f)) if f != "t" and f != "n" else row.get(f) for f in fields]


def build(d):
    idx = d.get("index") or {}
    today = (d.get("updated") or "")[:10]
    try:
        d0 = datetime.strptime(today, "%Y-%m-%d").date()
    except Exception:
        d0 = datetime.now(timezone(timedelta(hours=7))).date()
    dmax = d0 + timedelta(days=CAL_DAYS)

    out = {
        "updated": d.get("updated"),
        "delay_min": d.get("delay_min"),
        "mode": d.get("mode"),
        "idr_per_krw": d.get("idr_per_krw"),
        "fx_basis": d.get("fx_basis"),
        "freshness": {k: (d.get("sources") or {}).get(k) for k in ("idx_from_pc", "pc_age_min", "on_runner")},
        "note": "IDX Live 요약 컨텍스트. 여기에 없는 값은 '확인 불가'로 답할 것. 금액 단위는 data.json 원본과 동일(IDR).",
        "index": {k: num(idx.get(k)) if k not in ("session", "rank_src", "rank_asof", "rank_date", "foreign_date", "foreign_note") else idx.get(k)
                  for k in ("session", "high", "low", "prev", "ytd", "m1", "value_idr", "volume", "adv", "dec", "unch",
                            "foreign_net_idr", "foreign_buy", "foreign_sell", "foreign_date", "foreign_note",
                            "rank_asof", "rank_date")},
        "indices": [{"code": r.get("code"), "label": r.get("label"), "px": num(r.get("px")),
                     "pct": num(r.get("pct")), "prev": num(r.get("prev")), "asof": r.get("asof")}
                    for r in (d.get("indices") or [])],
        "global": [{"name": r.get("name"), "px": num(r.get("px")), "pct": num(r.get("pct"))}
                   for r in (d.get("global") or [])],
        "macro": [{"k": r.get("k_ko") or r.get("k"), "v": r.get("v"), "d": r.get("d"),
                   "ytd": r.get("ytd"), "note": r.get("note_ko") or r.get("note")}
                  for r in (d.get("macro") or [])],
        "sectors": [{"name": r.get("name"), "pct": num(r.get("pct")), "adv": r.get("adv"),
                     "dec": r.get("dec"), "val": num(r.get("val"))} for r in (d.get("sectors") or [])],
        # Catalyst — 오늘 재료가 있는 종목 상위 10 (점수 산식은 fetch_data.py catalyst_block 참고)
        "catalyst": [{"t": r.get("t"), "n": r.get("n"), "pct": num(r.get("pct")), "val": num(r.get("val")),
                      "event": r.get("ev_ko"), "headline": r.get("hl"), "score": r.get("score"),
                      "s_news": r.get("s_news"), "s_size": r.get("s_size"), "s_surge": r.get("s_surge")}
                     for r in (d.get("catalyst") or [])],
        "rank_fields": RANK_FIELDS,
        "rank": {k: [pick(r, RANK_FIELDS) for r in (d.get(k) or [])] for k in RANKS},
        "stock_fields": STOCK_FIELDS,
        "stocks": {},
        "news": [{"time": r.get("time"), "date": r.get("date"), "src": r.get("src"),
                  "t": r.get("t_ko") or r.get("t"), "t_id": r.get("t_id") or r.get("t"),
                  "tags": r.get("tags"), "url": r.get("url")} for r in (d.get("news") or [])[:NEWS_N]],
        "market_news": [{"time": r.get("time"), "date": r.get("date"), "src": r.get("src"),
                         "t": r.get("t_ko") or r.get("t"), "t_id": r.get("t_id") or r.get("t"),
                         "url": r.get("url")} for r in (d.get("market_news") or [])[:MKT_N]],
        "announcements": [{"date": r.get("date"), "time": r.get("time"), "t": r.get("t"),
                           "type": r.get("type"), "title": r.get("title_ko") or r.get("title"),
                           "ai": r.get("ai_ko") or "", "url": r.get("url")}
                          for r in (d.get("announcements") or [])[:ANN_N]],
        # AI 요약 — 지수는 한 줄, 종목은 '왜 움직였나'. fetch_data.py 가 만든 값 그대로 (없으면 비움)
        "ai_index": ((d.get("ai") or {}).get("index") or {}).get("ko") or "",
        "ai_stocks": {k: (v or {}).get("ko", "") for k, v in (((d.get("ai") or {}).get("stocks")) or {}).items() if (v or {}).get("ko")},
        "calendar": [],
    }

    for r in (d.get("stocks") or []):
        t = r.get("t")
        if t:
            out["stocks"][t] = pick(r, STOCK_FIELDS)

    for r in (d.get("calendar") or []):
        ds = r.get("date") or ""
        try:
            dd = datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            continue
        if d0 <= dd <= dmax:
            out["calendar"].append({"date": ds, "time": r.get("time"), "country": r.get("country"),
                                    "kind": r.get("kind"), "imp": r.get("imp"),
                                    "title": r.get("title_ko") or r.get("title"),
                                    "exp": r.get("exp"), "prev": r.get("prev"), "act": r.get("act")})
    return out


def main():
    if not SRC.exists():
        sys.exit(f"data.json 없음: {SRC}")
    d = json.loads(SRC.read_text(encoding="utf-8"))
    out = build(d)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(txt, encoding="utf-8")
    nann = sum(1 for a in out["announcements"] if a.get("ai"))
    print(f"chat_context.json 생성: {len(txt)/1024:.0f}KB · 종목 {len(out['stocks'])} · 뉴스 {len(out['news'])} · 일정 {len(out['calendar'])} · 공시 {len(out['announcements'])}(AI요약 {nann}) · 종목AI {len(out['ai_stocks'])} · 지수AI {'있음' if out['ai_index'] else '없음'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
