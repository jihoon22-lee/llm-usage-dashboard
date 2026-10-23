# 0.7.1 배포 후 수집 범위 최종 검토

## 범위와 방법

- 기준: main `e98e65b`, 운영 코드 0.7.1. 이번 작업은 수집 누락·기존 동작 변화·추가 수집 가능성의 검토이며 수집기 구현이나 운영 설정 변경은 포함하지 않는다.
- 독립 worktree에서 uv 0.12.23의 `uv sync --locked`로 검증 환경을 준비했다. 운영 DB·원본 파일은 읽기 전용으로 조회하고 재수집 재현은 임시 DB에서 수행했다.
- 다섯 단계로 확인했다: 설정과 실제 경로 목록 → 원본 요청 ID·숫자 대조 → 설치 버전과 공식 형식 확인 → 격리된 재현 → 배포 직전 백업과 비교.
- 설치된 Codex CLI 0.163.0, Claude Code 2.1.308과 실제 보존 원본을 기준으로 했다. 공개 기록에는 개인 경로·계정·요청 ID·실제 사용량을 넣지 않고 원인, 합성 재현과 검증 범위를 기록한다.

## 확인한 문제

### P1: Codex 요청별 원본이 있어도 누계 기준선에서 빠진 소비를 복구하지 못함

- [Codex 파서](../llm_usage/collect.py)는 `event_msg/token_count`만 처리하고 `token_usage_record`의 요청별 `response_id`·`usage`는 읽지 않는다. [누계 정규화](../llm_usage/store.py)는 반복된 누계를 건너뛰고 마지막 요청 성분이 0인 관측을 기준선으로 처리한다.
- 최근 실제 원본에는 두 종류가 함께 있다. 일부 과거 구간은 `token_usage_record`에 양수 요청 사용량이 있지만 뒤따르는 누계가 이전 기준선으로 돌아가 있다. 마지막 요청이 수집 시각보다 10분 이상 앞선 세션에서 요청 ID별 합계가 현재 로컬 이벤트 합계보다 큰 사례를 확인했다. 다른 세션은 같거나 보존 DB가 더 커 일괄 대체할 수 없다. 원본의 `session_id`와 파일 소유 thread ID가 다른 경우도 있어 파일의 첫 `session_meta.id`와 상속 이력 조건으로 범위를 맞췄다.
- 합성 재현: 요청별 원본이 120·350·12토큰이고 두 번째 요청 뒤 누계가 첫 번째 기준선으로 돌아가면, 현재 결과는 2요청·132토큰이다. 실제 요청별 합계는 3요청·482토큰이다. 새 원본 필드를 무시하므로 더 강한 근거가 있어도 복구하지 못한다.
- 누계만 있는 과거 이력에서는 0 기준선을 임의의 소비로 보지 않는 현재 원칙이 필요하다. 요청별 원본이 있는 경우에 한해 이를 우선하고 기존 누계 이벤트와 연결해야 한다. 두 경로를 단순히 더하면 중복이다.
- 이 차이는 로컬 세션·모델·프로젝트 귀속의 문제다. 계정 일별 누계 차이가 별도로 보완될 수 있어 같은 양을 대시보드 전체의 순누락으로 단정하지 않는다. 후속 작업은 원본 요청 ID 중복 제거, 부모/자식 이력과 누계 기준선, 계정 누계 보완량의 동시 조정이 필요하다.

### P1: Claude advisor 호출 사용량 누락

- [로컬 파서](../llm_usage/collect.py)와 [클라우드 메시지 파서](../llm_usage/cloud.py)는 최상위 `usage`만 읽고 `usage.iterations`의 `advisor_message`를 처리하지 않는다.
- 실제 로컬 원본에서 advisor 숫자가 존재하며, 해당 부모 요청은 클라우드 누계로 보완되는 범위에도 속하지 않았다. 따라서 이 경우 advisor 토큰·모델별 사용량·환산 비용이 빠진다.
- 합성 재현: 부모 입력 100·캐시 생성 30·출력 40, advisor 입력 300·출력 50인 메시지를 넣으면 현재 저장량은 170이다. 두 모델의 합계는 520이어야 한다. 로컬·클라우드 메시지 파서에서 같은 결과를 확인했다.
- [공식 advisor 사용량 설명](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool#usage-and-billing)은 최상위 값을 executor 사용량으로 정의하고 advisor를 별도 모델로 계산한다. `message` iteration을 다시 더하면 부모 사용량을 중복 계산하므로 advisor만 분리해야 한다.
- 후속 구현에는 부모 메시지와 iteration의 안정된 중복 제거, advisor 모델·계정·세션 귀속, 클라우드 누계와의 대조가 필요하다. 내부 호출을 기존 요청 수에 어떻게 표현할지도 명시해야 한다.

### P2: Claude reasoning의 실제 필드를 읽지 않음

- 같은 두 파서는 `reasoning_output_tokens`만 읽는다. 현재 원본에 있는 필드는 `usage.output_tokens_details.thinking_tokens`다.
- 실제 원본에 양수 thinking 값이 있지만 운영 DB의 Claude reasoning은 0이었다. 합성 메시지의 출력 40·thinking 25도 출력 40·reasoning 0으로 저장됐다.
- [공식 thinking 사용량 설명](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost#pricing)에 따르면 thinking은 이미 출력에 포함된다. 분류를 보완하되 총토큰이나 출력 비용에 다시 더하면 안 된다.
- 기존 체크포인트가 완료된 원본은 파서만 고쳐도 다시 읽지 않는다. 원본이 남은 범위의 명시적 재처리와 원본이 사라진 범위의 미확인 표시가 필요하다.

### P2: Claude 1시간 캐시 생성 구분 소실

- 원본의 `cache_creation.ephemeral_5m_input_tokens`와 `ephemeral_1h_input_tokens`를 저장하지 않고 `cache_creation_input_tokens` 합계만 보존한다. [비용 계산](../llm_usage/pricing.py)도 `cache_write` 단가 하나만 사용한다.
- 실제 원본에 1시간 캐시 생성량이 있다. 전체 생성 토큰 자체는 보존되지만 1시간 캐시를 사용하는 요청의 환산 비용과 TTL별 분석은 정확히 계산할 수 없다.
- [공식 캐시 설명](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)에서 두 TTL은 별도 가격이며 하위 두 값의 합이 상위 생성량이다. 하위 값을 전체 사용량에 추가하는 방식은 중복이다.
- 후속 구현은 합계와 TTL 분해를 함께 검증하고, 분해가 없는 과거 기록을 임의로 5분 캐시로 확정하지 않아야 한다. 스트리밍 관측 사이의 분해도 일관되게 선택해야 한다.

### P2: Codex 압축 기록을 탐색하지 않음

- `collect_local()`은 `*.jsonl`만 찾는다. 실제 Windows Codex 보존 경로에는 `.jsonl.zst`가 있다.
- 압축 파일만 있는 합성 폴더에 정상적인 120토큰 기록을 넣었지만 이벤트는 0개, source는 `ok`·“0개 기록 파일 확인”이었다. 압축된 이력을 처음 가져오는 설치에서는 조용히 누락될 수 있다.
- 현재 운영 DB의 복사본에 실제 압축 기록 전부를 스트리밍 해제해 기존 파서로 재생한 결과 추가 요청·토큰은 없었다. 이미 보존한 현재 이력이 삭제됐다는 증거는 없으며, 형식 지원과 신규 가져오기의 문제로 구분한다.
- 후속 구현은 스트리밍 압축 해제, 재시도 가능한 체크포인트, 압축 전후 사본 중복 제거, 읽지 못한 형식의 상태 표시를 함께 다뤄야 한다.

### P2: Codex telemetry는 요청 연결을 완료할 수 없음

- [수신기](../llm_usage/telemetry.py)는 Codex 숫자를 `telemetry_pending`에 넣고 `usage_aliases`를 기다린다. 현재 alias 생성 경로는 Claude에만 있다.
- 합성 `codex.sse_event` 한 건은 accepted 1·pending 1이었다. 같은 세션의 로컬 토큰 기록을 수집한 뒤에도 resolved 0·pending 1·Codex alias 0이었다.
- 운영에서는 telemetry 실수신 이력이 없으므로 이 경로로 실제 추가 수집이 완료됐다고 판단하지 않는다. 연결 없이 숫자를 합산하지 않는 현재 보수적 동작은 유지해야 한다.
- [공식 Codex telemetry 설명](https://learn.chatgpt.com/docs/config-file/config-advanced#what-gets-emitted)은 `response.completed` 토큰을 제공한다. 실제 native 기록의 요청 식별자와 연결하는 구현·실수신 검증이 별도로 필요하다.

## 기존 동작과 원본 대조

- 배포 직전 비공개 백업과 운영 DB를 비교했다. 기존 요청 ID 삭제, 기존 ID의 토큰 값 변화, Claude 계정 귀속 변화는 모두 없었다. 검토한 파서·탐색·telemetry의 위 제한은 0.7.0에도 존재했고 0.7.1 변경으로 발생한 회귀가 아니다.
- 구성된 Codex JSONL의 처리 완료 지점까지 있는 누계 관측은 저장된 누계 또는 명시적으로 제외한 상속 관측과 대응했다. 수집 이후 새로 작성된 부분은 수집 대기로 분리했다. 이 검사는 `token_usage_record`의 요청별 원본을 다루지 않으므로 위 기준선 차이를 검출하지 못한다.
- Claude의 현존 로컬 원본과 비교해 기존 최상위 입력·캐시·출력 값의 누락 또는 부족은 발견하지 않았다. 일부 과거 요청은 DB가 현재 원본보다 큰 출력 최대값을 보존하고 있었다. 배포 전에도 같았으며 사라진 과거 관측의 정답을 입증할 수 없어 줄이지 않았다. 이 검사는 위 advisor·reasoning·TTL 누락을 상쇄하지 않는다.
- OpenCode의 양수 원본 요청과 구성 토큰을 대조했다. 원본 `total`도 input + cache read/write + output + reasoning과 일치했다. reasoning을 출력에 포함하는 현재 변환은 이 원본 형식에 맞았다.
- `tests/verify_record_totals.py`가 통과했다. Codex는 완전한 누계·초기화·부분 시작을 구분하고 동일 시각의 모호한 누계는 검증 대상에서 제외한다. Antigravity·Devin은 보존 원본과 일치했다.
- 이 자동 검사는 Claude·OpenCode를 포함하지 않고 Codex도 이미 저장된 누계와 이벤트를 비교한다. 신규 원본 형식 전체의 수집을 보증하는 검사가 아니므로 검증 범위 확대가 필요하다.
- 잘린 JSON 마지막 줄, 원본이 삭제된 보존 이력, 계정 미확인 요청, 모델명이 없는 일부 Antigravity 요청은 그대로 구분했다. 누락된 원본을 0이나 정상으로 보정하지 않았다.

## 추가 수집 가능성과 현재 제약

- Claude SDK/headless 계정 귀속: 현재 훅·프로세스 근거가 없는 요청이 남는다. [공식 telemetry](https://code.claude.com/docs/en/monitoring-usage)는 세션·조직·계정 속성과 SDK 진입점을 제공하므로 필요한 식별값을 해시해 귀속 근거를 보완할 수 있다. 기존 수신기는 이 계정 근거를 연결하지 않으며 실제 exporter 연결도 별도 작업이다.
- Codex 원본의 `token_usage_record`에는 `response_id`와 요청별 숫자가 있고, 최근 `session_meta`에는 `creator_account_id`가 있다. 숫자 합계를 추가하기보다 요청 식별과 계정 귀속의 근거로 활용할 여지가 있다. 기존 누계·부모/자식 사본·계정 revision과의 대조가 먼저 필요하며 오래된 모든 기록을 소급 식별할 수 있는 것은 아니다.
- 추가 Claude 홈: 계정 자동 발견과 transcript 수집 경로 등록은 별개다. 별도 `projects` 폴더는 `sources`에 있어야 한다. 현재 추가 홈은 이미 수집하는 폴더의 심볼릭 링크였고, 발견한 미등록 Windows OpenCode DB에는 요청이 없었다.
- Claude 상태줄의 캐시 진단, telemetry의 요청 시간·재시도·오류·effort 등은 선택적으로 수집할 수 있다. 토큰 합계와 별도의 진단 값으로 설계해야 하며 대화 본문·도구 인수는 필요하지 않다.
- 현재 Codex 스레드 상세 RPC는 미제공 상태이며 계정 일별 합계는 별도로 수신 중이다. 원본에서 받지 않은 모델별·요청별 값을 계정 차이로 만들어서는 안 된다.

## 검증과 후속 작업

- 기존 검사: `test_collection_integrity.py` 14개, `test_cloud_usage.py` 28개, `test_reconciliation.py` 25개, 합계 67개 통과. 원본 대조 스크립트도 통과했다.
- 별도 합성 재현에서 Codex 요청별 원본 무시, advisor·thinking 누락, 압축 파일 무시, Codex telemetry 연결 대기를 확인했다. 기존 검사 통과와 이 미구현 동작을 구분한다.
- 권장 순서: Codex 요청별 원본과 누계 연결·Claude advisor 집계 → reasoning·캐시 TTL 보존 → 과거 원본 재처리와 검증 확대 → 압축 형식과 telemetry 연결 → 선택적 계정·진단 telemetry.
- 파서 수정 시 로컬·클라우드 재처리 버전을 올리고, 남아 있는 원본만 복구하며 기존 요청 ID·계정·롤업·누계 보완을 검증해야 한다. 변경된 숫자는 원본 보완과 기존 집계 보정을 구분해 기록한다.
- 이번 변경은 검토 문서와 알려진 제한의 설명뿐이다. 운영 코드·설정·인증·서비스·실제 DB를 수정하거나 새 릴리스를 게시하지 않는다. 개인 자료와 임시 재현 DB·스크립트·스키마 파일은 커밋하지 않는다.
