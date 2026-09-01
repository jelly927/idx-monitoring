# IDX Live 배포 — jelly927.github.io/idx-live 현재 상태와 남은 3단계

**지금 레포에 올라간 것:** `index.html` 하나뿐. `.github/workflows/update.yml`(수집기 자동 실행)과 `worker/worker.js`가 없음.
→ 그래서 화면은 8/31 데모 값이고, 지수 실시간은 프록시가 없어 "Worker 미설정"으로 멈춤. 아래 3개만 하면 살아난다.

## 1) 워크플로 파일 만들기 (숨김 폴더라 드래그 업로드에서 빠진 것)
1. 레포 → **Add file → Create new file**
2. 파일명 칸에 정확히 `.github/workflows/update.yml` 입력 (폴더가 자동 생성됨)
3. 이 zip 의 `SETUP_update.yml` 내용을 그대로 붙여넣기 (첫 줄 주석은 있어도 됨) → **Commit changes**
4. 같은 방법으로 `fetch_data.py`, `config.json`, `tickers.json`, `requirements.txt`, `data/manual.json` 도 올리기 (Add file → Upload files 로 한 번에 가능. 이 파일들은 숨김이 아니라 정상 업로드됨)

## 2) Pages 소스 바꾸기 + 첫 실행
1. Settings → Pages → Build and deployment → Source: **GitHub Actions**
2. Actions 탭 → `update-data` → **Run workflow** → 2~3분 후 `https://jelly927.github.io/idx-live/` 새로고침
3. 상단 상태가 `실시간 · …` 또는 `갱신 2026-09-01 hh:mm` 으로 바뀌면 data.json 이 붙은 것
   - Actions 로그에 `IDX 403` 이 반복되면 GitHub 러너 IP 차단 → 3) 의 Worker 주소를 Settings → Secrets and variables → Actions → **Variables** 에 `IDX_PROXY` 로 등록하면 우회됨

## 3) Cloudflare Worker (지수 실시간 틱)
1. dash.cloudflare.com 가입 → Workers & Pages → **Create → Start with Hello World** → 이름 `idx-live` → Deploy
2. **Edit code** → 기존 코드 지우고 `worker/worker.js` 붙여넣기 → Deploy
3. 주소 복사 (`https://idx-live.<계정>.workers.dev`)
4. 레포의 `index.html` 열기 → 연필(Edit) → `const LIVE_PROXY=''` 를 `const LIVE_PROXY='https://idx-live.<계정>.workers.dev'` 로 → Commit
5. 1~2분 후 새로고침 → 상단이 `실시간 · 15분 지연 · Yahoo`, IHSG 숫자가 60초마다 갱신

## 회사 도메인 (나중에)
Settings → Pages → Custom domain `idxlive.kisi.co.id` → 사내 DNS 에 CNAME `jelly927.github.io`

## 막히면
Actions 실행 로그(빨간 X 클릭 → 펼친 화면) 스크린샷 하나면 바로 원인 잡힘.
