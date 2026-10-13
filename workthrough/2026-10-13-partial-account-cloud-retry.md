# 부분 계정 응답과 Claude 보조 조회 재시도 수정

## 범위와 원인

- 기준: v0.6.1 이후 main `67f6fc1`. PR #41의 [Codex 계정 연결 지적](https://github.com/jihoon22-lee/llm-usage-dashboard/pull/41#discussion_r4249434902)과 [Claude 보조 조회 지적](https://github.com/jihoon22-lee/llm-usage-dashboard/pull/41#discussion_r4249434885)을 반영한다.
- Codex는 계정 유형·이메일 기반 revision이 같아도 계정 ID가 빠지면 기존 계정 키를 알 수 없는 epoch로 바꿨다. DB 삭제 없이 기존 토큰 합계가 조회 대상에서 제외됐다. 한도 RPC 실패와 정상 한도 응답 양쪽에서 발생했다.
- Claude는 선택적 events API가 404이면 활성 세션의 teleport-events 본문까지 건너뛰었다. updated_at이 변하지 않은 최종 사용량은 다음 일일 재조회까지 늦어질 수 있었다.

## 구현

- Codex 계정 메타데이터 해석을 공통 함수로 묶고 한도 처리와 계정 사용량 처리에 같은 응답을 전달한다.
- freshly received revision이 저장된 revision과 일치하는 경우에만 알려진 계정 키 보존을 명시적으로 허용한다. 다른 계정 ID, 변경된 revision, 식별 불가능한 새 사용량의 기존 분리 동작은 유지한다. 인증 토큰을 비교하거나 저장하지 않는다.
- Claude 보조 API에 supplement_retry_at을 저장한다. 보조 조회가 대기 중이어도 최근 15분 내 갱신된 세션의 본문은 계속 읽는다. 본문 수신 시각 갱신이 보조 재시도 기한을 밀지 않는다.
- 기존 checkpoint는 마지막 checked + 24시간을 보조 재시도 기한으로 해석한다. 세션 변경과 재시도 기한 도달 시 보조 API를 다시 조회하며 성공하면 미제공 상태를 해제한다.

## 검증

- 독립 worktree에서 uv 0.12.23의 `uv sync --locked`로 의존성을 준비했다. 운영 가상환경은 동기화하지 않았다.
- 수정 전 추가 회귀 테스트: Codex 두 경로에서 계정 키 변경, Claude 지연 확정 output 20 → 40 미반영으로 총 3개 실패를 확인했다.
- 수정 후 `.venv/bin/python -m unittest discover -s tests -q`: 376개 통과.
- Codex: 동일 revision의 한도 실패·정상 응답·새 사용량 수신, 변경된 revision의 기존 합계 분리, 기존 전체 실패·잘못된 메타데이터·계정 전환 테스트를 확인했다. 사용량 API 합계와 이전 수신 시각 보존도 검사했다.
- Claude: updated_at 불변 상태에서 늦은 사용량 반영, 보조 API 요청 억제, 본문 수신 후에도 24시간 재시도 유지, 세션 변경 후 회복, 기존 checkpoint 호환을 확인했다.
- 전체 웹 JavaScript `node --check`, `node --test tests/js/*.test.mjs`: 30개 통과.
- `.venv/bin/python tests/browser_smoke.py`: 합성 Flask API로 PC·모바일, 화면 전환, 수집 상태, 갱신 UI 검증 통과. 실제 제공사 인증·수동 수집 검증과는 다르다.
- `.venv/bin/python tests/verify_record_totals.py`: 실제 보존 원본 읽기 전용 대조에서 Codex 완전 이력 1,324개·카운터 초기화 58개·불완전 시작 이력 12개 일치, 불일치 0. 같은 시각의 모호한 이력 59개는 별도 분류됐다. Antigravity 4,213건·Devin 42,137건도 누락·불일치·원본 오류 0.

## 남은 제약과 반영

- 실제 계정에서 의도적으로 부분 응답이나 404를 발생시키지는 않았다. 두 장애 경계의 검증은 합성 응답이며, 원본 대조 통과는 실제 장애 재현을 뜻하지 않는다.
- 최근 활성 범위를 벗어난 Claude 세션은 기존 일일 조회 정책을 유지한다. revision은 계정 유형·이메일 기반으로, 토큰 또는 workspace 동일성을 완전히 증명하지 않는다.
- README에 동작과 검증 범위를 기록했다. PR #43은 필수 CI 및 코드·보안 리뷰 완료 후 `f541fa8`로 main에 병합했다. 이후 사용자 요청으로 v0.6.2 릴리스와 실제 운영 배포를 같은 작업에 추가했다.

## v0.6.2 릴리스·배포 준비

- pyproject.toml과 uv.lock의 프로젝트 버전을 0.6.2로 올리고 CHANGELOG·설치·업데이트 안내를 맞췄다. 외부 의존성 버전은 변경하지 않았다.
- 버전 PR과 main의 필수 CI 성공 후 일치하는 태그를 게시하고 Release 워크플로의 검증된 산출물을 사용했다.
- 배포 준비 당시 운영은 v0.6.1 릴리스이며 웹·수집기가 모두 실행 중이었다. 서비스 정지에는 관리자 인증이 필요하고, 현재 무암호 sudo는 두 서비스의 restart만 허용한다. 실행 중 환경 동기화로 우회하지 않는다.
- 배포 시 두 서비스를 정지하고 uv 0.12.23 `sync --locked --no-dev` → 기존 배포기 → 두 프로세스 릴리스·health·새 heartbeat·사설 인증 화면을 확인한다. 실제 적용 결과는 아래에 기록했다.

- 독립 환경의 uv 0.12.23 `sync --locked`와 설치 메타데이터 0.6.2 일치, lock의 프로젝트 버전 외 변경 없음, 공개 파일·문서 링크·diff 검사를 확인했다.

## v0.6.2 게시·운영 검증

- 버전 PR #44는 필수 CI 및 코드·보안 리뷰를 통과했고 `a67c54e`로 병합했다. [main CI](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/38333889248) 성공 후 같은 commit에 v0.6.2 태그를 게시했다.
- 배포 전 기존 웹·수집기 프로세스는 모두 v0.6.1의 `e4c0de9`에서 실행됐고 health·heartbeat가 정상이었다. 서비스 설정의 DB 일치와 기존 주기 백업의 시각도 확인했다. 최근 백업은 확인 시점 약 24시간 전이며 배포 직전 스냅샷으로 간주하지 않는다.

- [Release 워크플로](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/38334122360)의 모든 검사와 게시가 성공했다. 공개 v0.6.2의 wheel·sdist·release-manifest.json·SHA256SUMS를 직접 내려받아 commit·tag·run ID와 모든 파일 해시를 대조했다. immutable 릴리스임을 확인하고 변경 사항·업데이트 주의사항·CI 출처를 릴리스 노트에 남겼다.

- 사용자 서비스 정지 후 웹·수집기 모두 inactive/dead, MainPID 0임을 확인했다. main과 v0.6.2 태그가 같은 `a67c54e`임을 확인한 뒤 uv 0.12.23 `sync --locked --no-dev`로 운영 프로젝트 메타데이터를 0.6.1 → 0.6.2로 갱신했다. 외부 패키지 변경은 없었다.
- 2026-10-13 10:59 KST에 기존 `packaging/deploy.py`로 릴리스를 전환·재시작했다. 배포기의 Python 376개·JavaScript 30개·전체 JS 구문 검사가 통과했고 새 heartbeat 확인 후 배포 성공을 반환했다.
- 웹·수집기가 모두 active이며 두 프로세스의 실제 cwd가 v0.6.2 태그 commit `a67c54ebdbef0d4f0a39001d0ce0378d185f8832`인 것을 확인했다. 운영 설치 메타데이터 0.6.2, health HTTP 200, 확인 시점 heartbeat 경과 2초였다.
- 재시작 후 claude-cloud·claude-cloud-supplement·codex·codex-account-usage가 모두 ok이며 확인 시점 관측 경과는 19–20초였다. 이는 정상 수집 확인이며, 실제 계정에 부분 응답·404를 강제해 회귀를 재현한 것은 아니다.
- 독립 Playwright 환경의 `tests/browser_smoke.py --origin`으로 실제 사설 Tailscale 인증 경로를 읽기 전용 검사했다. PC 1440px·모바일 390px, 사용량 카드·차트 표시, 가로 넘침·JS 오류 없음이 확인됐다. 제공사 강제 조회·설정 변경 요청은 실행하지 않았다.
- 운영은 v0.6.2 태그의 릴리스 디렉터리를 유지한다. 이 결과 기록 PR은 문서만 갱신하며 운영 코드를 다시 전환하지 않는다. 이전 릴리스·주기 백업·운영 가상환경은 복구와 실행에 필요하므로 보존한다.
