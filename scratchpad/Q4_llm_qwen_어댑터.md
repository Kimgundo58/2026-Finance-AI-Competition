# scripts/llm_qwen.py — Qwen API 어댑터 (레인 Q, 새 파일)

기존 vLLM 경로(`normalize_run.py`)는 예외 타입(`LLM실패`) 하나만 재사용하고 손대지
않음. 새 파일 `scripts/llm_qwen.py` 1개 추가 — `git status` 상 `??`(untracked)만
있고 기존 파일 diff는 0. 커밋 안 함.

## 1) 스키마 강제
`response_format={"type":"json_schema","strict":true}` — vLLM의 최상위 `guided_json`
은 이 엔드포인트에서 조용히 무시됨(레인 Q 1차 실측), 그래서 다른 방식을 씀.
스키마는 `llm_schema.판정_스키마()`/`체크코드_enum()`을 호출자가 만들어 그대로 넘김 —
이 파일은 스키마를 복제하지 않음.

## 2) 모델명 — 버전 고정만
`SUDDOE_QWEN_MODEL` 기본 `qwen3.7-plus`, `SUDDOE_QWEN_FALLBACK_MODEL` 기본
`qwen3.8-flash`. 맨 `qwen-plus`/`qwen-max`/`qwen-turbo`/`qwen-flash` 별칭은
`_금지_모델`에 넣어 호출 즉시 `LLM실패`로 막음 — 근거: 오늘 실측은 재현됐지만
공식 문서 지원 목록에 없는 별칭이라(Q2 §①) 배선에 안 씀.

## 3) 스위치 SUDDOE_LLM=vllm|qwen — **부분 구현, 구조적 한계 발견**
🔴 완전한 스위치는 이 레인 범위(새 파일만)로 못 끝낸다. 이유:
- `orchestrate.py`는 `from normalize_run import llm_호출`로 **import 시점에 이름을
  복사**한다 → ④(판정) 호출은 `orchestrate.llm_호출`을 나중에 덮어쓰면 바뀐다.
- `normalize_run.정규화()`는 **자기 모듈 전역의 llm_호출을 직접 참조**한다 → ①(정규화)
  호출을 바꾸려면 `normalize_run.llm_호출` 자체를 덮어써야 한다.
- 즉 두 자리를 **따로** 몽키패치해야 하고, 그것도 `orchestrate.py`가 import되기
  **전에** 실행돼야 한다(`eval_e2e.py:356,362`가 테스트용으로 이미 같은 패턴을 씀).
- 이걸 걸 자리(서버 기동 스크립트? orchestrate.py의 CLI 진입점?)는 기존 파일을
  건드려야 해서 "새 파일만" 범위를 벗어난다. 필요한 스니펫은 `llm_qwen.py`
  docstring에 그대로 적어뒀다 — 진입점만 정해지면 바로 붙일 수 있다.

**요청**: 이 스위치를 어느 진입점에 걸지(서버 기동 vs orchestrate.py CLI vs 별도
부트스트랩 파일) 정해주면 다음 턴에 그 파일만 최소 diff로 건드리겠다.

## 4) 판정 1건 실제 통과 — 원출력
gold_id=330(예비창업패키지, "회의 한 번에 1인당 6만원짜리 식사") 실프롬프트(24,221자·
s맵 79개·code 40개)로 `llm_qwen.llm_호출()` 직접 호출 (모델 기본값 `qwen3.7-plus`,
위반유도 없음 — 순수 정상 호출).

```json
메타: {"지연ms": 52601, "토큰": {"prompt_tokens": 13947, "completion_tokens": 2906,
       "total_tokens": 16853}, "종료이유": "stop", "모델": "qwen3.7-plus",
       "추론content있음": true, "추론content길이": 6761}

출력: {
  "판정": "불가",
  "요약": "연구활동비(S22)에서는 참여연구자만 참여하는 회의의 식비를 계상할 수 없으며, ...
           연구혁신비로 계상하더라도 S04 제3호에 따라 인건비의 성격을 가지는 비용은
           연구혁신비로 계상할 수 없다.",
  "해야할일": [],
  "인용": ["S07", "S22", "S04"],
  "전제": []
}
```
- **정답판정 == 모델출력판정 (둘 다 "불가")** ✅
- enum 밖 인용: 0건, 판정 enum 위반: 없음
- 🔴 completion_tokens 2,906 / 요청 max_tokens 3,000 — **여유 94토큰뿐**. qwen3.7-plus
  도 사고를 많이 하면(이번엔 추론content 6,761자) 상한에 바짝 붙는다. flash처럼 통째로
  폭주하진 않지만, 3,000은 여유가 박하다 — 프로덕션에서는 3,500~4,000 권장.

## 유료 호출 집계 (이번 지시분, 10회 이내)
이전 재현 3건(6-4b 통합)에 이어 이번에 1회 추가 — 총 5회. 예산 10회 중 5회 사용.

## 산출
- `scripts/llm_qwen.py` (신규, 커밋 안 함)
- `scratchpad/Q4_llm_qwen_실통과.json` (원출력 전문)
