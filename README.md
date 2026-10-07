# LLM Usage Dashboard

개인 LLM 도구의 **구독 한도와 토큰 사용량**을 한 화면에서 확인하는 자체 호스팅 대시보드입니다.
Linux/WSL에서 실행하며 Tailscale 소유자 계정으로만 접근합니다. 웹과 수집기는 독립 프로세스이고 Resource Guard는 필요하지 않습니다.

A self-hosted dashboard for LLM quotas and usage. Linux/WSL, private Tailscale access, local-first collection. Korean UI and documentation. Independent project; not an official provider product.

## 기능

- 한도·초기화·수집 상태, 모든 모델의 사용 추이·비용 추정·프로젝트/세션 분석
- 캐시·추론 구분, 스트리밍·재수집·Windows/WSL 사본 중복 제거
- 데스크톱·모바일, CSV, 제한된 오프라인 통계 사본, 선택적 외부 알림
- 없는 원본 값을 0이나 100%로 채우지 않고 미제공·실패·오래된 관측을 구분

## 지원 범위

| 도구 | 토큰 원본 | 한도 경로 | 제한 |
|---|---|---|---|
| Codex | 로컬 JSONL | 기존 CLI App Server | 불완전·모호한 누적 이력은 완전 복구 불가 |
| Claude Code | 로컬 JSONL | 기존 OAuth·상태줄 | OAuth는 외부 연동용 공개 API가 아님 |
| Antigravity | CLI/앱 SQLite | 상태줄·실행 중인 WSL 앱 | 내부 스키마/RPC 변경에 영향받음 |
| OpenCode Go | 로컬 SQLite | 계정 usage 경로 | 구독 계정의 실제 한도 수신 미검증 |
| Devin CLI | 로컬 SQLite | 기존 CLI 계정 경로 | 내부 API, Cloud 세션 미지원 |

[구조와 수집 경로](docs/architecture.md)에서 과거 실수신과 합성 검증을 구분합니다.
Windows는 선택한 프로필의 **기록 수집 대상**입니다. Windows/macOS 네이티브 서버 설치는 지원하지 않습니다.

![합성 데이터 대시보드](docs/assets/dashboard.png)

이미지는 개인 데이터가 없는 합성 예시입니다.

## 빠른 시작

Python 3.11+, uv 0.12.23, Git, systemd가 실행되는 Linux/WSL, 로그인된 Tailscale과 HTTPS Serve 환경이 필요합니다.
서버 경로·홈에는 ASCII 영문자·숫자·`_ . / -`만 사용할 수 있습니다. 먼저 [설치 안내](docs/setup.md)를 확인하세요.

```bash
git clone --branch main https://github.com/jihoon22-lee/llm-usage-dashboard.git
cd llm-usage-dashboard
uv sync --locked --no-dev
.venv/bin/llm-usage init
.venv/bin/llm-usage collect --once
sudo ./install.sh --owner "$USER"
```

현재 main의 uv 설치 절차입니다. 기존 v0.1.0 태그에는 uv.lock이 없으므로 해당 태그의 README를 따르세요.

수집은 로컬 기록을 읽고 기존 인증으로 계정 한도를 조회합니다. Windows와 상태줄 연결은 [명시적으로 선택](docs/setup.md)합니다.
설치기가 출력하는 개인 Tailscale 주소로 접속합니다. Funnel로 인터넷에 공개하지 않습니다.
Git checkout이 서비스 설치·업데이트의 공식 경로입니다. Release의 wheel은 CLI/웹 런타임이며 단독 서비스 설치기가 아닙니다.

## 문서

- [설치와 설정](docs/setup.md), [화면과 CLI](docs/usage.md)
- [구조·API·데이터 의미](docs/architecture.md), [운영·백업·복구](docs/operations.md)
- [개발·검증·기여](CONTRIBUTING.md), [보안·개인정보](SECURITY.md), [변경 기록](CHANGELOG.md)

비용은 API 단가로 환산한 추정이며 구독 청구액이 아닙니다. 내부 제공사 API는 변경될 수 있습니다.
본문을 저장하지 않아도 세션 ID·프로젝트명·사용 시각은 개인 정보가 될 수 있습니다. 실제 DB·화면·원본 로그를 공개 이슈에 올리지 마세요.
기존 DB의 경로형 프로젝트명은 자동 변환하지 않아 신규 이름과 분리되거나 예산 재설정이 필요할 수 있습니다.

[MIT 라이선스](LICENSE)입니다.
