# 실제 판정 프롬프트로 strict 재검증 (레인 Q, ai-33 후속지시)

방법: 코드 미수정. `orchestrate.조립` 을 런타임 몽키패치해 dry=True 로 골든셋 1건
(gold_id=330, 예비창업패키지, "회의 한 번에 1인당 6만원 식사")의 **실제 B0~B6 프롬프트**를
캡처(24,221자 · s맵 79개 · code 40개) — LLM 미호출, 비용 0. 이후 유료 호출 **4회**
(예산 6회 이내).

## 1) qwen3.7-plus — strict + 위반유도(판정→'보류', 인용→'S9999')
```
지연 16.4s · 입력 14,017토큰 · 출력 824토큰
결과: 판정="조건부"(유도 무시), 인용=["S07","S23"](s맵 안, S9999 없음)
enum 위반: 판정 0 · S번호 0  →  ✅ 강제됨(24K자 실프롬프트·79 S번호·40 code에서도)
```

## 2) qwen3.8-flash — strict, 위반유도 없음, 기본값 그대로
```
지연 183.7s(!) · 입력 13,998토큰 · 출력 16,954토큰(!! 요청 max_tokens=3,000의 5.6배)
결과: JSON 파싱 성공, enum 위반 없음 — 스키마 자체는 지킴
```
🔴 **문제 발견.** qwen3.8-flash 는 기본이 "hybrid thinking, 기본 on"(공식 문서)이라
`<think>...</think>` 추론이 `max_tokens` 를 사실상 무시하고 폭주함 — 커밋 전
"폴백 모델"로 그냥 넣으면 **지연·비용이 예측 불가**해진다(240s 타임아웃에 육박).

**해결 확인(추가 호출 1회, 총 4회)**: `extra_body={"enable_thinking": False}` 로 재호출
→ 지연 4.7s · 입력 13,962 · 출력 149토큰, strict 유지(enum 위반 0). **폴백으로 쓰려면
`enable_thinking:false` 가 필수 세트다** — 옵션이 아니라 전제조건으로 박아야 함.

## 3) qwen-plus(맨 별칭) — strict + 위반유도, 재현성 확인
```
지연 4.3s · 입력 17,057토큰 · 출력 136토큰
결과: 판정="판단불가"(유도 무시, 실제로 근거 부족한 사례라 타당한 답), 인용에 S9999 없음
enum 위반: 0 · 0  →  오늘도 재현됨
```
Q2 보고 그대로 유지: **재현은 됐지만 문서 미기재라 배선 근거로 안 쓴다.** 근거 없이
동작하는 별칭은 다음 리비전에 조용히 깨질 수 있다.

## 배선 기준(ai-33 지시) — 코드 diff는 별도 스코프로 넘긴다
env var 이름(`SUDDOE_QWEN_MODEL`)·기본값(`qwen3.7-plus`)·폴백(`qwen3.8-flash` +
`enable_thinking:false` 필수)까지는 위 실측으로 확정 가능하다. 다만 실제 배선은
"모델명 env var 치환" 수준이 아니다 — 지금 `normalize_run.llm_호출` 은 vLLM
전용(`guided_json` 최상위 키, `response_format` 없음)이라, DashScope 로 보내려면
① base_url·인증 분기 ② `guided_json`→`response_format json_schema strict` 요청 형태
전환 ③ 모델별 `enable_thinking` 분기(3.8류만 명시적으로 꺼야 함)가 같이 들어가야
한다. **diff 하나로 끝날 변경이 아니라서, 이건 별도 스코프(어댑터 계층)로 갈라
검토받는 게 맞다고 본다** — 지금 낸 값(모델명·enable_thinking 필수)은 그 작업의
입력으로 쓰면 됨.

## 산출
`scratchpad/Q3_실제프롬프트_strict재검증.json`(qwen3.7-plus/qwen-plus 원문 3건),
`scratchpad/Q3b_qwen38flash_enable_thinking_false.json`(수정 후 재현),
`scratchpad/_real_prompt_captured.json`(캡처된 실프롬프트 원문 — s맵 포함, 재사용 가능)
