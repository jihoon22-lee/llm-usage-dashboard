# 첫 공개 릴리스 준비와 검증

## 결정과 범위

새 공개 Git 이력, MIT, 실제 Actions 검증과 첫 릴리스까지 진행한다. 기존 개발 이력은 비공개 복원본에 보존한다.
현재 개발 경로는 유지하며 운영 가상환경·설정·DB·릴리스는 보존한다. 구현은 별도 worktree와 독립 가상환경에서 진행한다.
운영 자동 CD·실제 DB 변환·PyPI·Windows/macOS 네이티브 서버는 제외한다.

## 순서와 호환성

비공개 백업→분야별 병렬 구현→통합·교차 검토→정제된 공개 이력→실제 CI·첫 릴리스→개발 경로 전환·정리 순서다.
Windows 홈은 init에서, Windows hook 쓰기는 매 실행 선택한다. 기존 config는 자동 덮어쓰지 않는다.
프로젝트명 변경은 신규/재수집부터 적용하고 기존 DB 전체 변환은 하지 않는다. 구·신 이름과 예산 분리가 남을 수 있다.
설정 API는 오프라인 캐시 저장·복원에서 제외하고 기존 비허용 사본을 정리한다. 과거 오프라인 기기는 새 코드를 받아야 한다.
공식 서비스 설치는 Git 릴리스 checkout의 로컬 main이며 wheel은 CLI/웹 런타임이다.
운영 rollback은 코드에 한정되고 의존성·DB·브라우저 smoke 실패까지 자동 복구하지 않는다.

## 진행 기록

- 초기 비공개 Git bundle 생성·복원 검사와 운영 실행 경로·가상환경 해시 기록 완료.
- 독립 작업 checkout·가상환경 준비. 초기 Python 209개 검사 통과.
- Gitleaks 8.30.1 전체 이력 검사에서 비밀 미검출. 개인 주소·경로는 별도 정리 대상이다.
- pip-audit에서 Werkzeug 3.1.8 취약점을 발견해 고정 버전과 패키지 하한을 3.1.9로 수정한다. 운영 가상환경은 변경하지 않는다.
- 개인정보·Windows 선택·설치기·문서·CI 구현과 세 분야 교차 검토 완료.
- 교차 검토로 서버 장애/늦은 응답의 설정 복원, 구형 Python URL 파서, 배포 전 주소 검증, 원격 태그 변경 경계를 추가 수정했다.
- 로컬 Python 246개, JavaScript 17개, 합성 브라우저 11개 통과. 합성 원본 합계 대조 0 불일치.
- 실제 Gunicorn Unix/TCP 인증·CSRF 및 최소 25.1.0 검사 통과. 운영 서비스에는 접근하지 않았다.
- Git 태그 checkout→새 venv→editable 설치·합성 init, 소스 밖 wheel 설치·16개 웹 자산 검사 통과.
- 동일 스냅샷 독립 빌드의 wheel·정규화 sdist 바이트 일치 확인. 릴리스 가드/게시/아카이브 회귀 13개 통과.
- actionlint, 새 파일 포함 문서 링크·개인경로 검사, runtime+dev pip-audit 통과.
- 실제 GitHub Actions·CodeQL·공개 다운로드·개발 경로 전환 결과는 아래에 기록한다.

## 검증 구분

단위·합성 브라우저·실제 Gunicorn transport, 공개 Actions·다운로드, 실제 계정·운영 배포는 별도로 보고한다.
이번 작업은 실제 계정을 재조회하거나 운영을 재시작하지 않는다. 과거 실수신은 구조 문서에 시점 한계와 함께 기록한다.

## 공개 검증

- 최초 정제 main push를 초기 설정 예외로 사용했다. [첫 Actions](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37636211253)의 모든 검사가 성공했다.
- 이후 관리자에게도 적용하는 PR 필수·`checks / ci-required`·최신 main 기반 검사를 설정했다. v* 태그 수정/삭제 금지와 immutable releases를 활성화했다.
- 비공개 취약점 제보·비밀 탐지·push protection·의존성 알림을 활성화했다.
- [검증 PR](https://github.com/jihoon22-lee/llm-usage-dashboard/pull/6)은 manifest 버전/태그 불일치를 검출하는 실패 검사를 먼저 실행한다. 수정 후 성공한 결과로만 병합한다.
- wheel과 Git 설치의 `pip check`도 검사하여 선언 의존성과 설치된 버전의 불일치를 거부한다.

- 첫 CI 성공 후 GitHub 보안 경고 목록과 교차 확인해 high 경고 2건과 SARIF 확장 규칙 누락을 발견했다. 성공 상태만으로 릴리스하지 않고 집계·TLS 최소 버전·HTML 문자열 처리를 추가 수정한다.
- [실패 실행](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37636693561)에서 Python 검사와 ci-required가 실패했으며 PR은 BLOCKED였다. 실패 단계에서는 태그·릴리스를 만들지 않았다.

- SARIF driver/extension 규칙 참조를 모두 검사하도록 수정했고 실제 최초 분석 SARIF로 high 2건의 실패를 재현했다. 규칙 누락·모호성·잘못된 점수도 실패 처리한다. PR도 전체 소스를 검사한다.
- Antigravity loopback RPC의 TLS 최소 버전을 1.2로 명시하고, 한도 요약은 HTML 제거 대신 숫자에서 직접 생성하도록 수정했다. 합성 TLS/데스크톱·모바일 회귀가 통과했다.

## 최종 실행 결과

- [공개 저장소](https://github.com/jihoon22-lee/llm-usage-dashboard): 기존 비공개 이력 없이 정제된 새 이력으로 생성했다. 작성자·병합 이메일은 GitHub noreply이며 전역 Git 설정은 바꾸지 않았다.
- [수정 PR #6](https://github.com/jihoon22-lee/llm-usage-dashboard/pull/6)은 실패→수정→[모든 검사 성공](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37637974527)→보호 규칙을 지킨 merge commit 순서로 완료했다. 자동 리뷰의 동일 지적도 수정 확인 후 해결했다.
- [병합된 main CI](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37639666167) 성공. Python 3.11/3.14에서 255개, JavaScript 17개, 합성 브라우저 11종, 설치·패키지·보안 검사가 통과했다. 공개 main의 열린 CodeQL 경고는 0건이었다.
- [v0.1.0](https://github.com/jihoon22-lee/llm-usage-dashboard/releases/tag/v0.1.0)은 커밋 `29613a4d61f3898b6179faf69325afb7aadab739`에서 생성했다. [태그 workflow](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37639966734)의 검증·draft 업로드·재다운로드 검사 후 immutable release로 게시했다.
- 최초 게시 artifact ID는 `11491371936`이다. 공개 wheel·sdist·manifest·SHA256SUMS 4개를 다운로드해 해당 CI artifact와 바이트 단위로 같음을 확인했다. CI artifact 보존 기간은 14일이며 릴리스 자산과 체크섬은 별도로 게시돼 있다.
- 공개 태그를 새로 clone하고 local main→새 venv→고정 의존성·editable 설치→합성 init을 검증했다. Codex 없는 환경의 설치기 fixture 5개가 통과했다. 공개 wheel은 소스 밖 새 venv에서 CLI·웹 자산을 확인했다. 두 환경의 pip check도 통과했다.
- 같은 태그 workflow 전체를 재실행한 두 번째 시도도 성공했다. 게시기는 기존 파일 검증 후 무변경 종료했으며 릴리스 ID·게시 시각·자산 ID·해시·생성/수정 시각이 모두 유지됐다.
- 기존 개발 디렉터리의 Git·추적 파일을 공개 main으로 전환했다. 연결 worktree·파일 충돌·외부 Git 객체 의존성 없음, 추적 파일 바이트 일치, Git fsck, 깨끗한 작업 상태를 확인했다. 기존 Git 정보와 추적 소스를 함께 복원할 비공개 백업을 보존했다.
- 전환 전후 두 운영 서비스의 PID·시작 시각·실행 경로, 릴리스 링크, 운영 .venv 파일 1,796개의 해시가 동일했다. localhost health도 정상이다. 설치기·운영 업데이트·서비스 재시작은 실행하지 않았다.
- 구현용 브랜치·관리 worktree·공개 준비 checkout·독립 개발 venv·임시 DB·브라우저 프로세스·다운로드·복원 검사용 clone을 정리했다. 기존 운영 파일과 작업 전에 있던 사용자 산출물, 복구 백업은 보존한다.

| 공개 배포 파일 | SHA256 |
|---|---|
| llm_usage_dashboard-0.1.0-py3-none-any.whl | `598577fdfcd80e1e035f03df925dca82960cb7fa32282d4c4b040882751d9dcf` |
| llm_usage_dashboard-0.1.0.tar.gz | `d456575a77075bf315caa8271e4f68ce58d61095530c18be56e6b0ce9d8354dc` |

## 남은 범위와 제약

최종 기록 PR 병합 뒤 main 검사 [37641365761](https://github.com/jihoon22-lee/llm-usage-dashboard/actions/runs/37641365761)에서 브라우저 테스트의 설정 화면 재진입 경합이 발견됐다. 이전 화면의 입력·행 수 검사가 먼저 통과한 뒤 비동기 설정 로드가 버튼을 교체하면 크기 조회가 `None`을 반환할 수 있었다. 이전 버튼이 교체될 때까지 기다린 뒤 기존 표시·너비 검사를 수행하도록 테스트만 수정했다. 독립 가상환경에서 정상 응답과 설정 응답 200ms 지연 검사를 통과했다. 제품 코드와 불변 v0.1.0 자산은 변경하지 않는다.

실제 Tailscale 계정 인증·제공사 수신과 운영 배포는 이번 공개 CI/설치 fixture 검증과 다르다. 운영에는 이번 수정과 의존성 업데이트를 배포하지 않았다.
기존 DB 전체 이름 변환·원격 오프라인 기기의 과거 캐시 제거·PyPI·자동 운영 CD는 수행하지 않았다. 제공사별 실수신 한계는 구조 문서에 유지한다.
Dependabot이 자동 생성한 미병합 의존성 업데이트 PR 5건은 이번 릴리스에 포함하지 않았으며, 다른 변경을 폐기하지 않는 원칙에 따라 별도 검토 대상으로 보존한다.
