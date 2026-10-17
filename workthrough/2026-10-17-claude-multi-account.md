# Claude 다중 계정 구분과 구독 종료 설정

## 범위와 배경

- 기준: v0.6.2 이후 main `0e1f878`. paseo + zerosub로 Claude 계정이 하나 추가됐다(기존 Pro: 기본 홈 `~/.claude`, 신규 Max 5x: zerosub 관리 홈 `~/.paseo/zerosub/homes/claude-<id>`).
- zerosub는 계정마다 `CLAUDE_CONFIG_DIR` 홈을 만들고 `projects`·`settings.json` 등을 기본 홈에 심볼릭 링크로 공유한다. 계정 전환은 세션을 닫고 새 계정 환경으로 다시 연다(`paseo agent reload`). 턴 도중에는 옮기지 않는다.
- 기존 수집기는 기본 홈 인증만 조회했고, 공유 transcript의 신규 계정 요청을 기존 계정 사용량에 섞어 집계했다. transcript·사용량 기록에는 계정 식별값이 없다(원격 제어 기록의 `ownerAccountUuid`만 예외).
- 사용자 추가 요청: Devin 구독이 끝났으므로 웹에서 구독 종료를 설정해 한도 추적을 멈출 수 있어야 한다.

## 설계

- Claude Code는 한 서비스(route `claude-code`)로 유지하고 계정은 하위 차원으로 둔다. 기존에 식별된 계정 키(`resource_accounts['claude-code']`, 운영 DB 값과 일치 확인)는 `claude-code` 경로를 유지하고, 다른 계정은 `claude-code:<계정 키 12자>` 경로로 한도·자원·소스·구독료를 저장한다.
- 사용량은 events.account 열과 계정 포함 rollup(v2)으로 계정별 집계를 지원한다. 서비스·모델 보기는 계정을 합친 그대로다.
- 계정 근거: (1) llm-usage Claude Code 훅(SessionStart·UserPromptSubmit·Stop, 세션·시각·해시된 계정·홈 경로), (2) 각 홈의 Claude Code 프로세스 기록 `sessions/<pid>.json`(프로세스 시작·busy 전환). 정상 종료 시 이 파일이 지워지므로 수집기가 5초마다 관찰해 근거를 보관한다. 요청 시각 이전의 최신 근거가 계정을 정한다.
- 근거가 없으면: 한 계정만 쓰는 폴더는 그 계정, 공유 폴더는 공유 링크 생성 시각 전에는 원래 계정이고 이후에는 `claude-code:unknown`. 클라우드 세션은 읽은 계정으로 바로 배정한다.
- zerosub의 state.json·usage.json은 읽지 않는다. Claude Code 자체 파일과 이 프로젝트의 훅만 사용한다.
- 이메일·이름은 저장하지 않는다(SECURITY.md). 계정 이름은 요금제(Pro, Max 5x) 또는 설정에서 입력한 표시 이름이다.

## 구현

- `claude_accounts.py`: 홈 탐색(기본·설정·zerosub·훅 관측·Windows), 계정 키·요금제·토큰 만료만 읽는 inspect, 원래 계정 바인딩, 계정별 조회 컨텍스트, 계정 레지스트리와 표시 이름.
- `limits.py`·`resources.py`·`cloud.py`: 기존 단일 계정 함수를 계정 컨텍스트로 매개변수화했다(기존 호출 형식과 동작 유지). `poll_limits`가 계정마다 OAuth 사용량·선불 잔액·초기화권·클라우드 세션을 조회하고 Windows 인증값은 읽지 않는다.
- `claudehook.py`: 출력 없이 항상 0으로 끝나는 독립 훅. `install-hooks`가 기존 paseo 훅과 병합해 등록한다(중복 방지·백업).
- `claude_attribution.py`: 훅 수신함·프로세스 기록 수집, 폴더 정책, 순수 판정 함수, 대기·재판정 처리, 상태 행 `claude-accounts`.
- `statusline.py`: 기본 홈이 아닌 홈의 상태줄을 `claude-code@<홈 태그>.json`에 계정 키와 함께 기록해 계정끼리 덮어쓰지 않는다.
- store·reconciliation·webapp: 계정 경로의 한도 창 토큰·부모 창 차단·소스 대기, 구독 계정별 분류·Claude 계정 범위·계정별 구독 가치·세션별 계정, 계정 이름 설정 API.
- 모델별 주간 한도: 사용량 응답의 `limits` 목록에서 `weekly_scoped` 항목(모델 또는 surface 이름, 사용 비율)을 `seven_day_<이름>` 한도로 기록한다. 신규 Max 계정의 실제 응답에 Fable 주간 항목이 있음을 읽기 전용 조회로 구조만 확인했다(기존 Pro 계정에는 없음). 상위 필드와 같은 창은 중복 기록하지 않는다.
- 구독 종료: local.json `ended_subscriptions`. 조회·상태줄 수신을 건너뛰고 소스를 `ended`로 남기며, 한도 행을 ‘구독 종료’로 접고 다음 초기화·구독 가치·알림에서 뺀다. 해제하면 다음 수집에서 다시 조회한다.
- UI: Claude 카드 안 계정별 구획(이름·요금제·로그인 홈·마지막 확인), 모바일 목록의 계정별 잔여, `Claude(Max 5x) 5시간` 표기, 구독 계정별 분류·Claude 계정 범위, 세션 계정, 계정별 구독 가치, 설정의 구독 구획(구독료·구독 종료·표시 이름), 상태 소스 이름.

## 검증

- 독립 worktree에서 uv 0.12.23의 `uv sync --locked`로 환경을 준비했다. 운영 가상환경은 동기화하지 않았다.
- `.venv/bin/python -m unittest discover -s tests -q`: 409개 통과(기준 376개), Python 3.11 임시 환경에서도 통과. 신규: 구독 종료 8개, 시작 시 열 추가 경쟁 1개, 다중 계정 22개(모델별 주간 한도, 키 동일성, 홈 탐색, 계정별 조회·토큰 분리, 한도 창·부모 창, 판정 규칙, 폴더 정책, 훅·프로세스·미확인·늦은 근거·동시 프로세스·클라우드 사본, 단일 계정 회귀, 상태줄 분리, 사용량 분류·범위·구독 가치, 훅 설치 병합, 훅 무출력).
- `node --check`(전체 web JS), `node --test tests/js/*.test.mjs`: 31개 통과.
- 단일 계정 회귀: 변경 전·후 합성 fixture의 `/api/limits`·`/api/usage`·`/api/config` 응답을 시간 필드를 제외하고 비교했다. 기존 값 변화 없이 새 필드만 추가됐다.
- 운영 DB 사본(SQLite backup, 임시 폴더) + 실제 transcript·프로세스 기록 읽기 전용 실행(제공사 조회 없음): 마이그레이션·rollup 0.5초, 판정 0.6초, 대기 0건, rollup 일치. 기존 계정 27,413건, 신규 계정 159건, 미확인 39건(이 작업 세션의 첫 프로세스 기록이 남기 전 21건과 커밋마다 실행된 짧은 SDK 보안 리뷰 세션 12개). 두 계정 프로세스가 같은 세션을 연 사례에서 12:51–13:10 요청은 기존 계정의 원격 제어 프로세스(busy 근거)로 판정됐다.
- PR 자동 리뷰(Codex) 2회의 지적 3건 반영: 구독 종료로 건너뛴 상태줄 소스도 `ended`로 바꿔 이전 실패가 알림으로 남지 않게 했고, 계정별 선불 잔액·초기화권 소스에도 15분 조회 주기에 맞는 신선도 기준을 적용했다. 홈이 사라져 더 이상 탐색되지 않는 구독 종료 Claude 계정도 기존 소스를 `ended`로 바꾼다(없던 소스 행은 만들지 않음). 각각 수정 전 실패하는 회귀 테스트를 추가했다. 자동 리뷰는 사용자 요청에 따라 PR당 최대 3회로 제한한다.
- 브라우저: `tests/browser_claude_accounts.py`(합성 Flask API, PC 1440·모바일 390, CSP·JS 오류·가로 넘침 검사)를 추가하고 browser_ci에 포함했다. 화면은 스크린샷으로 확인했다.

## 남은 제약

- 훅은 Claude Code 프로세스 시작 시 적용되므로 설치 전부터 실행 중인 프로세스는 프로세스 기록으로만 구분한다. Python Agent SDK 등 사용자 설정을 읽지 않는 실행은 훅이 동작하지 않을 수 있어 프로세스 기록(5초 관찰)에 의존한다. 매우 짧게 끝나는 실행이나 수집기 중단 중의 실행은 ‘계정 미확인’으로 남는다.
- 같은 세션을 두 계정 프로세스가 동시에 진행하면 각 턴의 최신 근거로 판정하며, 근거 사이 간격이 겹치면 오판될 수 있다.
- 공유 폴더의 계정 연결 시각은 공유 링크 생성 시각으로 판단한다. 그 이전에 같은 홈에서 다른 계정으로 다시 로그인한 이력은 알 수 없어 원래 계정으로 본다.
- 계정 이름은 요금제 또는 사용자가 입력한 이름이다. 이메일을 보여 주지 않는다.
- 계정별 한도·자원 조회는 각 홈의 로그인 토큰이 유효할 때만 가능하다. 오래 쓰지 않은 계정은 ‘인증 확인 필요’로 표시하며 토큰을 갱신하지 않는다.

## 병합과 운영 배포

- 첫 PR #49는 전체 이력 Gitleaks가 테스트의 합성 경로 문자열(`claude-oauth:` + 12자리 hex)을 generic-api-key로 오탐해 실패했다. 허용 목록을 추가하지 않고 같은 변경을 낮은 엔트로피 합성 값으로 다시 만든 PR #50으로 대체했고, PR #49 브랜치는 전체 브랜치 검사에서 빠지도록 삭제했다.
- PR #50은 필수 CI(`ci-required`)와 자동 리뷰 2회(지적 3건 반영, 대화 모두 해결) 후 병합 커밋 `2f4e1a1`로 main에 병합했다.
- 배포 직전 운영 DB 복구용 백업 `~/.local/share/llm-usage/backups/pre-claude-accounts-20261017T225548.db.gz`를 만들었다(주기 백업 정리 대상이 아닌 이름, 보존). 외부 의존성·프로젝트 버전 변경이 없어 운영 `.venv`는 동기화하지 않았다.
- 2026-10-17 22:57 KST 기존 `packaging/deploy.py`로 릴리스 `2f4e1a1`을 전환했다. 배포기의 Python 409개·JavaScript 검사 통과, 웹·수집기 재시작과 새 heartbeat 확인 후 성공을 반환했다. 두 프로세스의 실제 cwd가 새 릴리스이고 health 200이다.
- 배포 후 운영 DB(읽기 전용 확인): rollup v2·events.account 적용, 대기 0건, Claude 요청 27,829건 = 계정별 합계(기존 계정 27,451 · 신규 계정 295 · 미확인 83). 원래 계정의 resource_accounts epoch·since가 배포 전과 같아 이력 연속성이 유지됐다. 미확인 83건은 첫 수집 전 이 작업 세션의 21건과 수집기 배포 전에 끝난 짧은 SDK 보안 리뷰 세션 27개다.
- 계정별 실조회: 기존 Pro 5시간 0%·주간 17%, 신규 Max 5x 5시간 45%·주간 97%·Fable 주간 100%가 각 계정 경로로 수신됐고 `claude-oauth`·`claude-credits`·`claude-resets`·`claude-cloud`와 `:<계정>` 소스가 정상이다. 신규 계정 클라우드 세션은 0개다.
- `.venv/bin/llm-usage install-hooks`로 계정 구분 훅을 연결했다. 기존 paseo 훅과 다른 설정은 그대로이며 SessionStart·UserPromptSubmit·Stop에 llm-usage 훅 그룹 1개씩만 추가됐고 설정 백업이 생성됐다. 상태줄 래퍼도 새 버전으로 복사됐다.
- 실제 사설 주소 읽기 전용 확인(PC 1440·모바일 390): Claude Code 카드 1개 안에 Pro·Max 5x 구획, 모바일 Claude 한 줄에 두 계정, `Claude(Pro) 5시간` 형식 초기화·알림, 구독 계정별 분류·Claude 계정 범위, 계정별 구독 가치(Max 5x 구독료 미설정), 상태의 `claude-accounts`를 확인했다. JS 오류 없음.
- 사용자 요청에 따라 설정 화면에서 Cognition / Devin을 구독 종료로 저장했다(저장 요청 1건만 허용). 한도 화면에서 ‘구독 종료’로 접히고 다음 초기화에서 빠졌으며, 다음 수집에서 `devin` 소스가 `ended`(설정에서 구독 종료로 표시 · 한도 조회 중지)로 바뀌었다.
- 훅 실제 실행: 이 작업을 진행한 paseo 에이전트 세션(SDK 모드)의 UserPromptSubmit가 신규 계정 키·zerosub 홈으로 기록됐다. 별도로 `ANTHROPIC_BASE_URL`을 닫힌 로컬 포트로 지정한 headless `claude -p` 두 번(기본 홈·zerosub 홈)에서 SessionStart·UserPromptSubmit가 각 계정 키로 기록됐다(API 호출 실패로 사용량 소모 없음, 테스트 transcript는 삭제). 수집기가 훅 기록 5건을 가져왔다.
- 커밋 때 실행되는 security-guidance 플러그인 리뷰는 Python Agent SDK를 `setting_sources=[]`로 실행해 사용자 설정 훅을 읽지 않는다. 이런 세션은 프로세스 기록(5초 관찰)으로만 구분된다.
- 상태의 `claude-accounts`는 전환 구간의 미확인 요청 때문에 최근 24시간 동안 ‘일부 미수집’으로 표시되며, 이후 새 요청은 훅·프로세스 기록으로 배정된다.
