# -*- coding: utf-8 -*-
"""Qwen(DashScope 국제판) LLM 어댑터.

`llm_호출` 은 `normalize_run.llm_호출` 과 같은 계약(파싱된 출력 또는 raw 문자열, 메타)을 따른다.
`스위치_적용()` 이 환경변수 `SUDDOE_LLM`(vllm|qwen) 에 따라 `normalize_run`·`orchestrate` 의
`llm_호출` 을 한 번에 바꿔 끼운다. 서버 기동 시 한 번은 반드시 불려야 한다.

JSON 강제는 `response_format`(json_schema, strict) 을 쓴다 — vLLM 의 `guided_json` 은 이 엔드포인트에서
조용히 무시된다.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import OpenAI

import normalize_run as _nr_모듈
from normalize_run import LLM실패  # noqa: E402 — 예외 타입만 재사용

# 패치 전 원본 llm_호출. SUDDOE_LLM=vllm 이면 이 객체 그 자체로 되돌린다.
_원본_vllm_llm_호출 = _nr_모듈.llm_호출

ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
기본_모델 = os.environ.get("SUDDOE_QWEN_MODEL", "qwen3.7-plus")
폴백_모델 = os.environ.get("SUDDOE_QWEN_FALLBACK_MODEL", "qwen3.8-flash")
# 판정 출력이 3,000 토큰에 바짝 붙으므로 Qwen 경로는 호출자가 뭘 넘기든 최소 이만큼은 준다.
_최소_출력토큰 = 3500

# 공식 문서의 json_schema 지원 목록에 없는 별칭. 배선 금지.
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
    """flash 계열(켜두면 출력 폭주)과 오픈소스 qwen3-* 계열(비스트리밍 호출을 400 으로 거부)은 thinking 을 끈다."""
    return "flash" in 모델 or 모델.startswith("qwen3-")


def _호출_1회(client: OpenAI, 모델: str, 프롬프트: str, 스키마: dict | None,
           온도: float, 최대토큰: int, 타임아웃: int,
           사고: bool | None = None) -> tuple[Any, dict]:
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
    # `사고=False` 를 명시하면 모델과 무관하게 thinking 을 끈다(정규화처럼 사고가 필요 없는 호출용).
    if 사고 is False or _thinking_꺼야하나(모델):
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
            재시도: int = 1,
            사고: bool | None = None,
            **_무시) -> tuple[Any, dict]:
    """`normalize_run.llm_호출` 과 같은 계약을 따르는 DashScope 판.

    `모델` 생략 시 `SUDDOE_QWEN_MODEL`. 재시도를 다 쓰면 `SUDDOE_QWEN_FALLBACK_MODEL` 로 1회 더 시도하되,
    호출자가 모델을 직접 지정했으면 폴백하지 않는다.
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
            return _호출_1회(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 사고)
        except json.JSONDecodeError as e:
            마지막 = f"JSON 파싱 실패: {e}"
        except Exception as e:  # noqa: BLE001 — SDK/HTTP/타임아웃 전부
            마지막 = f"{type(e).__name__}: {str(e)[:200]}"

    if 모델 is None and 폴백_모델 != 쓸_모델 and 폴백_모델 not in _금지_모델:
        try:
            return _호출_1회(client, 폴백_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 사고)
        except Exception as e:  # noqa: BLE001
            마지막 = f"{마지막} · 폴백({폴백_모델})도 실패: {type(e).__name__}: {str(e)[:200]}"

    raise LLM실패(마지막 or "알 수 없는 실패")


def 스위치_적용() -> str:
    """`SUDDOE_LLM`(vllm|qwen, 기본 vllm)에 맞춰 `normalize_run.llm_호출` 과 `orchestrate.llm_호출` 을
    같은 값으로 맞춘다. 몇 번을 불러도 안전하다. 적용된 백엔드 이름을 돌려준다.
    """
    backend = os.environ.get("SUDDOE_LLM", "vllm")
    if backend == "qwen":
        새값 = llm_호출
    elif backend == "vllm":
        새값 = _원본_vllm_llm_호출
    else:
        raise LLM실패(f"SUDDOE_LLM={backend!r} — 'vllm' 또는 'qwen' 만 허용")

    _nr_모듈.llm_호출 = 새값
    import orchestrate as _orch_모듈  # noqa: PLC0415 — 지연 import
    _orch_모듈.llm_호출 = 새값
    return backend
