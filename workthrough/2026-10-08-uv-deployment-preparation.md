# uv 전환과 운영 배포 준비

## 범위와 순서

CodeQL init/analyze 동시 갱신 → MarkupSafe·Playwright 갱신 → uv 프로젝트 전환 → Gunicorn 호환성 검증 → 운영 배포 후보 준비 순으로 진행한다.
공개 변경은 최신 main을 포함한 PR의 `ci-required` 성공 후 merge commit으로 병합한다.
현재 운영 서비스·공유 가상환경·설정·DB·current 링크는 변경하지 않는다. 배포 준비와 실제 활성화를 구분한다.

## CodeQL

Dependabot PR #4·#5의 이력을 함께 포함해 init/analyze를 동일한 upstream v4.38.2 SHA로 갱신한다.
이후 두 액션의 업데이트도 하나의 Dependabot 그룹으로 묶는다. 전체 Python·JavaScript 분석과 기존 SARIF 차단 검사는 유지한다.

## 수용 기준

- Python 3.11·3.14, JavaScript, 합성 브라우저, Git·wheel 설치 및 보안 검사를 통과한다.
- uv lock 불일치는 CI를 실패시키고, 최초 전환에서 승인되지 않은 런타임 버전 변경을 섞지 않는다.
- Gunicorn 최소 지원 버전과 업데이트 후보를 실제 TCP·Unix socket 및 운영과 같은 threaded worker 구성에서 검사한다.
- 동일 릴리스 빌드 산출물 검증·게시와 불변 릴리스 정책을 유지한다.
- 새 배포 후보와 의존성을 별도 환경에 준비하며, 준비 실패나 성공 모두 운영 링크·서비스·데이터에 영향을 주지 않는다.
- 완료 시 작업 브랜치·worktree·임시 산출물을 정리하고 복구 백업·배포 후보는 용도를 명시해 보존한다.

검증 결과와 최종 PR·후보 정보는 각 단계 완료 시 이 기록에 갱신한다.
