# -*- coding: utf-8 -*-
"""(1) 입력 정규화 호출 자리 + 공용 vLLM 클라이언트.

자연어 질문 한 줄을 판정 파이프라인이 먹는 JSON `{품목, 금액, 금액_추정여부, 용도, 비목후보[], 누락필드}` 로 만든다.
`orchestrate.py` 와 `judge_run.py` 가 같은 HTTP 호출을 쓰므로 vLLM 클라이언트를 여기에 둔다 (순환 import 방지).
`--dry` 는 LLM 없이 규칙만으로 정규화한다 — 배관 검증 전용.

실행:
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --q "맥북 250만원 디자이너용"
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --golden --dry
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402
from llm_schema import 비목_enum, 비목_정의  # noqa: E402
from adapter import 사고흔적_걷기  # noqa: E402

DSN = db.DSN
VLLM = os.environ.get("VLLM_URL", "http://localhost:8000")
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-32B-AWQ")
MODEL_1 = os.environ.get("VLLM_MODEL_1", "")     # 호출 자리 ① 을 다른 모델로 돌릴 때만


class LLM실패(Exception):
    """vLLM 호출·파싱 실패. 부르는 쪽은 이걸 잡아 판단불가로 닫는다."""


# ════════════════════════════════════════════════════════════════════════════
# 스트리밍 — RunPod 프록시(Cloudflare)의 125초 타임아웃을 우회한다
# ════════════════════════════════════════════════════════════════════════════
# 비스트리밍 요청은 응답 시작이 늦으면 HTTP 524 로 끊긴다. stream=true 로 첫
# 바이트를 먼저 보내면 524 의 조건이 성립하지 않는다. 판정 결과는 안 바뀐다 —
# 온도 0 · guided_json 은 logits 단계라 전송 방식과 무관하다.
#
# 타임아웃의 뜻이 갈린다. 비스트리밍에서는 응답 전체를 기다리는 시간이고,
# 스트리밍에서는 소켓 타임아웃이 조각 사이 간격에만 걸린다. 그래서 총 마감을
# 따로 잰다 — 안 그러면 서버가 조금씩 뱉으며 영원히 붙들 수 있다.
#
# 끄는 길: `SUDDOE_LLM_STREAM=0` 이면 종전(비스트리밍) 경로 그대로다.
_스트리밍기본 = os.environ.get("SUDDOE_LLM_STREAM", "1") == "1"
_무응답한계 = int(os.environ.get("SUDDOE_LLM_STREAM_IDLE", "60"))   # 조각 사이 최대 공백


def _sse_수집(r, *, 데드라인: float) -> dict:
    """vLLM 의 SSE 스트림을 모아 비스트리밍 응답과 같은 모양의 dict 로 돌려준다.

    `reasoning_content` 도 같이 모은다 — Qwen3 는 사고가 여기로 갈라져 나온다.
    """
    조각: list[str] = []
    추론조각: list[str] = []
    종료이유 = None
    usage: dict = {}
    for raw in r:                       # HTTPResponse 는 줄 단위로 순회된다
        if time.time() > 데드라인:
            raise TimeoutError("스트리밍 총 마감 초과 — 조각은 오는데 안 끝난다")
        줄 = raw.decode("utf-8", "replace").strip()
        if not 줄.startswith("data:"):
            continue                    # 빈 줄·주석(`: ping`) 은 버린다
        몸통 = 줄[5:].strip()
        if 몸통 == "[DONE]":
            break
        try:
            d = json.loads(몸통)
        except json.JSONDecodeError:
            continue
        if d.get("usage"):
            usage = d["usage"]          # stream_options.include_usage 로 마지막에 온다
        for ch in d.get("choices") or []:
            델타 = ch.get("delta") or {}
            if 델타.get("content"):
                조각.append(델타["content"])
            if 델타.get("reasoning_content"):
                추론조각.append(델타["reasoning_content"])
            if ch.get("finish_reason"):
                종료이유 = ch["finish_reason"]
    return {"choices": [{"message": {"content": "".join(조각),
                                     "reasoning_content": "".join(추론조각)},
                         "finish_reason": 종료이유}],
            "usage": usage}


def llm_호출(프롬프트: str, 스키마: dict | None, *,
            모델: str | None = None,
            온도: float = 0.0,
            최대토큰: int = 1500,
            타임아웃: int = 240,          # adapter.LLMAdapter.호출 과 값을 맞춘다
            재시도: int = 1,
            스트리밍: bool | None = None,
            # Qwen 판(`llm_qwen.llm_호출`)과 같은 인자 계약을 갖기 위한 자리다.
            # vLLM 은 서버 기동 옵션(`--reasoning-parser qwen3`)이 사고를 정하므로
            # 요청 단위로 못 끈다 — 받아서 무시한다.
            사고: bool | None = None,
            **_무시) -> tuple[Any, dict]:
    """vLLM OpenAI 호환 엔드포인트. (파싱된 출력, 메타).

    온도 0 고정이 기본이다 — 재현성이 이 도메인의 요건이다. 투표(N=3~5)를
    켤 때만 부르는 쪽이 온도를 올린다.

    스키마 위반은 1회 재시도한다. guided_json 이 걸려 있으면 사실상 안
    일어나지만, 서버가 그 키를 무시했을 때 여기서 걸린다.

    `타임아웃` 은 한 번의 시도에 걸리는 총 마감이다. 재시도가 붙으므로 최악은
    2배다. 스트리밍에서는 소켓 타임아웃(조각 사이 공백)이 `_무응답한계`,
    총 마감이 `타임아웃` 이다.
    """
    스트리밍 = _스트리밍기본 if 스트리밍 is None else 스트리밍
    본문 = {"model": 모델 or MODEL,
            "messages": [{"role": "user", "content": 프롬프트}],
            "temperature": 온도,
            "max_tokens": 최대토큰}
    if 스키마 is not None:
        본문["guided_json"] = 스키마          # 🔴 최상위. extra_body 로 감싸지 않는다
    if 스트리밍:
        본문["stream"] = True
        # 🔴 이게 없으면 usage 가 «안 온다». 토큰 수는 잘림(finish_reason=length) 판별의
        #    한쪽 축이라 비워두면 run 194 에서 고친 계측 구멍이 되살아난다.
        본문["stream_options"] = {"include_usage": True}
    data = json.dumps(본문, ensure_ascii=False).encode()

    마지막 = None
    for 회차 in range(재시도 + 1):
        t = time.time()
        try:
            # User-Agent 를 명시한다 — RunPod 프록시 앞단 Cloudflare 가 기본
            # urllib UA 를 봇으로 보고 끊는다. 로컬 vLLM 직결에서는 무해하다.
            # 주소는 호출 시점에 푼다 — 모듈 상수 `VLLM` 로 굳히면 팟 주소가
            # 바뀌었을 때 안 따라간다.
            try:
                from adapter import vllm_url                       # noqa: PLC0415
                base = vllm_url()
            except Exception:                                      # noqa: BLE001
                base = VLLM
            req = urllib.request.Request(
                f"{base}/v1/chat/completions", data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "suddoe-judge/1.0"})
            if 스트리밍:
                # 소켓 타임아웃은 조각 사이 간격(`_무응답한계`)에만 건다 — 총 마감
                # (`타임아웃`)을 그대로 넣으면 서버가 계속 조각을 뱉는 한 소켓이 안
                # 끊긴다. 총 마감은 `_sse_수집` 이 `데드라인` 으로 따로 지킨다.
                with urllib.request.urlopen(req, timeout=_무응답한계) as r:
                    d = _sse_수집(r, 데드라인=t + 타임아웃)
            else:
                with urllib.request.urlopen(req, timeout=타임아웃) as r:
                    d = json.loads(r.read().decode())
            _메시지 = d["choices"][0]["message"]
            내용 = _메시지.get("content") or ""
            # 파서가 갈라 준 경우 `reasoning_content` 에 사고가 들어가고
            # `content` 는 이미 깨끗하다. 안 갈라진 경우만 여기서 걷어낸다.
            내용, 사고사실 = 사고흔적_걷기(내용)
            # `사고흔적_걷기()` 는 `content` 안에 `<think>` 가 섞여 들어온(파서 실패)
            # 경우만 본다 — 정상 분리된 `reasoning_content` 도 따로 재서 합쳐 넣는다.
            # 완료토큰 예산(`max_tokens`)은 사고도 먹으므로 길이를 놓치면 안 된다.
            추론content = _메시지.get("reasoning_content") or ""
            메타 = {"지연ms": int((time.time() - t) * 1000),
                   "토큰": d.get("usage", {}),
                   "종료이유": d["choices"][0].get("finish_reason"),
                   "모델": 본문["model"], "재시도": 회차,
                   **사고사실,
                   "추론content있음": bool(추론content),
                   "추론content길이": len(추론content)}
            if 스키마 is None:
                return 내용, 메타
            try:
                return json.loads(내용), 메타
            except json.JSONDecodeError as e:
                마지막 = f"JSON 파싱 실패: {e} · 앞 200자 {내용[:200]!r}"
        except urllib.error.HTTPError as e:
            마지막 = f"HTTP {e.code}: {e.read()[:300]!r}"
        except Exception as e:                                  # 타임아웃·연결 끊김 포함
            마지막 = f"{type(e).__name__}: {str(e)[:200]}"
    raise LLM실패(마지막 or "알 수 없는 실패")


def 사고흔적_제거(내용: str) -> str:
    """호환용 얇은 래퍼 — 실제 정의는 `adapter.사고흔적_걷기()` 하나뿐이다.

    사고 흔적이 있었는지·몇 자였는지가 필요하면 `사고흔적_걷기()` 를 직접 불러라.
    """
    정리됨, _ = 사고흔적_걷기(내용)
    return 정리됨




# ════════════════════════════════════════════════════════════════════════════
# 호출 자리 ① 스키마
# ════════════════════════════════════════════════════════════════════════════
def 호출자리1_스키마(비목목록: list[str] | None = None) -> dict:
    """정규화 출력 스키마. `비목후보.비목` 만 용어 사전 enum 으로 닫는다.

    품목·용도는 자유 문자열이다 — 닫을 수 있는 폐쇄 목록이 없고, `비목확정()` 이
    `item_alias` 정확조회 → 벡터 유사도로 받아내는 구조라 원문 표기가 오히려 재료다.
    """
    비목 = 비목목록 if 비목목록 is not None else 비목_enum()
    return {
        "type": "object", "additionalProperties": False,
        "required": ["품목", "금액", "금액_추정여부", "용도", "비목후보", "누락필드"],
        "properties": {
            "품목": {"type": "string", "minLength": 1, "maxLength": 60},
            # 금액이 없으면 되묻지 않고 null 로 둔다. 화면 4 에서 확인받는다 (§3-5)
            "금액": {"type": ["number", "null"], "minimum": 0},
            "금액_추정여부": {"type": "boolean"},
            "용도": {"type": "string", "maxLength": 200},
            "비목후보": {
                "type": "array", "minItems": 0, "maxItems": 3,
                "items": {"type": "object", "additionalProperties": False,
                          "required": ["비목", "신뢰도"],
                          "properties": {"비목": {"type": "string", "enum": 비목},
                                         "신뢰도": {"type": "number",
                                                  "minimum": 0, "maximum": 1}}},
            },
            "누락필드": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        },
    }


def _별칭_힌트(질문: str) -> str:
    """질문에 실제로 등장한 상품명을 `corpus.item_alias` 에서 찾아 비목과 함께 보여준다.

    질문에 나온 것만 싣는다 — 전체를 넣으면 프롬프트가 커지고 관계없는 별칭이
    오히려 다른 비목으로 유도한다. 조회일 뿐이다 — 못 찾으면 빈 문자열이고,
    DB 가 죽어도 정규화를 죽이지 않는다.
    """
    q = (질문 or "").replace(" ", "")
    if not q:
        return ""
    try:
        from _lib import db as _db                      # noqa: PLC0415 — 지연 import
        with _db.connect(autocommit=True) as conn:
            행 = conn.execute('SELECT "상품명", "비목" FROM corpus.item_alias').fetchall()
    except Exception as e:                              # noqa: BLE001
        print(f"item_alias 조회 실패({type(e).__name__}) — 별칭 힌트 없이 간다", file=sys.stderr)
        return ""
    맞음: dict[str, str] = {}
    for 상품명, 비목 in 행:
        이름 = (상품명 or "").strip()
        if len(이름) >= 2 and 이름.replace(" ", "") in q:
            맞음.setdefault(이름, 비목)
    if not 맞음:
        return ""
    줄 = chr(10).join(f"- {k} -> {v}" for k, v in sorted(맞음.items()))
    머리 = "질문에 나온 품목 중 이미 등록된 것 (참고, 이대로 강제하지는 않는다)"
    return chr(10) * 2 + 머리 + chr(10) + 줄

def _비목_블록(enum: list[str], 질문: str = "") -> str:
    """`비목 목록:` 자리에 들어갈 이름 + 정의 여러 줄.

    이름만 나열하면 모델이 뜻을 모른 채 고른다 — 정의를 같이 준다. 정의가 없는
    비목은 이름만 낸다(지어내지 않는다). 정의 파일이 없으면 이름을 쉼표로 이은
    한 줄로 폴백한다.
    """
    정의 = 비목_정의()
    if not 정의:
        return ", ".join(enum) + _별칭_힌트(질문)
    빠짐 = [b for b in enum if b not in 정의]
    if 빠짐:
        print(f"⚠️ 비목 정의 없음 {len(빠짐)}종 {빠짐} — 이름만 넣는다", file=sys.stderr)
    본문 = "\n".join(f"- {b} — {정의[b]}" if b in 정의 else f"- {b}" for b in enum)
    return 본문 + _별칭_힌트(질문)


# ── 프롬프트. B0 과 달리 캐시 프리픽스가 아니므로 비목 목록을 안에 넣어도 된다 ──
_지시 = """다음 문장은 창업지원금으로 무언가를 사거나 지출하려는 사람의 질문이다.
판정에 필요한 사실만 뽑아 JSON 으로 정규화하라.

규칙
1. 품목    무엇을 사는가. 상품명·서비스명 그대로. 문장을 요약하지 마라
2. 금액    숫자만(원 단위). 문장에 없으면 null 이고 금액_추정여부=false
           "약", "정도" 처럼 어림수면 그 값을 넣고 금액_추정여부=true
3. 용도    무엇에 쓰는가. 문장에 근거가 없으면 빈 문자열로 두고,
           그때는 누락필드에 "용도" 를 «반드시» 넣는다. 지어내지 마라
4. 비목후보 아래 목록 안에서만 고른다. 확신이 없으면 **둘을 나란히 낮은 신뢰도로** 둔다
           — 갈리면 코드가 두 경로를 모두 판정한다. 억지로 하나를 고르지 마라.
           비우는 것은 목록의 어느 비목에도 «해당하지 않을» 때뿐이다
5. 누락필드 판정에 필요한데 문장에 없는 것의 이름 (예: "금액", "용도", "집행시기")

판정을 하지 마라. 가능·불가를 여기서 말하지 않는다. 사실만 뽑는다.

비목 목록 (이 이름만 쓴다)
{비목}

질문: {질문}"""


# ════════════════════════════════════════════════════════════════════════════
# 프롬프트 변형 — 정규화 지연을 줄이는 실험 자리. `enable_thinking=false` 는
# 쓰지 않는다(`--reasoning-parser qwen3` 를 끄는 효과가 있고 guided_json ×
# thinking 무한루프 조건을 되살릴 위험이 있다) — 프롬프트로만 유도한다.
# ════════════════════════════════════════════════════════════════════════════
_지시_짧게 = ("\n\n생각은 짧게 한다 — 위 다섯 항목을 확인하는 데 필요한 만큼만 사고하고, "
           "여러 경우의 수를 길게 따지지 않는다. 결론이 서면 바로 JSON 을 출력하라.")

# 예시의 비목은 형태만 보여준다 — 골든셋 표본과 겹치지 않게 새로 지었다.
# 중괄호를 두 겹으로 쓴다 — 이 문자열이 `_지시_조립()` 을 거쳐 `.format()` 을
# 한 번 더 타므로, 홑겹이면 예시의 JSON 리터럴을 포맷 자리표시자로 오인한다.
_지시_예시 = ('예시\n질문: 시제품에 쓸 방수 커넥터 40만원 사도 되나요?\n'
             '출력: {{"품목":"방수 커넥터","금액":400000,"금액_추정여부":false,'
             '"용도":"시제품 제작","비목후보":[{{"비목":"재료비","신뢰도":0.9}}],'
             '"누락필드":[]}}')

_변형들: dict[str, str] = {
    "N0": "기준선 — 원문 그대로",
    "N1": "끝에 「생각은 짧게」 지시 한 문단 추가",
    "N2": "질문 앞에 1-shot 예시(사고 과정 없이 최종 JSON 만) 추가 — 출력 형태를 먼저 보여준다",
    "N3": "N1 + N2 병합",
}


def _지시_조립(변형: str = "N0") -> str:
    """변형별 지시문. `_지시` 원문은 그대로 두고 여기서만 이어붙인다."""
    if 변형 not in _변형들:
        raise ValueError(f"모르는 정규화 프롬프트 변형: {변형} (알려진 것: {list(_변형들)})")
    본문 = _지시
    if 변형 in ("N2", "N3"):
        본문 = 본문.replace("질문: {질문}", _지시_예시 + "\n\n질문: {질문}")
    if 변형 in ("N1", "N3"):
        본문 = 본문 + _지시_짧게
    return 본문


# ── dry 규칙 정규화 — 배관 검증 전용 ────────────────────────────────────────
_RE_금액 = re.compile(
    r"(?:(\d[\d,]*)\s*(억|천만|백만|만)?\s*원)|(?:(\d[\d,]*)\s*(억|천만|백만|만))")
_배수 = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, None: 1}
_불용 = re.compile(r"(구매|구입|결제|지출|사도|써도|사용|가능|되나요|될까요|하려|합니다|"
                   r"인데|인가요|해도|되는지|괜찮|처리|집행)")


# 수량 어절 — 어절 통째로 수량일 때만 버린다. 부분 치환하면 '200만원짜리' 가
# '짜리' 로, '1인' 이 '인' 으로 남아 품목이 더 더러워진다.
_RE_수량어절 = re.compile(
    r"^\d[\d,]*\s*(?:억|천만|백만|만|천)?\s*(?:원|개|인|일|회|명|건|년|월|시간|%|퍼센트)?\s*"
    r"(?:짜리|정도|가량|쯤|이내|이상|이하|초과|미만)?$")
# 조사 — 어절 끝에서만 뗀다. 가운데를 건드리면 '재료비' 가 '재료' 가 된다
_조사 = re.compile(r"(?:으로|로|를|을|이|가|은|는|에서|에|의|도|만|와|과|랑)$")
_비목표기_캐시 = None

_구두점 = re.compile(r"^[\"'(\[]+|[,.\"')\]·]+$")


def _어절정리(w: str) -> str:
    """앞뒤 구두점을 떼고 끝의 조사 1개를 뗀다.

    조사를 떼고 남는 게 한 글자면 떼지 않는다 — '회의' 의 '의' 를 조사로 보면
    '회' 가 되어 비목확정이 엉뚱한 걸 집는다.
    """
    w = _구두점.sub("", w)
    벗 = _조사.sub("", w)
    return w if len(벗) < 2 else 벗




def 비목표기(k: str):
    """표기 변형 -> 기준 문서 비목. 용어 사전 10종이 기준 문서이다 (`_비목_어휘집.json`).

    질문은 `특허권 등 무형자산 취득비` 처럼 띄어 쓰고 용어 사전은 붙여 쓴다.
    공백·가운뎃점을 접어 둘 다 같은 키가 되게 한다.
    """
    global _비목표기_캐시
    if _비목표기_캐시 is None:
        표 = {}
        for b in 비목_enum():
            for v in (b, b.replace(" ", ""), b.replace("·", ""),
                      "특허권 등 무형자산 취득비" if b.startswith("특허권") else b):
                표[v] = b
        표.update({"홍보비": "광고선전비", "광고비": "광고선전비", "교육비": "교육훈련비",
                  "출장비": "여비", "외주비": "외주용역비", "수수료": "지급수수료",
                  "멘토링비": "지급수수료", "회의비": "지급수수료"})
        _비목표기_캐시 = 표
    return _비목표기_캐시.get(k)


def 규칙_정규화(질문: str) -> dict:
    """LLM 없이 뽑는다. `dry=True` 전용 — 판정 품질 측정에 쓰면 안 된다.

    이걸로 잰 숫자는 정규화 품질이 아니라 배관이 뚫렸는지만 말한다.
    """
    금액, 추정 = None, False
    m = _RE_금액.search(질문.replace(" ", ""))
    if m:
        수 = (m.group(1) or m.group(3) or "").replace(",", "")
        단위 = m.group(2) or m.group(4)
        if 수:
            금액 = int(수) * _배수.get(단위, 1)
            추정 = bool(re.search(r"(약|정도|가량|쯤)", 질문))
    # 품목: 첫 명사구 근사 — 금액·수량·비목명·조사·서술어를 떼고 앞 2어절
    # 금액과 비목명을 품목에 남기면 안 된다 — `rule_lookup.비목확정()` 이 품목
    # 문자열을 통째로 매칭하므로 섞이면 매칭이 깨진다. 비목명은 버리지 않고
    # `비목후보` 로 올린다. `특허권 등 무형자산 취득비` 처럼 띄어 쓴 비목이
    # 있어 4어절까지 이어 붙여 긴 것부터 본다 — 짧은 것부터 보면 '수수료' 가
    # 먼저 걸려 앞말을 잘라 먹는다.
    머리 = re.split(r"[?？.\n]", 질문.strip())[0]
    토큰 = [_어절정리(w) for w in 머리.split() if not _불용.search(w)]
    토큰 = [w for w in 토큰 if w and not _RE_수량어절.match(w)]

    후보비목, 어절, i = [], [], 0
    while i < len(토큰):
        for n in (4, 3, 2, 1):
            정본 = 비목표기(" ".join(토큰[i:i + n])) if i + n <= len(토큰) else None
            if 정본:
                if 정본 not in 후보비목:
                    후보비목.append(정본)
                i += n
                break
        else:
            어절.append(토큰[i])
            i += 1
    품목 = " ".join(어절[:2])[:60] or " ".join(머리.split()[:2])[:60]
    return {"품목": 품목, "금액": 금액, "금액_추정여부": 추정,
            "용도": "", "비목후보": 후보비목, "누락필드": [] if 금액 else ["금액"],
            "_출처": "규칙(dry)"}


def 정규화(질문: str, *, dry: bool = False, 비목목록: list[str] | None = None,
         타임아웃: int = 240, 변형: str = "N0") -> tuple[dict, dict]:
    """(정규화 JSON, 메타). 실패는 예외로 올린다 — 부르는 쪽이 판단불가로 닫는다.

    `변형` : 프롬프트 실험 — `_변형들` 참고. 기본 N0 는 `_지시` 원문 그대로다.

    `타임아웃` 기본값은 `llm_호출()`·`adapter.py` 의 기본값과 맞춘다 — 이 함수를
    부르는 쪽이 `타임아웃` 을 안 넘기면 여기 기본값으로 조용히 잘리므로, 자리마다
    기본값이 다르면 그 차이가 곧 버그가 된다. RunPod 프록시의 실측 천장은 약
    125초라 그 위는 스트리밍(`SUDDOE_LLM_STREAM`)이 있어야 넘어간다.
    """
    if dry:
        return 규칙_정규화(질문), {"지연ms": 0, "모델": "규칙(dry)", "토큰": {}}
    스키마 = 호출자리1_스키마(비목목록)
    프롬프트 = _지시_조립(변형).format(
        비목=_비목_블록(스키마["properties"]["비목후보"]["items"]
                     ["properties"]["비목"]["enum"], 질문), 질문=질문)
    # 400 이 아니라 2000 이다 — Qwen3 는 thinking 이 기본이라 `<think>...</think>`
    # 가 출력 토큰을 먹는다. 서버가 사고를 못 갈라낸 응답에서 토큰이 모자라면
    # `content` 가 `finish_reason='length'` 로 중간에 잘린다. 잘려도 예외는 안
    # 나고 `LLM실패` 로 올라가 판단불가가 된다 — 「모델이 모른다」가 아니라
    # 「자리를 안 줬다」다.
    # 사고=False — 정규화는 문장에서 값을 뽑는 추출이라 사고가 필요 없다.
    # vLLM 경로는 이 인자를 무시한다.
    출력, 메타 = llm_호출(프롬프트, 스키마, 사고=False, 모델=MODEL_1 or None,
                       최대토큰=2000, 타임아웃=타임아웃)
    # guided_json 이 걸려 있어도 서버가 무시했을 때를 대비해 최소 형태만 확인한다
    if not isinstance(출력, dict) or "품목" not in 출력:
        raise LLM실패(f"슬롯① 출력이 스키마 밖: {str(출력)[:200]}")
    출력.setdefault("비목후보", [])
    출력.setdefault("누락필드", [])
    출력["_출처"] = "llm"
    return 출력, 메타


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", help="질문 한 줄")
    ap.add_argument("--golden", action="store_true", help="골든셋 전량")
    ap.add_argument("--dry", action="store_true", help="LLM 없이 규칙으로")
    ap.add_argument("--변형", default="N0",
                    help="F2 프롬프트 변형. N0=기준선 · N1~N3 (normalize_run._변형들)")
    a = ap.parse_args()

    if a.golden:
        with db.connect() as conn:
            rows = conn.execute("SELECT gold_id, 질문 FROM eval.golden_set "
                                "ORDER BY gold_id").fetchall()
        for gid, q in rows:
            out, _ = 정규화(q, dry=a.dry, 변형=a.변형)
            print(f"{gid:3} {json.dumps(out, ensure_ascii=False)}")
        return
    if not a.q:
        ap.error("--q 또는 --golden")
    out, 메타 = 정규화(a.q, dry=a.dry, 변형=a.변형)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n{메타}")


if __name__ == "__main__":
    main()
