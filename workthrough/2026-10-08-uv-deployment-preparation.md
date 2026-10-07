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

## uv 전환 검증

- pyproject 개발 그룹과 uv.lock을 기준으로 통합하고, 첫 lock의 런타임 버전은 기존 승인 범위를 유지한다. uv 0.12.23 및 setup-uv 전체 SHA를 고정한다.
- 독립 환경에서 Python 255개, 브라우저 11종, 태그 checkout 설치, 소스 밖 표준 pip wheel 설치·파일 허용목록·SHA256 검사 통과. 전체 lock export의 pip-audit는 알려진 취약점 없음.
- uv build의 기본 .gitignore 생성 때문에 엄격한 배포 파일 검사가 실패해 --no-create-gitignore로 해결했다. 검사 허용 범위를 넓히지 않았다.
- Python 3.11/3.14 각각에서 locked/minimum 행렬로 검사한다. 최소 버전 override 후 --no-sync를 사용한다.
- 기존 v0.1.0은 변경하지 않는다. main의 uv 설치 문서와 기존 태그의 pip 설치 문서를 구분한다. 운영 환경 동기화는 금지한다.

## 배포 준비 경계

- uv 설치와 같은 경계를 사용하는 후보 준비 도구는 uv 전환 PR에 함께 검증한다. 실제 운영용 후보 생성은 Gunicorn 검증과 최종 main CI 이후에 수행한다.
- 기존 경로 충돌·더러운 main·부모 symlink·설치 실패·환경변수 격리·최종 경로 사용의 6개 회귀 검사를 추가했다. 구현 전 실패 확인 후 구현, 총 261개 Python 검사 통과.
- 준비 도구는 독립 환경에 --locked --no-dev --no-editable로 설치한다. 실패하면 이번에 생성한 후보만 제거하며 기존 current·유닛·DB·운영 venv에 쓰지 않는다.
- 복구는 두 서비스의 기존 drop-in 복원으로 코드와 환경을 함께 되돌린다. 활성화에는 재시작이 필요하며 이번 범위는 준비까지다.
