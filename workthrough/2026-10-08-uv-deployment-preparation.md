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

## GitHub 반영과 Gunicorn

- CodeQL init/analyze는 PR #9에서 동일 SHA로 통합하고 원래 #4/#5도 이력에 포함했다. [CI 성공](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37695878410).
- MarkupSafe #3과 Playwright #2는 각각 최신 main 재검증 후 merge commit으로 병합했다.
- uv 전환 #10은 [전체 CI](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37697312852) 및 독립 읽기 전용 검토를 통과했다. 후보 준비·실패 정리·환경 격리·복구 문서에서 추가 수정 사항은 발견되지 않았다.
- Gunicorn #1의 원본 변경을 통합하면서 삭제한 requirements.txt는 복원하지 않고 uv.lock에 26.2.0을 반영한다. 선언 하한 25.1은 유지하며 상한은 27 미만이다.
- 26.2.0으로 전체 Python 262개 검사 통과. TCP 신원 위조 거부·Unix 신원·Origin·CSRF 검사는 sync 및 운영 설정 gthread에서 검증한다. 최소 25.1에서도 두 transport 검사를 통과했다. CI는 Python 3.11/3.14 × locked/minimum 전체 행렬을 확인한다.
- 운영 후보는 검증한 main을 별도 최종 경로에 내보내 생성하며, 정확한 커밋·파일 해시·환경 버전은 비공개 candidate.json과 준비 기록에 보존한다. 서비스 재시작·current 변경·운영 venv 갱신·실제 계정 수신 검증은 수행하지 않는다.

- 실제 GitHub [Dependabot uv 실행](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37697527127)이 성공해 #12를 생성했다. #12의 pyproject·lock 업데이트도 #11에 통합했다. bot의 lock revision 3을 uv 0.12.23의 lock --check 및 sync --locked가 수용하며 별도 재해석·다른 의존성 변경은 없음을 확인했다.

## 최종 검증과 운영 경계

- Gunicorn #11(원본 #1 및 uv #12 포함)은 [최종 통합 CI](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37697806963) 성공 후 main에 병합했다.
- 전환 후 새로 생성된 setup-uv #13도 검토했다. v10의 주요 변경은 민감 이벤트의 자동 캐시 비활성화이며 이 저장소는 enable-cache: false를 명시한다. uv 0.12.23 고정은 유지하고 Action만 v10.2.0 전체 SHA로 갱신한다.
- 후보에 대한 실제 배포 전 검증은 합성 transport, 소스/lock 해시, 의존성 일치와 systemd 유닛 구문 검사다. health와 실제 계정·제공사 수신은 서로 다른 검증으로 기록한다.
- 작업 중 웹·수집기의 PID/시작 시각/실행 경로와 운영 venv 1,796개 일반 파일 SHA256이 작업 전과 일치했고 health가 정상임을 확인했다. 마지막에도 동일 기준으로 재확인한다.
- 운영 전환은 수행하지 않는다. 후보·복구 자료·Git 백업은 보존하고 작업 브랜치·worktree·검증용 산출물은 병합 확인 후 정리한다.

## 최종 범위 수정: 기존 운영 구조 유지

- 별도 백업·후보 디렉터리를 남기지 않고 기존 개발 checkout/venv 및 운영 releases/current 구조를 유지한다. 앞서 추가한 prepare_deployment.py와 해당 6개 검사는 제거하고 독립 후보/drop-in 전환 안내도 교체한다.
- 이번 작업에서 만든 별도 Git bundle·운영 스냅샷·준비 기록 디렉터리는 삭제했다. 비교 기준은 세션에서만 유지한다. 기존 운영 데이터와 이전에 보존하기로 한 공개 전환 이력은 건드리지 않는다.
- 최종 결과는 Git으로 관리되는 uv 전환·의존성 업데이트·CI와 격리된 설치/서버 검증이다. 운영용 별도 후보를 보존하거나 현재 운영 venv·서비스를 변경하지 않는다.
- 실제 의존성 배포에는 기존 구조에서 두 서비스 정지·환경 동기화·코드 전환·재시작 및 의존성 복원 검증이 필요하다. 무중단 전환이 검증됐다고 보고하지 않는다.

- 최종 범위 수정 후 Python 256개 및 새 Git checkout의 uv 고정 설치·합성 init·소스 밖 import 검사가 통과했다. 삭제한 후보 도구의 과거 6개 테스트는 최종 검사 수에 포함하지 않는다.

- 기존 구조의 환경 전환/복원을 임시 checkout의 동일 .venv 경로에서 검증했다. Git 67bb498의 의존성(Gunicorn 25.3.0, MarkupSafe 3.0.3) → 최종 lock(26.2.0, 3.0.4) → 이전 Git 의존성 복원이 일치했다. 새 환경 sync/gthread 및 복원된 환경의 실제 Gunicorn 인증/CSRF 검사 통과. 임시 checkout·venv는 즉시 삭제했고 운영 프로세스는 건드리지 않았다.

## 운영 배포 완료

- 후속 요청에서 실제 운영 배포·재시작을 승인받아 CI가 통과한 main `7f89e6b14f36a3818175cbb6807c51062f50dde0`을 배포했다. 기존 checkout/.venv 및 releases/current 구조를 유지했다.
- stop은 sudo 인증이 필요하여 사용자가 두 서비스를 정지했고, inactive 상태를 확인한 뒤 uv 0.12.23으로 `sync --locked --no-dev --offline`을 실행했다. Gunicorn 26.2.0, Werkzeug 3.1.9, MarkupSafe 3.0.4를 적용하고 의존성 일치 검사를 통과했다.
- 기존 current 링크를 새 Git 릴리스로 전환하고 웹·수집기를 시작했다. 두 프로세스의 실제 cwd가 해당 릴리스임을 확인했고 health 및 배포 이후의 새 collector heartbeat가 정상이다. 활성화 명령은 약 9초에 검증을 마쳤으며 사용자 정지 이후 전체 중단 시간을 의미하지는 않는다.
- 실제 사설 주소의 읽기 전용 Playwright smoke에서 인증 접속, 개요·소비 분석 차트, 모바일 가로 넘침, 브라우저 오류 검사가 통과했다. 수집 상태의 정상·종료 항목을 구분해 확인했으며 모든 제공사의 새 인증/한도 수신을 별도 검증한 것은 아니다.
- 별도 백업 디렉터리·새 유닛/drop-in은 만들지 않았다. 설치·복원 패키지와 브라우저 검증에 사용한 임시 환경은 배포 후 제거한다. 이전 릴리스는 기존 운영 릴리스 구조의 복귀 대상으로 유지한다.

- 운영 DB의 읽기 전용 원본 대조에서 Codex·Antigravity·Devin의 불일치/누락 오류가 없었다. 동일 시각 관측 순서를 확정할 수 없는 Codex 세션은 모호한 사례로 별도 집계하며 완전 검증됐다고 간주하지 않는다.
