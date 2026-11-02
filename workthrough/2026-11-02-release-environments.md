# 릴리스별 실행 환경과 README 화면 갱신

## 요청과 결정

- 다음 작업 묶음으로 두 가지를 진행했다. 하나는 운영 실행 환경을 개발 checkout에서 분리하는 것이고, 다른 하나는 README 화면 이미지를 0.11.0 화면으로 바꾸는 것이다.
- 지금까지 두 유닛은 내보낸 릴리스(`current`)의 코드를 기본 checkout의 `.venv`로 실행했다. 그래서 세 가지 문제가 있었다.
  - 의존성이 바뀌는 릴리스는 두 서비스를 멈추고 공유 `.venv`를 직접 동기화해야 했다.
  - `--rollback`은 코드만 되돌렸다.
  - checkout에서 `uv run`·`uv sync`를 하면 운영 패키지가 바로 바뀌었다.
- Docker 대신 릴리스별 가상환경을 택했다.
  - 다른 서비스(웹·API·DB가 닫힌 앱)와 달리 이 앱의 수집기는 호스트에 붙어 있다. 호스트의 Codex CLI를 실행하고, `/proc`의 프로세스 정보로 계정과 실행 중인 앱을 찾는다. 여러 제공사의 로컬 기록·인증 파일도 홈 곳곳과 `/mnt/c`에서 읽는다.
  - 컨테이너에 넣으려면 홈 대부분, `/mnt/c`, 호스트 PID 공간을 열고 CLI를 이미지에 넣어야 한다. 그러면 지금 유닛의 `ProtectSystem=strict`·`ProtectHome=read-only`·빈 권한 집합보다 오히려 약해진다.
  - 필요한 효과는 코드·의존성 동시 전환, 의존성까지 포함한 롤백, 개발·운영 분리다. 검증된 배포기를 유지하면서 릴리스별 가상환경으로 얻을 수 있다.

## 변경

- `packaging/deploy.py`
  - 릴리스마다 `releases/<commit>/.venv`를 그 릴리스의 `uv.lock`으로 만든다.
    - uv 0.12.23(`uvx`)으로 `sync --locked --no-dev --no-install-project --compile-bytecode`를 실행한다.
    - 인터프리터는 기본 checkout `.venv`의 원래 Python이다.
    - 가상환경은 절대 경로를 기록하므로 내보내기를 마친 최종 폴더에서 만든다.
    - 서비스는 홈을 읽기 전용으로 보므로 바이트코드를 미리 컴파일한다.
  - gunicorn과 flask import를 확인한 뒤 마지막에 `ENVIRONMENT` 표식을 쓴다. 표식이 있는 환경은 다시 동기화하지 않는다. 실행 중인 환경일 수 있기 때문이다. 표식 없이 남은 환경은 지우고 다시 만든다.
  - 환경을 만들지 못하면 서비스와 current 링크를 바꾸지 않고 멈춘다.
  - 두 유닛이 `current/.venv`에서 실행되는지 확인해 동작을 나눈다.
    - **옮긴 설치:** 새 릴리스 환경으로 단위 테스트를 실행한다. 전환 전에는 실패 시 돌아갈 현재 릴리스에도 환경이 있는지 확인한다.
    - **아직 옮기지 않은 설치:** 이전처럼 checkout `.venv`로 검사하고 코드만 전환한다. 끝에 한 번만 실행할 전환 명령을 안내한다.
  - `--prepare`는 항상 환경을 만들고 그 환경으로 검사한다. 릴리스 모드에서 실행 중인 릴리스와 다른 커밋을 준비하면 경고한다. 서비스가 다음 재시작에 배포기의 확인 없이 그 코드를 실행하게 되기 때문이다.
  - `--rollback`은 옮긴 설치에서 이전 릴리스에 환경이 없으면, 그 릴리스의 lock으로 먼저 만든다.
- `packaging/system_update.py --release`
  - 유닛을 바꾸기 전에 릴리스·표식·`bin/python`·`bin/gunicorn` 파일이 있는지만 확인한다. root로 릴리스 코드를 실행하거나 import하지 않는다.
  - 웹 `ExecStart`는 실행 파일만 `current/.venv/bin/gunicorn`으로 바꾸고 인자를 그대로 둔다. gunicorn 실행이 아니면 바꾸지 않고 멈춘다.
  - 수집기는 `current/.venv/bin/python -m llm_usage.cli collect`로 실행한다. `--checkout`은 두 유닛을 checkout `.venv`로 되돌린다.
  - 재시작 뒤 웹 health를 확인하고, 15초 동안 같은 수집기 프로세스가 실행 중인지 확인한다. 자동 재시작 횟수가 재시작 때 초기화되는지에 기대지 않는다. 실패하면 이번에 바꾼 유닛 파일을 되돌리고 다시 시작한다.
  - 백업 폴더 이름은 같은 초에 실행해도 겹치지 않게 `<시각>-<무작위>`로 만든다.
- 문서: `docs/operations.md`에 다음을 적었다.
  - 릴리스 환경과 공유 환경을 확인하는 방법
  - 전환 명령과 업데이트·롤백의 차이
  - 이미 릴리스로 실행 중인 설치의 전환 순서: 일반 배포로 코드를 먼저 바꾸고, 실행 중인 릴리스에 환경을 만든 뒤 유닛만 옮긴다. 그래야 코드 변경은 배포기의 heartbeat 확인과 자동 rollback을 거치고, 유닛 단계의 복원이 전환 직전 상태로 완전히 돌아간다.
  - 환경 생성 실패의 확인 항목
- 테스트: `tests/test_release_environments.py`를 추가했다.
  - 가짜 uv로 동기화 인자·환경 변수·표식, 한 번만 실행, 실패·중단 뒤 정리를 확인한다.
  - 배포 순서(옮긴 설치·옮기지 않은 설치·`--prepare`·`--rollback`)를 확인한다.
  - 유닛 갱신(인자 보존, 미완성 환경 거부, `--release`와 `--checkout` 왕복, 확인 실패 시 복원)과 수집기 실행 판정을 확인한다.
  - uv 버전이 CI와 같은지 확인한다.

## 검증

- 독립 worktree와 uv 0.12.23의 잠긴 환경에서 Python 단위 테스트 588개를 통과했다.
- 임시 루트에서 배포기의 실제 함수로 릴리스 환경을 만들었다. 서비스와 운영 릴리스 폴더는 사용하지 않았다.
  - 현재 main 릴리스는 0.5초에 만들어졌다. `flask 3.1.3`, `gunicorn 26.2.0`, Python 3.14.4가 들어갔다.
  - Playwright·pip-audit·앱 패키지는 들어가지 않았다. `.pyc`는 200개였다.
  - 환경이 없는 운영 직전 릴리스 `5caab17`은 uv의 오프라인 모드로 캐시에서 0.3초에 만들어졌다. 롤백 대상 환경을 네트워크 없이 다시 만들 수 있다는 확인이다.
  - 표식이 생긴 뒤 다시 호출하면 동기화하지 않았다.
- 그 릴리스 폴더에서 다음을 실행했다.
  - 환경의 gunicorn: 합성 설정으로 `/healthz` 200을 받았다.
  - 운영 유닛과 같은 형태의 수집기 1회: 임시 HOME, Codex CLI 없는 PATH에서 종료 코드 0이었고 수집기 상태와 수집원 14개를 임시 DB에 기록했다.
- 배포기와 같은 방식으로 실행했다. 릴리스 환경의 python으로, 깨끗한 checkout에서 단위 테스트 588개가 모두 통과했다. gunicorn을 직접 띄우는 전송 테스트도 포함된다.
- 실제 운영 유닛 파일의 사본에 `--release` 변환을 적용했다. 두 유닛에서 `ExecStart` 한 줄씩만 바뀌었고 gunicorn 인자는 그대로였다.
- uv 캐시와 `~/.local/lib/llm-usage`는 같은 ext4에 있다. 같은 파일시스템의 worktree 환경 파일은 링크 수가 4였다. 운영 릴리스 환경도 캐시 파일을 하드링크로 공유한다. 임시 루트(tmpfs)에서는 복사로 대신했다.
- 검증 전후로 운영 `current`·`history`·`releases`가 같았고 두 서비스는 정상이었다.

## 운영 적용

- 아직 적용하지 않았다. 운영은 그대로 `5caab17` 코드를 공유 `.venv`로 실행한다.
- 적용하려면 사용자가 운영 배포를 요청해야 하고, 한 번은 대화형 sudo가 필요하다. sudoers 규칙은 재시작만 허용한다.
  1. 기본 checkout의 main을 이 변경이 들어간 커밋이나 릴리스 태그로 fast-forward한다.
  2. `.venv/bin/python packaging/deploy.py`로 코드만 일반 배포한다. 배포기의 health·heartbeat 확인과 자동 rollback을 거친다.
  3. `.venv/bin/python packaging/deploy.py --prepare`로 실행 중인 그 릴리스에 환경을 만들고 검사한다.
  4. `sudo .venv/bin/python packaging/system_update.py --owner "$USER" --release`로 유닛만 옮긴다. 실패하면 유닛이 복원되어 3단계 직후 상태로 돌아간다.
- 적용 뒤에는 두 프로세스의 실행 파일·작업 폴더, health, 새 heartbeat, 실제 인증 화면을 확인한다. 그다음 AGENTS.md의 운영 `.venv` 규칙을 실제 구조에 맞게 고친다.

## 알려진 제약

- 릴리스 환경의 Python은 기본 checkout `.venv`의 원래 Python을 따른다. Python을 바꾸려면 checkout `.venv`를 새 Python으로 만든 뒤 배포한다. 이전 릴리스는 자기 환경을 유지한다.
- 새 lock의 패키지가 uv 캐시에 없으면 환경을 만들 때 네트워크가 필요하다.
- 웹은 gunicorn 실행 파일의 shebang이 실제 `releases/<commit>` 경로를 가리켜 프로세스마다 자기 릴리스 환경에 고정된다. 수집기는 `current/.venv/bin/python`으로 시작해 site-packages 경로가 `current`를 거친다. 그래서 링크 전환과 재시작 사이 몇 초 동안의 지연 import만 새 릴리스의 패키지를 읽을 수 있다. 배포기는 전환 직후 바로 재시작한다.
- rollback은 DB를 복구하지 않는다. system_update.py의 자동 복원은 유닛 파일만 대상으로 하며 sudoers는 되돌리지 않는다. 그 확인은 새 수집 heartbeat까지 기다리지 않는다.

## 함께 처리한 README 화면

- [PR #102](https://github.com/jihoon22-lee/llm-usage-dashboard/pull/102)에서 README 이미지를 새 기본 화면인 개요 탭으로 바꿨다. merge commit은 `f6bd103`이다.
- 이미지는 `tests/browser_overview.py --artifacts DIR`이 합성 데이터로 만든다. 포커스·포인터 전 상태를 차트와 '눈에 띄는 점'까지 찍는다.
- 합성 기록이 모두 같은 시각이라 생기던 두 번째 '사용이 가장 많은 시간대' 항목은 빼고 찍었다.
- `resource-expiry.png`는 다시 만들어 비교했지만 화면이 바뀌지 않아 그대로 뒀다.
- 두 브라우저 검사가 통과했고 PR의 CI 13개도 모두 통과했다. 이미지에는 합성 값만 있다.

## 0.12.0 릴리스 준비

- 버전을 0.12.0으로 올리고 uv 0.12.23으로 lock의 프로젝트 버전만 갱신했다. 변경 기록, 설치 안내, 운영 안내(업데이트 대상, 0.12.0 업데이트와 되돌리기)를 갱신했다.
- v0.11.0 이후 `llm_usage/`는 그대로다. 바뀐 것은 배포 도구, 문서·테스트, README 화면, 개발 의존성(Playwright 1.64.1, setuptools 84.2.1)과 CI 작업이다. DB 구조 변경이나 저장 데이터 쓰기 추가가 없어 배포 직전 DB 백업 대상이 아니다.
- `uv export --locked --no-dev`의 런타임 패키지 9개 중 8개가 운영 `.venv`(Python 3.14.4)와 같은 버전이다. 나머지 `backports-zstd`는 Python 3.14 미만 전용이다. 그래서 1단계 일반 배포는 코드 전환으로 충분하다.
- 0.11.0 배포기는 릴리스 환경을 만들지 않는다. 그래서 옮긴 뒤에는 0.12.0 이상의 배포기를 쓰라고 운영 안내에 적었다.
