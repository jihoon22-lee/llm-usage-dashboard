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
아래 기존 배포기는 코드만 전환합니다. uv 의존성 변경을 포함한 업데이트에는 사용하지 마세요. 운영 `.venv`에 `uv sync`를 실행하면 실행 중인 패키지가 바뀝니다. 독립 후보 환경 준비와 코드·환경의 동시 전환이 필요합니다.

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

## uv 의존성을 포함한 배포 후보 준비

새 후보는 **최종 실행 경로에서** 환경을 생성합니다. venv를 나중에 이동하면 실행 파일의 절대 경로가 깨질 수 있습니다. 실제 서비스 소유자 계정으로 실행하며 기존 후보 경로는 덮어쓰지 않습니다. 준비 명령은 설정·DB·계정에 접근하거나 current 링크·서비스를 바꾸지 않습니다.

```bash
# 최신 main의 필수 CI가 성공한 커밋인지 먼저 확인합니다.
git status --short
git rev-parse HEAD
mkdir -p "$HOME/.local/lib/llm-usage/candidates"
chmod 700 "$HOME/.local/lib/llm-usage/candidates"
python3 packaging/prepare_deployment.py \
  --uv /absolute/path/to/verified/uv \
  --python /usr/bin/python3 \
  --destination "$HOME/.local/lib/llm-usage/candidates/$(git rev-parse HEAD)"
```

`candidate.json`에는 소스 커밋·파일 해시·lock 해시·설치된 패키지·Python 버전이 기록됩니다. uv 0.12.23 공식 배포 파일의 SHA256을 검증하고 실행 경로를 명시하세요. 후보 안의 `.venv/bin/python`으로 합성 Gunicorn transport 검사를 실행하고, main의 CI 결과를 함께 보존합니다. 후보 준비 성공은 운영 배포나 실제 제공사 인증 성공을 뜻하지 않습니다.

### 이후 활성화와 복구

이 단계는 서비스 재시작이 필요한 별도 운영 작업입니다. 준비만 요청받았다면 실행하지 않습니다. 먼저 현재 두 유닛의 전체 내용·drop-in·실행 경로·PID와 시작 시각을 비공개 백업하고, config/DB 백업 위치 및 후보 소유권을 확인합니다.

1. 두 서비스의 전용 `90-llm-candidate.conf` drop-in을 준비합니다. 각 `[Service]`에서 `WorkingDirectory`를 후보의 절대 경로로 설정하고, 빈 `ExecStart=` 뒤 새 명령을 넣습니다. 웹은 `CANDIDATE/.venv/bin/python -m gunicorn --no-control-socket --workers 1 --threads 4 --timeout 120 --umask 0077 --bind 127.0.0.1:8766 --bind unix:/run/llm-usage/http.sock llm_usage.webapp:create_app()`이고 수집기는 `CANDIDATE/.venv/bin/python -m llm_usage.cli collect`입니다. 기존 User·Environment·보안·데이터 쓰기 범위는 유지합니다.
2. 기존 동일 이름 drop-in이 있다면 먼저 백업합니다. 두 파일을 설치하고 `systemctl daemon-reload` 후 웹과 수집기를 재시작합니다. 두 유닛의 실제 ExecStart/WorkingDirectory가 후보인지, health가 성공하는지, 새 collector heartbeat와 제공사별 상태·사설 브라우저 인증이 정상인지 확인합니다.
3. 실패하면 새 drop-in을 제거하거나 이전 파일을 복원하고 daemon-reload·두 서비스 재시작을 수행합니다. 기존 코드와 기존 venv를 함께 사용함을 확인하고 health·heartbeat를 다시 검사합니다. 기존 venv와 릴리스는 복구 검증 전 삭제하지 않습니다. DB 변경이 있다면 별도의 데이터 복구 절차가 필요합니다.

후보 drop-in을 사용하는 동안 기존 `deploy.py`, `system_update.py`, `install.sh`로 업데이트하지 않습니다. 이후에도 새로운 후보를 만들고 두 drop-in을 함께 교체하는 절차를 사용합니다. current 링크는 이 방식에서 사용하지 않으므로 자동으로 전환하지 않습니다. 운영 자동 CD는 제공하지 않습니다.

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
