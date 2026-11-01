# -*- coding: utf-8 -*-
"""정규화 호출: 자연어 질문 한 줄을 판정 파이프라인 입력 JSON 으로 바꾼다.

판정 파이프라인 1단계(orchestrate)와 /api/normalize(server/main)가 부른다. 모든 LLM 프롬프트는 scripts/ 에 둔다.
공개: 정규화(질문, *, dry, 비목목록, 타임아웃) -> (dict, 메타), 비목후보_추출(품목, 용도, 금액, 사업명) -> (list, 실패),
      별칭_힌트(문장), 규칙_정규화(질문). 출력 {품목, 금액, 금액_추정여부, 용도, 비목후보[], 누락필드}. 읽는 테이블: corpus.item_alias.
정규화 의 LLM 실패는 LLM실패 로 올린다. 호출자가 판단불가로 반환한다.
"""
from __future__ import annotations

import logging
import re
import time

from _lib import db
from llm_qwen import LLM실패, llm_호출
from llm_schema import 비목_enum, 정규화_스키마

_log = logging.getLogger(__name__)

# corpus.item_alias 의 (상품명, 비목) 행. 정규화 호출마다 전체를 다시 읽지 않도록 10분 TTL 로 캐시한다.
# 조회 실패는 캐시하지 않는다. 다음 호출이 다시 시도한다.
_별칭_TTL초 = 600
_별칭_캐시: tuple[float, list] | None = None


def _별칭_행() -> list | None:
    global _별칭_캐시
    if _별칭_캐시 is not None and time.time() - _별칭_캐시[0] < _별칭_TTL초:
        return _별칭_캐시[1]
    try:
        with db.connect(autocommit=True, connect_timeout=3) as conn:
            행 = conn.execute('SELECT "상품명", "비목" FROM corpus.item_alias').fetchall()
    except Exception as e:                              # noqa: BLE001
        _log.warning("item_alias 조회 실패(%s): 별칭 힌트 없이 진행한다", type(e).__name__)
        return None
    _별칭_캐시 = (time.time(), 행)
    return 행


def 별칭_힌트(질문: str) -> str:
    """질문에 실제로 나온 상품명을 `corpus.item_alias` 에서 찾아 비목과 함께 보여주는 프롬프트 조각.

    질문에 나온 것만 넣는다. 전체를 넣으면 프롬프트가 커지고 관계없는 별칭이 다른 비목으로 유도한다.
    찾지 못하거나 DB 조회가 실패하면 빈 문자열을 돌려준다. 정규화를 실패시키지 않는다.
    """
    q = (질문 or "").replace(" ", "")
    if not q:
        return ""
    행 = _별칭_행()
    if 행 is None:
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


# 정규화 프롬프트. 바이트가 바뀌면 판정이 바뀌므로 평가 없이 고치지 않는다.
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


# dry 규칙 정규화: LLM 없이 연결 확인(스모크 테스트)용
_RE_금액 = re.compile(
    r"(?:(\d[\d,]*)\s*(억|천만|백만|만)?\s*원)|(?:(\d[\d,]*)\s*(억|천만|백만|만))")
_배수 = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, None: 1}
_불용 = re.compile(r"(구매|구입|결제|지출|사도|써도|사용|가능|되나요|될까요|하려|합니다|"
                   r"인데|인가요|해도|되는지|괜찮|처리|집행)")

# 어절 전체가 수량일 때만 버린다. 부분 치환하면 '200만원짜리' 가 '짜리' 로, '1인' 이 '인' 으로 남는다.
_RE_수량어절 = re.compile(
    r"^\d[\d,]*\s*(?:억|천만|백만|만|천)?\s*(?:원|개|인|일|회|명|건|년|월|시간|%|퍼센트)?\s*"
    r"(?:짜리|정도|가량|쯤|이내|이상|이하|초과|미만)?$")
# 조사는 어절 끝에서만 뗀다. 가운데를 건드리면 '재료비' 가 '재료' 가 된다
_조사 = re.compile(r"(?:으로|로|를|을|이|가|은|는|에서|에|의|도|만|와|과|랑)$")
_구두점 = re.compile(r"^[\"'(\[]+|[,.\"')\]·]+$")
_비목표기_캐시: dict[str, str] | None = None


def _어절정리(w: str) -> str:
    """앞뒤 구두점을 떼고 끝의 조사 1개를 뗀다.

    조사를 떼고 남는 게 한 글자면 떼지 않는다. '회의' 의 '의' 를 조사로 보면 '회' 가 되어 비목확정이
    엉뚱한 항목을 고른다.
    """
    w = _구두점.sub("", w)
    벗 = _조사.sub("", w)
    return w if len(벗) < 2 else 벗


def 비목표기(k: str) -> str | None:
    """비목 표기 변형 -> 표준 표기. 표준 표기는 `_비목_어휘집.json` 의 enum 이다.

    질문은 `특허권 등 무형자산 취득비` 처럼 띄어 쓰고 어휘집은 붙여 쓴다. 공백·가운뎃점을 지운 표기도
    같은 비목으로 연결한다. 모르는 표기는 None.
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
    """LLM 없이 규칙으로 정규화한다. `정규화(dry=True)` 전용이다.

    결과는 파이프라인 연결 확인용이다. 판정 품질 측정에 쓰지 않는다.
    """
    금액, 추정 = None, False
    m = _RE_금액.search(질문.replace(" ", ""))
    if m:
        수 = (m.group(1) or m.group(3) or "").replace(",", "")
        단위 = m.group(2) or m.group(4)
        if 수:
            금액 = int(수) * _배수.get(단위, 1)
            추정 = bool(re.search(r"(약|정도|가량|쯤)", 질문))
    # 품목은 금액·수량·비목명·조사·서술어를 뗀 앞 2어절이다. rule_lookup.비목확정 이 품목 문자열 전체로
    # 매칭하므로 금액이나 비목명이 섞이면 안 된다. 비목명은 버리지 않고 비목후보로 옮긴다.
    # 띄어 쓴 비목명이 있어 4어절까지 이어 긴 것부터 본다. 짧은 것부터 보면 '수수료' 가 먼저 매칭되어
    # 앞 어절을 잘라낸다.
    머리 = re.split(r"[?？.\n]", 질문.strip())[0]
    토큰 = [_어절정리(w) for w in 머리.split() if not _불용.search(w)]
    토큰 = [w for w in 토큰 if w and not _RE_수량어절.match(w)]

    후보비목, 어절, i = [], [], 0
    while i < len(토큰):
        for n in (4, 3, 2, 1):
            표준 = 비목표기(" ".join(토큰[i:i + n])) if i + n <= len(토큰) else None
            if 표준:
                if 표준 not in 후보비목:
                    후보비목.append(표준)
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
         타임아웃: int = 240) -> tuple[dict, dict]:
    """질문 한 줄을 정규화해 (정규화 JSON, 메타) 를 돌려준다.

    LLM 호출 1회. 실패나 스키마 밖 출력은 LLM실패 로 올린다. `dry=True` 는 LLM 없이 규칙_정규화 를 쓴다.
    `비목목록` 을 주지 않으면 어휘집의 비목 enum 을 쓴다.
    """
    if dry:
        return 규칙_정규화(질문), {"지연ms": 0, "모델": "규칙(dry)", "토큰": {}}
    스키마 = 정규화_스키마(비목목록)
    비목들 = 스키마["properties"]["비목후보"]["items"]["properties"]["비목"]["enum"]
    프롬프트 = _지시.format(비목=", ".join(비목들) + 별칭_힌트(질문), 질문=질문)
    # 값을 뽑기만 하는 호출이라 추론을 끈다. 출력 토큰이 모자라면 finish_reason='length' 로 잘린 JSON 이
    # 파싱에 실패하고 LLM실패 가 된다
    출력, 메타 = llm_호출(프롬프트, 스키마, 추론=False, 최대토큰=2000, 타임아웃=타임아웃)
    # strict 스키마를 쓰지만 서버가 형식을 지키지 않는 경우에 대비해 최소 형태만 확인한다
    if not isinstance(출력, dict) or "품목" not in 출력:
        raise LLM실패(f"정규화 출력이 스키마 밖: {str(출력)[:200]}")
    출력.setdefault("비목후보", [])
    출력.setdefault("누락필드", [])
    출력["_출처"] = "llm"
    return 출력, 메타


# 폼 경로 프롬프트에 들어가는 비목 순서. server/_common.비목_ENUM 과 같은 순서여야 프롬프트가 같다.
_폼_비목순서: tuple[str, ...] = ("재료비", "외주용역비", "기계장치", "인건비", "지급수수료",
                             "여비", "교육훈련비", "광고선전비", "특허권등무형자산취득비", "창업활동비")


def 비목후보_추출(품목: str | None, 용도: str, 금액: float | None, 사업명: str | None, *,
              비목목록: list[str] | None = None) -> tuple[list[dict], bool]:
    """폼 입력(품목·용도·금액)에서 비목후보만 뽑는다. /api/normalize 의 폼 경로가 부른다.

    반환은 ([{"비목", "신뢰도"}] 최대 3건, 실패). 자연어 정규화의 비목후보와 같은 모양이다.
    LLM 실패나 응답 이상은 ([], True) 로 돌려준다. 판정을 막지 않고, 비목은 사용자가 비목 확정 화면에서 고른다.
    `비목목록` 을 주지 않으면 _폼_비목순서 를 쓴다. 이 순서가 그대로 프롬프트에 들어간다.
    """
    비목들 = list(비목목록) if 비목목록 is not None else list(_폼_비목순서)
    try:
        스키마 = {"type": "object", "additionalProperties": False,
                 "required": ["비목후보"],
                 "properties": {"비목후보": 정규화_스키마(비목들)["properties"]["비목후보"]}}
        금액문 = f"{int(금액):,}원" if 금액 is not None else "미상"
        프롬프트 = (
            "창업지원금으로 아래 지출을 하려 한다. 어느 비목으로 집행되는지 "
            "목록 안에서만 고르라.\n"
            # '비우거나' 라는 표현은 쓰지 않는다. 모델이 쉬운 쪽(빈 배열)을 고르기 쉬워진다
            "확신이 없으면 **둘을 나란히 낮은 신뢰도로** 둔다. 억지로 하나를 고르지 마라 — "
            "갈리면 코드가 두 경로를 모두 판정한다.\n"
            "판정을 하지 마라. 가능·불가를 여기서 말하지 않는다.\n\n"
            f"품목: {품목}\n금액: {금액문}\n용도: {용도 or '(적지 않음)'}\n"
            + (f"사업명: {사업명}\n" if 사업명 else "")
            + f"\n비목 목록: {', '.join(비목들)}"
            + 별칭_힌트(f"{품목 or ''} {용도 or ''}"))
        출력, _메타 = llm_호출(프롬프트, 스키마, 추론=False, 최대토큰=200, 타임아웃=60)
        후보 = 출력.get("비목후보") if isinstance(출력, dict) else None
        # enum 밖 항목을 먼저 버리고 3건으로 자른다. 순서가 반대면 앞자리의 enum 밖 항목이 유효한 후보를 밀어낸다
        return [{"비목": c["비목"], "신뢰도": float(c.get("신뢰도") or 0.0)}
                for c in (후보 or [])
                if isinstance(c, dict) and c.get("비목") in 비목들][:3], False
    except Exception:                                         # noqa: BLE001
        _log.exception("폼 경로 비목후보 추출 실패: 빈 후보로 폴백한다")
        return [], True
