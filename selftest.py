#!/usr/bin/env python3
"""오프라인 자체 점검 — 네트워크 없이 코드가 온전한지 확인한다.
    python selftest.py
수정 후 반드시 한 번 돌려서 [OK] 만 나오는지 확인할 것."""
import sys, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
fail = []
def chk(name, cond, detail=""):
    print(("[OK]   " if cond else "[FAIL] ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond: fail.append(name)

import fetch_data as F

IDX_METHODS = ["session", "get", "browser_get", "stock_summary", "index_summary",
               "index_chart", "calendar", "dividends", "announcements"]
miss = [m for m in IDX_METHODS if not hasattr(F.idx, m)]
chk("IDX 클래스 메서드 9개", not miss, "누락: " + str(miss))

for name in ("TR_ENGINES", "TR_CACHE", "TR_GOOD", "MYMEMORY", "TR_URL", "TR_ORIGIN", "MACRO_ID", "INVESTING_CAL"):
    chk("전역 정의 " + name, hasattr(F, name))
FUNCS = ["idx_market", "idx_index", "idx_corp_calendar", "saveticker_calendar", "investing_calendar",
         "idx_announcements_today", "bi_indicators", "kontan_sun10y", "news_block", "translate_news",
         "macro_block", "macro_calendar", "manual", "build", "idx_browser_close", "_idx_page",
         "_pw_session", "http_text", "browser_text", "_wait_cf", "all_tickers", "build_alias", "screen",
         "_tr_google", "_tr_mymemory", "_mm_parse", "_tr_parse", "_tr_browser_batch", "_tr_path", "translate_field",
         "investing_calendar", "investing_quote", "_inv_parse", "macro_calendar_auto", "_id_label", "_pw_browser", "_pw_call"]
miss = [f for f in FUNCS if not hasattr(F, f)]
chk("모듈 함수 %d개" % len(FUNCS), not miss, "누락: " + str(miss))

chk("config.translate 설정", isinstance(F.CFG.get("translate"), dict))
chk("화이트리스트 매체", len(F.CFG.get("whitelist", [])) >= 20, len(F.CFG.get("whitelist", [])))

# 외국인 수급 단위: ForeignBuy/Sell 은 주식 수 → 체결단가를 곱해야 IDR
rows = [{"StockCode": "TEST", "StockName": "Test Tbk.", "Previous": 1000.0, "Close": 1100.0, "Change": 100.0,
         "Volume": 1000000.0, "Value": 1050000000.0, "ForeignBuy": 600000.0, "ForeignSell": 100000.0,
         "NonRegularValue": 0.0}]
_ss, _now = F.idx.stock_summary, F.now_wib
F.idx.stock_summary = lambda d, force=False: rows if d == dt.date(2026, 9, 1) else []
F.now_wib = lambda: dt.datetime(2026, 9, 1, 17, 0, tzinfo=F.WIB)
try:
    mk = F.idx_market()
    vwap = 1050000000.0 / 1000000.0          # 1,050
    chk("외인 순매수 IDR 환산", abs(mk["foreign_net_idr"] - 500000 * vwap) < 1, mk["foreign_net_idr"])
    chk("외인 매수 IDR 환산", abs(mk["foreign_buy"] - 600000 * vwap) < 1, mk["foreign_buy"])
    chk("집계 근거 문구 미노출", mk["foreign_note"] is None, mk["foreign_note"])
    chk("랭킹 4종 생성", all(k in mk for k in ("value", "gainers", "losers", "turnover")))
finally:
    F.idx.stock_summary, F.now_wib = _ss, _now

_eng = F.TR_ENGINES.get("google")
F.TR_ENGINES["google"] = lambda t, sl="auto", tl="ko": "[KO]" + t
try:
    out = F.translate_news([{"t": "IHSG dibuka naik"}, {"t": "이미 한국어"}])
    chk("인니어 → 번역 부여", out[0].get("t_ko", "").startswith("[KO]"))
    chk("한국어 → 원문 유지", out[1].get("t_ko") == "이미 한국어")
finally:
    F.TR_ENGINES["google"] = _eng

print()
print("=== 실패 %d건 ===" % len(fail) if fail else "=== 전부 통과 ===")
sys.exit(1 if fail else 0)
