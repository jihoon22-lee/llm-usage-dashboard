# 클라우드 사용량 대조와 telemetry

사용량은 요청 기록과 서버 누계를 같은 범위에서 대조합니다. 보완분은 요청 이벤트와 별도 저장되며
요청 수를 만들지 않습니다. 뒤늦게 상세 기록이 들어오면 보완분이 줄어듭니다. 기존 한도·계정 조회와
5분 수집은 유지합니다. 계정 차이는 dot만의 사용량을 의미하지 않습니다.

## 한국시간과 합계

- 화면 날짜는 Asia/Seoul의 00:00–24:00입니다.
- 누계의 전체 발생 구간이 선택 기간 안에 있으면 기간 합계에 포함합니다. 일별 구간을 넘으면
  날짜 미배분량을 함께 표시하며 임의의 자정이나 시간 비율로 나누지 않습니다.
- 선택 기간 경계에 걸치는 구간은 그 기간 합계에 포함하지 않고 별도 표시합니다.
- Codex 원본 일별 구간은 KST 09:00–다음 날 09:00입니다. 진행 중인 날의 수신된 토큰은 조회 시각보다
  미래에 발생할 수 없으므로 조회 시각을 상한으로 사용합니다. 이는 조회 시각에 토큰을 썼다는 뜻이 아닙니다.
- 입력·출력 구분이 없는 토큰은 `unclassified_tokens`로 보존합니다. `total_tokens`는 이 값을 포함하며,
  비용·요청당 평균은 세부 기록으로 확인한 부분만 계산합니다. reasoning은 출력에 다시 더하지 않습니다.

`GET /api/usage`의 기존 필드를 유지하며 보완이 있을 때 총량에 `total_tokens`, `recorded_tokens`,
`supplemental_tokens`, `unclassified_tokens`를 추가합니다. `reconciliation`은 날짜 미배분 구간,
기간 경계와 겹치는 구간, 누계 충돌과 집계 기준을 제공합니다. 보완이 없는 기존 응답에서는 생략될 수 있습니다.
CSV의 날짜 미배분 행은 시각별 요청이 아닙니다. 세션·프로젝트 상세에도 시간 미배분 구간을 제공합니다.

## Claude 클라우드

기존 teleport 기록에 일반 세션 이벤트를 보완합니다. `parent_tool_use_id`로 전달된 하위 에이전트도
식별하고 메시지 ID를 기준으로 로컬·클라우드 복사본을 합칩니다. 일반 이벤트의 출력 토큰은 임시값일 수
있으므로 최종 `modelUsage` 누계로 대조합니다. 같은 누계는 반복 합산하지 않으며 감소·상세 초과는
충돌로 표시합니다. 다른 session ID로 시작한 실행은 별도 구간입니다. 같은 ID 안에서 초기화의 원인을
확인할 수 없는 경우 수치를 임의로 복원하지 않습니다. transcript와 일반 이벤트가 같은 `result`를 돌려주면
읽은 계정의 키로 한 누계만 저장해 한 번만 대조합니다([Claude 계정 구분](architecture.md#claude-계정-구분)).

새 파서 체크포인트로 과거 세션을 재수집합니다. transcript와 일반 이벤트 모두 20페이지 단위로
체크포인트를 저장해 이어받으며 남은 cursor가 있으면 미완료로 표시합니다. 전체 이벤트 조회 실패가 기존 transcript 수집을
롤백하지 않습니다. 원격 페이지를 읽는 동안 DB 쓰기 트랜잭션을 열어 두지 않습니다.
추가 `events` API가 404이면 성공한 transcript 체크포인트를 보존하고 `claude-cloud-supplement`에서
미제공 상태를 따로 표시합니다. 해당 세션이 바뀌거나 24시간이 지나면 다시 확인합니다.

## Codex 계정 분리와 독립 수집

계정 일별 합계는 계정 식별값의 해시 또는 미확인 계정의 관측 epoch별로 저장합니다. 계정 전환 뒤
빈 목록이나 일부 날짜만 받아도 이전 계정의 합계를 현재 합계에 섞지 않습니다. 구버전의 계정 미귀속
합계와 원래 테이블은 보존하지만 새 계정으로 소급 배정하지 않습니다. 새 계정별 테이블은 별도로 추가합니다. 과거 로컬 요청의 계정 귀속은 여전히 미확인입니다.

한도와 토큰 통계는 같은 App Server에서 각각 받으며 성공·실패를 독립 기록합니다. 한도 RPC 오류나
형식 오류가 있어도 받은 토큰 통계를 저장합니다. `account/read`는 강제 인증 갱신 없이 계정 범위를
확인하는 데 사용하며 이메일·인증키는 저장하지 않습니다. 계정 식별값이 없으면 미확인 epoch로 구분합니다.

## 원본 대조와 과거 재처리

로컬 파서와 Claude 클라우드 파서 버전이 바뀌면 남아 있는 과거 기록을 다시 읽습니다. 같은 요청 ID와 advisor 위치는 한 번만 집계하고, 원본 세부값은 전체 토큰에 다시 더하지 않습니다. 긴 로컬 재처리 중에는 커밋된 파일 사이에서 heartbeat를 갱신하되 수집 상태는 `collecting`으로 유지합니다.

`tests/verify_record_totals.py`와 일일 검사는 Codex 요청별 JSONL·zstd, Claude 부모·advisor·thinking·TTL, OpenCode의 원본을 수집 파서와 별도로 읽습니다. 기존 Codex 누계, Antigravity와 Devin 대조도 유지합니다. 파일 체크포인트 이전과 DB의 수집 완료 시각까지만 비교하므로 아직 쓰는 중인 파일이나 갱신된 메시지는 수집 대기로 구분합니다.

검사 결과는 일치·부분 확인·실패로 나뉩니다. 원본이 있는데 DB에 없거나 숫자가 부족하면 실패합니다. 잘린/깨진 원본, 합계와 맞지 않는 TTL, 상충하는 TTL, 현재 파일보다 큰 과거 최대값과 새 기록 대기는 부분 확인입니다. 원본이 사라졌다는 이유로 보존값을 줄이거나 누락량을 0으로 채우지 않습니다. 검사 범위 버전이 바뀌면 다음 수집에서 새 검사를 수행합니다.

## 선택적 로컬 telemetry 입력

수신기는 별도 명령이며 설치·배포만으로 자동 시작하거나 CLI 설정을 변경하지 않습니다.

```sh
llm-usage telemetry --port 4318 --token-file /absolute/private/telemetry-token
```

토큰 파일에는 32자 이상의 별도 난수를 넣고 권한을 0600으로 설정합니다. 제공사 OAuth 토큰이나
대시보드 세션 키를 재사용하지 않습니다. 수신기는 `127.0.0.1`에만 바인딩하며 `/v1/logs`에서
Bearer 인증된 OTLP/HTTP JSON만 받습니다. 요청 본문은 2 MiB로 제한합니다.

직접 실행하는 Claude의 사용자 환경에서 다음 값을 설정합니다. 기존 telemetry 목적지가 있다면
먼저 그 설정을 보존하세요. 프로젝트 설정 파일에는 exporter 설정을 넣지 않습니다.

```sh
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
# 별도 수신기 키를 Authorization=Bearer <키> 형식으로 OTEL_EXPORTER_OTLP_HEADERS에 설정
export OTEL_LOG_USER_PROMPTS=0
export OTEL_LOG_ASSISTANT_RESPONSES=0
export OTEL_LOG_TOOL_DETAILS=0
export OTEL_LOG_RAW_API_BODIES=0
```

수신기는 Claude `api_request`와 Codex `sse_event`의 `response.completed` 숫자만 해석합니다.
토큰 필드·요청 식별자가 없는 이벤트는 정확한 사용량으로 만들지 않습니다. 요청 ID를 transcript의
메시지 ID에 연결할 수 없으면 숫자 관측으로 보관하고 합계에 중복 추가하지 않습니다. 뒤에 파일·클라우드
기록에서 연결 정보가 들어오면 반영합니다. Codex는 `token_usage_record.response_id`와 연결합니다.
연결된 누계 이벤트와 요청별 원본을 한 번만 집계하며, 이전 누계만 남은 구간은 보존합니다.
수신기·요청 연결 fixture와 실제 CLI exporter 연결은 다른 검증입니다. [수집 범위 검토와 보완](../workthrough/2026-10-23-collection-coverage.md)를 참고하세요.

`GET /api/collection`의 `telemetry`와 상태 탭의 telemetry 수신 상태에서 마지막 수신과 연결 대기 건수를
확인할 수 있습니다. 로컬 기록에서 뒤늦게 요청이 연결되면 대기 건수·상태를 갱신하되 마지막 실제
수신 시각을 새로 만들지 않습니다. 수신기 테스트 성공은 실제 CLI에서 토큰을 받았다는 뜻이 아닙니다.

Claude는 `user.account_uuid`와 `organization.id`가 모두 있는 경우만 해시한 계정 근거로 사용합니다.
세션 시각보다 요청 ID 연결을 우선하며 부모와 advisor에 함께 적용합니다. 이메일과 `user.id`는 저장하거나
구독 계정 식별자로 사용하지 않습니다. 같은 요청의 계정이 충돌하면 기존 숫자를 덮어쓰지 않습니다.
Codex telemetry와 요청별 원본의 숫자가 다르면 대기·상충 상태로 남기며 두 값을 합치거나 최댓값으로 올리지 않습니다.
Codex의 native 계정 조회와 `creator_account_id`가 같은 식별자인 경우 해시로 연결합니다. 다른 계정으로 입증된
기록은 현재 계정 누계에서 차감하지 않지만, 계정 미제공이나 여러 workspace가 공유하는 식별자는 계속 미확인입니다.

Claude 클라우드 환경의 localhost는 대시보드 PC가 아닙니다. 직접 전송에는 별도의 도달 가능한 경로와
안전한 인증 전달이 필요합니다. 현재 기본 구성에는 외부 중계나 공개 수신기를 설치하지 않으며,
연결되지 않은 환경은 기존 5분 클라우드 수집으로 계속 동작합니다.

## 지원·정책·데이터 범위

- 내부 OAuth·세션 API는 공식적인 외부 사용량 API로 확인되지 않았습니다. 기존 본인 계정의 읽기 전용
  경로를 유지하지만 정책상 허용이나 장기 호환성을 보장하지 않습니다. 조회 간격을 늘리는 것만으로
  인증 방식의 지원 여부가 달라지지 않습니다.
- [공식 인증 안내](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use),
  [telemetry 안내](https://code.claude.com/docs/en/monitoring-usage),
  [SDK 누계 범위](https://code.claude.com/docs/en/agent-sdk/cost-tracking)를 참고합니다.
- 과거 로컬 기록에는 계정 귀속이 없는 경우가 있습니다. Codex 비교는 동일 route와 시간 범위의 대조이며,
  모든 과거 요청이 현재 계정에 속한다는 보증이나 청구 금액 검증은 아닙니다.
- 서버가 누계를 제공하지 않거나 기록이 삭제된 경우, 수치 충돌과 원본 미제공을 구분해 남깁니다.
  제공사 인증키·대화 본문·도구 인수는 통계 DB에 저장하지 않습니다.

주간 리포트는 완료된 주의 요청 기록·누계 보완·단가가 바뀌면 다시 계산하며, 기존 리포트 알림을
재전송하지 않습니다. 프로젝트의 상위 세션은 보완량을 합친 뒤 정렬합니다. 달력은 원래 요청 기록과
동일한 최근 조회 범위만 표시하며 그보다 오래된 사용량은 전체 기간 합계에 유지합니다.
