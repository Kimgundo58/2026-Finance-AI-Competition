# -*- coding: utf-8 -*-
"""판정 파이프라인의 LLM 클라이언트. OpenAI 호환 API(기본값 DashScope 국제 리전)를 부른다.

normalize_run(정규화 호출, 폼 경로 비목후보 추출)과 orchestrate(판정 호출)가 쓰는 유일한 LLM 백엔드다.
공개: llm_호출(프롬프트, 스키마, *, 모델, 온도, 최대토큰, 타임아웃, 재시도, 추론, 추론끔_여유초, 스트림, 폴백,
마감까지_재시도, 조각_대기초) -> (출력, 메타), 예외 LLM실패.
환경변수: SUDDOE_LLM_API_KEY(필수, 없으면 DASHSCOPE_API_KEY), SUDDOE_LLM_BASE_URL(기본값 DashScope 국제 리전),
SUDDOE_QWEN_MODEL, SUDDOE_QWEN_FALLBACK_MODEL. 다른 OpenAI 호환 라우터(예: Runyour AI)를 쓰면 BASE_URL 과
모델 ID(예: qwen/qwen3.7-plus)를 그 라우터 기준으로 넣는다.
스키마를 주면 response_format(json_schema, strict)으로 JSON 출력을 강제한다.
모든 실패는 LLM실패 로 올린다. 호출자는 이를 확인 필요(실패)로 반환한다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx
from openai import OpenAI
from research_runtime import current_budget, is_final_call


class LLM실패(Exception):
    """LLM 호출 또는 응답 파싱 실패. 호출자는 확인 필요(실패)로 반환한다."""


class _예산초과(LLM실패):
    """서버 예산(research_runtime) 의 LLM 횟수 · 시간이 끝났다. 다시 보내도 같으므로 재시도 · 폴백하지 않는다."""


class _출력잘림(LLM실패):
    """max_tokens 에 닿아(finish_reason length) JSON 이 끝나지 않았다. 추론을 켠 호출이면 추론이 출력 몫을 다 쓴 것이다."""


class _조각멈춤(LLM실패):
    """스트리밍 응답의 다음 조각이 `조각_대기초` 동안 오지 않았다. 연결이 멈춘 것으로 보고 바로 다시 보낸다(마감까지_재시도)."""


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
               조각_대기초: float | None = None) -> tuple[str, Any, Any, str | None, float]:
    """stream=True 로 받아 (내용, 추론내용, usage, 종료이유, 최대간격초) 를 돌려준다.

    게이트웨이 경로는 응답 바이트 없이 약 60초가 지나면 연결을 끊는다. 2026-11-15 운영 서버 IP 경유 실측, 같은 판정 프롬프트
    (70,334자, qwen3.7-plus, 추론 켬): 일반 요청 62.4초 APIConnectionError · 스트리밍 91.4초 완료(첫 조각 6.2초, 조각 사이 최대 0.3초).
    운영 판정 기록도 게이트웨이(모델명 qwen/…, 11-03~) 35건은 성공한 호출이 모두 60초 미만(최대 57.4초)이고, DashScope 를 바로
    부르던 9월에는 96초 성공도 있었다. 스트리밍은 추론 조각이 계속 와서 끊기지 않는다.
    SDK timeout 은 조각 사이 대기에만 걸리므로 전체 대기 상한(kwargs['timeout'])은 조각이 올 때마다 여기서 잰다. 넘으면
    TimeoutError(= 시간 초과 실패). 조각이 멈춘 동안은 SDK timeout(같은 값)까지 기다린다.
    `조각_대기초` 를 주면 조각 사이 대기(httpx read timeout)만 그 값으로 줄이고, 그동안 조각이 없으면 `_조각멈춤` 을 올린다.
    이때는 조각마다 서버 예산도 본다: 요청이 취소(판정 마감 초과 · 연결 끊김)되면 `_예산초과` 로 바로 멈춘다.
    최대간격초 = 요청 시작부터 첫 조각까지를 포함해 조각 사이 가장 긴 대기(메타 `최대간격ms`).
    """
    import openai
    전체초 = float(kwargs["timeout"])
    마감 = time.time() + 전체초
    budget = None
    if 조각_대기초 is not None:
        kwargs = {**kwargs, "timeout": httpx.Timeout(전체초, read=min(조각_대기초, 전체초))}
        budget = current_budget()
    직전 = time.time()
    최대간격 = 0.0
    내용: list[str] = []
    추론: list[str] = []
    usage = 종료 = None
    try:
        s = client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
    except openai.APITimeoutError as e:
        if 조각_대기초 is None:
            raise
        raise _조각멈춤(f"응답이 {조각_대기초:.0f}초 동안 오지 않음(첫 조각 전)") from e
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
                    # DashScope 는 reasoning_content, 게이트웨이는 reasoning 필드에 추론 조각을 담는다
                    조각 = getattr(d, "reasoning_content", None) or (getattr(d, "model_extra", None) or {}).get("reasoning")
                    if 조각:
                        추론.append(조각 if isinstance(조각, str) else json.dumps(조각, ensure_ascii=False))
                if c.finish_reason:
                    종료 = c.finish_reason
    except httpx.TimeoutException as e:
        if 조각_대기초 is None:
            raise
        raise _조각멈춤(f"다음 응답 조각이 {조각_대기초:.0f}초 동안 오지 않음"
                     f"(받은 조각 {len(내용) + len(추론)}개, 최대간격 {최대간격:.1f}초)") from e
    finally:
        s.close()
    return "".join(내용), "".join(추론), usage, 종료, 최대간격


def _호출_1회(client: OpenAI, 모델: str, 프롬프트: str, 스키마: dict | None,
           온도: float, 최대토큰: int, 타임아웃: int,
           추론: bool | None, 스트림: bool = False, *, 조각_대기초: float | None = None) -> tuple[Any, dict]:
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
        내용, 추론내용, usage, 종료이유, 최대간격 = _스트림_받기(client, kwargs, 조각_대기초)
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
        메타["최대간격ms"] = int(최대간격 * 1000)   # 스트리밍만. 첫 조각 대기를 포함한 조각 사이 가장 긴 대기
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
_일시오류_쉼초 = (1.0, 2.0, 4.0)
_일시오류_추가 = len(_일시오류_쉼초)


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
_마감재시도_쉼초 = (2.0, 4.0, 8.0, 15.0)


def _스트림끊김(e: Exception) -> bool:
    """스트리밍 도중 연결이 끊겼다(httpx 오류가 SDK 에 감싸이지 않고 올라온다). 시간 초과는 따로 본다."""
    return isinstance(e, httpx.TransportError) and not isinstance(e, httpx.TimeoutException)


def _마감까지_호출(client: OpenAI, 모델: str, 프롬프트: str, 스키마: dict | None, 온도: float, 최대토큰: int,
              타임아웃: int, 추론: bool | None, 스트림: bool, 조각_대기초: float | None) -> tuple[Any, dict]:
    """판정 호출 전용(`llm_호출(마감까지_재시도=True)`). 서버 예산의 남은 시간이 마감이다(예산이 없으면 `타임아웃` 초).

    - 일시 오류(403 · 429 · 5xx · 연결 끊김)는 `_마감재시도_쉼초`(2 · 4 · 8 · 15 · 15 …초) 만큼 쉬며 마감까지 다시 보낸다.
    - 조각 멈춤(`_조각멈춤`)과 그 밖의 시간 초과는 쉬지 않고 바로 다시 보낸다.
    - 추론을 끄고 다시 보내지 않는다. 출력 잘림은 max_tokens 를 두 배로 늘려 추론을 켠 채 한 번 더 보낸다.
    - 시도마다 대기 상한은 남은 시간 전체다. JSON 깨짐 · 그 밖의 즉시 오류는 한 번 더 보낸다.
    - 실패한 시도는 모두 경고 로그, 끝내 실패하면 오류 로그를 남긴다(운영 docker logs 로 셀 수 있게).
    메타에 `시도`(보낸 횟수)를 넣는다.
    """
    budget = current_budget()
    마감 = None if budget is not None else time.monotonic() + 타임아웃

    def 남은초() -> float:
        return budget.remaining_seconds() if budget is not None else max(0.0, 마감 - time.monotonic())

    n = 일시 = 0
    잘림재시도 = 즉시재시도 = False
    마지막 = None
    while True:
        남은 = 남은초()
        if 남은 <= 0:
            _log.error("[판정 LLM] 실패 — 시간 예산 끝남, %d회 시도, 마지막 오류: %s", n, 마지막)
            raise _예산초과(f"LLM 호출 시간 초과({n}회 시도, 마지막 오류: {마지막})")
        n += 1
        try:
            출력, 메타 = _호출_1회(client, 모델, 프롬프트, 스키마, 온도, 최대토큰, 남은, 추론, 스트림,
                              조각_대기초=조각_대기초)
            메타["시도"] = n
            if n > 1:
                _log.warning("[판정 LLM] %d번째 시도에 받음(앞선 실패: %s)", n, 마지막)
            return 출력, 메타
        except _예산초과 as e:
            보낸 = n - 1 if str(e).startswith("LLM 호출") else n    # 「LLM 호출 …」 은 보내기 전에 예약 · 시간에서 막힌 것
            _log.error("[판정 LLM] 실패 — 예산 끝남(%s), %d회 시도, 마지막 오류: %s", e, 보낸, 마지막)
            raise _예산초과(f"{e}({보낸}회 시도, 마지막 오류: {마지막})") from e
        except _출력잘림 as e:
            마지막 = str(e)
            if not 잘림재시도:
                잘림재시도, 최대토큰 = True, 최대토큰 * 2
                _log.warning("[판정 LLM] %d번째 시도 출력 잘림 — max_tokens %d 로 다시 보냄: %s", n, 최대토큰, e)
                continue
        except _조각멈춤 as e:
            마지막 = str(e)
            _log.warning("[판정 LLM] %d번째 시도 멈춤 — 바로 다시 보냄: %s", n, e)
            continue
        except json.JSONDecodeError as e:
            마지막 = f"JSON 파싱 실패: {e}"
            if not 즉시재시도:
                즉시재시도 = True
                _log.warning("[판정 LLM] %d번째 시도 %s — 한 번 더 보냄", n, 마지막)
                continue
        except Exception as e:  # noqa: BLE001 — SDK·HTTP·타임아웃 오류를 모두 LLM실패 로 바꾼다
            마지막 = f"{type(e).__name__}: {str(e)[:200]}"
            if _일시오류(e) or _스트림끊김(e):
                쉼 = _마감재시도_쉼초[min(일시, len(_마감재시도_쉼초) - 1)]
                if 남은초() > 쉼 + 1:
                    일시 += 1
                    _log.warning("[판정 LLM] %d번째 시도 일시 오류 — %.0f초 뒤 다시 보냄: %s", n, 쉼, 마지막)
                    time.sleep(쉼)
                    continue
            elif _시간초과(e):
                _log.warning("[판정 LLM] %d번째 시도 시간 초과 — 바로 다시 보냄: %s", n, 마지막)
                continue
            elif not 즉시재시도:
                즉시재시도 = True
                _log.warning("[판정 LLM] %d번째 시도 %s — 한 번 더 보냄", n, 마지막)
                continue
        break
    _log.error("[판정 LLM] 실패 — %d회 시도, 마지막 오류: %s", n, 마지막)
    raise LLM실패(f"{마지막 or '알 수 없는 실패'}({n}회 시도)")


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
            조각_대기초: float | None = None) -> tuple[Any, dict]:
    """프롬프트 하나를 보내고 (출력, 메타) 를 돌려준다.

    출력은 스키마를 주면 파싱한 JSON, 아니면 문자열이다. 메타에는 지연ms·토큰·종료이유·모델·
    추론내용있음/추론내용길이가 들어간다.
    기본 모델로 `재시도`+1 번 시도한다. 게이트웨이 일시 오류(`_일시오류`: 403 · 429 · 5xx · 연결 끊김)는 이 횟수와 따로
    `_일시오류_쉼초` 만큼 쉬며 최대 `_일시오류_추가` 번 더 보낸다. 마지막 실패가 JSON 깨짐이면 한 번 더 보낸다.
    그래도 실패하면 폴백 모델로 한 번 더 보낸다.
    `모델` 을 기본 모델이 아닌 값으로 직접 지정하면 폴백하지 않는다. 서버 예산이 끝난 실패는 다시 보내지 않는다.
    `추론=False` 는 모델과 무관하게 thinking 을 끈다(정규화처럼 값을 뽑기만 하는 호출용).
    추론을 켠 시도가 출력 잘림(`_출력잘림`)이나 시간 초과로 끝나면 같은 요청을 되풀이하지 않고 추론을 끄고 한 번 더 보낸다.
    `추론끔_여유초` 를 주면 추론을 켠 시도의 대기 시간을 서버 예산의 남은 시간에서 그만큼 빼고 준다. 그 시간이
    `_추론_최소초` 보다 짧으면 처음부터 추론을 끈다. 추론이 길어져도 추론 없는 답을 받을 시간이 남는다.
    `스트림=True` 는 응답을 조각으로 받는다(`_스트림_받기`, 60초 넘게 걸리는 호출이 게이트웨이 경로에서 끊기지 않게).
    `폴백=False` 면 폴백 모델로 보내지 않고 기본 모델의 마지막 실패를 올린다(판정 호출, 사용자 2026-11-15: 3.8 flash 말고 3.7-plus).
    `마감까지_재시도=True`(판정 호출, 사용자 2026-11-15)는 위 횟수 · 추론 끄기 · 폴백 규칙 대신 `_마감까지_호출` 규칙을 쓴다:
    서버 예산이 끝날 때까지 다시 보내고 추론을 끈 답은 받지 않는다. `재시도` · `추론끔_여유초` · `폴백` 은 보지 않는다.
    `조각_대기초` 는 스트리밍에서 다음 조각을 기다리는 상한이다(넘으면 그 시도를 멈춘다, `_스트림_받기`).
    모든 실패는 LLM실패 로 올린다.
    """
    쓸_모델 = 모델 or 기본_모델
    if _모델_이름(쓸_모델) in _금지_모델:
        raise LLM실패(f"'{쓸_모델}' 은 사용 금지 모델명이다 — json_schema 지원 목록에 없다")
    최대토큰 = max(최대토큰, _최소_출력토큰)

    client = _client_얻기()
    if 마감까지_재시도:
        return _마감까지_호출(client, 쓸_모델, 프롬프트, 스키마, 온도, 최대토큰, 타임아웃, 추론, 스트림, 조각_대기초)
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
                쉼 = _일시오류_쉼초[일시]
                budget = current_budget()
                if budget is None or budget.remaining_seconds() > 쉼 + 1:
                    일시 += 1
                    time.sleep(쉼)
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
