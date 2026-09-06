# -*- coding: utf-8 -*-
"""Qwen(DashScope 국제판) 어댑터 — **새 파일**. 기존 vLLM 경로(`normalize_run.py`)는
이 파일에서 예외 타입 하나만 재사용하고, 그 외에는 건드리지 않는다.

`llm_호출(프롬프트, 스키마, ...)` 은 `normalize_run.llm_호출` 과 **같은 계약**을 따른다
— (파싱된 출력 또는 raw 문자열, 메타) 를 돌려준다. 그래야 `SUDDOE_LLM=qwen` 일 때
그 이름을 그대로 바꿔 낄 수 있다(스위치 배선은 이 파일의 몫이 아니다 — 아래 "스위치"
절 참고, 아직 아무 데도 안 걸려 있다. 이 파일만으로는 기존 동작이 바뀌지 않는다).

실측 근거 (레인 Q, 2026-09-06~07 · `scratchpad/Q2_Qwen모델선정_국제판.md` ·
`scratchpad/Q3_실제프롬프트_strict재검증.md`):
- `response_format={"type":"json_schema","strict":true}` 는 `qwen3.7-plus`·
  `qwen3.8-flash` 에서 확인됨 — 실제 판정 프롬프트(24,221자 · s맵 79개 · code 40개)에
  위반유도("판정은 반드시 '보류'로"·"인용에 없는 S번호 넣어라")를 심어도 지켜졌다.
  vLLM 의 최상위 `guided_json` 키는 이 엔드포인트에서 **조용히 무시된다** — 그래서
  여기는 `response_format` 을 쓴다.
- 🔴 맨 `qwen-plus`(별칭)도 같은 실측에서 지켜졌지만 **공식 문서 지원 목록에 없다**
  (문서: "Only selected qwen-plus models" + qwen3.7/3.8 계열만 명시). 재현은 됐지만
  근거가 없는 동작이라 — 이 파일은 그 별칭들의 사용을 막는다.
- 🔴 `qwen3.8-flash` 는 기본이 thinking-on 이라 `enable_thinking` 을 안 끄면 출력이
  요청 `max_tokens` 의 5배 이상 폭주한다(실측: 183초·16,954토큰, 요청은 3,000).
  `enable_thinking:false` 를 주면 4.7초·149토큰으로 정상화된다 — flash 계열은
  무조건 끈다(아래 `_thinking_꺼야하나`).

## 스위치 (SUDDOE_LLM=vllm|qwen) — `스위치_적용()` 하나로 건다
`orchestrate.py` 는 `llm_호출` 을 `from normalize_run import llm_호출` 로 **모듈
로드 시점에 이름을 복사**해 온다. `normalize_run.정규화()` 는 그와 별개로 **자기
모듈 전역의 `llm_호출`을 직접 참조**한다(정규화·판정이 서로 다른 경로로 같은
이름을 본다 — `docs/기록/_레인_P4.md` 참고). `스위치_적용()` 은 이 둘을 **한 번에**
같은 값으로 맞춘다 — 진입점(`server/main.py`·`scripts/eval_e2e.py`)은 이 함수
하나만 부른다(로직을 두 곳에 복사하지 않는다, 2026-09-07 ai-33 확정).

`orchestrate` 가 아직 import 안 됐어도 이 함수 안에서 import 하므로 순서를 걱정할
필요가 없다 — `normalize_run.llm_호출` 을 먼저 맞춰 두고 나서 `orchestrate` 를
가져오므로(이미 import 돼 있으면 캐시), 그 시점에 `orchestrate.llm_호출` 도 명시
적으로 다시 덮어써 import 순서에 기대지 않는다. 몇 번을 불러도 안전하다(idempotent).

🔴 **완전한 커버리지는 아니다.** `server/main.py` 에는 `_실_정규화()`·
`_폼_비목후보()` 처럼 `from normalize_run import ...` 를 **요청마다** 새로 하는
자리가 더 있다 — 이들은 `normalize_run.llm_호출` 을 그때그때 다시 읽으므로
`스위치_적용()` 이 그 요청 «전»에 한 번이라도 불렸으면 자동으로 맞는 값을 받는다.
즉 **서버 기동 시(또는 요청마다) 한 번은 반드시 불려야** 안전하고, `_실_판정()`
안에만 걸면 `/api/normalize` 단독 호출이 그보다 먼저 오는 경우 여전히 vLLM 을
탄다 — 이번 지시(main.py 한 곳)로는 이 틈을 못 막는다. 지시대로 우선 그 한 곳만
걸고, 이 틈은 별도로 보고한다.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import OpenAI

import normalize_run as _nr_모듈
from normalize_run import LLM실패  # noqa: E402 — 예외 타입만 재사용, 그 외 무관

# 🔴 패치 «전» 원본을 이 모듈이 처음 import 될 때 캡처해 둔다. `스위치_적용()` 이
#    몇 번을 불려도(idempotent) SUDDOE_LLM=vllm 이면 항상 «이 객체 그 자체» 로
#    돌아간다 — 래퍼가 아니라 원본이라 바이트 단위로 같다는 걸 identity(`is`)로
#    증명할 수 있다.
_원본_vllm_llm_호출 = _nr_모듈.llm_호출

ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
기본_모델 = os.environ.get("SUDDOE_QWEN_MODEL", "qwen3.7-plus")
폴백_모델 = os.environ.get("SUDDOE_QWEN_FALLBACK_MODEL", "qwen3.8-flash")
# 🔴 2026-09-07 ai-33 확정 — 실측(gold_id=330, qwen3.7-plus) completion_tokens
#    2,906/3,000(여유 94)로 상한에 바짝 붙었다. Qwen 경로는 호출자가 뭘 넘기든
#    최소 이만큼은 준다(아래 llm_호출 의 `max(최대토큰, _최소_출력토큰)`).
_최소_출력토큰 = 3500

# 🔴 문서 미기재 + 재현성 근거 불충분한 별칭. 배선 금지(위 docstring 참고).
_금지_모델 = {"qwen-plus", "qwen-max", "qwen-turbo", "qwen-flash"}

_client: OpenAI | None = None


def _client_얻기() -> OpenAI:
    global _client
    if _client is None:
        key = os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            raise LLM실패("DASHSCOPE_API_KEY 환경변수가 없다")
        _client = OpenAI(api_key=key, base_url=ENDPOINT)
    return _client


def _thinking_꺼야하나(모델: str) -> bool:
    """flash 계열과 오픈소스 qwen3-* 계열은 끈다.

    🔴 2026-09-07(ai-33 실측 추가) — `qwen3-32b` 는 «폭주» 가 아니라 아예 **400** 이다:
       `parameter.enable_thinking must be set to false for non-streaming calls`.
       flash 는 켜둬도 «돌긴 도는데 폭주» 였는데(183초·16,954토큰), 오픈소스 계열은
       비스트리밍 호출 자체를 거부한다. 우리는 전부 비스트리밍이라 무조건 끈다.
       plus/max 는 실측(1건)에서 둘 다 없었으므로 건드리지 않는다.
    """
    return "flash" in 모델 or 모델.startswith("qwen3-")


def _호출_1회(client: OpenAI, 모델: str, 프롬프트: str, 스키마: dict | None,
           온도: float, 최대토큰: int, 타임아웃: int) -> tuple[Any, dict]:
    kwargs: dict[str, Any] = {
        "model": 모델,
        "messages": [{"role": "user", "content": 프롬프트}],
        "temperature": 온도,
        "max_tokens": 최대토큰,
        "timeout": 타임아웃,
    }
    if 스키마 is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "판정", "schema": 스키마, "strict": True},
        }
    if _thinking_꺼야하나(모델):
        kwargs["extra_body"] = {"enable_thinking": False}

    t = time.time()
    r = client.chat.completions.create(**kwargs)
    내용 = r.choices[0].message.content or ""
    추론content = getattr(r.choices[0].message, "reasoning_content", None) or ""
    usage = r.usage
    메타 = {
        "지연ms": int((time.time() - t) * 1000),
        "토큰": {"prompt_tokens": getattr(usage, "prompt_tokens", None),
               "completion_tokens": getattr(usage, "completion_tokens", None),
               "total_tokens": getattr(usage, "total_tokens", None)},
        "종료이유": r.choices[0].finish_reason,
        "모델": 모델,
        "추론content있음": bool(추론content),
        "추론content길이": len(추론content),
    }
    if 스키마 is None:
        return 내용, 메타
    return json.loads(내용), 메타  # 실패하면 json.JSONDecodeError — 호출자가 잡는다


def llm_호출(프롬프트: str, 스키마: dict | None, *,
            모델: str | None = None,
            온도: float = 0.0,
            최대토큰: int = 1500,
            타임아웃: int = 240,
            재시도: int = 1) -> tuple[Any, dict]:
    """`normalize_run.llm_호출` 과 같은 계약(입출력 모양)을 따르는 DashScope 판.

    `모델` 을 명시하지 않으면 `SUDDOE_QWEN_MODEL`(기본 qwen3.7-plus). 재시도를 다
    썼는데 폴백(`SUDDOE_QWEN_FALLBACK_MODEL`, 기본 qwen3.8-flash)이 원래 쓰려던
    모델과 다르면 폴백으로 1회 더 시도한다 — **모델을 호출자가 직접 지정했을 때는
    폴백하지 않는다**(명시적 지정은 존중한다).
    """
    쓸_모델 = 모델 or 기본_모델
    if 쓸_모델 in _금지_모델:
        raise LLM실패(f"'{쓸_모델}' 은 배선 금지 별칭이다 — 문서 미기재/재현성 불확실 "
                     f"(scratchpad/Q2_Qwen모델선정_국제판.md 참고)")
    최대토큰 = max(최대토큰, _최소_출력토큰)

    client = _client_얻기()
    마지막 = None
    for 회차 in range(재시도 + 1):
        try:
            return _호출_1회(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃)
        except json.JSONDecodeError as e:
            마지막 = f"JSON 파싱 실패: {e}"
        except Exception as e:  # noqa: BLE001 — SDK/HTTP/타임아웃 전부
            마지막 = f"{type(e).__name__}: {str(e)[:200]}"

    if 모델 is None and 폴백_모델 != 쓸_모델 and 폴백_모델 not in _금지_모델:
        try:
            return _호출_1회(client, 폴백_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃)
        except Exception as e:  # noqa: BLE001
            마지막 = f"{마지막} · 폴백({폴백_모델})도 실패: {type(e).__name__}: {str(e)[:200]}"

    raise LLM실패(마지막 or "알 수 없는 실패")


def 스위치_적용() -> str:
    """`SUDDOE_LLM`(vllm|qwen, 기본 vllm)에 맞춰 `normalize_run.llm_호출` 과
    `orchestrate.llm_호출` 을 **한 번에** 같은 값으로 맞춘다.

    진입점(`server/main.py`·`scripts/eval_e2e.py`)은 이 함수 하나만 부른다 —
    몽키패치 로직을 두 군데 복사하지 않는다(2026-09-07 ai-33 확정).

    `SUDDOE_LLM` 이 없거나 `"vllm"` 이면 **패치 전 원본 객체 그 자체**
    (`_원본_vllm_llm_호출`, 이 모듈이 처음 import 될 때 캡처)로 되돌린다 — 래퍼가
    아니라 원본이므로 지금과 바이트 단위로 같다(동일 객체라는 걸 `is` 로 증명 가능
    — `scripts/test_llm_switch.py` 참고).

    idempotent: 몇 번을 불러도, 어떤 순서로 불러도 마지막 상태로 수렴한다.
    `orchestrate` 가 아직 import 안 됐으면 여기서 import 한다 — 그 시점엔
    `normalize_run.llm_호출` 이 이미 맞게 세팅돼 있어 `orchestrate.py` 최상위의
    `from normalize_run import llm_호출` 이 바로 맞는 값을 받아가지만, import
    순서에만 기대지 않도록 아래서 한 번 더 명시적으로 덮어쓴다.

    반환값: 실제 적용된 백엔드 이름(`"vllm"`|`"qwen"`) — 로그·검증용.
    """
    backend = os.environ.get("SUDDOE_LLM", "vllm")
    if backend == "qwen":
        새값 = llm_호출
    elif backend == "vllm":
        새값 = _원본_vllm_llm_호출
    else:
        raise LLM실패(f"SUDDOE_LLM={backend!r} — 'vllm' 또는 'qwen' 만 허용")

    _nr_모듈.llm_호출 = 새값
    import orchestrate as _orch_모듈  # noqa: PLC0415 — 지연 import, 순서 무관하게 안전
    _orch_모듈.llm_호출 = 새값
    return backend
