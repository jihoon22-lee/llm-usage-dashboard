# 구조·수집 경로·API

```mermaid
flowchart LR
 A[로컬 JSONL / SQLite] --> C[독립 수집기]
 B[상태줄 / 계정·앱 한도] --> C
 C --> D[(통계 SQLite)]
 T[Tailscale Serve] --> U[권한 제한 Unix socket]
 U --> W[Gunicorn / Flask]
 W --> D
 W --> V[브라우저]
 W --> R[수동 요청 ID]
 R --> C
```

## 역할과 집계

로컬 기록·계정 한도는 기본 5분, 상태줄 수신함은 2초 루프에서 확인합니다. 최초 원본 수집은 더 오래 걸릴 수 있습니다.
웹은 수집 요청 ID를 기록하고 수집기는 완료 ID와 소스별 결과를 갱신합니다. Resource Guard는 선택적 통합입니다.
collect/antigravity/devin/limits/statusline은 원본·한도, store는 이벤트·상태·한도·롤업, pricing은 단가 이력, webapp은 인증·조회·설정 API를 담당합니다.
resources는 자원·계정 관측과 수동 기록을 담당합니다. 브라우저 observations/resources 모듈은 quota/app보다 먼저 로드하며 관측 시계·오프라인 경과·자원 갱신을 유지합니다.
프런트엔드는 빌드 없는 JavaScript이며 theme, core·format·charts, 기능 모듈의 로딩 순서에 의존합니다.
provider(모델 제작사)·route(구독 경로)·model을 구분하고 요청/세션 식별자로 스트리밍·재수집·사본·부모 상속 중복을 제거합니다.
Codex 누적 카운터의 초기화·부분 시작·동일 시각 모호성은 별도 표시합니다. Antigravity의 상충 요청 수치는 한 관측을 유지하고 경고합니다.
events와 usage_hourly는 같은 트랜잭션의 트리거로 유지합니다. 정확한 시각 경계·세션은 이벤트를 사용합니다. [합계 규칙](usage.md)을 따릅니다.

## 수집과 검증 수준

아래 실제 수신은 공개 전 개발 과정의 확인 기록입니다. 공개 CI는 합성 원본을 사용하며 계정을 조회하지 않습니다.

| 경로 | 원본·한도 | 과거 확인 및 제약 |
|---|---|---|
| Codex | sessions/archived_sessions JSONL, account/rateLimits/read | 토큰·계정 한도·크레딧·초기화권 실수신 확인; 저장된 누적 관측 대조는 원본 전체 대조와 다름 |
| Claude | projects JSONL, 기존 OAuth usage·선불 잔액·초기화권·상태줄 | 읽기 실수신 확인; OAuth는 공개 외부 연동 API 보장 없음, headless 상태줄 제약 |
| OpenCode Go | opencode.db, Go usage 경로 | 토큰 수집; 당시 계정 구독 오류로 한도 실수신 미확인 |
| Antigravity | CLI/앱 conversations DB, 상태줄·WSL RPC | CLI 1.1.28/language server 2.19.1 원본 확인; 앱 실행과 내부 스키마 의존 |
| Devin CLI | sessions.db, 기존 CLI 계정 상태 경로 | 로컬 요청·일/주간 한도 실수신; Cloud 미지원, 당시 Windows DB 빈 스키마 |

Antigravity의 loopback RPC는 자체 서명 인증서를 사용하는 로컬 서버에만 연결하며 최소 TLS 1.2를 요구합니다.
Antigravity는 RetrieveUserQuotaSummary를 우선하고 GetUserStatus로 대체할 수 있습니다. 응답에 없는 창은 만들지 않습니다. 일부 생략된 proto3 float 0은 확인된 스키마 의미대로 처리합니다.
Claude는 정상 조회 최소 5분, 429는 10/20/40/60분과 더 긴 Retry-After, 네트워크 실패는 1/2/4/8/10분 대기를 적용합니다. 인증 오류는 원래 인증 파일 변경을 기다립니다.
Claude 선불 잔액과 초기화권 조회는 정상 15분 간격이며 각각 별도 대기 상태를 가집니다. 기본 계정 메타데이터 누락은 인증값 교체 없이도 재시도합니다. 0.2.0의 추가 자원 경로는 Codex CLI 0.162.0과 Claude Code 2.1.288의 응답으로 확인했으며 전역 CLI 버전을 강제로 고정하지 않습니다.
일반 사용량 응답의 `iguana_necktie`는 클라우드 세션 전용 프로모션으로 별도 저장합니다. 원본 달러 금액과 만료를 보존하고 범용 선불 잔액·지출 한도에 더하지 않습니다. 초기화권의 `ineligible_reason=surface`는 빈 grants가 있어도 미확인으로 처리합니다. 설치된 CLI 버전의 User-Agent와 클라이언트 플랫폼 헤더를 사용하며 내부 응답 형식에 의존합니다.
인증 발급·갱신은 기존 도구가 담당합니다. 상세 인증 경로를 공개 예제에 넣지 마세요.

## 자원·관측 데이터

기존 events·usage_hourly·limits·limit_history는 보존합니다. 추가 테이블은 resource_accounts(불투명 계정 식별·관측 구간), quota_context(한도 적용 범위), resource_items(자동/수동 자원·수정 버전), resource_history(수치 관측)입니다. 반복 초기화가 안전한 추가 테이블 방식이며 기존 사용량을 다시 쓰지 않습니다.

계정 식별자는 해시로 보존하고, 계정·구독 조건 변경 시 이전 구간으로부터 속도를 계산하지 않습니다. 원본 한도 ID·묶음의 의미를 유지하며 공유 크레딧을 모델마다 합산하지 않습니다. 최근 응답에서 빠진 이전 한도를 최신으로 남기지 않습니다. 작업 가능 여부나 서비스 추천을 제공하지 않습니다. 별도 계정 식별이 없는 로컬 수신은 계정 조회와 같은 신뢰 수준으로 승격하지 않습니다.

원본의 초기화·유효 시각만 사용합니다. 잔여 증가·초기화·수집 오류·긴 공백은 관측 구간을 끊습니다. 화면에는 최근 30분 안에서 연속 15분 이상 확인된 실제 감소량만 표시하며 미래 소진 시간을 환산하지 않습니다. 계정 전체의 소비 범위를 입증할 수 없는 로컬 토큰으로 남은 요청 수를 환산하지 않습니다.

지출 여유는 보유 잔액이 아닌 사용 조건입니다. 독립 자원 카드에서는 제외하고 같은 계정의 크레딧에 관련 조건을 연결합니다. 활성화 여부와 통화 일치 여부를 분리하며 금액은 단위가 같을 때만 연결합니다. 통화의 원본 소수 단위 또는 확인된 변환만 적용하며, 없는 통화·무제한 조건·만료를 추론하지 않습니다. 초기화권은 실제 사용 전 잔여율에 더하지 않습니다. 수동 기록은 계정/적용 범위·확인 시각·수정 버전과 함께 보존합니다. 독립 API 기록은 구독 경로의 추가 자원이 아닙니다.

## Claude 계정 구분

Claude Code는 계정마다 설정 홈(`CLAUDE_CONFIG_DIR`, 기본 `~/.claude`)을 쓰지만 zerosub처럼 여러 홈이 `projects`를 심볼릭 링크로 공유하면 transcript에는 계정이 남지 않습니다. 계정은 하나의 서비스(route `claude-code`) 안의 하위 차원입니다.

- 계정 키: 각 홈의 `.claude.json` oauthAccount의 accountUuid·organizationUuid 해시(resources의 계정 키와 동일). DB에 이미 식별된 계정이 `claude-code` 경로를 유지하고, 다른 계정은 `claude-code:<키 앞 12자>` 경로입니다. 한도·자원·소스 상태(`claude-oauth:<…>` 등)·구독료는 이 경로별로 저장합니다.
- 홈 탐색: 기본 홈, 설정의 `claude_homes`, `~/.paseo/zerosub/homes/claude-*`(`claude_discover_homes: false`로 끔), 훅이 보고한 사용자 홈 아래의 홈, Windows 홈(transcript 귀속용, 인증값은 읽지 않음). 계정별로 만료되지 않은 로그인을 우선해 조회합니다.
- 요청 귀속: events.account에 계정 경로를 저장하고 usage_hourly(rollup v2)도 계정별로 유지합니다. 근거 시점은 claude_evidence에 보관합니다. 훅(SessionStart·UserPromptSubmit·Stop)과 각 홈의 `sessions/<pid>.json`(프로세스 시작·busy 전환, 수집기 5초 주기 관찰)이 근거입니다. 요청 시각 이전의 가장 최근 근거가 계정을 정합니다. 계정 전환은 새 프로세스로만 일어나고 턴 중간에 옮기지 않으므로, 근거를 남긴 프로세스가 요청을 만든 프로세스입니다.
- 근거가 없을 때: 한 계정만 쓰는 폴더(Windows 등)는 그 계정입니다. 공유 폴더는 두 번째 계정이 연결된 시각(공유 링크 생성 시각, 한 번 기록하면 늦어지지 않음) 전에는 폴더 원래 계정이고, 이후에는 `claude-code:unknown`(계정 미확인)입니다. 클라우드 세션은 읽은 계정으로 바로 배정합니다. 새 근거가 오면 그 시각 이후 요청만 다시 판정합니다.
- 추가 테이블·열만 사용합니다(claude_accounts, claude_evidence, claude_dirty, events.account). 이전 릴리스로 되돌려도 events·limits를 그대로 읽으며 rollup 버전이 다르면 서로 다시 만듭니다.

## API·신뢰 경계

| API | 역할 |
|---|---|
| GET /healthz | loopback TCP 상태 검사 |
| GET /api/usage | 기간·간격·분류·scope 통계 |
| GET /api/limits, /api/collection | 한도·자원 관측 / 수집 상태 |
| GET /api/session, /api/project | 세션·프로젝트 상세 |
| GET /api/reports, /api/notify/log | 저장된 주간 리포트와 이번 주 현황·비밀 제외 알림 결과 |
| GET /api/bootstrap, /api/config | CSRF 초기화·마스킹 설정 |
| POST /api/refresh | 202 수집 요청, collection의 완료 ID 확인 |
| POST /api/config/*, /api/notify/test | 허용 설정 저장·알림 시험 |
| POST /api/resources/manual | 수동 자원 생성, 같은 request_id의 재시도 중복 방지 |
| PATCH /api/resources/manual/{id} | revision이 일치할 때 수동 기록 수정 |
| DELETE /api/resources/manual/{id} | revision이 일치할 때 수동 기록 삭제 |

건강 검사 이외 요청은 Gunicorn의 실제 AF_UNIX 연결과 Tailscale 로그인 allowlist를 확인합니다. TCP 헤더 위조는 허용하지 않습니다.
쓰기는 정확한 Origin·JSON·세션 CSRF를 요구합니다. 쿠키는 독립 이름/키와 Secure·HttpOnly·SameSite=Strict를 사용합니다.
Serve는 0700 폴더의 socket으로 연결하며 Funnel을 거부합니다. 공용 reverse proxy로 임의 대체하지 마세요.
정적 자산은 내용 해시로 버전 관리합니다. 서버 no-store와 브라우저 명시적 통계 사본 저장은 별개입니다. [개인정보 범위](../SECURITY.md)를 확인하세요.

설정 API `GET /api/config`의 `subscription_routes`는 지원 서비스·관측 route·구독료 설정 route를 중복 없이 합친 문자열 배열입니다. 구독료 미설정과 편집 가능한 서비스 목록을 분리하며 기존 저장 API는 유지합니다.

`GET /api/limits`는 한도·resources·quota_policies와 관측 이력을 제공합니다. 0.4.0부터 planning 응답을 제공하지 않으며 이전 route/model/hours/pace/today_hours/week_hours 쿼리는 무시합니다. GET은 제공사 수집을 강제로 실행하지 않습니다.

`GET /api/usage?period=1h`는 서버 기준 최근 60분을 원본 events에서 집계합니다. 응답의 period, start, end_exclusive로 범위를 확인할 수 있습니다. 선택적인 as_of(Unix 초)는 이 기간에서만 허용하며 과거의 동일 종료 시각으로 상세를 조회하는 데 사용합니다. 미래·비유한 값은 400입니다. 브라우저는 core 응답의 종료 시각을 insights 요청에 전달합니다. 초 단위 종료 시각이 캐시 키에 포함돼 수집이 없더라도 구간이 이동합니다. 다른 기간은 기존 달력 경계와 롤업을 유지합니다.

수동 생성의 필수 필드는 route, kind(usage_credit/api_credit/reset), label, amount, unit, scope, checked입니다. 선택 필드로 model, expires, enabled, spend_remaining, spend_unlimited, auto_reload, linked_id를 받습니다. 확인/만료는 Unix 초 또는 시간대를 포함한 ISO 시각입니다. 새 기록의 request_id는 32자리 소문자 16진수이며 같은 본문·계정 조건의 재시도를 구분합니다. 수정·삭제에는 서버가 반환한 revision이 필요하고 충돌은 409입니다. 자동 관측은 이 API로 수정·삭제하지 않습니다.
