# 개발 의존성 filelock 보안 검사 복구

## 변경

클라우드·리뷰 기능의 구현·운영 배포 요청에 앞서, 기존 문서 PR의 필수 CI를 막던 개발 의존성을 별도 변경으로 보완했다.

- `pip-audit` → `cachecontrol[filecache]` → `filelock 4.0.12` 경로에서 보고된 `CVE-2026-112940`, `CVE-2026-112913`, `CVE-2026-112907`을 처리한다.
- uv 제약에 `filelock>=4.2.1`을 추가하고 해당 패키지만 잠금 갱신하여 [filelock 4.2.4](https://pypi.org/project/filelock/4.2.4/)를 선택했다.
- 런타임 의존성과 앱 버전은 유지한다. 운영 환경은 동기화하지 않았다.

## 검증

- 독립 worktree에서 uv 0.12.23으로 lock 생성과 `uv sync --locked`를 통과했다.
- 전체 잠금 의존성을 export한 뒤 `pip_audit --strict`: 알려진 취약점 없음.
- Python 단위 검사 526개 통과.
- diff와 공개 파일·문서 링크 검사 후 관련 파일만 커밋한다. main 병합에는 필수 CI 성공을 요구한다.
