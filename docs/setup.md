# 설치와 설정

## 준비와 설치

서버는 Python 3.11+, Git, uv 0.12.23, systemd, 로그인된 Tailscale이 있는 Linux/WSL에서 실행합니다.
WSL은 systemd를 활성화한 배포판을 사용합니다. Windows/macOS 네이티브 서버는 지원하지 않습니다.
설치 경로·사용자 홈·데이터 경로는 ASCII 영문자·숫자·`_ . / -`만 지원합니다. 공백·한글·systemd 특수문자는 거부합니다. 업데이트 검사에는 Node 24도 필요합니다.
Tailscale에서 HTTPS와 Serve를 준비하고 `tailscale status`로 본인 로그인을 확인합니다. tagged device는 개인 계정으로 초기화할 수 없습니다.
HTTPS 9444와 로컬 8766을 사용합니다. 설치기는 기존 Funnel·다른 프록시 충돌을 거부합니다.

[README의 v0.2.0 설치 명령](../README.md)을 따릅니다. v0.2.0은 uv.lock을 포함한 릴리스이며 원격 main은 후속 개발 변경을 포함할 수 있습니다. 릴리스 태그 clone은 detached HEAD이므로 `git switch -c main`으로 설치·업데이트용 로컬 main을 생성합니다.
가상환경은 checkout의 `.venv`에 만듭니다. wheel·소스 ZIP만 있는 환경은 Git 기반 서비스 업데이트를 지원하지 않습니다.
`init`은 설정을 생성하고 `collect --once`는 로컬 수집·계정 조회를 실행합니다.
설치기는 root로 시스템 유닛·Serve를 설정하지만 서비스는 지정한 일반 사용자로 실행합니다.
owner를 생략하면 유효한 비root SUDO_USER를 사용합니다. root 직접 실행에서는 일반 사용자 `--owner`를 명시합니다.
uv 설치는 [공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)에서 버전 0.12.23을 선택합니다. 프로젝트 환경은 `uv sync --locked --no-dev`로 재현합니다.
첫 설치는 checkout 실행이며 [릴리스 실행 전환](operations.md)으로 개발과 운영 코드를 분리할 수 있습니다.

## Windows와 상태줄

Windows 홈은 자동 탐색하지 않습니다. 선택한 프로필의 WSL 절대 경로를 명시하며 여러 홈은 인수를 반복합니다.

```bash
.venv/bin/llm-usage init --windows-home /mnt/c/Users/Example
.venv/bin/llm-usage install-hooks
.venv/bin/llm-usage install-hooks --windows-home /mnt/c/Users/Example
```

다른 드라이브도 `wslpath`로 변환 가능해야 합니다. Windows 상태줄은 Windows의 `python` 명령이 필요합니다.
기존 config가 있으면 init은 덮어쓰지 않습니다. 수집할 homes·sources·status_inboxes를 직접 검토해 변경하거나 원본 설정을 비공개 백업하고 새로 초기화합니다.
Windows 상태줄 쓰기는 init의 선택과 별도로 매 실행 명시해야 합니다. Linux 홈은 기본 연결 대상입니다.
상태줄은 기존 stdin/stdout/종료값을 유지하는 래퍼이며 기존 `.claude/settings.json`, `.gemini/antigravity-cli/settings.json`을 같은 위치의 `settings.json.llm-usage-backup-*`로 백업합니다.
원복은 도구 종료 후 올바른 백업의 statusLine을 복구합니다. 이후 다른 설정 변경이 있었다면 파일 전체를 덮어쓰지 마세요.
TUI가 아닌 앱·headless·원격 worker에서는 상태줄이 실행되지 않을 수 있습니다. 계정/앱 한도 조회와 구분합니다.

## 개인 설정

| 위치 | 내용 |
|---|---|
| `~/.config/llm-usage/config.json` | 신원·서명키·DB·원본 경로 |
| DB 폴더의 `local.json` | 웹 구독료·예산·알림·표시 설정 |
| DB 폴더의 `pricing.json` | 웹에서 수정한 단가 |
| `~/.local/share/llm-usage/usage.db` | 기본 통계 DB |

CLI/직접 웹 실행은 LLM_USAGE_CONFIG로 기본 설정 경로를 바꿀 수 있습니다. 시스템 설치기는 owner의 기본 config 경로를 사용합니다.
웹 수정 키는 local.json이 기본 설정을 덮어씁니다. 단가 우선순위는 내장 → config pricing → config 폴더 pricing.json → DB 폴더 pricing.json입니다.
웹 쓰기는 systemd 쓰기 허용 데이터 폴더에 저장합니다. [비밀·백업 취급](../SECURITY.md)을 따르세요.
사용하지 않는 제공사 경로는 추가할 필요가 없습니다. 인증 발급·갱신은 원래 도구에서 수행합니다.

Claude 추가 자원 조회는 선택한 인증 홈의 `.claude/.credentials.json`과 같은 홈의 `.claude.json`에 있는 기본 계정·조직 정보를 사용합니다. 기본 도구에서 로그인한 뒤 수집 상태를 확인하세요. 인증 파일을 비표준 경로로 지정했다면 기본 config의 `claude_account_file`에 해당 기본 계정 메타데이터 파일의 절대 경로를 별도로 지정할 수 있습니다. 파일 내용·인증값을 대시보드 설정이나 공개 로그에 붙이지 않습니다.

Codex·Claude 크레딧/초기화권은 원본이 제공하는 범위만 자동 조회합니다. API 전용 크레딧이나 자동 수신되지 않는 자원은 개요의 수동 기록으로 보완합니다. 최초 계정 확인 후 작업 전망의 관측 시간이 필요하며, 자세한 조건은 [화면 안내](usage.md)를 따릅니다.

## 제거

```bash
sudo systemctl disable --now llm-usage.service
sudo systemctl stop llm-usage-collector.service
sudo tailscale serve --https=9444 off
```

9444가 이 앱의 설정인지 먼저 확인하세요. 이 앱의 두 systemd 유닛과 `/etc/sudoers.d/llm-usage`만 검토해 제거하고 daemon-reload를 실행합니다.
상태줄 원복 후 checkout을 제거합니다. 설정·통계·복구 백업은 자동 삭제하지 않습니다. Resource Guard를 선택 설치했다면 해당 등록도 해제합니다.
