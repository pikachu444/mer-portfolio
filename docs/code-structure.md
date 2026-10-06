# 메르AI 포트폴리오 코드 구조

| 단계 | 주요 파일 | 책임 |
|---|---|---|
| 글 수집·요약 | `fetch_mer.py` | 메르 블로그 글 수집과 글별 요약 |
| 판단 | `analyze.py`, `system_prompt.py` | 투자 관련성, 핵심 논지, 직접 언급/AI 추론, 참고 종목 판단 |
| 제공자 선택·요청 계수 | `decision_providers.py`, `gemini_utils.py` | Gemini 기본값, 명시적 ChatGPT 선택, 실제 생성 요청 직전 예산 검사 |
| 선택적 구독 로그인 | `chatgpt_auth.py`, `chatgpt_client.py` | 공식 OAuth·계정 분리·회전 토큰 갱신, 모델 목록과 완료된 Responses 스트림 |
| 실험 문맥 | `decision_context.py` | 현재 판단·원문 부모 연결·미해결 위험 보존, 반복 이력 압축, 혼합 방향 검토 표시 |
| 상태 검증 | `portfolio_schema.py`, `portfolio_provenance.py` | 원문 근거와 현재 상태 보존 |
| 비중 보호 | `portfolio_allocator.py`, `portfolio_runtime.py` | 제안 비중의 종목별 상한과 잔여 현금 계산 |
| 성과 비교 | `track_returns.py`, `portfolio_metrics.py` | 원장·수익률과 KOSPI200/S&P500 비교 벤치마크 |
| 사용자 출력 | `portfolio_output.py`, `telegram_notify.py`, `generate_dashboard.py` | Telegram, HTML, Markdown 생성 |
| 동일 입력 비교 | `compare_decisions.py` | 캐시만 읽고 같은 스냅샷으로 세 경로 비교, 공통 검증, 요청·사용량·파일 불변 기록 |

`output/portfolio_state.json`은 현재 참고 포트폴리오 상태이고, `output/model_portfolio_ledger.json`은 과거 거래·NAV 이력을 보존한다. 광범위 지수 ETF 자동 편입 정책에서 제외된 기존 기록은 원장에 행정적 정책 제거 기록으로 남긴다.

`analyze_posts_structured`는 기본적으로 기존 Gemini·기존 문맥을 사용한다. `focused`에서만
출처·방향·재조정 보유 검토를 기존 1회 JSON 교정 단계에서 함께 검사한다. 새로운 비중
정책은 만들지 않으며 최종 운영 상태 검증도 유지한다.

`compare_decisions.py`는 `main.py`를 실행하지 않는다. 비교 결과는
`experiments/comparison-*/comparison.json`에만 저장하고 입력 스냅샷과 실제 운영
`output/`의 실행 전후 해시를 확인한다. 비교는 실가격 조회를 수행하지 않는다.
인증은 저장소 밖에 저장하며 비교 아티팩트에 포함하지 않는다.

기존 `schedule.yml`은 유지한다. `tests.yml`은 PR에서 자격 증명 없이 회귀 검사와
오프라인 비교를 실행한다. `model-comparison.yml`은 수동 실행이며 실모델 비교는
main 브랜치의 인증된 고정 자체 서버에서만 실행한다.

`gemini-comparison.yml`은 기존 키를 사용하는 별도 Gemini 실호출 비교다. 기능 브랜치의
해당 파일 변경과 수동 실행에서만 동작하며 임시 Ubuntu 실행 서버에서 캐시를 읽고
기존·개선 두 경로를 호출한다. ChatGPT 인증 서버가 없어도 실행할 수 있다.
