# 개발과 기여

[구조](docs/architecture.md)와 [보안](SECURITY.md)을 먼저 확인하세요. 실제 계정·DB 없이 합성 테스트로 기여할 수 있습니다.
운영 가상환경을 사용하지 말고 별도 checkout과 독립 .venv에서 개발합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -q
for script in llm_usage/web/*.js; do node --check "$script" || exit; done
node --test 'tests/js/*.test.mjs'
PYTHONPATH=. .venv/bin/python tests/browser_ci.py
```

Node24를 사용합니다. Linux 브라우저 라이브러리가 없으면 playwright install --with-deps chromium이 필요합니다.
기본 브라우저 검사는 임시 DB·합성 응답·예제 origin을 사용합니다. 실제 레이아웃·CSP·모바일 검사와 실제 계정 인증은 다릅니다.
`browser_smoke.py --origin https://dashboard.example.ts.net:9444`는 본인의 사설 사이트를 명시하는 운영 검사이며 공개 CI에서 실행하지 않습니다.

## 변경과 릴리스

작업 브랜치에서 변경·회귀 검증 후 PR을 엽니다. ci-required 성공 후 merge commit으로 병합하고 로컬 main을 동기화합니다.
첫 root push만 bootstrap 예외입니다. 실패 검사를 숨기거나 보호를 우회하지 않습니다. 에이전트는 [AGENTS.md](AGENTS.md)를 따릅니다.
한 작업의 계획·실제 결과·제약은 workthrough의 날짜별 문서 하나에 기록하고 개인 주소·경로·로그는 넣지 않습니다.
태그는 공개 main에 포함되고 패키지 버전과 같아야 합니다. 검증한 동일 산출물을 게시하고 게시 job은 재빌드하지 않습니다.
공개 태그·파일은 덮어쓰지 않습니다. wheel은 런타임이고 시스템 서비스 설치에는 Git checkout이 필요합니다.
운영 자동 배포·PyPI는 별도 범위입니다. 실제 수신과 fixture 성공을 구분해 기록합니다.
