# 클라우드에서 실행된 Claude·Codex 사용량 수집

## 요청과 범위

사용자 보고: 2026-10-12 하루 동안 ChatGPT 에이전트 dot으로 agent-browser 등 여러 작업을 했고,
Claude 클라우드 세션도 사용했다. 그런데 이 사용량이 대시보드에 반영되지 않았다.
시작 main은 `fa8b7a0`(0.4.0)이다. `feat/cloud-usage-sources` worktree에서 작업했고
운영 환경은 변경하지 않았다. 설치된 uv는 0.12.5이며 `uv sync --locked`는 성공했다.

## 원인 조사

- **Claude:** Remote Control(`bridge`) 세션은 이 PC에서 실행되어 `~/.claude/projects`에 기록되고,
  이미 수집되고 있었다(예: 이 작업 세션, 19:04 세션). 빠진 것은 `environment_kind=anthropic_cloud`
  세션뿐이다. 이 세션의 기록은 Anthropic 서버에만 있다. 10-12에는 18:23–20:04 KST에
  agent-browser 세션 1개가 있었고, 계정 전체로는 클라우드 세션이 5개다.
- **Codex/dot:** dot은 ChatGPT 메시징의 상시(durable) 에이전트(`cloud-aeon`)다. 모델 루프와 기록은
  OpenAI 서버에 있고, PC에서는 원격 환경(`ccarenv_…`)으로 명령만 실행한다. 그래서 커밋은 PC에 남지만
  다음 위치 어디에도 그날 낮의 스레드나 토큰 기록이 없었다: `~/.codex`, `~/.codex/sqlite`,
  Windows `.codex`, Codex 앱 패키지. app-server의 `thread/read`도 `thread not loaded`를 반환한다.
- Codex app-server `account/usage/read`는 계정 전체의 UTC 일별 토큰 합계를 준다. dot이 없던
  09-25~09-28에는 로컬 기록이 계정 합계의 0.98–0.99와 일치한다. dot을 쓰기 시작한 09-30부터
  격차가 생기며 10-11은 336.7M, 10-12는 75.7M이다(조회 시점 기준). WSL·Windows의 `auth.json`은
  dot과 같은 계정이다.

## 구현

- `llm_usage/cloud.py`
  - `claude --teleport`가 쓰는 읽기 전용 API로 클라우드 세션을 수집한다
    (`GET /v1/sessions`, `GET /v1/code/sessions/{id}/teleport-events`). 인증은 기존 OAuth 토큰에
    `x-organization-uuid`를 더해 쓰며, 리디렉션을 따라가지 않는다.
  - `anthropic_cloud` 세션만 읽는다. assistant 메시지의 usage만 `identity('claude-code', msg_id)`로
    upsert하므로 로컬 transcript와 겹쳐도 한 번만 집계된다. 본문은 메모리에서 버린다.
  - 세션 `updated_at`이 바뀔 때만 다시 읽는다. 세션별 실패는 해당 트랜잭션만 롤백하고 다음 주기에
    재시도한다(`partial`). 상태는 `claude-cloud` source 행에 남긴다.
- Codex
  - `read_codex`가 같은 app-server 프로세스에서 `account/usage/read`도 함께 요청한다. 응답은
    최대 10초까지 기다리며, 응답이 없어도 한도 값은 그대로 반환한다.
  - 일별 합계는 별도 `account_tokens` 테이블에 저장한다. 모델·입출력 구분이 없으므로 events·가격·롤업·
    원본 대조에는 넣지 않는다. 기록이 실패해도 한도 기록은 유지된다(`codex-account-usage`).
  - `Store.account_gap`은 조회 시점에 계산한다: UTC 일별 (계정 합계 − 로컬 `codex` route 합계).
    `codex:<provider>` route는 제외한다. 음수는 0으로 표시하고 `over`로 따로 알린다.
- 웹: 상태 탭의 수집 범위 설명 아래에 "Codex 계정 합계 대비 로컬 미기록" 표(최근 7일)를 추가했다.
  `/api/usage`의 `account_gap`은 캐시 밖에서 매번 계산한다. 수집 범위 문구도 갱신했다.

## 검증

- 단위 테스트: `tests/test_cloud_usage.py` 9개를 추가했고 전체 테스트가 통과했다. 범위는 다음과 같다.
  - 클라우드 세션의 중복 제거와 하위 에이전트 처리, bridge 세션 제외, 본문과 토큰 비저장
  - 페이지 넘김, 변경 없는 세션 재조회 생략, 만료 인증, 부분 실패 후 재시도
  - 같은 프로세스에서의 계정 사용량 조회, 응답 없음 유예, 일별 격차 계산, poll 기록
- JS: `node --check` 전체, `node --test` 29개 통과. `tests/browser_smoke.py`도 통과했다. fixture에
  계정 합계를 넣어 표 3행이 그려지는지 확인한다.
- 실제 데이터(운영 DB의 임시 사본에 실제 계정 API로 수집, 운영 DB는 변경하지 않음):
  - Claude 클라우드 세션 5개에서 claude-code 이벤트 340건이 추가됐다. 10-12 클라우드 세션은 114건이며
    출력 176,796, 캐시 읽기 25.2M, 프로젝트 agent-browser로 들어왔다.
  - Codex 격차는 10-11 336.7M, 10-12 75.7M, 최근 13일 1.29B이다. 상태 탭의 데스크톱·모바일 화면도
    확인했다.

## 남은 제약

- dot·Codex 클라우드는 일별 총 토큰만 받을 수 있다. 시간대·모델·입출력·비용으로는 나눌 수 없고, UTC 날짜 기준이다.
  dot이 아닌 날에도 1–2% 차이가 남는다. 계정 합계는 수시로 갱신되지 않을 수 있다.
- Claude 클라우드 세션의 결과 이벤트에 있는 `modelUsage` 누계는 transcript 메시지 합보다 약 1.6배 크다
  (10-12 세션: 캐시 읽기 39.9M 대 25.2M, 출력 272K 대 177K). 원인은 확인하지 못했다.
  `usage.iterations` 합은 상위 필드와 정확히 같으므로 파싱 문제는 아니다. transcript에 메시지로 남지 않는
  호출(auto mode 분류기·advisor·compaction 등)일 가능성이 있다. bridge 세션의 서버 결과 이벤트는 신뢰 기기에서만
  읽을 수 있어, 로컬 transcript 수집에도 같은 차이가 있는지는 비교하지 못했다. 이번 변경은 로컬과 같은 기준
  (transcript 메시지)을 따른다.
- 이 PR 시점에는 운영 배포 전이었다. 배포 후 첫 수집에서 과거 클라우드 세션 전체를 백필하며, 실제 배포와 첫 수집 결과는 아래 ‘운영 배포’에 적었다.

## 병합과 0.5.0 릴리스

- 2026-10-12 사용자 지시로 PR #35를 merge commit `0d59543`으로 병합했다(필수 CI 전부 통과).
- 0.5.0 준비: pyproject·uv.lock 프로젝트 버전, CHANGELOG, README·설치·업데이트 안내를 갱신했다.
  외부 의존성은 바뀌지 않았다. 전체 단위 테스트 346개가 통과했다.
- 버전 PR #36에서 Codex 리뷰 봇이 README 상단의 현재 릴리스 링크가 v0.4.0으로 남은 것을 짚어 수정했다. 병합 commit은 `667d5c4`다.
- 메인 CI 첫 실행은 security 작업이 gitleaks 내려받기 504로 실패했다. 코드와 무관한 일시 오류였다.
  이 결과를 확인하기 전에 `v0.5.0` 태그를 push한 것은 절차 위반이었다. 실패 작업만 재실행해 메인 CI가 성공했다.
- Release 워크플로가 성공해 [v0.5.0](https://github.com/jihoon22-lee/llm-usage-dashboard/releases/tag/v0.5.0)을
  immutable 릴리스로 게시했다. wheel·sdist·release-manifest.json을 내려받아 SHA256SUMS와 대조했고 모두 일치했다.

## 운영 배포

- 사용자가 두 서비스를 정지했다. 기본 checkout을 `667d5c4`로 fast-forward한 뒤 기존 `.venv`에
  `uv sync --locked --no-dev`를 적용했다. 프로젝트 버전만 0.4.0에서 0.5.0으로 바뀌었고 외부 패키지는 같았다.
  단위 테스트 346개 통과 후 `packaging/deploy.py`로 릴리스를 전환하고 서비스를 시작했다.
- 두 프로세스 모두 `releases/667d5c4…`에서 실행된다. health는 200이고 새 수집 heartbeat를 확인했다.
- 운영 DB 첫 수집 결과: `claude-cloud`와 `codex-account-usage`가 모두 ok다.
  - 10-12 클라우드 세션 114건(출력 176,796)이 집계됐다.
  - 계정 일별 합계는 68일치가 들어왔다.
- 사설 주소의 index와 웹 자산에 새 상태 표(`account-gap`, `renderAccountGap`)가 포함된 것을 확인했다.
- 복구 기준: 이전 릴리스 `86ea63d`(0.4.0, 외부 의존성 동일)와 2026-10-12 11:11 DB 백업.
