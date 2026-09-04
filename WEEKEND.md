# 주말·PC 꺼짐 대비 — IDX Live 자립 운영

PC 가 꺼져 있어도 사이트가 계속 돌게 하는 설정과, 무엇이 되고 무엇이 안 되는지.

## 지금 구조

```
평일·PC 켜짐                          주말·PC 꺼짐
─────────────────                    ─────────────────
PC run.py                            (없음)
  → idx_part.json ─┐                        ┐
                   │                        │
GitHub Actions 러너 ┴→ data.json ← 러너가 IDX 직접 수집 ┘
  → GitHub raw → Cloudflare Worker → 사이트
```

Worker 와 GitHub 는 PC 와 무관하게 24시간 돌아간다. **끊기는 건 PC 가 담당하던 두 가지뿐이다.**

## 반드시 등록해야 할 설정 2개

### 1. IDX_PROXY (Repository variable) — 공시·시세 수집

러너 IP 가 IDX 에 막히면 공시가 아예 안 들어온다. Worker 의 프록시를 경유시킨다.

`github.com/jelly927/idx-monitoring` → **Settings** → **Secrets and variables** → **Actions**
→ **Variables** 탭 → **New repository variable**

| Name | Value |
|---|---|
| `IDX_PROXY` | `https://idx-live.hjaelim0.workers.dev` |

Worker 의 `ALLOW` 목록에 `www.idx.co.id` 와 Yahoo 가 이미 들어 있어 그대로 동작한다.

### 2. GEMINI_API_KEY (Repository secret) — 주말 AI 요약

같은 화면 → **Secrets** 탭 → **New repository secret**

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | Gemini API 키 (`secrets.json` 의 `gemini_api_key` 와 같은 값) |

등록하면 주말에도 **지수 요약과 공시 요약**이 계속 만들어진다.
등록하지 않으면 기존 캐시만 표시되고 새 요약은 안 생긴다 (사이트는 정상 동작).

## 러너에서 무엇이 돌고 무엇이 안 도는가

| 기능 | PC 켜짐 | PC 꺼짐 (러너) | 비고 |
|---|:---:|:---:|---|
| 지수·종목 시세 | O | O | Yahoo |
| 공시 수집 | O | O | `IDX_PROXY` 필요 |
| 뉴스 수집 | O | O | RSS |
| 캘린더·지표 | O | O | |
| 주목(Catalyst)·시가총액 | O | O | 룰 기반, AI 불필요 |
| **지수 AI 요약** | O | O | `GEMINI_API_KEY` 시크릿 필요 |
| **공시 AI 요약** | O | O | 시크릿 필요 · 빌드당 6건 → 3건으로 감속 |
| **종목 AI 요약** | O | **X** | 빌드당 40종목이라 할당량 보호를 위해 PC 전용 |
| 뉴스 한국어 번역 | Claude CLI | 구글 MT | 품질 차이 있음 (아래 참고) |

**종목 AI 요약을 러너에서 막은 이유**: 5분마다 40종목이면 하루 수천 건 호출이라 무료 등급이 즉시 소진된다.
주말에는 기존 캐시가 표시되고, PC 를 켜면 다시 채워진다.

## 번역 품질

평일에는 Claude CLI 가 번역하고 결과를 `tr_claude.json` 에 쌓는다. 주말 러너에는 Claude CLI 가 없어
새 헤드라인은 구글 기계번역으로 처리된다. 이미 번역된 것은 캐시에서 그대로 나온다.

구글 MT 는 티커를 일반 단어로 오역하는 경우가 있다 (예: `CUAN` → "이익", `SINI` → "여기").
주말 헤드라인이 어색하면 인도네시아어 원문을 확인하는 편이 안전하다.

## 스케줄

`.github/workflows/update.yml`

| 시간대 (WIB) | 주기 |
|---|---|
| 평일 08:00~17:59 | 5분 |
| 평일 18:00~23:59 | 15분 |
| 매일 00:00~07:59 | 1시간 |
| **주말 08:00~23:00** | **2시간** |

주말에 시장이 닫혀 있어 2시간 주기로 충분하다. 해외 지표·뉴스는 계속 갱신된다.

## 신선도 확인

`data.json` 의 `sources` 에 아래가 기록된다.

- `idx_from_pc` — PC 수집분 저장 시각
- `pc_age_min` — 그게 몇 분 전인지 (PC 가 꺼져 있으면 계속 커진다)
- `on_runner` — 러너가 만든 데이터인지

빠른 점검: `https://idx-live.hjaelim0.workers.dev/api/feed` 를 열어 `updated` 와 `freshness` 를 본다.
챗봇도 이 값을 근거로 데이터가 오래됐는지 답할 수 있다.

## 문제가 생겼을 때

| 증상 | 원인 | 조치 |
|---|---|---|
| 공시가 안 늘어남 | 러너가 IDX 에 막힘 | `IDX_PROXY` 등록 확인 |
| 지수 요약이 안 바뀜 | 시크릿 미등록 또는 할당량 초과 | Secrets 확인 → AI Studio 할당량 확인 |
| 사이트 자체가 안 열림 | Worker 문제 | Cloudflare 대시보드 → idx-live → Deploy 이력 확인 |
| 데이터가 몇 시간째 그대로 | Actions 실패 | Actions 탭에서 빨간 X 확인 후 Re-run |
