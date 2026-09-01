# IDX Live — 인도네시아 시장 실시간 대시보드

```
idx-live/
├─ index.html        화면. data.json 을 60초마다 다시 읽음 (없으면 데모 데이터)
├─ fetch_data.py     수집기 v2. IDX 공식 엔드포인트 + BI 보도자료 + Kontan + Yahoo + 화이트리스트 RSS → data.json
├─ config.json       RSS 목록, 유니버스, YTD 기준값, 원화 환산율
├─ tickers.json      종목코드 ↔ 회사명 별칭 (뉴스 스크리닝)
└─ data/manual.json  자동 수집을 덮어쓰는 수기값 (BI Rate, 매크로 일정, 확정치 교정)
   data/cache/       IDX 일별 전종목 요약 캐시 (20일 평균 대금 계산용)
```

## 실행
```
pip install requests feedparser yfinance pandas beautifulsoup4 lxml cloudscraper playwright && playwright install chromium
python fetch_data.py                 # 1회 — 첫 실행은 과거 20영업일 캐시를 채우느라 1~2분
python fetch_data.py --loop 300      # 5분마다 갱신 (장중)
python -m http.server 8080           # 같은 폴더 → http://localhost:8080
```
서버 배포: cron `*/5 9-16 * * 1-5 python fetch_data.py` 후 index.html + data.json 을 정적 호스팅(Netlify/S3/사내 서버).

## 자동 수집 항목과 출처
| 항목 | 출처 | 갱신 |
|---|---|---|
| IHSG·LQ45·USD/IDR 실시간 (장중) | Yahoo Finance 1분봉 (≈15분 지연) — 장중엔 전일 종가를 절대 현재가로 쓰지 않음 | 5분 |
| JCI·LQ45 확정 종가/고저/거래대금 (장 마감 후) | IDX `GetIndexSummary`, `GetIndexChart` | 장 마감 후 |
| 글로벌 매크로 캘린더 (예상·이전·실제·중요도) | saveticker.com/calendar (playwright 렌더 파싱) → 실패 시 investing.com 캘린더 XHR (US·ID) | 매 실행 |
| 실시간 공시 스트림 (최근 30시간) | IDX `GetAnnouncement` | 매 실행 |
| 상승/하락/보합 종목 수, 외국인 매수·매도·순매수(전체시장), 비정규시장 대금 | IDX `GetStockSummary` 전종목 합산 | 장중 |
| 급등/급락(대금 Rp10억+), 거래대금 급증(20일 평균 대비), 외인 순매수 상·하위 종목 | IDX `GetStockSummary` + 캐시 | 장중 |
| 기업 일정: RUPS·Public Expose·Cum/Ex date, 배당(현금배당·배당락·지급일), 실적 공시 | IDX `GetCalendar`, `LINK_DIVIDEND`, `GetAnnouncement` | 매 실행 |
| 루피아 종가(bid), 국채 10Y, DXY, UST 10Y, CDS 5Y, 비거주자 주간 순매수(주식/SBN/SRBI) | BI 보도자료 "Perkembangan Indikator Stabilitas Nilai Rupiah" | 월·금 |
| 국채 10Y (BI 미발표일) | Kontan pusatdata `yield_sun_acuan` | 일간 |
| USD/IDR·IDR/KRW·Brent·DXY·UST (보조) | Yahoo Finance | 15분 지연 |
| 종목 뉴스 | config.json 화이트리스트 RSS → tickers.json 매칭 | 매 실행 |

## 화면 (index.html)
- 상단 탭: 증시 / 뉴스 / 캘린더. 증시 = 주요 지수 3카드(IHSG·LQ45·USD/IDR 장중 스파크) + 외국인 수급(전일 확정치, 순매수·순매도 상위) + 종목 랭킹(거래대금·상승·하락·대금급증) + 시장 지표 + 오늘 일정
- 종목 클릭 → 오른쪽 드로어: 현재가·등락·대금·외인 순매수 + 화이트리스트 매체 헤드라인 + 해당 종목 일정
- 캘린더: 날짜 이동, 중요도(★) 필터, 매크로/기업 구분, 예상·이전·실제
- 로고: `assets.stockbit.com/logos/companies/{코드}.png` → 실패 시 IDX 로고 → 실패 시 이니셜 원형. claude.ai 아티팩트 미리보기에서는 외부 이미지가 차단되어 이니셜만 보임(자체 호스팅 시 정상)

## 수기 입력이 남는 것 (data/manual.json)
- `bi_rate` — BI 금리 (RDG 후 갱신)
- `macrocal` — CPI·PMI·FOMC 등 매크로 일정 (date/time/title/imp 1~3/exp/prev/act)
- `idr_per_krw`, `fx_basis` — 월요일 주간환율
- `index.*` — IDX Daily Statistics PDF 확정치로 자동값을 교정하고 싶을 때만

## 언어
화면 우상단 KO / ID 토글. UI 문구만 전환(뉴스·공시 원문은 매체 언어 그대로). 영어 추가는 index.html 의 `I18N` 사전에 `en` 블록만 추가.

## 주의
- saveticker 는 로그인 없이 렌더되는 공개 페이지지만 DOM 구조가 바뀌면 파서가 비어 나온다 → `data/cache/saveticker.html` 저장본 보고 정규식 조정. 그동안은 investing.com 캘린더로 자동 대체.
- IDX 는 Cloudflare 뒤. 403 이 나오면 `pip install cloudscraper` (자동 사용됨). 과도한 호출 금지 — 5분 주기 권장.
- IDX `GetStockSummary` 의 ForeignBuy/Sell 은 전체시장(정규+협상+현금) 합산. 정규시장만 보려면 `NonRegularValue` 를 빼서 판단.
- RSS 주소 7개는 매체별로 바뀔 수 있음 → 로그에 `rss empty` 뜨면 config.json 수정.
- 못 구한 값은 "확인 필요"로 표시되며 추정하지 않음.
