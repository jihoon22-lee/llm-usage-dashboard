# 운영·업데이트·복구

## 서비스 확인

```bash
systemctl status llm-usage.service llm-usage-collector.service
curl --fail http://127.0.0.1:8766/healthz
journalctl -u llm-usage.service -u llm-usage-collector.service --since '10 minutes ago'
```

웹 health만으로 수집 성공을 판단하지 않습니다. collector heartbeat와 소스별 상태를 확인합니다.
웹 stop/restart는 수집기에 전달됩니다. 수집기는 외부 종료 신호 후 재시작하지만 명시적인 systemctl stop 이후에는 시작하지 않습니다.

## 릴리스 실행과 업데이트

다음 최초 전환 명령은 실제 서비스와 릴리스 링크를 변경합니다. 깨끗한 Git main, Node24, 검증된 설정이 필요합니다.

```bash
.venv/bin/python packaging/deploy.py --prepare
sudo .venv/bin/python packaging/system_update.py --owner "$USER" --release
```

--prepare는 읽기 전용 명령이 아닙니다. 코드를 `~/.local/lib/llm-usage/releases/<commit>`으로 내보내고 current 링크로 선택합니다.
아래 기존 배포기는 코드만 전환합니다. uv 의존성 변경을 포함한 업데이트에는 사용하지 마세요. 운영 `.venv`에 `uv sync`를 실행하면 실행 중인 패키지가 바뀝니다. 임시 환경의 사전 검증과 실제 배포 시 코드·환경의 동시 전환이 필요합니다.

의존성이 동일한 코드 업데이트는 실제 존재하는 다음 태그를 명시합니다. 아래 v0.1.1은 명령 형태의 예시입니다.

```bash
git fetch origin --tags
git switch main
git merge --ff-only v0.1.1
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python packaging/deploy.py
```

개인 수정 때문에 fast-forward가 안 되면 강제로 초기화하지 않습니다. 변경을 보존하고 통합·검증하세요.

```bash
.venv/bin/python packaging/deploy.py --rollback
```

현재 배포기의 사전 DB 백업은 기본 데이터 경로만 지원합니다. config의 database를 변경했다면 별도 백업을 먼저 확보하세요.
현재 rollback은 코드 전환이며 **공유 .venv의 의존성과 DB까지 복구하지 않습니다.** 의존성 갱신 전에 버전·복구 방법을 확보하세요.
실제 브라우저 검사는 `--smoke --smoke-origin HTTPS_ORIGIN`을 함께 지정해야 합니다. 생략한 기본 브라우저 스크립트는 합성 데이터 검사입니다.
브라우저 smoke는 배포 이후 별도 검사이므로 실패가 자동 rollback으로 연결되지 않습니다. 복귀 재시작 결과도 health·heartbeat로 직접 확인해야 합니다.
공개 CI/릴리스는 운영 자동 배포가 아닙니다. [기여 안내](../CONTRIBUTING.md)의 합성 검사와 실제 사설 인증을 구분합니다.

## uv 전환의 배포 준비

개발 checkout과 `.venv`, 기존 `releases/<commit>` 및 `current` 운영 구조를 유지합니다. 별도 후보·Git 백업 디렉터리나 새 systemd drop-in을 만들지 않습니다. 소스와 의존성 변경은 Git 이력으로 관리합니다. 기존 운영 데이터 백업 기능은 소스 코드 버전 관리와 별개입니다.

배포 전에는 임시 checkout의 독립 환경에서 `uv sync --locked`와 테스트를 수행합니다. PR/main의 Python 3.11/3.14 × 고정/최소 Gunicorn, 실제 TCP/Unix transport, 브라우저, Git 설치 및 wheel 검사가 모두 성공해야 합니다. 검증에 사용한 임시 checkout과 가상환경은 종료 후 삭제합니다.

운영 `.venv`를 실행 중에 동기화하지 않습니다. 의존성 변경이 있는 실제 배포에서는 기존 두 서비스의 정지·환경 동기화(`uv sync --locked --no-dev`)·기존 릴리스 전환·재시작을 함께 계획해야 합니다. 이 작업은 서비스 중단 가능성이 있으므로 배포 준비와 구분합니다. 기존 배포기의 `--rollback`은 코드만 복구하며 의존성을 되돌리지 않습니다. 이전 Git 버전의 의존성 선언으로 환경을 복원하는 절차도 필요합니다. 이번 전환은 임시 환경의 동일 .venv 경로에서 이전 requirements를 `uv pip sync`로 설치하고, 새 lock의 `uv sync --locked --no-dev` 적용 후 이전 Git 선언으로 복원하는 검증을 통과했습니다. 운영 DB·계정 상태 복원이나 무중단 전환을 검증한 것은 아닙니다.

이번 uv 전환은 소스·의존성·CI 및 사전 검증까지입니다. 운영 환경 동기화, 릴리스 링크 전환, systemd 수정·재시작과 실제 계정 수신 검증은 포함하지 않습니다.

## 백업·복구

수집기는 SQLite 온라인 백업과 pricing/local 파일을 데이터 폴더 backups에 만들고 최근 7일·월요일 4주를 보관합니다.
local.json 백업에도 알림 비밀이 들어갑니다. 기본 config의 서명키·원본 설정은 별도로 안전하게 백업합니다.
복구 시 두 서비스를 정지하고 현재 DB·WAL/SHM·설정을 보존합니다. 압축 백업을 새 파일로 풀고 PRAGMA quick_check를 확인한 뒤 소유권·권한을 맞춰 교체합니다.
과거 WAL/SHM을 새 DB에 섞지 마세요. 같은 시각의 단가·설정을 선택적으로 복원하고 서비스 시작 후 health·heartbeat·합계를 확인합니다.

```bash
.venv/bin/python tests/verify_record_totals.py --database /path/to/private-copy.db
```

원본 검증은 읽기 전용 사본에 수행할 수 있습니다. Codex는 저장된 누적 관측, Antigravity/Devin은 접근 가능한 원본을 대조합니다. Claude/OpenCode 원본 전체 대조는 아닙니다.
개인 DB·로그·대화는 공개 CI나 이슈에 올리지 않습니다.

## 문제 해결

| 증상 | 확인 |
|---|---|
| 403 | Tailscale 소유자, Serve Unix socket, origin/allowed_logins |
| 웹 정상·수집 오래됨 | collector·heartbeat·원본 경로·읽기 권한 |
| 한도 없음 | 구독·인증·앱 실행·TUI/headless 차이 |
| 수동 갱신 뒤 동일 값 | 소스 오류·제공사 캐시·재시도 대기·완료 ID |
| 단가 불일치 | DB 폴더 pricing이 config 폴더보다 우선함 |
| 오프라인 통계 잔존 | 로컬 사본 삭제·사이트 데이터 삭제 |
| 프로젝트 분리 | 이전 경로형 이름과 신규 이름·예산 매핑 |

실제 계정 수신·운영 적용은 합성 테스트와 별도로 확인하고 기록합니다.
