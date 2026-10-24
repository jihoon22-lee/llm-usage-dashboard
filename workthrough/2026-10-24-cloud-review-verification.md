# Codex 클라우드·자동 리뷰 사용량 조회 범위 검증

## 범위와 기준

- 요청: 클라우드 세션과 코드 리뷰의 사용량을 실제로 어디까지 확인할 수 있는지 상세 검증한다.
- 기준: main `f284b67`, 운영 코드 v0.8.1, 설치된 Codex CLI 0.163.0, 개인 Pro 계정의 기존 CLI 인증 및 사용자가 직접 로그인한 웹 화면.
- 독립 `docs/cloud-usage-verification` worktree에서 조사했다. 이 변경은 검증 기록과 README 보완이다. 추가 수집기 구현·배포와 구분한다.
- 검증 표본을 만들기 위한 새 모델 작업이나 리뷰를 생성하지 않고 기존 기록을 조회했다. 인증값·대화 본문·원본 응답 전문·계정/작업 식별자·개인별 사용량 수치는 공개 기록에 포함하지 않는다.

## 결론

계정 일별 토큰만 확인할 수 있다는 기존 설명은 확인 범위가 좁았다. 자동 리뷰의 작업 목록, PR 연결, 실행 상태, 모델과 실제 스레드 ID는 조회할 수 있다. 웹이 사용하는 새 사용량 조회에서는 일부 클라우드 작업과 리뷰의 모델별 한도 소비율도 수신했다.

다만 추가로 받은 소비율과 크레딧은 원본 토큰 수가 아니다. 현재 검증한 경로에서 개별 클라우드 작업·리뷰의 입력·출력·캐시 토큰은 확보하지 못했다. 없는 값을 0으로 채우거나 계정 차액을 전부 리뷰에 배정해서는 안 된다.

## 실제 조회 결과

| 조회 | 실제 결과 | 판단 가능한 범위 |
| --- | --- | --- |
| App Server `account/usage/read` | 계정 summary와 일별 토큰 수신 | 계정 총량. 개별 작업·모델·입출력 배분은 없음 |
| 기존 프로필 조회와 공식 RPC 대조 | 반환된 모든 일별 구간과 lifetime 값 일치 | 같은 원본 통계를 중복 합산하지 않음 |
| 기본 `codex cloud list`, 작업 목록 기본/`all`/`archived` | 검사 시점에 빈 목록 | 자동 리뷰 부재를 의미하지 않음 |
| 작업 목록 `task_filter=code_review` | 실제 리뷰 목록과 cursor 수신, 끝 페이지까지 확인 | 작업·PR·생성/갱신 시각·상태 확인 가능 |
| 완료·실패·취소 리뷰 상세 17건 | 모두 HTTP 200. 상세·이벤트 전체에서 토큰 필드를 찾지 못함 | 모델·작업 이력은 확인 가능. 토큰 수신 증거는 아님 |
| 완료 리뷰 15개의 실제 스레드 ID로 공식 RPC 조회 | 모두 `threadUsage=null` | 최근 로컬 스레드만 검사하던 기존 진단보다 범위를 넓혔지만 상세 토큰은 미수신 |
| 구형 `thread_usage/query`, `thread-estimates/query` | 현재 인증으로 HTTP 403 | 이 인증 경로에서 사용 불가. 인증·권한을 바꿔 우회하지 않음 |
| 웹의 `thread_usage/query_v2` | 완료 리뷰 15건 중 4건 `partial`, 11건 `unavailable`; 실패 리뷰도 `partial` | 일부 작업의 한도 소비율·크레딧·모델·추론 강도·속도 수신 |
| 실제 클라우드 세션과 dot 세션의 웹/v2 대조 | 클라우드 표본은 `partial`이며 웹의 반올림된 소비율과 일치. dot 표본은 `unavailable` | 클라우드 전체 또는 모든 dot의 지원 여부로 일반화하지 않음 |
| 웹 일별 분해, 7일·30일 범위 | HTTP 200, `units=percent` | 모델·기능·제품 화면별 한도 소비 분해. 토큰 수가 아님 |
| 플랜 한도 이력 | `coverage_complete=false`, `approximate=true`, `accounting_complete=false` | 불완전한 집계와 근사값으로 취급 |

내부 HTTP 경로는 설치된 클라이언트 또는 실제 로그인된 웹의 요청에서 확인했다. 요청 형식의 검증 오류는 올바른 식별자를 사용한 조회와 구분했고, 정상 응답·미제공·권한 오류를 별도로 판독했다. 내부 경로의 존재나 HTTP 200만으로 공식 외부 연동 지원을 보장하지 않는다.

## 자동 리뷰 목록의 누락 원인

기존 CLI 목록은 리뷰 목록 전체를 대표하지 않는다. 서버는 `task_filter`에 `all`, `current`, `archived`, `code_review`, `security_review`를 구분한다. 실제로 `all`은 비어 있어도 `code_review`에는 기록이 있었다.

- cursor를 따라 마지막 페이지까지 읽었고 task ID 중복과 남은 cursor를 확인했다.
- 목록의 PR 식별 정보와 상세의 PR 연결을 확인했다. 리뷰 실행 횟수와 고유 PR 수는 서로 다른 지표다.
- 상세의 `current_assistant_turn`과 `current_diff_task_turn`은 같은 실행을 중복 표현할 수 있다. 둘을 별도 실행으로 세면 안 된다.
- `thread/started`와 실제 이벤트에서 작업에 속한 스레드 ID를 찾았다. task ID와 실행 스레드 ID는 서로 다른 식별자다.
- `current_assistant_turn.model_version` 등에서 실제 모델을 확인했다. 요청된 모델 이름, UI 선택값, 실제 실행 모델을 구분해야 한다.
- 취소된 표본에는 연결할 실행 스레드가 없었다. 이를 토큰 0의 증거로 쓰지 않는다.

현재 수집기의 `poll_codex_thread()`는 가장 최근 로컬 스레드 하나를 검사한다. 이 결과만으로 클라우드·리뷰 전체의 조회 가능성을 판단할 수 없으며, 리뷰 목록 조회도 현재 자동 수집에 구현돼 있지 않다.

## 웹 사용량 분석에서 확인한 추가 데이터

로그인된 설정의 사용량 분석 화면과 그 화면이 호출한 경로를 대조했다.

### 일별 분해

`GET /backend-api/wham/usage/daily-token-usage-breakdown`은 `start_date`, `end_date`, `group_by=day`로 조회했다. 응답에는 다음 값이 있었다.

- `units`, `group_by`, 일별 `date`
- `attribution`: `thread_source`, `turn_trigger`, `model`, `surface`, `value`
- `product_surface_usage_values`
- `models`: `model`, `speed`, `credits`

확인한 계정의 응답 단위는 `percent`였다. URL의 `token`이나 내부 필드명 `credits`만 보고 토큰 수 또는 결제 크레딧으로 해석하면 잘못된다. 반드시 응답 단위와 화면 의미를 함께 보존해야 한다.

자동 코드 리뷰, 보안 검토, dot, 하위 에이전트, 메모리·제목 생성, 승인 검토 등의 출처가 구분됐다. 이들은 기존 계정 차액을 특정 용도로 명명하는 것보다 직접적인 출처 근거다. 하지만 비용·한도 가중치가 다른 소비율을 원본 토큰으로 환산할 수는 없다.

7일 범위에서는 모델·기능 분해 합계를 대조했다. 30일 범위에서는 한 날짜의 기능 분해 합계가 모델·제품 화면 합계보다 작았다. 서로 다른 분해를 더하거나 누락분을 특정 출처에 강제로 배정하지 않는다. 상세 분해의 완전성은 계정 총량 수신과 별도다.

### 작업별 새 조회

`POST /backend-api/wham/usage/thread_usage/query_v2`는 실제 스레드 ID를 `threads` 배열로 보내는 읽기 조회다. 원본 응답에는 다음 값이 있었다.

- `data_as_of`, `comparison_plan`
- 스레드별 `data_status`, `usage_source`
- `five_hour_limit_percent`, `weekly_limit_percent`, `balance_usage_credits`
- `groups`의 모델·추론 강도·속도별 동일한 소비 필드

값이 반환된 표본에서 모델별 주간 소비율 합계는 스레드 합계와 일치했다. 웹의 클라우드 세션 소비율도 같은 값의 반올림 표시였다. `partial`과 `unavailable`을 그대로 구분했으며, 원본 숫자 문자열의 명시적인 크레딧 0과 null을 혼동하지 않았다.

실패한 리뷰에서도 양수 소비율을 받았다. 완료되지 않았다는 이유로 소비량을 0으로 처리해서는 안 된다. 오래된 표본과 최근 표본 모두 미제공 사례가 있어, 단순히 기다리면 모든 기록이 복구된다고 결론내리지 않는다.

이 응답에는 원본 입력·출력·캐시 토큰 필드가 없었다. 구형 App Server의 `threadUsage` 자료형에 토큰 필드가 존재한다는 사실과 새 웹 조회에서 해당 숫자를 수신했다는 주장은 다르다.

## 현재 구현에 반영된 범위와 후속 작업

- 기존 Codex 계정 일별 토큰과 Claude 클라우드 수집은 운영에서 별도로 유지되고 있다. Claude의 원격 요청·모델 누계 수집을 Codex의 한계와 혼동하지 않는다.
- 이번에 찾은 리뷰 목록·새 작업별 한도 소비·일별 출처 분해는 조회 가능성을 검증한 단계다. 통계 DB나 자동 수집에 새로 연결하지 않았다.
- 후속 구현은 리뷰/PR 활동과 한도 소비를 원본 토큰 통계와 구분해 저장·표시해야 한다. 출처, 단위, 조회 시각, 집계 기준 시각, 구간, `partial`/`unavailable`, 근사·완전성 표시가 필요하다.
- task/turn/thread/PR의 연결을 보존하고 같은 실행의 중복 표현을 제거해야 한다. 실패·취소·여러 시도·페이지 재개를 검증해야 한다.
- 토큰 수를 추가하려면 별도의 원본 토큰 응답이나 관리 가능한 실행 환경의 실제 usage/telemetry 수신이 필요하다. 로컬 exporter 설정만으로 OpenAI가 실행하는 자동 리뷰의 기록이 전달되지는 않는다.
- 조직 Analytics API는 별도의 워크스페이스 관리자 인증이 필요한 경로다. 이번 개인 계정 검증에서 실제 사용 가능하다고 판정하지 않았다.

## 검증과 문서 관리

실제 계정 조회·웹 표시·완료/실패/취소 표본·페이지 끝·식별자 중복·수치 합계·응답 단위를 검증했다. 합성 fixture를 실계정 수신 결과로 대체하지 않았다. 문서 변경의 diff·공개 파일 검사·상대 링크 검사를 통과했다. 임시 조사 도구와 비공개 메타데이터는 공개 커밋에서 제외하고 제거했으며, 조사에 사용한 브라우저 탭도 닫았다.

### 문서 PR의 병합 제약

[PR #80](https://github.com/jihoon22-lee/llm-usage-dashboard/pull/80)의 문서 커밋 `e70d253`에 대한 [CI 보안 검사](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/40045401456/job/120725845287)가 기존 `filelock 4.0.12`의 취약점 세 건 때문에 실패했다. `pip-audit`이 보고한 식별자는 `CVE-2026-112940`, `CVE-2026-112913`, `CVE-2026-112907`이며 수정 버전은 4.2.1이다.

잠금 파일에서 `pip-audit` → `cachecontrol[filecache]` → `filelock` 개발 의존성 경로를 확인했다. 이번 문서 변경으로 추가된 의존성이 아니며, 운영 환경을 동기화하거나 의존성을 바꾸지 않았다. 별도 의존성 수정과 필수 CI 성공이 필요하므로 main 병합은 보류하고 문서 커밋·PR·작업 브랜치를 보존한다. 보안 검사를 생략하거나 main 보호 규칙을 우회하지 않는다.

## 참고한 공식 문서

- [App Server의 계정 토큰 조회](https://learn.chatgpt.com/docs/app-server#7-token-usage-chatgpt)
- [Codex CLI의 cloud 명령](https://learn.chatgpt.com/docs/cli/reference#codex-cloud)
- [사용량 분석 지표 해석](https://learn.chatgpt.com/docs/enterprise/usage-insights)
- [워크스페이스 Analytics API](https://learn.chatgpt.com/docs/enterprise/analytics-api)
- [Codex telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
