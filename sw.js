/* IDX Live — 알림 서비스 워커 (2단계: 탭을 닫아도 알림)
 *
 * 동작
 *   ① Worker 가 "뭔가 바뀌었다"는 신호(내용 없는 푸시)만 보낸다 — 알림 문구를 서버가 만들지 않는다.
 *   ② 이 워커가 data.json 을 직접 읽고, 브라우저에 저장된 사용자 규칙으로 판단해 알림을 띄운다.
 *   ③ 따라서 관심 종목·알림 조건은 서버로 나가지 않는다. 서버에 남는 건 구독 주소 하나뿐.
 *
 * 규칙 판정은 index.html 의 알림 모듈과 같은 기준이다. 한쪽을 고치면 다른 쪽도 같이 고칠 것.
 * (같은 파일을 공유하지 않는 이유: index.html 은 단일 파일로 유지하고, 서빙 경로도 sw.js 하나만 늘리기 위함)
 */
'use strict';
var CACHE = 'idxalrt-v1', SKEY = '/__alrt_state';
var DATA_URL = '/data.json';
var RAW_URL = 'https://raw.githubusercontent.com/jelly927/idx-monitoring/main/data.json';

self.addEventListener('install', function(e){ self.skipWaiting(); });
self.addEventListener('activate', function(e){ e.waitUntil(self.clients.claim()); });

/* 페이지가 규칙·관심종목을 보내면 그대로 보관한다(브라우저를 껐다 켜도 남는다) */
self.addEventListener('message', function(e){
  var m = e.data || {};
  if (m.type === 'alrt-state') e.waitUntil(putState(m.state));
});

self.addEventListener('push', function(e){ e.waitUntil(run('push')); });
self.addEventListener('pushsubscriptionchange', function(e){ /* 재구독은 페이지가 다음 방문에 처리 */ });

self.addEventListener('notificationclick', function(e){
  e.notification.close();
  var t = (e.notification.data && e.notification.data.t) || '';
  e.waitUntil(self.clients.matchAll({type:'window', includeUncontrolled:true}).then(function(ws){
    for (var i=0;i<ws.length;i++) if ('focus' in ws[i]) { ws[i].focus(); if (t) ws[i].postMessage({type:'alrt-open', t:t}); return; }
    return self.clients.openWindow('/' + (t ? '?t=' + encodeURIComponent(t) : ''));
  }));
});

/* ── 보관소 ─────────────────────────────────────────────────── */
function putState(s){
  return caches.open(CACHE).then(function(c){
    return c.put(SKEY, new Response(JSON.stringify(s||{}), {headers:{'Content-Type':'application/json'}}));
  });
}
function getState(){
  return caches.open(CACHE).then(function(c){ return c.match(SKEY); })
    .then(function(r){ return r ? r.json() : null; }).catch(function(){ return null; });
}

/* ── 본체 ──────────────────────────────────────────────────── */
function run(){
  var st;
  /* 탭이 열려 보이는 중이면 페이지가 직접 알린다 — 워커까지 띄우면 같은 알림이 두 번 온다 */
  return self.clients.matchAll({type:'window', includeUncontrolled:true}).then(function(ws){
    for (var i=0;i<ws.length;i++) if (ws[i].visibilityState === 'visible') return true;
    return false;
  }).catch(function(){ return false; }).then(function(open){
    if (open) return null;
    return getState();
  }).then(function(s){
    if (s === null) return null;
    st = s || {};
    if (!st.on || !st.rules) return null;
    return fetch(DATA_URL + '?t=' + Date.now(), {cache:'no-store'})
      .then(function(r){ return r.ok ? r.json() : Promise.reject(0); })
      .catch(function(){ return fetch(RAW_URL + '?t=' + Date.now(), {cache:'no-store'}).then(function(r){ return r.json(); }); });
  }).then(function(d){
    if (!d) return;
    var out = evaluate(d, st);
    var p = [];
    for (var i=0;i<out.length && i<6;i++) p.push(show(out[i]));
    return Promise.all(p).then(function(){ return putState(st); });
  }).catch(function(){});
}

function show(n){
  return self.registration.showNotification(n.title, {
    body: n.body, tag: n.title + '|' + n.body, renotify: false,
    data: {t: n.t || ''}, icon: '/assets/icon-192.png', badge: '/favicon.ico'
  });
}

/* ── 문구 ──────────────────────────────────────────────────── */
var TX = {
  ko:{nPx:'종목알림',nNews:'뉴스 업로드',nAnn:'공시 업로드',nTop:'주요뉴스',nCat:'주목 진입',nKw:'키워드',nCal:'일정',nStale:'수집 정체',
      hi20:'20일 최고가 돌파',lo20:'20일 최저가 이탈',ratio:'거래대금 급증',tp:'목표가',sl:'손절가',by:'KISI 리서치 의견',
      min:'분',x:'배',after:'후',basis:'기준',delay:'15분 지연',stale1:'분째 미갱신 · 마지막'},
  id:{nPx:'Notifikasi saham',nNews:'Berita baru',nAnn:'Keterbukaan baru',nTop:'Berita utama',nCat:'Masuk Watchout',nKw:'Kata kunci',nCal:'Agenda',nStale:'Data tertahan',
      hi20:'Tembus tertinggi 20 hari',lo20:'Tembus terendah 20 hari',ratio:'Lonjakan nilai transaksi',tp:'Target',sl:'Stop loss',by:'Opini KISI Research',
      min:'mnt',x:'x',after:'lagi',basis:'per',delay:'tertunda 15 mnt',stale1:'menit tidak diperbarui · terakhir'}
};

/* ── 규칙 판정 — index.html 알림 모듈과 같은 기준 ───────────── */
function evaluate(d, st){
  var ID = st.lang === 'id', X = function(k){ return TX[ID?'id':'ko'][k]; };
  var R = st.rules || {}, W = st.wl || [], KWL = st.kw || [], sn = st.seen = st.seen || {};
  sn.ann = sn.ann || []; sn.news = sn.news || []; sn.cal = sn.cal || []; sn.cat = sn.cat || []; sn.once = sn.once || {};
  var day = new Date(Date.now()+7*3600e3).toISOString().slice(0,10);
  if (sn.day !== day) { sn.once = {}; sn.cat = []; sn.day = day; }
  var out = [];
  function once(k){ if (sn.once[k]) return false; sn.once[k]=1; return true; }
  function L(o, b){ return (ID ? (o[b+'_id']||o[b]) : (o[b+'_ko']||o[b])) || o[b] || ''; }
  function rp(v){ return 'Rp' + Number(Math.round(v)).toLocaleString('en-US'); }
  function sg(v){ return (v>0?'+':'') + (Math.round(v*100)/100) + '%'; }

  /* 시각이 정해진 알림 먼저 — 건수 상한 밖 */
  if (R.cal && R.cal.on) (d.calendar||[]).forEach(function(e){
    if ((e.imp||0) < 3) return;
    var m = String(e.date||'').match(/(\d{4})-(\d\d)-(\d\d)/), hm = String(e.time||'').split(':');
    if (!m || hm.length !== 2) return;
    var left = Math.round(((Date.UTC(+m[1],+m[2]-1,+m[3],+hm[0],+hm[1]) - 7*3600e3) - Date.now())/60000);
    if (left < 0 || left > (+R.cal.min||30)) return;
    var k = (e.date||'')+'|'+(e.time||'')+'|'+String(e.title||'').slice(0,40);
    if (sn.cal.indexOf(k) >= 0) return; sn.cal.push(k);
    out.push({title: X('nCal')+' · '+left+X('min')+' '+X('after'), body:(e.country?e.country+' · ':'')+L(e,'title')});
  });
  if (R.stale && R.stale.on && st.inHouse) {
    var mm = String(d.updated||'').match(/(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)/);
    if (mm) {
      var age = Math.floor((Date.now() - (Date.UTC(+mm[1],+mm[2]-1,+mm[3],+mm[4],+mm[5]) - 7*3600e3))/60000);
      var lim = +R.stale.min||15;
      if (age >= lim && once(day+'|stale|'+Math.floor(age/lim)))
        out.push({title:X('nStale'), body:age+X('stale1')+' '+(d.updated||'')});
    }
  }

  /* 종목 가격 — 관심 종목 기준 */
  W.forEach(function(t){
    var o = null, s = d.stocks||[], i;
    for (i=0;i<s.length;i++) if (s[i].t===t) { o = s[i]; break; }
    if (!o || o.px == null) return;
    var asof = (d.index&&d.index.rank_asof) || d.updated || ''; var bs = (asof ? ' · '+asof+' '+X('basis') : '') + ' ('+X('delay')+')';
    var tc = (d.tech||{})[t];
    if (R.pct && R.pct.on && o.pct != null && Math.abs(o.pct) >= Math.abs(+R.pct.v||5) && once(day+'|'+t+'|pct|'+(o.pct>0?'u':'d')))
      out.push({title:X('nPx')+' · '+t, body:sg(o.pct)+' · '+rp(o.px)+bs, t:t});
    if (tc && R.hi20 && R.hi20.on && tc.hi20 && o.px > tc.hi20 && once(day+'|'+t+'|hi'))
      out.push({title:X('nPx')+' · '+t, body:X('hi20')+' · '+rp(o.px)+bs, t:t});
    if (tc && R.lo20 && R.lo20.on && tc.lo20 && o.px < tc.lo20 && once(day+'|'+t+'|lo'))
      out.push({title:X('nPx')+' · '+t, body:X('lo20')+' · '+rp(o.px)+bs, t:t});
    if (R.ratio && R.ratio.on && o.ratio && o.ratio >= (+R.ratio.v||3) && once(day+'|'+t+'|rt'))
      out.push({title:X('nPx')+' · '+t, body:X('ratio')+' '+(Math.round(o.ratio*10)/10)+X('x')+bs, t:t});
  });

  /* 애널리스트 의견 가격대 — 목표가·손절가는 사람이 쓴 의견일 때만 */
  if (R.call && R.call.on) (d.watch||[]).forEach(function(c){
    if (!c || !c.t) return;
    var o = null, s = d.stocks||[], i;
    for (i=0;i<s.length;i++) if (s[i].t===c.t) { o = s[i]; break; }
    if (!o || o.px == null) return;
    var asof = (d.index&&d.index.rank_asof) || d.updated || ''; var bs = (asof ? ' · '+asof+' '+X('basis') : '') + ' ('+X('delay')+')';
    var tps = [].concat(c.tp||[]);
    for (i=0;i<tps.length;i++) if (tps[i] && o.px >= tps[i] && once(day+'|'+c.t+'|tp'+i)) {
      out.push({title:X('nPx')+' · '+c.t, body:X('tp')+' '+rp(tps[i])+' · '+rp(o.px)+bs+' · '+X('by'), t:c.t}); break; }
    if (c.sl && o.px <= c.sl && once(day+'|'+c.t+'|sl'))
      out.push({title:X('nPx')+' · '+c.t, body:X('sl')+' '+rp(c.sl)+' · '+rp(o.px)+bs+' · '+X('by'), t:c.t});
  });

  /* 공시 — 처음 켠 시점의 물량은 기준만 잡는다 */
  var annFirst = sn.ann.length === 0;
  (d.announcements||[]).forEach(function(a){
    var k = (a.date||'')+'|'+(a.t||'')+'|'+(a.time||'')+'|'+(a.url||'');
    if (sn.ann.indexOf(k) >= 0) return; sn.ann.push(k);
    if (annFirst) return;
    var mine = W.indexOf(a.t) >= 0;
    if (!((R.annWl && R.annWl.on && mine) || (R.ann && R.ann.on && (a.rk||0) >= (+R.ann.q||80)))) return;
    var txt = String((ID ? (a.ai_id||a.title_id) : (a.ai_ko||a.title_ko)) || a.title || '').replace(/^[·\s]+/,'').split('\n')[0];
    out.push({title:X('nAnn')+' · '+(a.t||'IDX'), body:txt.slice(0,110), t:(a.t&&a.t!=='IDX')?a.t:''});
  });

  /* 뉴스 */
  var newsFirst = sn.news.length === 0;
  (d.top_news||[]).forEach(function(n){
    var k = n.url||n.t; if (!k || sn.news.indexOf(k) >= 0) return; sn.news.push(k);
    if (newsFirst || !(R.news && R.news.on)) return;
    out.push({title:X('nTop'), body:String(L(n,'t')).slice(0,110)});
  });
  (d.news||[]).forEach(function(n){
    var k = n.url||n.t; if (!k || sn.news.indexOf(k) >= 0) return; sn.news.push(k);
    if (newsFirst || !(R.newsWl && R.newsWl.on)) return;
    var tags = n.tags||[], mine = null, i;
    for (i=0;i<tags.length;i++) if (W.indexOf(tags[i]) >= 0) { mine = tags[i]; break; }
    if (!mine) return;
    out.push({title:X('nPx')+' · '+mine, body:X('nNews')+' · '+String(L(n,'t')).slice(0,110), t:mine});
  });

  /* 키워드 뉴스 — 페이지와 같은 규칙. 키워드 목록은 이 브라우저 안에만 있고 서버로 나가지 않는다 */
  if (R.kw && R.kw.on && KWL.length) {
    var kwFirst = !sn.kw || !sn.kw.length; sn.kw = sn.kw || [];
    (d.news||[]).concat(d.market_news||[], d.kisi_news||[]).forEach(function(n){
      var u = n.url; if (!u || sn.kw.indexOf(u) >= 0) return;
      var hay = [n.t,n.t_ko,n.t_id,n.ai,n.ai_ko,n.ai_id].filter(Boolean).join(' ').toLowerCase(), hit = null, z;
      for (z=0;z<KWL.length;z++) if (hay.indexOf(String(KWL[z]).toLowerCase()) >= 0) { hit = KWL[z]; break; }
      if (!hit) return;
      sn.kw.push(u);
      if (kwFirst) return;                       /* 처음 한 번은 기준만 잡고 보내지 않는다 */
      out.push({title:X('nKw')+' \u00b7 '+hit, body:String(L(n,'t')).slice(0,110)});
    });
    sn.kw = sn.kw.slice(-800);
  }

  /* 주목 신규 진입 */
  if (R.catNew && R.catNew.on) {
    var first = sn.cat.length === 0;
    (d.catalyst||[]).forEach(function(c){
      var t = c && c.t; if (!t || sn.cat.indexOf(t) >= 0) return; sn.cat.push(t);
      if (first) return;
      out.push({title:X('nCat')+' · '+t, body:(c.n||t)+(c.score!=null?' · '+c.score:''), t:t});
    });
  }

  /* 보관 용량 정리 — 공시는 하루 200건대라 건수로 자르면 이틀 만에 기준이 사라진다 */
  var cut = new Date(Date.now()+7*3600e3-4*864e5).toISOString().slice(0,10);
  sn.ann = sn.ann.filter(function(k){ return k.slice(0,10) >= cut; }).slice(-2000);
  sn.news = sn.news.slice(-600); sn.cal = sn.cal.slice(-200); sn.cat = sn.cat.slice(-100);
  return out;
}
