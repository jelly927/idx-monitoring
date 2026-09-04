# IDX Live — AI 챗봇(Gemini) 붙이기

사내용. 접속 암구호(CHAT_TOKEN)를 아는 사람만 쓸 수 있고, 답변 근거는 IDX Live 화면 데이터로 한정된다.

## 구조

```
GitHub Actions (5분마다)
  fetch_data.py            → data.json (1.4MB)
  make_chat_context.py     → data/cache/chat_context.json (114KB)   ← 새로 추가
        │
        ▼  raw.githubusercontent.com
Cloudflare Worker (idx-live)
  GET  /               index.html  (챗봇 위젯 포함)
  GET  /api/feed       chat_context.json 그대로 — 크롤러·데일리시황 참고용
  POST /api/chat       질문 → 관련 종목만 추려 컨텍스트 구성 → Gemini 호출 → 답변
        │
        ▼
  generativelanguage.googleapis.com/v1beta/interactions
```

**API 키는 Cloudflare Worker Secret 에만 있다.** 저장소(jelly927/idx-monitoring)는 공개이고
index.html·data.json 은 누구나 볼 수 있으므로, 키를 프론트엔드나 저장소 파일에 넣으면 즉시 유출된다.

## 순서대로 할 일

### 1. GitHub 에 파일 올리기 (PC에서)

```
python publish.py --data
```

올라가는 새 파일: `make_chat_context.py`, `worker/worker.js`, `index.html`,
`.github/workflows/update.yml`, `data/cache/chat_context.json`

### 2. Cloudflare 에 비밀값 등록

dash.cloudflare.com → Workers & Pages → **idx-live** → Settings → Variables and Secrets

| 이름 | 종류 | 값 |
|---|---|---|
| `GEMINI_API_KEY` | Secret (Encrypt) | Gemini API 키 |
| `CHAT_TOKEN` | Secret (Encrypt) | 사내 배포용 암구호 (직접 정한다) |
| `GEMINI_MODEL` | Variable (선택) | 비워두면 `gemini-3.8-flash` |

`CHAT_TOKEN` 을 등록하지 않으면 `/api/chat` 은 503 으로 잠긴 상태를 유지한다 (기본값이 잠김).

### 3. Worker 배포

같은 화면 → Edit code → 전체 선택 후 `worker/worker.js` 내용으로 교체 → Deploy

### 4. 확인

1. `https://idx-live.hjaelim0.workers.dev/api/feed` → JSON 이 보이면 컨텍스트 정상
2. 사이트 우하단 초록 **AI** 버튼 → 암구호 입력 → "오늘 시장 한 줄 요약"
3. 실패 시 화면에 뜨는 오류 메시지로 원인 구분:
   - `CHAT_TOKEN 미설정` → 2번 누락
   - `GEMINI_API_KEY 미설정` → 2번 누락
   - `접속 암구호가 맞지 않습니다` → 입력값 불일치
   - `Gemini 400/403` → 키 무효 또는 모델명 불일치 (`GEMINI_MODEL` 조정)
   - `시장 데이터를 읽지 못했습니다` → 1번 누락 또는 Actions 미실행

## 설계상 정해둔 것

- **컨텍스트 슬라이싱**: 전체 835종목(67KB)을 매번 넣지 않고, 질문에 대문자 티커나 회사명이 있으면
  그 종목만(최대 40개), 없으면 거래대금 상위 30개만 싣는다. 요청당 프롬프트 약 48~50KB.
- **`store: false`**: 대화 내용을 구글 쪽에 남기지 않는다.
- **입력 상한**: 질문 1,000자 / 히스토리 8턴·4,000자.
- **프롬프트 규칙**: DATA 에 없는 숫자 생성 금지, 없으면 "확인 불가", 추정형 표현 금지,
  숫자에 비교 부착, IDR→KRW 병기, 지연 데이터임을 명시, 투자 권유 금지.
- **뉴스 한국어 제목**: 기계번역이라 티커가 단어로 오역되는 경우가 있어(예: CUAN→"이익"),
  제목이 어색하면 인도네시아어 원문(`t_id`)을 근거로 삼도록 프롬프트에 명시했다.

## 확인 필요 (미검증)

- **API 키 유효성**: 이 환경에서 `generativelanguage.googleapis.com` 이 차단되어 실호출 테스트를 못 했다.
  4번 단계가 첫 실검증이다.
- **모델명**: `gemini-3.8-flash` 기준. 400 이 나면 `GEMINI_MODEL` 변수로 교체한다 (코드 수정 불필요).
- **Worker CPU 한도**: 요청당 114KB JSON 파싱이 들어간다. 무료 플랜에서 CPU 초과(1102 오류)가 나면
  `make_chat_context.py` 의 `stocks` 를 별도 파일로 분리하는 방식으로 줄인다.
