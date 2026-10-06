# 메르AI 포트폴리오 자동 분석

## 전체 구성 요약

이 프로젝트는 기존 Gemini 자동화와 두 페르소나를 제공하며, 자동화 코드에 선택적 ChatGPT 로그인 판단과 모델 비교 기능을 추가했습니다.

| 방식 | 위치 | 자동화 | 필요한 것 |
|------|------|--------|---------|
| **GitHub Actions 자동화** | 이 폴더 | ✅ 매일 자동 실행 | Gemini API 키 |
| **ChatGPT GPTs 페르소나** | `personas/chatgpt-gpt/` | 수동 트리거 | ChatGPT Plus (이미 있음) |
| **Gemini Gems 페르소나** | `personas/gemini-gem/` | 수동 트리거 | Gemini Advanced (이미 있음) |
| **ChatGPT 로그인 판단·비교** | `chatgpt_auth.py`, `compare_decisions.py` | 로컬 또는 고정 자체 서버 | 공식 로그인으로 허용된 계정·모델 |

→ 기존 Gemini 자동화는 그대로 사용할 수 있습니다. ChatGPT 연결은 선택 사항입니다.
→ 채팅으로 빠르게 쓰고 싶으면 Gems, 자동으로 쌓아두고 싶으면 GitHub Actions.

메르(blog.naver.com/ranto28)의 블로그 글을 자동 수집해서
Gemini AI가 메르 스타일로 한국·미국 주식 포트폴리오를 분석해주는 시스템.

prophit_(blog.naver.com/prophit_)이 수동으로 하던 "메르ai포트"를 완전 자동화한 버전.

**비용:** GitHub Actions와 Gemini API 무료 tier 범위에서는 $0. 현재 기본 모델 두 개 모두 무료 tier가 있지만, 운영량이 무료 quota를 넘으면 Google Cloud Billing 연결이 필요할 수 있습니다.

---

## 작동 방식

```
[GitHub Actions 스케줄러 — 매일 자정 KST]
        ↓
[메르 블로그 RSS 파싱 + 전문 스크래핑]
        ↓
[원문/Flash-Lite 요약 + 검증 신호원장 + Gemini 3.5 Flash 구조화 판단]
        ↓
[output/report_YYYYMMDD.md 로 자동 저장 + 커밋]
```

`report_YYYYMMDD.md`의 날짜는 한국시간 기준입니다. GitHub Actions 실행 환경이 UTC여도
Telegram, HTML, Markdown 보고서는 투자자가 보는 한국 날짜로 생성됩니다.

---

## 셋업 (5분)

### 1단계 — Gemini API 키 발급 (무료)

1. https://aistudio.google.com/app/apikey 접속
2. Google 계정으로 로그인
3. **"Create API key"** 클릭
4. 발급된 키 복사 (형식: `AIza...`)

> Gemini API 무료 한도는 모델과 Google Cloud 프로젝트 상태에 따라 달라집니다.
> 정확한 현재 한도는 Google AI Studio의 rate limits 화면에서 확인하세요.

---

### 2단계 — GitHub 저장소 만들기

1. https://github.com/new 에서 새 레포 생성
   - 이름 예: `mer-portfolio`
   - **Private** 권장 (투자 정보이므로)
   - README 체크 해제

2. 이 프로젝트 파일들을 레포에 업로드
   ```
   mer-portfolio/
   ├── .github/workflows/schedule.yml
   ├── output/.gitkeep
   ├── fetch_mer.py
   ├── system_prompt.py
   ├── analyze.py
   ├── main.py
   └── requirements.txt
   ```

---

### 3단계 — API 키를 GitHub Secrets에 등록

1. 레포 페이지 → **Settings** 탭
2. 왼쪽 메뉴 → **Secrets and variables** → **Actions**
3. **"New repository secret"** 클릭
4. Name: `GEMINI_API_KEY`
5. Secret: 1단계에서 복사한 키 붙여넣기
6. **"Add secret"** 클릭

---

### 4단계 — 첫 실행 테스트

1. 레포 → **Actions** 탭
2. 왼쪽에서 **"메르AI 포트폴리오 분석"** 클릭
3. **"Run workflow"** 버튼 클릭 → **"Run workflow"** 확인
4. 실행 완료 후 `output/report_YYYYMMDD.md`와 `output/dashboard.html` 확인

---

## 자동 실행 스케줄

| 실행 시점 | 수집 기간 |
|-----------|-----------|
| 매일 자정 (KST) | 신규 글 |

수동 실행은 Actions → Run workflow에서 가능합니다.
`scheduled`는 신규 글만 분석합니다. 마지막 실제 리밸런싱 후 `7일`이 지났으면 그 이후
누적된 투자 관련 글로 주간 리밸런싱합니다. 최초 기준일이 없거나 수동 리밸런싱을 할 때는
최근 `14일`의 원문·요약을 분석 근거로 사용합니다. 신규 근거가 없으면 리밸런싱 날짜를 갱신하지 않고
다음 관련 글까지 연기합니다. `verify`는 Gemini 호출 없이 현재 상태 기준 Telegram/HTML/PNG
출력을 검증합니다. 실제 수집, Flash-Lite 요약, 3.5 Flash 판단까지 확인해야 할 때만 `full_verify`를
실행합니다. `test`는 API 호출 없이 mock 테스트만 실행합니다.

---

## 출력 파일

```
output/
├── report_20260515.md     ← 날짜별 리포트
├── report_20260501.md
├── dashboard.html         ← HTML 대시보드
├── chart_latest.png       ← Telegram 첨부용 현재 차트
├── portfolio_state.json   ← 현재 모델 포트폴리오 기준 자료
└── ...
```

리포트 형식:
```markdown
# 메르AI 포트폴리오 리포트
분석 기간: 2026-05-03 ~ 2026-05-15

## 📌 시장 분석 핵심 인사이트
### 인사이트 1: 호르무즈 봉쇄 장기화와 조선업 나비효과
1. 확인된 사실 ...
해석(나비효과): ...
투자판단: Buy 강 — ...

## 📊 포트폴리오 추천
### 🇰🇷 국내주식
| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
...
### 🇺🇸 해외주식
...
```

---

## 환경변수 옵션

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `GEMINI_API_KEY` | (필수) | Google AI Studio API 키 |
| `RUN_MODE` | `scheduled` | `scheduled`, `rebalance`, `verify`, `full_verify`, `test` |
| `FETCH_DAYS` | 모드별 기본값 | RSS 조회 범위를 임시 조정할 때만 사용하는 일수 |
| `GEMINI_SUMMARY_MODEL` | `gemini-3.1-flash-lite` | 글별 요약과 원문 근거 후보 추출 모델 |
| `GEMINI_DECISION_MODEL` | `gemini-3.5-flash` | 구조화 포트폴리오 판단 모델 |
| `MER_DECISION_PROVIDER` | `gemini` | 판단 제공자: `gemini`, `chatgpt`. 글별 요약은 Gemini 유지 |
| `MER_DECISION_CONTEXT` | `baseline` | 기존 입력 `baseline`, 원문·현재 판단 중심 실험 입력 `focused` |
| `CHATGPT_DECISION_MODEL` | `gpt-6.1-sol` | 공식 로그인 계정의 모델 목록에서 선택 |
| `CHATGPT_AUTH_DIR` | `~/.config/mer-portfolio/chatgpt` | 저장소 외부의 보호된 인증 디렉터리 |
| `CHATGPT_ACCOUNT` | 저장된 선택 계정 | 여러 로그인 중 사용할 계정 레이블 |
| `ENABLE_POST_SUMMARIES` | 켜짐 | 신규 글별 1차 요약 API 호출 여부. Actions의 `verify`에서는 꺼지고 `full_verify`에서는 켜짐 |
| `OUTPUT_DIR` | `output` | 리포트 저장 경로 |

로컬 실행 예:
```bash
# 기본 실행
export GEMINI_API_KEY="AIza..."
python main.py

# 최근 14일 수집, 리밸런싱 모드
RUN_MODE=rebalance FETCH_DAYS=14 python main.py
```

---

## 모델 및 한도 운영 정책

기본 설정은 기존 Gemini·기존 문맥입니다. 선택적 연결은 [ChatGPT 로그인](docs/chatgpt-login.md),
원문 연결과 문맥 개선은 [개선 실험](docs/context-experiment.md)을 참고하세요.
운영을 바꾸지 않고 비교하려면 아래 명령을 사용합니다.

```bash
# API·Telegram 전송 없이 저장된 리밸런싱 입력 검사
python compare_decisions.py --dry-run --run-type rebalance

# Gemini 키와 ChatGPT 최초 인증을 준비한 후에만 실행
python compare_decisions.py --live --run-type rebalance --max-requests 3
```

실제 요청 상한에는 재시도와 교정 요청도 포함됩니다. 자세한 범위와 한계는
[동일 입력 모델 비교](docs/model-comparison.md), 구현 검증은
[2026-10-06 검증 기록](docs/validation/chatgpt-provider-20261006.md)을 참고하세요.
Gemini 실제 호출 결과와 수정 내용은 [실모델 검증 기록](docs/validation/gemini-live-20261006.md)에 있습니다.
ChatGPT 실계정 호출과 수익률 개선은 아직 검증하지 않았습니다.

- 신규 글 원문은 고정 글자 수로 자르지 않고 저장합니다.
- 신규 글은 모두 Flash로 1차 요약하고, 투자 관련 여부와 분류 이유를 함께 기록합니다.
- 글별 Flash 요약 응답이 깨졌거나 비어 있으면 해당 글만 분석 보류로 저장하고 다음 실행에서 재시도합니다.
- 분석 보류 글은 Telegram에 제목과 URL을 짧게 표시하고, HTML/Markdown에는 제목, URL, 날짜, 실패 사유를 남깁니다.
- 투자와 무관한 글은 DB에 저장하지만 3.5 Flash 투자 판단 입력에서는 제외합니다.
- 검증된 요약이 없는 글은 3.5 Flash에 원문 그대로 넘기지 않습니다.
- 저장 원문은 유지하며, Flash 요약 요청에서 모델 입력 한도의 `80%`를 넘는 비정상 요청에만 전송용 본문 끝부분을 줄입니다.
- Actions의 정상 `scheduled`, `rebalance`, `full_verify`에서는 글별 Flash 요약을 켜고, 반복 출력 검증용 `verify`에서는 Gemini를 호출하지 않습니다.
- 구조화 판단 JSON은 `gemini-3.5-flash`를 사용하고, Markdown 보고서는 검증된 JSON에서 결정론적으로 생성합니다.
- 400/403/404 모델 오류, 429 한도, 500/503/504 일시 장애를 구분하며 모든 판단 실패는 상태를 변경하지 않습니다.
- 일별 분석은 관련 신규 글만, `주간(7일)` 리밸런싱은 마지막 실제 리밸런싱 이후 누적 관련 글만 사용합니다.
  기준일이 없는 최초·수동 리밸런싱은 최근 `14일`의 원문·요약을 분석 근거로 사용합니다.
- 관련 신규 글이 없으면 LLM을 호출하지 않고 가격, 성과, 출력만 갱신합니다.
- HTML, PNG, Telegram, Markdown 보고서는 추가 LLM 호출 없이 현재 모델 포트폴리오 상태와 핵심 인사이트로 생성합니다.
- 투자 판단 실패 시 더 약한 모델로 조용히 대체하지 않고 기존 상태를 유지합니다.
- `gemini-3.1-flash-lite`는 원문 근거 추출에만 사용하고 최종 투자 판단은 `gemini-3.5-flash`가 담당합니다.
- rate limit 또는 quota 오류가 나면 모델별 호출 간격을 두고 재시도합니다.
- 1차 포트폴리오 판단에서 3.5 Flash가 한도 초과 또는 일시 장애로 실패하면 새 판단을 만들지 않고 기존 상태로 오늘 날짜 보고서, HTML, PNG, Telegram을 갱신합니다.
- API 키 미설정 같은 구성 오류는 실행 실패로 처리합니다.

기본 호출 간격:

| 모델 | 최소 간격 |
|------|-----------|
| `gemini-3.5-flash` | 8초 |
| `gemini-3.1-flash-lite` | 8초 |

---

## 로컬 개발 환경 셋업

```bash
# 패키지 설치
pip install -r requirements.txt

# 스크래핑만 테스트
python fetch_mer.py

# 분석만 테스트 (API 키 필요)
export GEMINI_API_KEY="AIza..."
python analyze.py

# 전체 실행
python main.py
```

---

## 주의사항

- 이 리포트는 **참고용**이며 투자 권유가 아닙니다
- 메르 블로그 분석을 AI가 재해석한 것으로, 실제 메르의 견해와 다를 수 있습니다
- 네이버 블로그 구조 변경 시 스크래핑이 일시 중단될 수 있습니다
  - 이 경우 RSS 요약본으로 자동 폴백됩니다
- Gemini 무료 모델이 업데이트되면 역할별 `GEMINI_SUMMARY_MODEL`, `GEMINI_DECISION_MODEL`로 조정하세요
