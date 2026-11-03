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
둘 다 실패하면 판정은 판단불가로 반환된다. json_schema 를 지원하지 않는 모델 별칭
(qwen-plus 등)은 코드에서 사용을 막는다. 최신 성능은 [[0_현황]].

코드: `scripts/llm_qwen.py` · `llm_호출()`
```python
    for _ in range(재시도 + 1):
        try:
            return _호출_1회(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론)
        ...
    if 모델 is None and 폴백_모델 != 쓸_모델 and _모델_이름(폴백_모델) not in _금지_모델:
        try:
            return _호출_1회(client, 폴백_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론)
        ...
    raise LLM실패(마지막 or "알 수 없는 실패")
```

"DashScope 는 thinking·json_schema 를 같이 못 쓴다"는 `qwen3-32b` 한정이다.
`qwen3.7-plus`는 둘을 같이 쓴다. 정규화는 추출 작업이라 thinking 을 꺼서 44.0초 → 4.1초
(출력토큰 2,466→97)로 줄었다. 판정(④)은 thinking 을 켠 채로 쓴다.

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
