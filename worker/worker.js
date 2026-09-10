// Cloudflare Worker — IDX Live 공개 사이트 + Gemini 챗봇 API.
// 화면(index.html)·데이터(data.json)를 GitHub 저장소(main)에서 그때그때 읽어 서빙한다.
//   → Worker 는 한 번만 배포하면 되고, 이후 GitHub 에 올라간 파일이 그대로 사이트에 반영된다 (PC 꺼져 있어도).
//   → 보는 사람의 브라우저는 workers.dev 만 접속하므로 회사망에서 github.io 가 막혀 있어도 열린다.
// 배포: dash.cloudflare.com → Workers & Pages → (기존 Worker) → Edit code → 전체를 이 파일로 교체 → Deploy
//
// 추가 라우트
//   GET  /api/feed   chat_context.json 그대로 (외부 크롤러·데일리시황 참고용, 인증 없음)
//   POST /api/chat   Gemini 챗봇. 사내용 — CHAT_TOKEN 없으면 전부 거부한다.
//
// 필요한 설정 (Cloudflare 대시보드 → Worker → Settings → Variables and Secrets)
//   GEMINI_API_KEY  (Secret, 필수)  Gemini API 키. 절대 저장소·index.html 에 넣지 말 것 — 저장소는 공개다.
//   CHAT_TOKEN      (Secret, 필수)  사내 접속 암구호. 미설정이면 /api/chat 은 503 으로 닫힌다.
//   GEMINI_MODEL    (Variable, 선택) 기본 gemini-3.8-flash. 모델 교체 시 코드 수정 없이 여기만 바꾼다.
//   GEMINI_SEARCH   (Variable, 선택) off 로 두면 구글 검색 그라운딩을 끈다. 기본 켜짐 — 화이트리스트 매체 site: 검색만 허용.
//   GEMINI_THINKING (Variable, 선택) low / medium / high / off. 비워두면 gemini-3.8 계열에만 low 를 넣는다.
//   QUOTA_MSG       (Variable, 선택) 할당량 초과(429) 때 화면에 뜨는 문구. 비워두면 기본 문구.
//   GEMINI_BASE     (Variable, 선택) Gemini 호출 기지 주소. 기본은 구글 직통.
//                   구글이 Cloudflare 데이터센터 IP 를 위치 미지원으로 막을 때(400 "not available in your
//                   current location") Cloudflare AI Gateway 주소를 넣으면 우회된다:
//                   https://gateway.ai.cloudflare.com/v1/<account_id>/<gateway_name>/google-ai-studio

const REPO = "jelly927/idx-monitoring", BRANCH = "main";
const RAW = `https://raw.githubusercontent.com/${REPO}/${BRANCH}`;
const CTX_PATH = "/data/cache/chat_context.json";
const GEMINI_HOST = "https://generativelanguage.googleapis.com";
const GEMINI_PATH = "/v1beta/interactions";
const MODEL_DEFAULT = "gemini-3.8-flash";
const TYPES = { html: "text/html; charset=utf-8", json: "application/json; charset=utf-8", js: "text/javascript; charset=utf-8", css: "text/css; charset=utf-8", png: "image/png", svg: "image/svg+xml", ico: "image/x-icon", pdf: "application/pdf" };
const ALLOW = ["query1.finance.yahoo.com", "query2.finance.yahoo.com", "www.idx.co.id"];   // (선택) ?url= 프록시 — GitHub 러너가 IDX 에 막힐 때 사용

// 챗봇 입력 상한 — 비용·남용 방어
const MAX_Q = 1000, MAX_TURNS = 8, MAX_HIST_CHARS = 4000, MAX_STOCKS = 40, TOP_STOCKS = 30;
// gemini-3.8-flash 는 사고형 모델이라 thought 토큰이 출력 예산을 함께 쓴다 — 넉넉히 잡아야 빈 응답이 안 난다
const MAX_OUT = 8192;


// 검색 그라운딩 허용 매체 — config.json whitelist 31곳 + IDX 공식. 검색어에 site: 로 강제하고, 인용도 이 도메인만 남긴다
const WL_DOMAINS = ["reuters.com","bloomberg.com","bloombergtechnoz.com","thejakartapost.com","kompas.com","tempo.co","antaranews.com","cnnindonesia.com","detik.com","liputan6.com","tvonenews.com","kompas.tv","tvrinews.com","jawapos.com","bisnis.com","kontan.co.id","cnbcindonesia.com","investor.id","idnfinancials.com","emitennews.com","idxchannel.com","infobanknews.com","katadata.co.id","mediaindonesia.com","rm.id","tribunnews.com","kumparan.com","beritasatu.com","medcom.id","republika.co.id","swa.co.id","idx.co.id"];
const WL_SITE = WL_DOMAINS.slice(0, 12).map(d => "site:" + d).join(" OR ");   // 검색어에 붙일 site: 절 (너무 길면 검색이 실패하므로 주요 12곳)
const wlOk = (url) => { try { const h = new URL(url).hostname.replace(/^www\./, ""); return WL_DOMAINS.some(d => h === d || h.endsWith("." + d)); } catch { return false; } };

const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "Content-Type,x-chat-token" };
const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { "Content-Type": TYPES.json, ...CORS } });

export default {
  async fetch(req, env) {
    const u = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    const target = u.searchParams.get("url");
    if (target) return proxy(target);

    if (u.pathname === "/api/feed") return feed();
    if (u.pathname === "/api/diag") return diag(req, env);
    if (u.pathname === "/api/chat") {
      if (req.method !== "POST") return json({ error: "POST 만 허용" }, 405);
      try { return await chat(req, env); }
      catch (e) { return json({ error: "chat 처리 실패: " + (e && e.message || e) }, 500); }
    }

    let p = u.pathname === "/" ? "/index.html" : u.pathname;
    if (p.includes("..")) return new Response("bad path", { status: 400 });
    const ext = (p.split(".").pop() || "").toLowerCase();
    const ttl = ext === "html" ? 60 : 30;   // GitHub raw 캐시(약 5분)와 별도로 Cloudflare 엣지 캐시
    const r = await fetch(RAW + p, { cf: { cacheTtl: ttl, cacheEverything: true } });
    if (!r.ok) return new Response("not found: " + p, { status: 404 });
    const h = new Headers();
    h.set("Content-Type", TYPES[ext] || "application/octet-stream");
    h.set("Cache-Control", `public, max-age=${ttl}`);
    h.set("Access-Control-Allow-Origin", "*");
    return new Response(r.body, { status: 200, headers: h });
  }
};

async function proxy(target) {
  let t; try { t = new URL(target); } catch { return new Response("bad url", { status: 400 }); }
  if (!ALLOW.includes(t.hostname)) return new Response("host not allowed", { status: 403 });
  const r = await fetch(t.toString(), { headers: { "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/133 Safari/537.36", "Accept": "application/json,text/plain,*/*", "Referer": "https://www.idx.co.id/" }, cf: { cacheTtl: 30 } });
  const h = new Headers(r.headers);
  h.set("Access-Control-Allow-Origin", "*"); h.set("Cache-Control", "public, max-age=30"); h.delete("content-security-policy");
  return new Response(r.body, { status: r.status, headers: h });
}

async function ctxRaw() {
  const r = await fetch(RAW + CTX_PATH, { cf: { cacheTtl: 60, cacheEverything: true } });
  if (!r.ok) return null;
  return await r.json();
}

async function feed() {
  const c = await ctxRaw();
  if (!c) return json({ error: "chat_context.json 없음 — make_chat_context.py 가 아직 돌지 않았습니다" }, 503);
  return json(c);
}

// ── 질문에 걸리는 종목만 골라 컨텍스트를 줄인다 (전체 835종목 = 67KB, 매 요청 넣으면 낭비) ──
function sliceCtx(c, text) {
  const { stocks, ai_stocks, ...base } = c;
  const src = text || "", low = src.toLowerCase();
  const hit = {};
  let n = 0;
  // 티커는 대소문자 무관 (hatm → HATM). 실제 종목 목록에 있는 4글자만 채택하므로 news/high 같은 일반 단어는 걸리지 않는다
  // 단, 소문자로 적힌 일반 단어(bank·emas·beli·good…)가 우연히 티커와 같은 경우는 제외 — 대문자로 적으면 항상 티커로 본다
  const COMMON = new Set(["bank","emas","beli","jual","naik","same","good","best","fast","cash","mark","news","high","open","week","year","true","data","kali","juta","bisa","akan","dari","ini","yang","untuk","hari","ada","apa","saja","tadi","pagi","sore"]);
  for (const m of src.matchAll(/\b[A-Za-z]{4}\b/g)) {
    const raw = m[0], t = raw.toUpperCase();
    if (raw !== t && COMMON.has(raw.toLowerCase())) continue;
    if (stocks[t] && !hit[t] && n < MAX_STOCKS) { hit[t] = stocks[t]; n++; }
  }
  if (n < MAX_STOCKS) {
    for (const t in stocks) {
      if (hit[t]) continue;
      const name = (stocks[t][0] || "").toLowerCase();
      if (name.length > 3 && low.includes(name)) { hit[t] = stocks[t]; if (++n >= MAX_STOCKS) break; }
    }
  }
  base.stocks_matched = hit;
  const ais = c.ai_stocks || {};
  const aiHit = {};
  for (const t in hit) if (ais[t]) aiHit[t] = ais[t];
  if (n === 0) { let k = 0; for (const t in ais) { if (k++ >= 10) break; aiHit[t] = ais[t]; } }
  base.ai_stocks = aiHit;
  if (n === 0) {
    // 종목 특정이 안 되면 거래대금 상위만 넣는다
    const top = Object.entries(stocks).sort((a, b) => (b[1][3] || 0) - (a[1][3] || 0)).slice(0, TOP_STOCKS);
    base.stocks_top = Object.fromEntries(top);
  }
  base.stocks_note = `전체 ${Object.keys(stocks).length}개 종목 중 질문에 관련된 것만 실었다. 여기 없는 종목은 '확인 불가'로 답할 것.`;
  // 장기기억: 종목 카드는 질문에 걸린 종목만, 주간 카드·미결 스레드는 항상 (작다)
  const mem = c.memory || {};
  const memStocks = {};
  for (const t in hit) if (mem.stocks && mem.stocks[t]) memStocks[t] = mem.stocks[t];
  base.memory = { made: mem.made || null, weeks: mem.weeks || {}, stocks: memStocks,
                  threads: (mem.threads || []).filter(th => n === 0 || hit[th.t]).slice(0, 20) };
  return base;
}

function sysPrompt(ctx, lang) {
  const rules = [
    "너는 KISI Research 의 인도네시아 증시 데이터 어시스턴트다. 사용자는 증권사 리서치 실무자다.",
    "",
    "[절대 규칙]",
    "1. 주가·등락률·거래대금·수급 같은 숫자는 아래 <DATA> 에 있는 값만 사용한다. DATA 에 없는 숫자는 절대 만들어내지 않는다.",
    "2. 숫자가 DATA 에 없으면 '확인 불가'라고 명시한다. 추정치로 채우지 않는다.",
    "2-1. [기사 검색] 왜 움직였는지·배경·회사 사정처럼 DATA 만으로 답이 안 되는 질문은 google_search 도구로 기사를 찾아 근거로 쓴다. 검색어에는 반드시 다음 site: 절을 붙인다: (" + WL_SITE + ") — 이 화이트리스트 매체 밖의 기사는 근거로 쓰지 않는다. 기사에서 가져온 내용은 '○○(매체명, 날짜) 보도에 따르면' 처럼 출처를 문장 안에 밝히고, 기사 내용과 DATA 숫자를 섞지 않는다. 시세 질문만이면 검색하지 않는다.",
    "3. '~인 것 같다', '~로 보인다' 같은 추정형 표현을 쓰지 않는다. '~로 확인됨', '~ 영향으로 판단됨' 처럼 근거 기반으로 쓴다.",
    "4. 숫자는 단독으로 두지 않고 비교를 붙인다 (전일 대비, YTD, 시장 대비, 섹터 대비).",
    "5. 설명은 '데이터 → 원인 → 결과' 순서로 연결한다. 원인이 DATA 로 뒷받침되지 않으면 원인을 쓰지 않는다.",
    "6. 답변은 3~6줄. 표가 더 명확하면 짧은 표를 쓴다. 서론·배경 설명은 넣지 않는다.",
    "7. IDR 금액을 말할 때는 idr_per_krw 로 환산한 원화를 괄호로 짧게 병기한다(예: Rp6,621억(약 505억원)). 환산 기준·환율 출처·'11:31 Yahoo' 같은 기준 표기 문구는 답변에 쓰지 않는다 (사용자가 이미 안다).",
    "8. 지수·주가는 delay_min 분 지연 데이터다. 실시간이라고 말하지 않는다. 기준 표기는 필요할 때 날짜만(예: 9/10 기준) 쓰고, 시각(11:35 같은 시:분)은 절대 쓰지 않는다. 답변 끝에 기준·출처·주석 줄을 따로 붙이지 않는다.",
    "9. 투자 권유(매수/매도 추천)는 하지 않는다. 데이터 해석까지만 한다.",
    "",
    "[데이터 읽는 법]",
    "- indices: 지수 카드. code=COMPOSITE 가 IHSG(자카르타종합지수), px=현재, prev=전일종가, pct=전일대비%.",
    "- index: 장중 고저·YTD·거래대금(value_idr)·상승(adv)/하락(dec)/보합(unch)·외국인 순매수(foreign_net_idr, 기준일 foreign_date).",
    "- rank: 배열 순서는 rank_fields 와 같다. value=거래대금 상위, gainers=상승률, losers=하락률, turnover=거래대금 급증, foreign_top/bottom=외국인 순매수 상하위.",
    "- stocks_matched / stocks_top: 배열 순서는 stock_fields 와 같다.",
    "- news/market_news: t 는 한국어, t_id 는 인도네시아어 원문. tags 는 관련 티커. ai 는 기사 본문 2문장 요약(있을 때만) — 종목 사정을 물으면 먼저 이 요약을 근거로 쓰고, 부족할 때 검색한다.",
    "- calendar: imp 는 중요도(별 개수). act=실제, exp=예상, prev=이전.",
    "- announcements: IDX 공시.",
    "- macro: v 는 이미 포맷된 문자열이다. 그대로 인용한다.",
    "- ai_index 는 오늘 지수가 왜 움직였는지 한 줄 요약, ai_stocks 는 종목별 등락 사유, announcements[].ai 는 공시 요약이다. 이미 만들어진 요약이니 근거로 인용하되 숫자는 원본 필드로 검증한다.",
    "- freshness.pc_age_min 이 180 이상이면 수집 PC 가 꺼져 있어 공시·종목 요약이 오래된 값일 수 있다. 그럴 때는 답변 끝에 데이터 기준 시각을 밝힌다.",
    "- memory 는 장기기억이다. memory.weeks 는 주간 시장 카드(주차별 요약·사건), memory.stocks[티커] 는 그 종목의 과거 이벤트 카드(date·type·summary·numbers·follow_up·due), memory.threads 는 아직 확인이 안 끝난 후속 사항(q=질문, due=확인 시점, last=최근 진전)이다. 종목·시장을 물으면 현재 DATA 와 함께 '지난 X주 카드에 따르면 …였고, 이번 주 후속은 …' 식으로 과거 맥락과 후속 상태를 이어서 말한다. 카드의 날짜를 밝히고, 카드에 없는 후속 결과를 지어내지 않는다. memory 가 비어 있으면 언급하지 않는다.",
    "- catalyst 는 오늘 재료(뉴스·공시·배당락)가 있는 종목 상위 10이다. score = s_news×0.4 + s_size×0.3 + s_surge×0.3 로 계산된 값이며, event 는 재료 유형, headline 은 근거 기사다. 순위 근거를 물으면 이 세 점수를 그대로 제시한다.",
    "",
    lang === "id"
      ? "[출력 언어] 반드시 인도네시아어(Bahasa Indonesia)로만 답한다."
      : "[출력 언어] 반드시 한국어로만 답한다. 번역투를 쓰지 않고 국내 증권사 리서치 문체로 쓴다.",
    "",
    "[뉴스 한국어 제목 주의] news.t 는 기계번역이라 티커가 단어로 오역된 경우가 있다 (예: CUAN → '이익'). 제목이 어색하면 t_id 원문을 근거로 삼는다.",
    "",
    "<DATA>",
    JSON.stringify(ctx),
    "</DATA>",
  ];
  return rules.join("\n");
}

// thinking_level 은 사고형 모델에만 넣는다. GEMINI_THINKING 이 "off" 면 어떤 모델에도 넣지 않는다.
function genCfg(env, model) {
  const g = { max_output_tokens: MAX_OUT };
  const want = (env && env.GEMINI_THINKING) || "";
  if (want === "off") return g;
  if (want) { g.thinking_level = want; return g; }
  if (/^gemini-3\.8/.test(model)) g.thinking_level = "low";
  return g;
}

function outputText(j) {
  const steps = (j && j.steps) || [];
  let out = "";
  for (const s of steps) {
    if (s.type !== "model_output") continue;
    for (const c of (s.content || [])) if (c.type === "text" && c.text) out += c.text;
  }
  return out.trim();
}

// 검색 그라운딩 인용 — 화이트리스트 도메인만 남긴다 (url_citation annotations + 검색어)
function citations(j) {
  const steps = (j && j.steps) || [];
  const urls = new Map(); const queries = []; let searched = false;
  for (const s of steps) {
    for (const c of (s.content || [])) {
      if (c.type === "google_search_call") { searched = true; for (const q of (c.queries || [])) queries.push(q); }
      for (const a of (c.annotations || [])) {
        if (a.type === "url_citation" && a.url && !urls.has(a.url)) urls.set(a.url, a.title || "");
      }
    }
  }
  const wl = [...urls.entries()].filter(([u]) => wlOk(u)).map(([url, title]) => ({ url, title }));
  return { searched, queries, sources: wl, dropped: urls.size - wl.length };
}

// 문제 생겼을 때 원인을 한 화면에서 보기 위한 진단 라우트 (CHAT_TOKEN 필요)
async function diag(req, env) {
  const gate = env && env.CHAT_TOKEN;
  if (!gate) return json({ error: "CHAT_TOKEN 미설정" }, 503);
  const u = new URL(req.url);
  if ((req.headers.get("x-chat-token") || u.searchParams.get("token") || "").trim() !== gate.trim()) return json({ error: "접속 암구호가 맞지 않습니다." }, 401);

  const cf = req.cf || {};
  const base = ((env && env.GEMINI_BASE) || GEMINI_HOST).replace(/\/+$/, "");
  const out = {
    colo: cf.colo, country: cf.country, city: cf.city,
    base, model: (env && env.GEMINI_MODEL) || MODEL_DEFAULT,
    has_key: !!(env && env.GEMINI_API_KEY), has_token: !!gate,
  };
  try {
    const r = await fetch(base + GEMINI_PATH, {
      method: "POST",
      headers: { "x-goog-api-key": env.GEMINI_API_KEY || "", "Content-Type": "application/json" },
      body: JSON.stringify({ model: out.model, input: "한 단어로만 답하세요: 안녕", generation_config: genCfg(env, out.model), store: false }),
    });
    const t = await r.text();
    out.gemini_status = r.status;
    if (!r.ok) { out.gemini_body = t.slice(0, 600); }   // 실패면 원문을 그대로 보여준다 (원인이 여기 들어있다)
    else {
      try { const j = JSON.parse(t); out.gemini_reply = outputText(j); out.gemini_state = j.status; out.gemini_usage = j.usage || null; }
      catch { out.gemini_body = t.slice(0, 300); }
    }
  } catch (e) { out.gemini_error = String(e && e.message || e); }
  const ctx = await ctxRaw();
  out.context_ok = !!ctx; out.context_updated = ctx && ctx.updated;
  return json(out);
}

async function chat(req, env) {
  const key = env && env.GEMINI_API_KEY, gate = env && env.CHAT_TOKEN;
  if (!gate) return json({ error: "CHAT_TOKEN 미설정 — 챗봇이 잠겨 있습니다. Cloudflare Worker 설정에서 CHAT_TOKEN 을 등록하세요." }, 503);
  if (!key) return json({ error: "GEMINI_API_KEY 미설정 — Cloudflare Worker Secret 에 등록하세요." }, 503);

  const given = (req.headers.get("x-chat-token") || "").trim();
  if (given !== gate.trim()) return json({ error: "접속 암구호가 맞지 않습니다." }, 401);

  let b; try { b = await req.json(); } catch { return json({ error: "JSON 본문이 아닙니다." }, 400); }
  const q = String(b.q || "").trim();
  if (!q) return json({ error: "질문이 비어 있습니다." }, 400);
  if (q.length > MAX_Q) return json({ error: `질문이 너무 깁니다 (${MAX_Q}자 이내).` }, 413);

  const lang = b.lang === "id" ? "id" : "ko";
  let hist = Array.isArray(b.history) ? b.history.slice(-MAX_TURNS) : [];
  let histText = hist.map(h => `${h.role === "model" ? "어시스턴트" : "사용자"}: ${String(h.text || "").slice(0, 800)}`).join("\n");
  if (histText.length > MAX_HIST_CHARS) histText = histText.slice(-MAX_HIST_CHARS);

  const c = await ctxRaw();
  if (!c) return json({ error: "시장 데이터(chat_context.json)를 읽지 못했습니다." }, 503);

  const ctx = sliceCtx(c, q + " " + histText);
  const model = (env && env.GEMINI_MODEL) || MODEL_DEFAULT;
  const base = ((env && env.GEMINI_BASE) || GEMINI_HOST).replace(/\/+$/, "");

  const r = await fetch(base + GEMINI_PATH, {
    method: "POST",
    headers: { "x-goog-api-key": key, "Content-Type": "application/json" },
    body: JSON.stringify({
      model,
      input: (histText ? `[이전 대화]\n${histText}\n\n` : "") + `[질문]\n${q}`,
      system_instruction: sysPrompt(ctx, lang),
      generation_config: genCfg(env, model),
      ...(((env && env.GEMINI_SEARCH) || "") === "off" ? {} : { tools: [{ type: "google_search" }] }),   // 화이트리스트 매체 site: 검색 (프롬프트로 강제)
      store: false,          // 대화를 구글 쪽에 남기지 않는다 (사내 데이터)
    }),
  });

  const raw = await r.text();
  if (r.status === 429) {
    return json({
      error: (env && env.QUOTA_MSG) || (lang === "id"
        ? "Kuota AI gratis hari ini sudah habis.\n\nMr. Jay, mohon aktifkan pembayaran 🙏\n\n— Batas free tier Gemini API tercapai. Pulih otomatis setelah akun penagihan ditautkan."
        : "오늘 무료 사용량을 모두 썼습니다.\n\nMr. Jay, 결제 부탁드립니다 🙏\n\n— Gemini API 무료 등급 한도 소진. 결제 계정을 연결하면 즉시 복구됩니다."),
      quota_exceeded: true, model, detail: raw.slice(0, 300),
    }, 429);
  }
  if (!r.ok) {
    const geo = r.status === 400 && /not available in your current location/i.test(raw);
    return json({
      error: `Gemini ${r.status}`,
      detail: raw.slice(0, 500),
      model, base,
      hint: geo ? "구글이 이 서버의 IP 를 위치 미지원으로 막았습니다. Cloudflare AI Gateway 주소를 GEMINI_BASE 변수에 넣으면 우회됩니다." : undefined,
    }, 502);
  }

  let j; try { j = JSON.parse(raw); } catch { return json({ error: "Gemini 응답 파싱 실패", detail: raw.slice(0, 300) }, 502); }
  const text = outputText(j);
  if (!text) {
    const th = (j.usage && j.usage.total_thought_tokens) || 0;
    return json({
      error: j.status === "incomplete"
        ? `답변이 생성되기 전에 출력 한도(${MAX_OUT})에 걸렸습니다. 질문을 좁히거나 GEMINI_THINKING 을 low 로 두세요.`
        : "빈 응답",
      status: j.status, thought_tokens: th, usage: j.usage || null,
    }, 502);
  }

  const cit = citations(j);
  let textOut = text;
  if (cit.sources.length) {
    const label = lang === "id" ? "Sumber" : "출처";
    textOut += "\n\n" + label + ": " + cit.sources.slice(0, 5).map(s2 => { let h = ""; try { h = new URL(s2.url).hostname.replace(/^www\./, ""); } catch {} return `[${h}](${s2.url})`; }).join(" · ");
  }
  return json({
    text: textOut,
    model,
    updated: c.updated,
    delay_min: c.delay_min,
    search: cit.searched ? { queries: cit.queries.slice(0, 5), sources: cit.sources.length, dropped: cit.dropped } : null,
    usage: j.usage || null,
  });
}
