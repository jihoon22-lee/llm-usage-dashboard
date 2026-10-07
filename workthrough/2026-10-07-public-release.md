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
- 실제 GitHub Actions·CodeQL·공개 다운로드·개발 경로 전환은 다음 단계이며 아래 기록으로 갱신한다.

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
