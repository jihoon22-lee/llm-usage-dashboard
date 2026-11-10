# 내장 단가표 점검과 갱신

## 요청과 확인

- 단가가 없는 모델과 단가가 바뀐 모델을 찾아 고친다. 작업 시작 시 main은 `bc3754d`(v0.12.0), 브랜치는 `fix/pricing-table-refresh`다.
- 운영 DB를 읽기 전용으로 열어 `usage_hourly`에 기록된 모델 38종을 현재 적용 단가(내장 → config 폴더 `pricing.json` → DB 폴더 `pricing.json`)와 대조했다. DB와 설정은 바꾸지 않았다.
- 2026-11-10에 각 제공사의 공개 단가표를 확인했다: [Anthropic](https://platform.claude.com/docs/en/about-claude/pricing), [OpenAI](https://developers.openai.com/api/docs/pricing), [Google](https://aistudio.google.com/docs/pricing), [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing), OpenCode [Zen](https://opencode.ai/docs/zen/)·[Go](https://opencode.ai/docs/go/)(Muse Spark).

## 찾은 차이

| 모델 | 적용 중이던 값 | 공개 단가 | 영향 |
| --- | --- | --- | --- |
| `claude-haiku-5-5`, `claude-fable-5-2` | 없음(미산정) | $0.10/$0.50, $8/$40 | 11월부터 수집된 사용이 비용에서 빠짐 |
| `claude-sonnet-5-5` | 캐시 읽기 $0.20, 1시간 캐시 쓰기 없음 | 캐시 읽기 $0.10, 1시간 쓰기 $4 | 누적 환산이 약 $160 높게 나옴 |
| `claude-sonnet-5` | 9월 1일부터 $3/$15 | 인상 취소, $2/$10 유지 | 9월 1~12일 사용이 높게 나옴(1시간 쓰기 반영과 상쇄돼 약 -$2) |
| `claude-opus-5`, `claude-opus-4-7`, `claude-haiku-4-5` | 1시간 캐시 쓰기 없음(5분 단가로 계산) | $10, $10, $2 | Opus 5 누적이 약 $78 낮게 나옴 |
| `gemini-2.5-pro`, `gemini-2.5-flash` | 캐시 $0.31, $0.075 | $0.125, $0.03 | 사용 기록 없음 |
| 내장 `claude-haiku-4` | 존재하지 않는 모델 이름 | `claude-haiku-4-5` | 어떤 모델에도 적용되지 않던 항목 |

앞의 네 줄은 내장표가 아니라 운영 PC의 개인 단가 파일에 있던 값이다. 금액은 운영 기록 전체에 새 단가를 적용해 본 차이이며, 전체 환산 합계는 약 $56,018에서 $55,932로 0.15% 줄어든다.

GPT-6 Sol·6.1 Sol·Luna·Astra, GPT-5.6 Sol·Terra·Luna, Gemini 3.6~3.8 Flash·3.1 Pro·4 Argon, Muse Spark 1.2·1.3과 Contributor, DeepSeek V4.1 Flash는 적용 값과 공개 단가가 같았다.

## 변경

- 내장표에 Claude Fable 5~5.2, Opus 4.1~5, Sonnet 4.5~5.5, Haiku 4.5·5.5, GPT-6 Astra·6.1 Terra, Gemini 3.1 Pro·4 Argon, Muse Spark 1.2·1.3(Contributor 포함), DeepSeek V4.1 Flash를 추가했다. 지금까지 개인 단가 파일로만 맞추던 모델들이다.
- `claude-haiku-4`를 `claude-haiku-4-5`로 바로잡고 Gemini 2.5의 캐시 단가를 고쳤다.
- 길이·시간대별 단가가 있는 모델은 기본(가장 낮은) 구간을 쓴다는 원칙을 표 주석과 사용법에 적었다. 저장된 사용량으로는 요청이 어느 구간이었는지 알 수 없기 때문이다.
- 예고된 인상(GPT-5.6 Sol 프로모션 11월 30일까지, Gemini 4 Argon 12월 1일, Gemini 3.x Flash 2027년 1월 1일)은 기존 원칙대로 예약하지 않았다. Sonnet 5처럼 예고가 취소된 사례가 있다.
- `swe-2`는 Cognition이 토큰 단가를 공개하지 않아 내장표에 넣지 않았다. 운영 PC의 개인 값($3/$15, 기반 모델 Kimi K3 단가)은 사용자의 가정으로 남는다.

## 검증

- `tests/test_pricing_resolution.py`에 내장표 검사를 추가했다: 모든 항목의 형식, 현재 모델 21종의 단가와 변형 이름 해석, 새 버전이 이전 단가를 상속하지 않는지, 1시간 캐시 쓰기가 전용 단가로 계산되는지.
- `.venv/bin/python -m unittest discover -s tests -q` 통과, `tests/verify_record_totals.py`의 원본 대조에서 누락·불일치 0건(단가는 토큰 집계에 영향이 없다).

## 남은 일

- 운영에 반영하려면 배포가 필요하다. 그 뒤에도 개인 단가 파일의 같은 이름 항목이 내장표를 덮어쓰므로, Sonnet 5·5.5, Opus 5·4.7, Haiku 4.5의 개인 항목을 지워야 고친 값이 적용된다.
- 요청별 프롬프트 길이에 따른 구간 단가는 계산하지 않는다.
