// Cloudflare Worker — IDX Live 공개 사이트.
// 화면(index.html)·데이터(data.json)를 GitHub 저장소(main)에서 그때그때 읽어 서빙한다.
//   → Worker 는 한 번만 배포하면 되고, 이후 GitHub 에 올라간 파일이 그대로 사이트에 반영된다 (PC 꺼져 있어도).
//   → 보는 사람의 브라우저는 workers.dev 만 접속하므로 회사망에서 github.io 가 막혀 있어도 열린다.
// 배포: dash.cloudflare.com → Workers & Pages → (기존 Worker) → Edit code → 전체를 이 파일로 교체 → Deploy
const REPO = "jelly927/idx-monitoring", BRANCH = "main";
const RAW = `https://raw.githubusercontent.com/${REPO}/${BRANCH}`;
const TYPES = { html: "text/html; charset=utf-8", json: "application/json; charset=utf-8", js: "text/javascript; charset=utf-8", css: "text/css; charset=utf-8", png: "image/png", svg: "image/svg+xml", ico: "image/x-icon" };
const ALLOW = ["query1.finance.yahoo.com", "query2.finance.yahoo.com", "www.idx.co.id"];   // (선택) ?url= 프록시 — GitHub 러너가 IDX 에 막힐 때 사용

export default {
  async fetch(req) {
    const u = new URL(req.url);
    const target = u.searchParams.get("url");
    if (target) return proxy(target);
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
