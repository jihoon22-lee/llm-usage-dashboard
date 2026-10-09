# 개발과 기여

[구조](docs/architecture.md)와 [보안](SECURITY.md)을 먼저 확인하세요. 실제 계정·DB 없이 합성 테스트로 기여할 수 있습니다.
운영 가상환경을 사용하지 말고 별도 checkout과 독립 .venv에서 개발합니다.

```bash
uv sync --locked
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -q
for script in llm_usage/web/*.js; do node --check "$script" || exit; done
node --test tests/js/*.test.mjs
PYTHONPATH=. .venv/bin/python tests/browser_ci.py
```

Node24를 사용합니다. Linux 브라우저 라이브러리가 없으면 playwright install --with-deps chromium이 필요합니다.
기본 브라우저 검사는 임시 DB·합성 응답·예제 origin을 사용합니다. 실제 레이아웃·CSP·모바일 검사와 실제 계정 인증은 다릅니다.
`tests/browser_resource_planning.py`는 실제 Flask API와 320/390/1440px에서 작업 판단·모델 제약·수동 기록 충돌·중복 저장 방지·오프라인 복구를 검사합니다. 제공사 읽기와 초기화권 소비·구매 검증을 혼동하지 않습니다. 공개 fixture에는 실제 잔액·계정 식별자·인증정보를 넣지 않습니다.
`browser_smoke.py --origin https://dashboard.example.ts.net:9444`는 본인의 사설 사이트를 명시하는 운영 검사이며 공개 CI에서 실행하지 않습니다.

## 변경과 릴리스

작업 브랜치에서 변경·회귀 검증 후 PR을 엽니다. ci-required 성공 후 merge commit으로 병합하고 로컬 main을 동기화합니다.
첫 root push만 bootstrap 예외입니다. 실패 검사를 숨기거나 보호를 우회하지 않습니다. 에이전트는 [AGENTS.md](AGENTS.md)를 따릅니다.
한 작업의 계획·실제 결과·제약은 workthrough의 날짜별 문서 하나에 기록하고 개인 주소·경로·로그는 넣지 않습니다.
태그는 공개 main에 포함되고 패키지 버전과 같아야 합니다. 검증한 동일 산출물을 게시하고 게시 job은 재빌드하지 않습니다.
공개 태그·파일은 덮어쓰지 않습니다. wheel은 런타임이고 시스템 서비스 설치에는 Git checkout이 필요합니다.
웹 자산을 추가하면 `scripts/check_package.py`의 명시적 자산 목록도 갱신하고 wheel·소스 배포본과 설치된 환경에서 실제로 제공되는지 확인합니다.
버전은 pyproject.toml과 uv.lock의 프로젝트 항목을 함께 갱신하고 CHANGELOG·설치/업데이트 안내를 맞춥니다. 버전 PR이 병합되고 필수 CI가 성공한 main 커밋에 일치하는 `vX.Y.Z` 태그를 만듭니다.
태그 push는 Release 워크플로를 실행합니다. 태그/버전/main 포함 검사와 전체 검사를 통과한 wheel·소스 배포본·release-manifest.json·SHA256SUMS를 같은 artifact ID에서 가져와 초안에 업로드하고, 다시 다운로드해 검증한 뒤 게시합니다. 로컬 재빌드 파일로 교체하지 않습니다. 게시 후 내려받은 파일도 manifest·SHA256SUMS와 대조하고 릴리스 노트에 변경 사항·업데이트 주의사항·CI 출처를 남깁니다.
운영 자동 배포·PyPI는 별도 범위입니다. 실제 수신과 fixture 성공을 구분해 기록합니다.

의존성은 pyproject.toml과 uv.lock으로 관리하며 uv 0.12.23을 사용합니다. `uv lock --upgrade-package NAME`으로 의도한 패키지만 갱신하고 lock diff를 검토합니다. `uv sync --locked`는 lock 일치 여부를 검사하며 불필요한 패키지를 제거하므로 운영 환경에서 실행하지 않습니다. 최소 버전 검사에서 별도 override를 적용한 뒤에는 `uv run --no-sync`로 자동 동기화를 막습니다. wheel 호환 검사만 독립된 표준 pip 설치를 유지합니다.
