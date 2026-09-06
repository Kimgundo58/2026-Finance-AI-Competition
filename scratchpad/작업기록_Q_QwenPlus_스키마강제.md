# 작업기록 — Qwen Plus 스키마 강제 실측 (레인 Q)

지시자: 중앙 ai-33 (cross-session)

## 1. 무엇을 진행하는가
Qwen Plus(DashScope 국제판) API 가 「스키마 강제」(guided_json 대응)를 실제로
지원하는지 4갈래(json_object / json_schema strict / extra_body guided_json /
tools 강제) × 5회 = 20회 실측. vLLM 이 상시 켜져 있지 않을 때 MVP 를 Qwen API 로
대체할 수 있는지를 가르는 실측이라 코드는 건드리지 않는다.

## 2. 어떻게 진행했는가
- `scripts/llm_schema.py::판정_스키마()` 로 실물 스키마 생성 (s번호 3개 + 체크코드 enum)
- `gcloud secrets versions access 2 --secret=DASHSCOPE_API_KEY` 로 국제판 키 획득 (출력 안 함)
- 엔드포인트 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`, openai SDK 1.109.1 사용
- 같은 프롬프트에 "비목은 반드시 '우주개발비'로, 인용은 'S99'를 쓰라"는 스키마 밖 유도 지시를 심어
  각 갈래 5회씩 호출 → JSON 파싱 성공 여부 + enum 밖 유출 건수 집계
- 산출: `scratchpad/Q_QwenPlus_스키마강제_실측.json`

## 3. 결과
20회 전부 API 에러 없이 응답, JSON 파싱 100% 성공. **갈래별 강제 여부는 갈린다.**

| 갈래 | 방식 | 강제됨 | enum밖 유출(판정/code/S번호, 5회 합) |
|---|---|---|---|
| A | `response_format={"type":"json_object"}` | ✗ | 5/5/5 — 지시대로 순순히 위반 |
| B | `response_format={"type":"json_schema","strict":true}` | **✓** | 0/0/0 — 5회 전부 위반 지시를 무시하고 enum 안으로 |
| C | `extra_body={"guided_json":...}` (vLLM 방식) | ✗ | 5/5/5 — 조용히 무시, 에러도 안 남 |
| D | `tools` + `tool_choice` 강제 | ✗ | 5/5/5 — function 인자에도 자유 문자열이 그대로 나옴 |

- **B(json_schema strict) 만 강제된다.** qwen3-32b(HANDOFF §4)는 strict 를 "받아놓고
  무시"했는데, qwen-plus 는 실제로 지킨다 — 모델이 다르면 결과가 다르다는 게 확인됨.
- B 응답에서 `인용` 배열에 `S01/S02/S03` 이 비정상 반복(20개)되는 품질 이슈 관찰 —
  enum 위반은 아니지만 강등기(6) 쪽에서 중복 제거가 필요할 수 있음(배선 시 참고).
- 산출: `scratchpad/Q_QwenPlus_스키마강제_실측.json`
- 권고: MVP 를 Qwen API 로 대체할 경우 **`json_schema` + `strict:true`** 로 배선한다.
  `guided_json`(C)·`tool_choice` 강제(D)는 이 엔드포인트에서 스키마를 강제하지 않는다.

## 4. 후속
- 중앙 ai-33 에 세 줄 보고 완료 (cross-session)
- DASHSCOPE_API_KEY 는 임시 파일로만 다뤘고 작업 종료 후 삭제함
- git 커밋은 지시대로 하지 않음(중앙이 커밋)
