# -*- coding: utf-8 -*-
"""(1) 입력 정규화 호출 자리 + 공용 vLLM 클라이언트.

`Agent.md` §1 (1) · `LLM.md` §1 (1)·§3-5. 자연어 한 줄 → 판정 파이프라인이 먹을 JSON.

## 이 파일이 vLLM 클라이언트를 겸하는 이유
`orchestrate.py` 와 `judge_run.py` 가 같은 HTTP 호출을 쓴다. 둘 중 한쪽에 두면
다른 쪽이 import 하면서 순환한다 (judge_run 은 --live 에서 orchestrate 를 부른다).
호출 자리 ① 은 파이프라인 맨 앞이라 아무것도 import 하지 않는다 — 여기가 순환이 없는 자리다.

## 🔴 `guided_json` 은 최상위다
`extra_body` 로 감싸는 건 파이썬 OpenAI SDK 문법이다. HTTP 를 직접 치면 서버가 그 키를
모르고 **에러 없이 버린다** — 모델이 필드 이름을 제멋대로 짓는다 (2026-08-31 실측:
스키마가 {판정,요약,해야할일,인용,전제} 인데 {결과,근거,이유} 가 나왔다). 무음 실패다.

## 🔴 스키마가 `llm_schema.정규화_스키마()` 와 다르다 — 의도된 것이다
`llm_schema.정규화_스키마()` 는 `{비목, 금액, 집행예정일, 거래처, 불확실}` 인데
`0831_최종구현.md` §4 의 동결 인터페이스가 요구하는 건 **품목·용도**다:

    rule_lookup.비목확정(cur, 품목, 사업명)
    rule_lookup.금지적중(cur, 품목, 용도, 사업명, 비목)

품목·용도가 없으면 B 를 부를 수 없다. `LLM.md` §3-5 의 호출 자리 ① 실물도
`{품목, 금액, 금액_추정여부, 용도, 비목후보[], 누락필드}` 다 — 그쪽이 기준 문서이라 보고
여기서 그 모양으로 만든다. `llm_schema.py` 는 오늘 아무 세션의 소유도 아니라 손대지 않았다.
(둘 중 하나를 지우는 정리는 내일 몫 — `결과보고.md` 에 남긴다)

## dry 모드
LLM 없이 규칙만으로 정규화한다. **배관 검증 전용**이고 판정 품질 측정에 쓰지 않는다.
GPU 를 열기 전에 77문항 프롬프트 조립이 끝까지 도는지 보는 것이 목적이다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --q "맥북 250만원 디자이너용"
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --q "..." --dry
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --golden --dry   # 77문항 일괄
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
from llm_schema import 비목_enum  # noqa: E402
from adapter import 사고흔적_걷기  # noqa: E402  # 🔴 공용 방어 — 정의는 adapter.py 하나뿐이다

DSN = db.DSN
VLLM = os.environ.get("VLLM_URL", "http://localhost:8000")
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-32B-AWQ")
MODEL_1 = os.environ.get("VLLM_MODEL_1", "")     # 호출 자리 ① 을 다른 모델로 돌릴 때만


class LLM실패(Exception):
    """vLLM 호출·파싱 실패. 부르는 쪽은 이걸 잡아 **판단불가**로 닫는다 (`Agent.md` §8)."""


def llm_호출(프롬프트: str, 스키마: dict | None, *,
            모델: str | None = None,
            온도: float = 0.0,
            최대토큰: int = 1500,
            타임아웃: int = 180,
            재시도: int = 1) -> tuple[Any, dict]:
    """vLLM OpenAI 호환 엔드포인트. (파싱된 출력, 메타).

    온도 0 고정이 기본이다 — 재현성이 이 도메인의 **요건**이지 편의가 아니다
    (`Agent.md` §6). 투표(N=3~5)를 켤 때만 부르는 쪽이 온도를 올린다.

    스키마 위반은 1회 재시도한다 (`Agent.md` §8 "(4) 스키마 위반 → 1회 재시도").
    guided_json 이 걸려 있으면 사실상 안 일어나지만, 서버가 그 키를 무시했을 때
    (위 주석의 무음 실패) 여기서 걸린다.
    """
    본문 = {"model": 모델 or MODEL,
            "messages": [{"role": "user", "content": 프롬프트}],
            "temperature": 온도,
            "max_tokens": 최대토큰}
    if 스키마 is not None:
        본문["guided_json"] = 스키마          # 🔴 최상위. extra_body 로 감싸지 않는다
    data = json.dumps(본문, ensure_ascii=False).encode()

    마지막 = None
    for 회차 in range(재시도 + 1):
        t = time.time()
        try:
            # 🔴 User-Agent 를 명시한다. RunPod 의 `*.proxy.runpod.net` 앞단 Cloudflare 가
            #    `Python-urllib/3.x` 를 봇으로 보고 **403 error code 1010** 으로 끊는다
            #    (2026-09-01 실측 — 같은 요청이 curl 로는 통과했다).
            #    로컬 vLLM 직결에서는 무해하고, 프록시 경유에서만 필요하다.
            # 🔴 주소는 호출 «시점» 에 푼다 — 모듈 상수 `VLLM` 로 굳히면 안 된다.
            #    `ops.gpu_pod` 로 팟 주소를 옮긴 뒤(2026-09-05) 이 자리만 import 시점의
            #    env 를 들고 있어서 정규화가 `localhost:8000` 을 쳤고, 판정 전체가
            #    「(1) 정규화 실패: WinError 10061 연결 거부」로 죽었다. adapter·
            #    gpu_watchdog 만 고치고 여기를 빠뜨린 결과다 — **세 자리가 같이 움직인다.**
            try:
                from adapter import vllm_url                       # noqa: PLC0415
                base = vllm_url()
            except Exception:                                      # noqa: BLE001
                base = VLLM
            req = urllib.request.Request(
                f"{base}/v1/chat/completions", data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "suddoe-judge/1.0"})
            with urllib.request.urlopen(req, timeout=타임아웃) as r:
                d = json.loads(r.read().decode())
            _메시지 = d["choices"][0]["message"]
            내용 = _메시지.get("content") or ""
            # 🔴 파서가 갈라 준 경우 `reasoning_content` 에 사고가 들어가고
            #    `content` 는 이미 깨끗하다. 안 갈라진 경우만 여기서 걷어낸다.
            내용, 사고사실 = 사고흔적_걷기(내용)
            # 🔴 F1(레인F, 2026-09-05) — **정상 분리된 사고는 지금까지 한 번도 안 쟀다.**
            #    `사고흔적_걷기()` 는 `content` 안에 `<think>` 가 «섞여 들어온»(파서 실패)
            #    경우만 본다. `--reasoning-parser qwen3` 가 정상 동작하면 사고는
            #    `reasoning_content` 로 깨끗이 갈라지는데, 그 경우는 위 사고사실이
            #    항상 {있음:False, 길이:0} 으로 찍혀 「사고를 안 했다」로 잘못 읽힌다.
            #    완료토큰 예산(`max_tokens`)은 이 필드와 무관하게 똑같이 먹히므로
            #    (사고도 생성 토큰이다) 이 길이를 안 재면 F1 의 분포 자체가 반쪽이다.
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
    """🔴 호환용 얇은 래퍼 — 실제 정의는 `adapter.사고흔적_걷기()` 하나뿐이다(2026-09-04 통합).

    이 파일에 따로 구현이 있었다(`a70c874`). 🔴 처음엔 "판정 호출(adapter.LocalVLLM)엔 이
    방어가 없다"고 봤는데 틀렸다(ai-c4 재현) — `orchestrate.py:52` 가 이 모듈의
    `llm_호출` 을 직접 부르므로 **판정도 정규화도 여기 하나를 이미 타고 있었다.**
    옮긴 이유는 방어 추가가 아니라 **계측**이다 — 옛 함수는 사고흔적을 걷어내기만
    하고 있었는가·몇 자였는가를 안 남겨 빈도를 못 셌다. 로직을 `adapter.py` 로 빼서
    (기존 호출부·시그니처 호환 — 사실 정보가 필요하면 `사고흔적_걷기()` 를 직접 불러라)
    다음에 호출 자리가 늘어도 한 곳만 고치면 전부 같이 움직이게 했다.
    """
    정리됨, _ = 사고흔적_걷기(내용)
    return 정리됨




# ════════════════════════════════════════════════════════════════════════════
# 호출 자리 ① 스키마
# ════════════════════════════════════════════════════════════════════════════
def 호출자리1_스키마(비목목록: list[str] | None = None) -> dict:
    """`LLM.md` §3-5 (1) 정규화 출력. `비목후보.비목` 만 용어 사전 enum 으로 닫는다.

    품목·용도는 자유 문자열이다 — 닫을 수 있는 폐쇄 목록이 없고, B 의 `비목확정()` 이
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


# ── 프롬프트. B0 과 달리 캐시 프리픽스가 아니므로 비목 목록을 안에 넣어도 된다 ──
_지시 = """다음 문장은 창업지원금으로 무언가를 사거나 지출하려는 사람의 질문이다.
판정에 필요한 사실만 뽑아 JSON 으로 정규화하라.

규칙
1. 품목    무엇을 사는가. 상품명·서비스명 그대로. 문장을 요약하지 마라
2. 금액    숫자만(원 단위). 문장에 없으면 null 이고 금액_추정여부=false
           "약", "정도" 처럼 어림수면 그 값을 넣고 금액_추정여부=true
3. 용도    무엇에 쓰는가. 문장에 근거가 없으면 빈 문자열. 지어내지 마라
4. 비목후보 아래 목록 안에서만 고른다. 확신이 없으면 **비우거나 둘을 나란히** 둔다.
           억지로 하나를 고르지 마라 — 갈리면 코드가 두 경로를 모두 판정한다
5. 누락필드 판정에 필요한데 문장에 없는 것의 이름 (예: "금액", "용도", "집행시기")

판정을 하지 마라. 가능·불가를 여기서 말하지 않는다. 사실만 뽑는다.

비목 목록: {비목}

질문: {질문}"""


# ════════════════════════════════════════════════════════════════════════════
# F2 프롬프트 변형 — 🔴 정규화(①) 지연의 76%가 사고(reasoning_content, F1 실측
# 중앙 1,691자·p90 4,065자)다. 판정(④)의 A12(`assemble_context.변형들`)와 같은
# 방식 — **후보를 먼저 선언**하고 채택 기준(속도만이 아니라 정확도도, F1 기준선
# 63%)을 결과 보기 전에 못박는다. `enable_thinking=false` 는 안 쓴다
# (`--reasoning-parser qwen3` 를 끄는 효과가 있고, 과거 guided_json×thinking
# 무한루프의 조건을 되살릴 위험이 있다 — 프롬프트로만 유도한다).
# ════════════════════════════════════════════════════════════════════════════
_지시_짧게 = ("\n\n생각은 짧게 한다 — 위 다섯 항목을 확인하는 데 필요한 만큼만 사고하고, "
           "여러 경우의 수를 길게 따지지 않는다. 결론이 서면 바로 JSON 을 출력하라.")

# 🔴 예시의 비목은 **의도적으로 형태만 보여준다.** "재료비" 정답 자체는 F1
#    표본에 실재하는 물음(gold_id=556, 알루미늄 판재)과 겹치지 않게 새로 지었다 —
#    표본 문항을 예시로 쓰면 그 문항만 "정답을 알려준" 채 채점하게 된다.
# 🔴 중괄호를 **두 겹**으로 쓴다 — 이 문자열이 `_지시_조립()` 을 거쳐 최종적으로
#    `.format(비목=..., 질문=...)` 을 한 번 더 타므로, 홑겹이면 예시의 JSON 리터럴을
#    포맷 자리표시자로 오인해 `KeyError` 가 난다(실측 — 처음 짤 때 이렇게 터졌다).
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


# 수량 어절 — **어절 통째로** 수량일 때만 버린다. 부분 치환하면 '200만원짜리' 가
# '짜리' 로, '1인' 이 '인' 으로 남아 품목이 더 더러워진다 (2026-09-01 실측).
_RE_수량어절 = re.compile(
    r"^\d[\d,]*\s*(?:억|천만|백만|만|천)?\s*(?:원|개|인|일|회|명|건|년|월|시간|%|퍼센트)?\s*"
    r"(?:짜리|정도|가량|쯤|이내|이상|이하|초과|미만)?$")
# 조사 — 어절 **끝**에서만 뗀다. 가운데를 건드리면 '재료비' 가 '재료' 가 된다
_조사 = re.compile(r"(?:으로|로|를|을|이|가|은|는|에서|에|의|도|만|와|과|랑)$")
_비목표기_캐시 = None

_구두점 = re.compile(r"^[\"'(\[]+|[,.\"')\]·]+$")


def _어절정리(w: str) -> str:
    """앞뒤 구두점을 떼고 **끝의 조사 1개**를 뗀다.

    🔴 조사를 떼고 남는 게 한 글자면 떼지 않는다 — '회의' 의 '의' 를 조사로 보면
       '회' 가 되어 비목확정이 엉뚱한 걸 집는다. '사무실의' 는 '사무실'(3자)이라 뗀다.
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
    """LLM 없이 뽑는다. **`dry=True` 전용** — 판정 품질 측정에 쓰면 안 된다.

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
    # 🔴 **금액과 비목명을 품목에 남기면 안 된다** (2026-09-01, ai-40 진단).
    #    예전 코드는 앞 3어절을 그대로 이어 붙였다:
    #        "재료비로 시약 300만원 사도 되나요" -> 품목 '재료비로 시약 300만원'
    #    `rule_lookup.비목확정()` 은 이 문자열을 통째로 매칭한다. 낱개로는
    #    '시약' 0.95 · '재료비' 1.0 으로 잘 붙는 것이 이어 붙이면 **0건**이 된다.
    #    -> 비목 None -> 룰 조회 없음 -> `l3게이팅` 까지 통째로 스킵.
    #    즉 dry 완주는 «배관이 뚫렸다» 가 아니라 «중간에서 끊긴 채 끝까지 갔다» 였다.
    #    (`비목확정` 함수 자체는 멀쩡하다. 입력이 오염된 것이다)
    #
    #    비목명은 버리지 않고 `비목후보` 로 올린다 — 질문이 비목을 밝혔다는 건
    #    정보이지 잡음이 아니다. 버리면 뒤에서 그걸 다시 추측해야 한다.
    #    `특허권 등 무형자산 취득비` 처럼 띄어 쓴 비목이 있어 **4어절까지 이어 붙여**
    #    긴 것부터 본다 — 짧은 것부터 보면 '수수료' 가 먼저 걸려 앞말을 잘라 먹는다.
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
         타임아웃: int = 60, 변형: str = "N0") -> tuple[dict, dict]:
    """(정규화 JSON, 메타). 실패는 예외로 올린다 — 부르는 쪽이 판단불가로 닫는다.

    `변형` : F2(레인F) 프롬프트 실험 — `_변형들` 참고. **기본 N0 는 스위치 넣기 전과
             바이트 단위로 같은 프롬프트**를 만든다(`_지시` 원문 그대로).
    """
    if dry:
        return 규칙_정규화(질문), {"지연ms": 0, "모델": "규칙(dry)", "토큰": {}}
    스키마 = 호출자리1_스키마(비목목록)
    프롬프트 = _지시_조립(변형).format(
        비목=", ".join(스키마["properties"]["비목후보"]["items"]
                      ["properties"]["비목"]["enum"]), 질문=질문)
    # 🔴 400 이 아니라 2000 이다 (2026-09-03 실서버 실측).
    #    Qwen3 는 thinking 이 기본이라 `<think>...</think>` 가 «출력 토큰» 을 먹는다.
    #    서버에 `--reasoning-parser qwen3` 를 줘도 갈라지지 않는 응답이 있다 —
    #    같은 호출에서 `reasoning_content` 는 None 이고 `content` 가 `<think>` 로 시작했다.
    #    그 상태에서 400 이면 생각만 하다 끝나 `content` 가 `'{"'` 에서 잘린다
    #    (`finish_reason='length'`). 정규화 3회 연속 동일 실패 — 「팔레트」가 그 사례다.
    #    ⚠️ 잘려도 예외는 안 난다. `LLM실패` 로 올라가 판단불가가 되는데, 그건
    #       「모델이 모른다」가 아니라 「우리가 자리를 안 줬다」다. 두 개를 갈라야 한다.
    출력, 메타 = llm_호출(프롬프트, 스키마, 모델=MODEL_1 or None,
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
