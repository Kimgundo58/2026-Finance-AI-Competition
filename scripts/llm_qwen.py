# -*- coding: utf-8 -*-
"""판정 파이프라인의 LLM 클라이언트. OpenAI 호환 API(기본값 DashScope 국제 리전)를 부른다.

normalize_run(정규화 호출, 폼 경로 비목후보 추출)과 orchestrate(판정 호출)가 쓰는 유일한 LLM 백엔드다.
공개: llm_호출(프롬프트, 스키마, *, 모델, 온도, 최대토큰, 타임아웃, 재시도, 추론, 추론끔_여유초, 스트림, 폴백,
마감까지_재시도, 청크_대기초) -> (출력, 메타), 예외 LLM실패.
환경변수: SUDDOE_LLM_API_KEY(필수, 없으면 DASHSCOPE_API_KEY), SUDDOE_LLM_BASE_URL(기본값 DashScope 국제 리전),
SUDDOE_QWEN_MODEL, SUDDOE_QWEN_FALLBACK_MODEL. 다른 OpenAI 호환 라우터(예: Runyour AI)를 쓰면 BASE_URL 과
모델 ID(예: qwen/qwen3.7-plus)를 그 라우터 기준으로 넣는다.
스키마를 주면 response_format(json_schema, strict)으로 JSON 출력을 강제한다.
마감까지_재시도=False 의 실패는 LLM실패 로 올린다. 마감까지_재시도=True(운영 경로)의 실패는 LLM시스템오류 로 올린다:
호출자는 이를 확인 필요로 바꾸지 않고 판정실패 · 다시시도로 응답한다(v053).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx
from openai import OpenAI
from research_runtime import current_budget, is_final_call, 단계상한초과, 시스템오류, 재시도_기록


class LLM실패(Exception):
    """LLM 호출 또는 응답 파싱 실패. 호출자는 확인 필요(실패)로 반환한다."""


class _예산초과(LLM실패):
    """서버 예산(research_runtime) 의 LLM 횟수 · 시간이 끝났다. 다시 보내도 같으므로 재시도 · 폴백하지 않는다."""


class _출력잘림(LLM실패):
    """max_tokens 에 닿아(finish_reason length) JSON 이 끝나지 않았다. 추론을 켠 호출이면 추론이 출력 몫을 다 쓴 것이다."""


class _청크무응답(LLM실패):
    """스트리밍 응답의 다음 청크가 `청크_대기초` 동안 오지 않았다. 연결이 멈춘 것으로 보고 바로 다시 보낸다(마감까지_재시도)."""


_log = logging.getLogger(__name__)


_DASHSCOPE_ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
ENDPOINT = os.environ.get("SUDDOE_LLM_BASE_URL", "").strip() or _DASHSCOPE_ENDPOINT
기본_모델 = os.environ.get("SUDDOE_QWEN_MODEL", "qwen3.7-plus")
폴백_모델 = os.environ.get("SUDDOE_QWEN_FALLBACK_MODEL", "qwen3.8-flash")
# 호출자가 더 작게 넘겨도 최소 이만큼은 준다. 추론을 켠 판정은 추론 토큰이 이 대부분을 쓴다
# (2026-11-12 운영 게이트웨이 실측, 같은 판정 프롬프트: 추론 1,436~3,436 토큰, 답 JSON 은 100 토큰 안팎).
_최소_출력토큰 = 3500
# 추론을 켠 호출에 `추론끔_여유초` 를 남기고도 이만큼 시간이 없으면 처음부터 추론을 끄고 보낸다.
# 같은 실측에서 추론을 켠 판정은 27~58초, 끈 판정은 3~4초였다.
_추론_최소초 = 20

# DashScope 문서의 json_schema 지원 모델 목록에 없는 별칭이라 출력 형식을 보장할 수 없다. 사용 금지.
_금지_모델 = {"qwen-plus", "qwen-max", "qwen-turbo", "qwen-flash"}

_client: OpenAI | None = None


def _client_얻기() -> OpenAI:
    global _client
    if _client is None:
        key = (os.environ.get("SUDDOE_LLM_API_KEY", "").strip()
               or os.environ.get("DASHSCOPE_API_KEY", "").strip())
        if not key:
            raise LLM실패("LLM API 키가 없다(SUDDOE_LLM_API_KEY 또는 DASHSCOPE_API_KEY)")
        # SDK 자체 재시도(기본 2회)를 끈다. 재시도는 llm_호출 의 루프 한 곳에서만 한다
        _client = OpenAI(api_key=key, base_url=ENDPOINT, max_retries=0)
    return _client


def _모델_이름(모델: str) -> str:
    """게이트웨이용 'qwen/qwen3.7-plus' 같은 공급자 접두어를 떼고 모델 이름만 돌려준다."""
    return 모델.rsplit("/", 1)[-1]


def _추론_비활성_대상(모델: str) -> bool:
    """flash 계열은 추론을 켜면 출력이 지나치게 길어지고, 오픈소스 qwen3-* 계열은 비스트리밍
    호출에서 추론을 켜면 400 으로 거부한다. 두 계열은 항상 끈다."""
    이름 = _모델_이름(모델)
    return "flash" in 이름 or 이름.startswith("qwen3-")


def _추론_끄기_옵션() -> dict:
    """DashScope 는 enable_thinking 을 받는다. OpenAI 호환 게이트웨이(Runyour 등)는 이 값을
    무시하고 reasoning.effort='none' 만 추론을 끈다. effort 를 low/medium 으로 주면
    게이트웨이가 thinking_budget 8192 로 바꿔 보내므로 예산 조절에는 쓰지 않는다.
    2026-11-12 Runyour 실측: reasoning.max_tokens · thinking_budget(최상위, 통과 헤더 포함)도 추론 길이를 줄이지 못했다.
    추론 길이를 묶을 방법이 없어 시간으로 묶는다(llm_호출 의 `추론끔_여유초`)."""
    if ENDPOINT == _DASHSCOPE_ENDPOINT:
        return {"enable_thinking": False}
    return {"reasoning": {"effort": "none"}}


def _스트림_받기(client: OpenAI, kwargs: dict[str, Any],
               청크_대기초: float | None = None) -> tuple[str, Any, Any, str | None, float]:
    """stream=True 로 받아 (내용, 추론내용, usage, 종료이유, 최대간격초) 를 돌려준다.

    게이트웨이 경로는 응답 바이트 없이 약 60초가 지나면 연결을 끊는다. 2026-11-15 운영 서버 IP 경유 실측, 같은 판정 프롬프트
    (70,334자, qwen3.7-plus, 추론 켬): 일반 요청 62.4초 APIConnectionError · 스트리밍 91.4초 완료(첫 청크 6.2초, 청크 사이 최대 0.3초).
    운영 판정 기록도 게이트웨이(모델명 qwen/…, 11-03~) 35건은 성공한 호출이 모두 60초 미만(최대 57.4초)이고, DashScope 를 바로
    부르던 9월에는 96초 성공도 있었다. 스트리밍은 추론 청크가 계속 와서 끊기지 않는다.
    SDK timeout 은 청크 사이 대기에만 걸리므로 전체 대기 상한(kwargs['timeout'])은 청크가 올 때마다 여기서 잰다. 넘으면
    TimeoutError(= 시간 초과 실패). 청크가 멈춘 동안은 SDK timeout(같은 값)까지 기다린다.
    `청크_대기초` 를 주면 청크 사이 대기(httpx read timeout)만 그 값으로 줄이고, 그동안 청크가 없으면 `_청크무응답` 을 올린다.
    이때는 청크마다 서버 예산도 본다: 요청이 취소(판정 마감 초과 · 연결 끊김)되면 `_예산초과` 로 바로 멈춘다.
    최대간격초 = 요청 시작부터 첫 청크까지를 포함해 청크 사이 가장 긴 대기(메타 `최대청크간격ms`).
    """
    import openai
    전체초 = float(kwargs["timeout"])
    마감 = time.time() + 전체초
    budget = None
    if 청크_대기초 is not None:
        kwargs = {**kwargs, "timeout": httpx.Timeout(전체초, read=min(청크_대기초, 전체초))}
        budget = current_budget()
    직전 = time.time()
    최대간격 = 0.0
    내용: list[str] = []
    추론: list[str] = []
    usage = 종료 = None
    try:
        s = client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
    except openai.APITimeoutError as e:
        if 청크_대기초 is None:
            raise
        raise _청크무응답(f"응답이 {청크_대기초:.0f}초 동안 오지 않음(첫 청크 전)") from e
    try:
        for ch in s:
            지금 = time.time()
            최대간격, 직전 = max(최대간격, 지금 - 직전), 지금
            if 지금 > 마감:
                raise TimeoutError(f"스트리밍 응답이 {전체초:.0f}초 안에 끝나지 않음")
            if budget is not None and budget.remaining_seconds() <= 0:
                raise _예산초과("LLM 응답 중 시간 예산이 끝나거나 요청이 취소됨")
            if getattr(ch, "usage", None):
                usage = ch.usage
            for c in ch.choices or ():
                d = c.delta
                if d is not None:
                    if d.content:
                        내용.append(d.content)
                    # DashScope 는 reasoning_content, 게이트웨이는 reasoning 필드에 추론 청크를 담는다
                    청크 = getattr(d, "reasoning_content", None) or (getattr(d, "model_extra", None) or {}).get("reasoning")
                    if 청크:
                        추론.append(청크 if isinstance(청크, str) else json.dumps(청크, ensure_ascii=False))
                if c.finish_reason:
                    종료 = c.finish_reason
    except httpx.TimeoutException as e:
        if 청크_대기초 is None:
            raise
        raise _청크무응답(f"다음 응답 청크가 {청크_대기초:.0f}초 동안 오지 않음"
                     f"(받은 청크 {len(내용) + len(추론)}개, 최대간격 {최대간격:.1f}초)") from e
    finally:
        s.close()
    return "".join(내용), "".join(추론), usage, 종료, 최대간격


def _호출_1회(client: OpenAI, 모델: str, 프롬프트: str, 스키마: dict | None,
           온도: float, 최대토큰: int, 타임아웃: int,
           추론: bool | None, 스트림: bool = False, *, 청크_대기초: float | None = None) -> tuple[Any, dict]:
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
        kwargs["extra_body"] = _추론_끄기_옵션()

    budget = current_budget()
    if budget is not None:
        if not budget.reserve_llm(final=is_final_call()):
            raise _예산초과("LLM 호출 예산 또는 시간 초과")
        남은시간 = budget.remaining_seconds()
        if 남은시간 <= 0:
            raise _예산초과("LLM 호출 시간 초과")
        kwargs["timeout"] = min(타임아웃, 남은시간)
    t = time.time()
    최대간격 = None
    if 스트림:
        내용, 추론내용, usage, 종료이유, 최대간격 = _스트림_받기(client, kwargs, 청크_대기초)
    else:
        r = client.chat.completions.create(**kwargs)
        msg = r.choices[0].message
        내용 = msg.content or ""
        # DashScope 는 reasoning_content, 게이트웨이는 reasoning 필드에 추론 텍스트를 담는다
        추론내용 = (getattr(msg, "reasoning_content", None)
                 or (msg.model_extra or {}).get("reasoning") or "")
        usage, 종료이유 = r.usage, r.choices[0].finish_reason
    if budget is not None and budget.remaining_seconds() <= 0:
        raise _예산초과("LLM 응답 시간 초과")
    if not isinstance(추론내용, str):
        추론내용 = json.dumps(추론내용, ensure_ascii=False)
    메타 = {
        "지연ms": int((time.time() - t) * 1000),
        "토큰": {"prompt_tokens": getattr(usage, "prompt_tokens", None),
               "completion_tokens": getattr(usage, "completion_tokens", None),
               "total_tokens": getattr(usage, "total_tokens", None)},
        "종료이유": 종료이유,
        "모델": 모델,
        "스트림": 스트림,
        "프롬프트자수": len(프롬프트),
        "스키마자수": len(json.dumps(스키마, ensure_ascii=False)) if 스키마 is not None else 0,
        "최대토큰": 최대토큰,        # 실제로 보낸 max_tokens (_최소_출력토큰 적용 후)
        "추론내용있음": bool(추론내용),
        "추론내용길이": len(추론내용),
    }
    if 최대간격 is not None:
        메타["최대청크간격ms"] = int(최대간격 * 1000)   # 스트리밍만. 첫 청크 대기를 포함한 청크 사이 가장 긴 대기
    if 스키마 is None:
        return 내용, 메타
    try:
        return json.loads(내용), 메타  # 파싱 실패는 json.JSONDecodeError 로 올라가 호출자가 잡는다
    except json.JSONDecodeError as e:
        if 메타["종료이유"] == "length":
            raise _출력잘림(f"출력 잘림(max_tokens {최대토큰}, 출력 {메타['토큰']['completion_tokens']} 토큰, "
                         f"추론 {메타['추론내용길이']}자)") from e
        raise


# 게이트웨이의 간헐 거부(403 Workspace endpoint access denied · 429 · 5xx · 연결 끊김). 같은 요청을 잠시 뒤 다시 보내면
# 대개 통과한다. 2026-11-12 로컬 디버그: 한 번 판정 흐름 LLM 12회 중 4회 403, 확인 질문은 403 두 번 뒤 폴백(flash)이 질문 0개.
# 주 모델로 최대 _일시오류_추가 번 더 보낸다(사용자 2026-11-12: 호출 수 · 비용은 상관없다)
_일시오류_백오프초 = (1.0, 2.0, 4.0)
_일시오류_추가 = len(_일시오류_백오프초)


def _일시오류(e: Exception) -> bool:
    import openai
    return isinstance(e, (openai.PermissionDeniedError, openai.RateLimitError,
                          openai.InternalServerError, openai.APIConnectionError)) and not _시간초과(e)


def _시간초과(e: Exception) -> bool:
    """응답을 기다리다 끝난 실패인가. 그런 실패는 다시 보내면 시간만 더 쓴다."""
    import openai
    return isinstance(e, (openai.APITimeoutError, TimeoutError))


# 판정 호출(마감까지_재시도)의 일시 오류 재시도 간격. 다섯 번째부터는 마지막 값(15초)을 되풀이한다(사용자 2026-11-15:
# 시간 · 호출 수는 상관없고 판정이 오류로 끝나지 않게). 2026-11-15 전체 실행 두 번: LLM 호출 35회 중 8회 · 28회 중 5회가 403,
# 모두 다음 시도에 통과했다.
_마감재시도_백오프초 = (2.0, 4.0, 8.0, 15.0)
# 마감까지_재시도 의 비스트리밍 시도 하나의 대기 하한(초). 호출자의 `타임아웃`(질문 20 · 분류 40초 등)이 더 짧아도 이만큼은 기다린다:
# 짧게 끊으면 느린 정상 응답이 매번 시간 초과로 끝나 마감까지 같은 실패만 되풀이한다. 게이트웨이가 응답 없는 연결을 약 60초에 끊는다(_스트림_받기)
_시도_최소초 = 60.0


def _스트림끊김(e: Exception) -> bool:
    """스트리밍 도중 연결이 끊겼다(httpx 오류가 SDK 에 감싸이지 않고 올라온다). 시간 초과는 따로 본다."""
    return isinstance(e, httpx.TransportError) and not isinstance(e, httpx.TimeoutException)


class LLM시스템오류(시스템오류):
    """마감까지_재시도 호출이 마감(서버 예산의 남은 시간, `마감초` 가 더 짧으면 그 값)까지 받을 수 있는 출력을 받지 못했다(v053).
    LLM실패 를 잇지 않는다: `except LLM실패` 로 확인 필요 · 질문 없음으로 넘기던 곳에 걸리지 않고 라우트까지 올라가
    판정실패 · 다시시도가 된다(research_runtime.시스템오류)."""


class LLM단계상한초과(LLM시스템오류, 단계상한초과):
    """서버 예산보다 `마감초`(그 단계의 시간 상한)가 먼저 끝나 멈췄다(v060). 예산은 남아 있다. 받지 않은 곳에서는 LLM시스템오류 와 같다."""


def _재시도_대상_아님(e: Exception) -> bool:
    """다시 보내도 같은 결과인 요청 오류(400 · 401 · 404 · 422). 한 번 더 · 폴백 한 번 뒤에는 마감까지 기다리지 않는다."""
    import openai
    종류 = tuple(c for c in (getattr(openai, n, None) for n in ("BadRequestError", "AuthenticationError", "NotFoundError",
                                                              "UnprocessableEntityError")) if isinstance(c, type))
    return bool(종류) and isinstance(e, 종류)


def _마감까지_호출(client: OpenAI, 모델: str, 프롬프트: str, 스키마: dict | None, 온도: float, 최대토큰: int,
              타임아웃: int, 추론: bool | None, 스트림: bool, 청크_대기초: float | None, *,
              폴백: bool = False, 마감초: float | None = None, 단계: str = "판정") -> tuple[Any, dict]:
    """운영 경로의 LLM 호출(`llm_호출(마감까지_재시도=True)`, 판정 v052 · 나머지 단계 v053). 마감까지 다시 보낸다.

    마감 = 서버 예산의 남은 시간과 `마감초`(그 단계의 상한, 예: ② 품목 조사의 남은 조사 시간) 중 짧은 쪽.
    예산도 `마감초` 도 없으면(CLI · 검사) `타임아웃` 초.
    - 일시 오류(403 · 429 · 5xx · 연결 끊김)는 `_마감재시도_백오프초`(2 · 4 · 8 · 15 · 15 …초) 만큼 기다리며(지수 백오프) 마감까지 다시 보낸다.
    - 청크 무응답(`_청크무응답`)과 그 밖의 시간 초과는 기다리지 않고 바로 다시 보낸다.
    - 추론을 끄고 다시 보내지 않는다. 출력 잘림은 max_tokens 를 두 배로 늘려 한 번 더 보낸다.
    - JSON 깨짐 · 출력 잘림 반복은 한 번은 바로, 그다음부터는 지수 백오프로 마감까지 다시 보낸다.
      요청 오류(400 · 401 · 404 · 422, `_재시도_대상_아님`)는 한 번 더 보낸 뒤 마감을 기다리지 않고 실패로 끝낸다.
    - 폴백: `폴백=True` 이고 기본 모델이면 즉시 오류가 두 번 나거나 일시 오류가 `_일시오류_추가` 번 나면 폴백 모델로 한 번 보낸다
      (기존 llm_호출 폴백 규칙). 그 뒤에도 실패하면 기본 모델로 마감까지 다시 보낸다. 판정 LLM 은 폴백=False(사용자 2026-11-15).
    - 한 시도의 대기 상한: 스트리밍 + 청크_대기초(판정)는 마감까지 전체(청크 무응답이 멈춤을 잡는다), 그 밖은 max(`타임아웃`, `_시도_최소초`) 와 마감 중 짧은 쪽.
    - 실패한 시도는 모두 경고 로그 · 재시도 기록(research_runtime.재시도_기록), 끝내 실패하면 오류 로그를 남긴다.
    메타에 `시도`(보낸 횟수)를 넣는다. 끝내 실패하면 LLM시스템오류(단계 · 원인 · 시도). 서버 예산이 남았는데 `마감초` 가 먼저 끝났으면
    그 하위 클래스 LLM단계상한초과 다(v060: 공개 조사 계획 LLM 이 「조사 시간 끝남」 으로 받는다).
    """
    budget = current_budget()
    지금 = time.monotonic()
    로컬마감 = 지금 + 마감초 if 마감초 is not None else (지금 + 타임아웃 if budget is None else None)

    def 남은초() -> float:
        남 = [budget.remaining_seconds()] if budget is not None else []
        if 로컬마감 is not None:
            남.append(max(0.0, 로컬마감 - time.monotonic()))
        return min(남)

    def 단계상한이_먼저() -> bool:
        """서버 예산보다 `마감초`(단계 시간 상한)가 1초 넘게 먼저 끝나는가. 호출자가 예산의 남은 시간을 `마감초` 로 넘기면 두 값이
        거의 같아 예산 끝남으로 본다. 예산이 없는 호출(CLI · 검사)은 단계 상한이 아니라 `타임아웃` 이 마감이다."""
        return (마감초 is not None and budget is not None
                and 로컬마감 - time.monotonic() + 1.0 < budget.remaining_seconds())

    def 마감_실패(앞말: str) -> LLM시스템오류:
        """마감 실패 예외와 오류 로그. 단계 시간 상한이 먼저면 LLM단계상한초과, 서버 예산이 마감이면 LLM시스템오류."""
        상한 = 단계상한이_먼저()
        이름 = "단계 시간 상한" if 상한 else "마감"
        _log.error("[%s LLM] 실패 — %s%s, %d회 시도, 마지막 오류: %s", 단계, 이름, 앞말, n, 마지막)
        return (LLM단계상한초과 if 상한 else LLM시스템오류)(f"{단계} LLM", f"{이름}({마지막 or '시도 전'})", n)

    def 기록(원인: str, 대기: float = 0.0) -> None:
        재시도_기록(f"{단계} LLM", 원인, n, 대기)

    n = 백오프 = 일시 = 즉시 = 0
    잘림재시도 = 폴백함 = False
    마지막 = None
    쓸모델 = 모델
    while True:
        남은 = 남은초()
        if 남은 <= 0:
            raise 마감_실패("")
        n += 1
        이번모델 = 쓸모델
        한도 = 남은 if (스트림 and 청크_대기초 is not None) else min(max(float(타임아웃), _시도_최소초), 남은)
        try:
            출력, 메타 = _호출_1회(client, 이번모델, 프롬프트, 스키마, 온도, 최대토큰, 한도, 추론, 스트림,
                              청크_대기초=청크_대기초)
            메타["시도"] = n
            if n > 1:
                _log.warning("[%s LLM] %d번째 시도에 받음(앞선 실패: %s)", 단계, n, 마지막)
            return 출력, 메타
        except _예산초과 as e:
            보낸 = n - 1 if str(e).startswith("LLM 호출") else n    # 「LLM 호출 …」 은 보내기 전에 예약 · 시간에서 막힌 것
            _log.error("[%s LLM] 실패 — 예산 끝남(%s), %d회 시도, 마지막 오류: %s", 단계, e, 보낸, 마지막)
            raise LLM시스템오류(f"{단계} LLM", f"예산 끝남({마지막 or e})", 보낸) from e
        except _출력잘림 as e:
            마지막 = str(e)
            if not 잘림재시도:
                잘림재시도, 최대토큰 = True, 최대토큰 * 2
                _log.warning("[%s LLM] %d번째 시도 출력 잘림 — max_tokens %d 로 재시도: %s", 단계, n, 최대토큰, e)
                기록("출력 잘림")
                continue
            종류 = "즉시"
        except _청크무응답 as e:
            마지막 = str(e)
            _log.warning("[%s LLM] %d번째 시도 청크 무응답 — 바로 재시도: %s", 단계, n, e)
            기록("청크 무응답")
            continue
        except json.JSONDecodeError as e:
            마지막 = f"JSON 파싱 실패: {e}"
            종류 = "즉시"
        except Exception as e:  # noqa: BLE001 — SDK·HTTP·타임아웃 오류를 모두 시스템 오류로 다룬다
            마지막 = f"{type(e).__name__}: {str(e)[:200]}"
            if _일시오류(e) or _스트림끊김(e):
                종류 = "일시"
            elif _시간초과(e):
                _log.warning("[%s LLM] %d번째 시도 시간 초과 — 바로 재시도: %s", 단계, n, 마지막)
                기록("시간 초과")
                continue
            elif _재시도_대상_아님(e):
                종류 = "요청"
            else:
                종류 = "즉시"
        if 종류 == "일시":
            일시 += 1
        else:
            즉시 += 1
        # 폴백 모델로 한 번(기존 규칙: 즉시 오류 두 번 · 일시 오류 _일시오류_추가 번). 그 뒤는 기본 모델로 돌아간다
        if (폴백 and not 폴백함 and 모델 == 기본_모델 and 폴백_모델 != 모델 and _모델_이름(폴백_모델) not in _금지_모델
                and (즉시 >= 2 or 일시 > _일시오류_추가)):
            폴백함 = True
            쓸모델 = 폴백_모델
            _log.warning("[%s LLM] %d번째 시도 실패 — 폴백 모델(%s)로 한 번 보냄: %s", 단계, n, 폴백_모델, 마지막)
            기록(f"폴백 전환: {마지막}")
            continue
        쓸모델 = 모델
        if 종류 == "요청" and 즉시 >= 2:
            break
        if 종류 != "일시" and 즉시 == 1:
            _log.warning("[%s LLM] %d번째 시도 %s — 한 번 더 재시도", 단계, n, 마지막)
            기록(마지막)
            continue
        대기 = _마감재시도_백오프초[min(백오프, len(_마감재시도_백오프초) - 1)]
        if 남은초() <= 대기 + 1:
            raise 마감_실패(" 전에 다시 보낼 시간 없음")
        백오프 += 1
        _log.warning("[%s LLM] %d번째 시도 %s 오류 — %.0f초 뒤 재시도: %s", 단계, n, 종류, 대기, 마지막)
        기록(마지막, 대기)
        time.sleep(대기)
    _log.error("[%s LLM] 실패 — 요청 오류, %d회 시도, 마지막 오류: %s", 단계, n, 마지막)
    raise LLM시스템오류(f"{단계} LLM", f"요청 오류({마지막})", n)


def llm_호출(프롬프트: str, 스키마: dict | None, *,
            모델: str | None = None,
            온도: float = 0.0,
            최대토큰: int = 1500,
            타임아웃: int = 240,
            재시도: int = 1,
            추론: bool | None = None,
            추론끔_여유초: float = 0,
            스트림: bool = False,
            폴백: bool = True,
            마감까지_재시도: bool = False,
            청크_대기초: float | None = None,
            마감초: float | None = None,
            단계: str = "판정") -> tuple[Any, dict]:
    """프롬프트 하나를 보내고 (출력, 메타) 를 돌려준다.

    출력은 스키마를 주면 파싱한 JSON, 아니면 문자열이다. 메타에는 지연ms·토큰·종료이유·모델·
    추론내용있음/추론내용길이가 들어간다.
    기본 모델로 `재시도`+1 번 시도한다. 게이트웨이 일시 오류(`_일시오류`: 403 · 429 · 5xx · 연결 끊김)는 이 횟수와 따로
    `_일시오류_백오프초` 만큼 기다리며(지수 백오프) 최대 `_일시오류_추가` 번 더 보낸다. 마지막 실패가 JSON 깨짐이면 한 번 더 보낸다.
    그래도 실패하면 폴백 모델로 한 번 더 보낸다.
    `모델` 을 기본 모델이 아닌 값으로 직접 지정하면 폴백하지 않는다. 서버 예산이 끝난 실패는 다시 보내지 않는다.
    `추론=False` 는 모델과 무관하게 thinking 을 끈다(정규화처럼 값을 뽑기만 하는 호출용).
    추론을 켠 시도가 출력 잘림(`_출력잘림`)이나 시간 초과로 끝나면 같은 요청을 되풀이하지 않고 추론을 끄고 한 번 더 보낸다.
    `추론끔_여유초` 를 주면 추론을 켠 시도의 대기 시간을 서버 예산의 남은 시간에서 그만큼 빼고 준다. 그 시간이
    `_추론_최소초` 보다 짧으면 처음부터 추론을 끈다. 추론이 길어져도 추론 없는 답을 받을 시간이 남는다.
    `스트림=True` 는 응답을 청크로 받는다(`_스트림_받기`, 60초 넘게 걸리는 호출이 게이트웨이 경로에서 끊기지 않게).
    `폴백=False` 면 폴백 모델로 보내지 않고 기본 모델의 마지막 실패를 올린다(판정 호출, 사용자 2026-11-15: 3.8 flash 말고 3.7-plus).
    `마감까지_재시도=True`(운영 경로의 모든 LLM 호출: 판정 v052, 나머지 단계 v053)는 위 횟수 · 추론 끄기 규칙 대신 `_마감까지_호출` 규칙을 쓴다:
    마감(서버 예산의 남은 시간, `마감초` 가 더 짧으면 그 값)까지 다시 보내고 추론을 끈 답은 받지 않는다. `재시도` · `추론끔_여유초` 는 보지 않고,
    `폴백` 은 같은 규칙(즉시 오류 두 번 · 일시 오류 네 번이면 폴백 모델로 한 번)으로 쓴다. 끝내 실패하면 LLM실패 가 아니라 LLM시스템오류 다.
    `단계` 는 로그 · 재시도 기록의 단계 이름이다.
    `청크_대기초` 는 스트리밍에서 다음 청크를 기다리는 상한이다(넘으면 그 시도를 멈춘다, `_스트림_받기`).
    모든 실패는 LLM실패 로 올린다.
    """
    쓸_모델 = 모델 or 기본_모델
    if _모델_이름(쓸_모델) in _금지_모델:
        raise LLM실패(f"'{쓸_모델}' 은 사용 금지 모델명이다 — json_schema 지원 목록에 없다")
    최대토큰 = max(최대토큰, _최소_출력토큰)

    client = _client_얻기()
    if 마감까지_재시도:
        return _마감까지_호출(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론, 스트림, 청크_대기초,
                          폴백=폴백 and 모델 in (None, 기본_모델), 마감초=마감초, 단계=단계)
    마지막 = None
    시도, 더보냄, n, 일시 = 재시도 + 1, False, 0, 0
    추론켬 = 추론 is not False and not _추론_비활성_대상(쓸_모델)
    while n < 시도:
        n += 1
        이번_타임아웃 = 타임아웃
        budget = current_budget()
        if 추론켬 and 추론끔_여유초 > 0 and budget is not None:
            이번_타임아웃 = min(타임아웃, budget.remaining_seconds() - 추론끔_여유초)
            if 이번_타임아웃 < _추론_최소초:
                추론켬, 이번_타임아웃 = False, 타임아웃
        try:
            return _호출_1회(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 이번_타임아웃, 추론 if 추론켬 else False, 스트림)
        except _예산초과:
            raise
        except _출력잘림 as e:
            마지막, 즉시오류 = str(e), True
            if 추론켬:          # 같은 요청은 또 잘린다. 추론을 끄고 보낸다(재시도 횟수와 따로 한 번)
                추론켬 = False
                시도 = max(시도, n + 1)
                continue
        except json.JSONDecodeError as e:
            마지막, 즉시오류 = f"JSON 파싱 실패: {e}", True
        except Exception as e:  # noqa: BLE001 — SDK·HTTP·타임아웃 오류를 모두 LLM실패 로 바꾼다
            마지막, 즉시오류 = f"{type(e).__name__}: {str(e)[:200]}", not _시간초과(e)
            if 추론켬 and _시간초과(e):   # 추론이 시간 안에 끝나지 않았다. 추론을 끄고 보낸다
                추론켬 = False
                시도 = max(시도, n + 1)
                continue
            if _일시오류(e) and 일시 < _일시오류_추가:
                대기 = _일시오류_백오프초[일시]
                budget = current_budget()
                if budget is None or budget.remaining_seconds() > 대기 + 1:
                    일시 += 1
                    time.sleep(대기)
                    시도 += 1      # 일시 오류로 다시 보내는 것은 `재시도` 횟수에서 빼지 않는다
                    continue
        if n == 시도 and 즉시오류 and not 더보냄:
            시도, 더보냄 = 시도 + 1, True

    if 폴백 and (모델 is None or 모델 == 기본_모델) and 폴백_모델 != 쓸_모델 and _모델_이름(폴백_모델) not in _금지_모델:
        try:
            return _호출_1회(client, 폴백_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론, 스트림)
        except _예산초과:
            raise
        except Exception as e:  # noqa: BLE001
            마지막 = f"{마지막} · 폴백({폴백_모델})도 실패: {type(e).__name__}: {str(e)[:200]}"

    raise LLM실패(마지막 or "알 수 없는 실패")
