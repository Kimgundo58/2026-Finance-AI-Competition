# LLM 백엔드 — 모델·thinking·검증

이 문서가 답하는 질문:
- 호출 지점별로 지금 어떤 모델이 실행되는가
- 모델을 어떤 절차로 검증·교체하나

(호출 지점·예산·분기 자체는 [[6-1_호출_설계]].)

## 1. 모델 (`SUDDOE_QWEN_MODEL=qwen3.7-plus`)

LLM 백엔드는 DashScope 국제 리전(International) 하나다(`scripts/llm_qwen.py`, OpenAI 호환 엔드포인트,
`response_format` JSON schema strict). 백엔드 선택 환경변수는 없다.
LLM 호출 지점은 ① 정규화·④ 판정 생성 둘뿐이다. L3 파싱과 문의초안은 코드다([[6-1_호출_설계]] §1).

코드: `scripts/llm_qwen.py` · 모듈 상수
```python
_DASHSCOPE_ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
ENDPOINT = os.environ.get("SUDDOE_LLM_BASE_URL", "").strip() or _DASHSCOPE_ENDPOINT
기본_모델 = os.environ.get("SUDDOE_QWEN_MODEL", "qwen3.7-plus")
폴백_모델 = os.environ.get("SUDDOE_QWEN_FALLBACK_MODEL", "qwen3.8-flash")
...
# DashScope 문서의 json_schema 지원 모델 목록에 없는 별칭이라 출력 형식을 보장할 수 없다. 사용 금지.
_금지_모델 = {"qwen-plus", "qwen-max", "qwen-turbo", "qwen-flash"}
```

코드: `scripts/llm_qwen.py` · `_호출_1회()`
```python
    if 스키마 is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "판정", "schema": 스키마, "strict": True},
        }
    if 추론 is False or _추론_비활성_대상(모델):
        kwargs["extra_body"] = _추론_끄기_옵션()
```

| 호출 지점 | 모델 | 실행 | thinking |
|---|---|---|---|
| ① 정규화 | Qwen3.7-plus | DashScope 국제 리전 | Off(추출 작업이라 불필요) |
| ④ 판정 생성 | Qwen3.7-plus | DashScope 국제 리전 | On |

기본 모델 호출이 실패하면 `SUDDOE_QWEN_FALLBACK_MODEL`(기본 qwen3.8-flash)로 한 번 더 호출한다.
④ 판정만은 폴백하지 않고 qwen3.7-plus 로만 낸다(`폴백=False`, 사용자 2026-11-15).
판정은 스트리밍으로 받는다(`스트림=True`): 게이트웨이 경로는 응답 바이트 없이 약 60초가 지나면 연결을 끊는다
(2026-11-15 운영 IP 실측, 같은 판정 프롬프트: 일반 요청 62.4초 연결 오류 · 스트리밍 91.4초 완료).
판정은 `마감까지_재시도=True`(`_마감까지_호출`)로 부른다. 마감은 서버 예산의 남은 시간이고, /api/judge 가 시작할 때 600초 · LLM 40회 이상으로 늘린다(`Budget.ensure_remaining`, 서버 마감 660초).
```text
├─ 403 · 429 · 5xx · 연결 끊김 → 2 · 4 · 8 · 15 · 15 …초 쉬고 다시 / 30초 동안 조각 없음 · 시간 초과 → 바로 다시
├─ 출력 잘림 → max_tokens 2배 · 추론 켠 채 한 번 더(추론 끈 답은 받지 않음) / JSON 깨짐 · 그 밖 → 한 번 더
└─ 실패한 시도마다 경고 로그 「[판정 LLM] …」, 끝내 실패하면 오류 로그 + 화면 「다시 점검」(응답 `판정실패`)
```
메타 `시도` · `최대간격ms`(첫 조각 대기를 포함한 조각 사이 최대 대기)는 `decisions.모델` 에 남는다(실측 전).
json_schema 를 지원하지 않는 모델 별칭(qwen-plus 등)은 코드에서 사용을 막는다. 최신 성능은 [[0_현황]].

코드: `scripts/llm_qwen.py` · `llm_호출()`
```python
    while n < 시도:
        n += 1
        ...
        try:
            return _호출_1회(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 이번_타임아웃, 추론 if 추론켬 else False, 스트림)
        ...
    if 폴백 and (모델 is None or 모델 == 기본_모델) and 폴백_모델 != 쓸_모델 and _모델_이름(폴백_모델) not in _금지_모델:
        try:
            return _호출_1회(client, 폴백_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론, 스트림)
        ...
    raise LLM실패(마지막 or "알 수 없는 실패")
```

"DashScope 는 thinking·json_schema 를 같이 못 쓴다"는 `qwen3-32b` 한정이다.
`qwen3.7-plus`는 둘을 같이 쓴다. 정규화는 추출 작업이라 thinking 을 꺼서 44.0초 → 4.1초
(출력토큰 2,466→97)로 줄었다. 판정(④)은 thinking 을 켠 채로 쓴다.
게이트웨이(Runyour)는 추론 길이를 묶지 못한다(effort low → thinking_budget 8192, reasoning.max_tokens ·
thinking_budget 무시, 2026-11-12 실측). 그래서 판정은 max_tokens 8192(+조건마다 120)로 두고, 잘리면 두 배로 한 번 더 보낸다.
추론을 끄고 다시 보내던 길(`추론끔_여유초`)은 판정에서 뺐다: 운영 decision 4308 의 「가능」이 그 답이었다.
같은 판정 프롬프트 실측: 추론 켬 27~58초 · 출력 1,544~3,544 토큰, 추론 끔 3~5초 · 출력 103~120 토큰.
(스트리밍 전 게이트웨이 경로 운영 판정 35건(11-03~11-14)은 성공한 호출이 모두 60초 미만, 최대 57.4초였다.
DashScope 를 바로 부르던 9월에는 96초 성공도 있었다.)

코드: `scripts/normalize_run.py` · `정규화()`
```python
    # 값을 뽑기만 하는 호출이라 추론을 끈다. 출력 토큰이 모자라면 finish_reason='length' 로 잘린 JSON 이
    # 파싱에 실패하고 LLM실패 가 된다
    출력, 메타 = llm_호출(프롬프트, 스키마, 추론=False, 최대토큰=2000, 타임아웃=타임아웃)
```

## 2. 모델 교체 검증 절차

```
1. baseline → 2. 투표 N=3~5 → 3. 금지목록 판정을 코드로 분리 → 4. 최상위 모델과 성능 차이 측정
→ 5. 차이가 있으면 신뢰도 보정 → 6. 배포 구성 그대로 골든셋 재실행
```

투표 불일치는 판단불가로 보낸다. 파인튜닝을 한다면 대상은 **판정 신뢰도 보정**이다
(기관별 수치 학습 금지). 모델이나 프롬프트를 바꿀 때는 이 절차를 다시 거치고,
골든셋 재실행(`python scripts/orchestrate.py --golden`)에서 '가능' 오판 0건을 유지해야 한다.

## 관련 문서

[[6-1_호출_설계]] · [[6-4_검증과_하향]] · [[0_현황]]
