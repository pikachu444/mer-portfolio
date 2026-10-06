# 선택적 ChatGPT 구독 로그인

기존 Gemini 글 요약과 기본 투자 판단은 유지한다. 이 연결은 투자 판단 제공자를
명시적으로 `chatgpt`로 선택할 때만 사용하며, 인증 실패 시 Gemini로 자동 전환하지 않는다.

## 최초 연결

브라우저를 열 수 있는 개인 PC 또는 지속 실행 서버에서 의존성을 설치한 뒤 실행한다.

```bash
python -m pip install -r requirements.txt
python chatgpt_auth.py login
python chatgpt_auth.py status
python chatgpt_auth.py models
```

로그인은 OpenAI 공식 페이지에서 수행한다. 계정과 작업공간을 선택하고 이 앱이
ChatGPT 구독 사용량을 이용하도록 허용한다. 출력된 모델 목록의 `slug`를
`CHATGPT_DECISION_MODEL`에 지정한다. 로그인은 기존 Codex의 인증 파일을 읽지 않는다.

여러 계정 또는 작업공간을 저장할 수 있다. `login --new`로 등록하고, `status`에 표시되는
계정 레이블을 아래와 같이 선택한다. 같은 이메일이더라도 등록별 자격 증명은 분리한다.

```bash
python chatgpt_auth.py login --new
python chatgpt_auth.py --account ACCOUNT_LABEL use
```

기본 인증 저장 위치는 `~/.config/mer-portfolio/chatgpt`다. 필요하면 저장소 외부의
`CHATGPT_AUTH_DIR`를 지정하고, 특정 저장 계정은 `CHATGPT_ACCOUNT`로 선택한다.
Linux/WSL에서는 디렉터리 0700·파일 0600을 적용한다. Windows에서 직접 실행한다면
인증 디렉터리에 현재 사용자만 접근하도록 Windows ACL을 설정한다.

인증 디렉터리와 토큰을 git, Actions Secrets, 로그, 아티팩트로 복사하지 않는다.
GitHub 기본 임시 실행 서버마다 로그인 파일을 업로드하는 대신, 이 앱을 로그인한
고정된 자체 실행 서버에서 실행한다. 현재 로그인 명령은 시스템 브라우저를 열어
127.0.0.1 콜백을 받는다. 브라우저가 없는 서버에서는 포트 포워딩만으로 실행할 수 없다.
최초 로그인은 브라우저가 있는 개인 PC 또는 브라우저 세션이 있는 자체 서버에서 수행한다.
서버를 새로 구성하면 새 호스트로 로그인하며 다른 호스트의
호스트 ID나 토큰을 복제하지 않는다.

## GitHub에서 비교 실행

추가한 `메르 판단 모델 비교 (운영 상태 유지)` 워크플로는 기본 `offline`이며 GitHub의
임시 Ubuntu 실행 서버에서 API 없이 입력을 검사한다. `live`는 main 브랜치에서만
`self-hosted`, `linux`, `mer-chatgpt` 레이블을 갖춘 고정 실행 서버에 배정한다.

1. 고정 Linux PC 또는 브라우저 세션이 있는 서버에 GitHub 자체 실행 서버를 등록하고 `mer-chatgpt` 레이블을 추가한다.
2. 실행 서버 서비스와 같은 OS 사용자로 이 저장소의 의존성을 설치하고 `python chatgpt_auth.py login`을 한 번 수행한다.
3. `python chatgpt_auth.py models`의 사용 가능한 모델 이름을 확인한다. 별도 인증 경로·계정이면 저장소 Actions 변수 `CHATGPT_AUTH_DIR`, `CHATGPT_ACCOUNT`에 경로와 레이블만 지정한다.
4. 워크플로를 main에서 수동 실행하고 `execution=live`, `run_type=rebalance`, 원하는 모델과 요청 상한을 선택한다. 결과는 `comparison.json` 아티팩트에서 확인한다.

기존 `GEMINI_API_KEY`는 Gemini 비교에만 사용한다. Telegram 토큰은 이 워크플로에
전달하지 않는다. 기존 매일 Gemini 운영 워크플로는 변경하지 않았다.
자체 서버 등록과 사용자의 최초 OAuth 승인은 코드 추가만으로 완료되지 않는다.

## 갱신과 연결 해제

액세스 토큰 만료 직전에 공식 토큰 엔드포인트로 갱신한다. 갱신은 프로세스 간 잠금으로
직렬화하며, 회전된 refresh token을 저장한 후 다음 요청을 보낸다. 로그아웃하면 로컬
토큰을 삭제하고, 다음 로그인을 위해 계정/클라이언트 등록과 호스트 ID를 보존한다.

```bash
python chatgpt_auth.py logout
```

원격 해제를 확인하지 못했다는 메시지가 나오면 ChatGPT 설정에서 앱 연결을 해제한다.
사용량과 앱 한도는 [ChatGPT 설정](https://chatgpt.com/#settings/usage)에서 확인한다.

## 검증 상태

OAuth 콜백·PKCE·서명 검증·계정 분리·회전 토큰 갱신과 Responses 스트림은 가짜 HTTP 및
서명 토큰으로 테스트한다. 실제 계정의 승인 및 실제 모델 비교는 인증 후 별도로
기록해야 한다. 오프라인 테스트 통과는 실계정 연결 성공이나 수익률 개선을 의미하지 않는다.

공식 구현 기준:

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
