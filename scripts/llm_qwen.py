# -*- coding: utf-8 -*-
"""판정 파이프라인의 LLM 클라이언트. DashScope 국제 리전(International)의 OpenAI 호환 API 를 부른다.

normalize_run(정규화 호출, 폼 경로 비목후보 추출)과 orchestrate(판정 호출)가 쓰는 유일한 LLM 백엔드다.
공개: llm_호출(프롬프트, 스키마, *, 모델, 온도, 최대토큰, 타임아웃, 재시도, 추론) -> (출력, 메타), 예외 LLM실패.
환경변수: DASHSCOPE_API_KEY(필수), SUDDOE_QWEN_MODEL, SUDDOE_QWEN_FALLBACK_MODEL.
스키마를 주면 response_format(json_schema, strict)으로 JSON 출력을 강제한다.
모든 실패는 LLM실패 로 올린다. 호출자는 이를 판단불가로 반환한다.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import OpenAI


class LLM실패(Exception):
    """LLM 호출 또는 응답 파싱 실패. 호출자는 판단불가로 반환한다."""


ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
기본_모델 = os.environ.get("SUDDOE_QWEN_MODEL", "qwen3.7-plus")
폴백_모델 = os.environ.get("SUDDOE_QWEN_FALLBACK_MODEL", "qwen3.8-flash")
# 판정 출력이 3,000 토큰 가까이 나오므로 호출자가 더 작게 넘겨도 최소 이만큼은 준다.
_최소_출력토큰 = 3500

# DashScope 문서의 json_schema 지원 모델 목록에 없는 별칭이라 출력 형식을 보장할 수 없다. 사용 금지.
_금지_모델 = {"qwen-plus", "qwen-max", "qwen-turbo", "qwen-flash"}

_client: OpenAI | None = None


def _client_얻기() -> OpenAI:
    global _client
    if _client is None:
        key = os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            raise LLM실패("DASHSCOPE_API_KEY 환경변수가 없다")
        # SDK 자체 재시도(기본 2회)를 끈다. 재시도는 llm_호출 의 루프 한 곳에서만 한다
        _client = OpenAI(api_key=key, base_url=ENDPOINT, max_retries=0)
    return _client


def _추론_비활성_대상(모델: str) -> bool:
    """flash 계열은 추론을 켜면 출력이 지나치게 길어지고, 오픈소스 qwen3-* 계열은 비스트리밍
    호출에서 추론을 켜면 400 으로 거부한다. 두 계열은 항상 끈다."""
    return "flash" in 모델 or 모델.startswith("qwen3-")


def _호출_1회(client: OpenAI, 모델: str, 프롬프트: str, 스키마: dict | None,
           온도: float, 최대토큰: int, 타임아웃: int,
           추론: bool | None) -> tuple[Any, dict]:
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
    if 추론 is False or _추론_비활성_대상(모델):
        kwargs["extra_body"] = {"enable_thinking": False}

    t = time.time()
    r = client.chat.completions.create(**kwargs)
    내용 = r.choices[0].message.content or ""
    추론내용 = getattr(r.choices[0].message, "reasoning_content", None) or ""
    usage = r.usage
    메타 = {
        "지연ms": int((time.time() - t) * 1000),
        "토큰": {"prompt_tokens": getattr(usage, "prompt_tokens", None),
               "completion_tokens": getattr(usage, "completion_tokens", None),
               "total_tokens": getattr(usage, "total_tokens", None)},
        "종료이유": r.choices[0].finish_reason,
        "모델": 모델,
        "최대토큰": 최대토큰,        # 실제로 보낸 max_tokens (_최소_출력토큰 적용 후)
        "추론내용있음": bool(추론내용),
        "추론내용길이": len(추론내용),
    }
    if 스키마 is None:
        return 내용, 메타
    return json.loads(내용), 메타  # 파싱 실패는 json.JSONDecodeError 로 올라가 호출자가 잡는다


def llm_호출(프롬프트: str, 스키마: dict | None, *,
            모델: str | None = None,
            온도: float = 0.0,
            최대토큰: int = 1500,
            타임아웃: int = 240,
            재시도: int = 1,
            추론: bool | None = None) -> tuple[Any, dict]:
    """프롬프트 하나를 보내고 (출력, 메타) 를 돌려준다.

    출력은 스키마를 주면 파싱한 JSON, 아니면 문자열이다. 메타에는 지연ms·토큰·종료이유·모델·
    추론내용있음/추론내용길이가 들어간다.
    `모델` 을 생략하면 기본 모델로 `재시도`+1 번 시도하고, 모두 실패하면 폴백 모델로 한 번 더
    시도한다. 모델을 직접 지정하면 폴백하지 않는다. 최악의 지연은 (재시도+2) x 타임아웃이다.
    `추론=False` 는 모델과 무관하게 thinking 을 끈다(정규화처럼 값을 뽑기만 하는 호출용).
    모든 실패는 LLM실패 로 올린다.
    """
    쓸_모델 = 모델 or 기본_모델
    if 쓸_모델 in _금지_모델:
        raise LLM실패(f"'{쓸_모델}' 은 사용 금지 모델명이다 — json_schema 지원 목록에 없다")
    최대토큰 = max(최대토큰, _최소_출력토큰)

    client = _client_얻기()
    마지막 = None
    for _ in range(재시도 + 1):
        try:
            return _호출_1회(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론)
        except json.JSONDecodeError as e:
            마지막 = f"JSON 파싱 실패: {e}"
        except Exception as e:  # noqa: BLE001 — SDK·HTTP·타임아웃 오류를 모두 LLM실패 로 바꾼다
            마지막 = f"{type(e).__name__}: {str(e)[:200]}"

    if 모델 is None and 폴백_모델 != 쓸_모델 and 폴백_모델 not in _금지_모델:
        try:
            return _호출_1회(client, 폴백_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론)
        except Exception as e:  # noqa: BLE001
            마지막 = f"{마지막} · 폴백({폴백_모델})도 실패: {type(e).__name__}: {str(e)[:200]}"

    raise LLM실패(마지막 or "알 수 없는 실패")
