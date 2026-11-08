#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""코드 게이트: 적용 규범과 입력값으로 금지 · 한도 · 임계값 · 부가세를 판단한다 (docs/5_룰/5-6_판정_결합과_검증.md).

orchestrate 의 새 경로와 question_generate 가 부른다. DB · LLM 을 쓰지 않는다. 규범은 ontology_lookup.적용_규범 결과다.
코드는 판정을 낮추기만 한다. '가능' 을 만들지 않는다. 게이트는 `AI검증` 이상 규범 + 화면에서 사용자가 확정한 노드에서만 발동한다.
해석미결 규범은 금지 · 한도 · 임계값에 쓰지 않는다(문맥에만 들어간다).

공개: 게이트(규범들, 입력, 확정=, 세목표시=, 우선순위_적용=) -> dict, 합치기(게이트결과, LLM판정) -> (판정, 하향여부),
      추가입력_칸(규범들, 세목표시=) -> list, 부가세_질문_필요(규범들, 입력) -> bool, 화면_조건(게이트결과) -> list,
      화면_조건_나누기(조건들) -> dict, 경로밖금지_구분(규범, 품목들) -> str.
화면 문구에 노드 ID · 규범 ID · 검증 코드를 넣지 않는다. 원천 `결과.문장` 이 없고 발췌가 표 행 · 조각이면 화면에는 쉬운 대체 문장을 낸다(상한은 그대로).
실행: PYTHONIOENCODING=utf-8 python scripts/ontology_gate.py --self-test
"""
from __future__ import annotations

import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ontology_norm import 조번호_표시  # noqa: E402

# 글 속 금액 표현
_금액표현 = re.compile(r"(?:(?:\d{1,3}(?:,\d{3}(?!\d))+|\d+)(?:\.\d+)?(?:천만|백만|십만|억|만|천|백|십)?)+")

# 규범 수치의 분모 → 입력 키 · 화면 라벨 · 단위
_분모표 = {
    "인": ("인원", "인원", "명"), "일": ("일수", "일수", "일"), "박": ("박수", "박수", "박"),
    "시간": ("시간", "시간", "시간"), "건": ("건수", "건수", "건"), "월": ("월수", "개월 수", "개월"),
    "계약총액": ("계약총액", "계약 총액", "원"), "용역비총액": ("용역비총액", "용역비 총액", "원"),
}
_비율분모 = {"계약총액", "용역비총액"}
_누적표 = {"동일업체합계": ("동일업체누적", "같은 거래처와의 누적 거래금액"), "월": ("누적금액", "이번 달 누적 금액"),
         "협약기간": ("누적금액", "협약 기간 누적 금액")}
_지역값 = ("특별시", "광역시", "그밖")
_제약종류 = ("금지", "한도", "임계값", "의무")
# 화면 추가 입력에서 받는 키. 게이트가 읽는 입력은 이것과 금액 · 부가세구분뿐이다
입력키 = ("인원", "일수", "박수", "시간", "건수", "월수", "계약총액", "용역비총액", "출장지구분", "수량", "동일업체누적", "누적금액")
# 화면을 쓰는 사람(창업 쪽)이 아닌 것이 확실한 수범자: 기관 · 심의 조직 · 정부 · 멘토 · 거래 상대 업체. 이것만 적힌 의무는 할 일에 내지 않는다.
# 이 목록에 없는 값은 사용자 쪽으로 본다. 새 주체 값이 조용히 화면에서 빠지면 안 되기 때문이다(더 안전한 쪽). 수범자가 빈 의무도 그대로 둔다.
다른쪽_수범자 = frozenset(f"주체:{x}" for x in (
    "주관기관", "주관기관의장", "전문기관", "전문기관의장", "전담기관", "전담조직의장", "전담인력", "총괄기관", "총괄기관의장", "총괄부서", "담당부서",
    "지역허브기관", "지역허브기관의장", "지역창업전담기관", "운영기관", "운영사", "협력기관", "창업진흥원", "주무부처", "중소벤처기업부", "중소벤처기업부장관",
    "기획예산처장관", "중앙관서의장", "중앙행정기관의장", "지방자치단체의장", "행정청", "공공기관", "공무원", "지방공기업", "검증기관", "납부기관", "예탁기관",
    "사업운영위원회", "사업운영위원회소위원회", "보조사업자선정위원회", "부정수급심의위원회", "심의위원", "위원회",
    "멘토", "멘토기관", "책임멘토", "기술멘토", "선배멘토", "공급기업", "외주용역업체", "회계법인", "채권관리업체", "채권추심업체", "결제대행사"))


def _사용가능(n: dict) -> bool:
    """코드 판정에 쓸 수 있는 규범: `AI검증` 이상이고 해석미결이 아니다."""
    return bool(n.get("verified")) and not n.get("해석미결")


def _다른_수범자(n: dict) -> bool:
    """수범자가 적혀 있고 전부 사용자가 아닌 주체다. 빈 칸 · 모르는 값은 참이 아니다(모르는 것을 빼지 않는다)."""
    수범자 = n.get("수범자") or []
    return bool(수범자) and set(수범자) <= 다른쪽_수범자


def _의무_문장(n: dict, 문서표시: dict[str, str] | None = None) -> tuple[str, str]:
    """의무 규범의 (조건 문장, 구분). 다른 주체의 의무라도 사용자 집행을 막는 집행 전 절차면 사용자가 일정을 잡아야 순서가 맞아 화면에 낸다(리뷰 F5).

    집행을 말하는 문장만 해당한다. 기관장(`…의장`) 내부 서류 · 기관 사이 승인 · 지급 절차는 사용자가 요청할 일이 아니라 문맥에만 둔다.
    """
    문장 = _화면문장(n, 문서표시)
    if not _다른_수범자(n):
        return 문장, "의무"
    수범자 = [str(x).split(":")[-1] for x in n.get("수범자") or []]
    if (str((n.get("결과") or {}).get("시점") or "").startswith("집행 전") and "집행" in 문장
            and not any(x.endswith("의장") for x in 수범자)):
        return f"{문장} 결제 전에 {수범자[0]}에 요청해 주세요", "의무"
    return 문장, "다른수범자"


def _해당시(n: dict, 입력: dict) -> bool:
    """규범에 이 지출에서 확인되지 않은 조건이 있다. 그 조건에 해당할 때만 걸리므로 화면 첫 줄에 두지 않는다(리뷰 F6)."""
    return bool(_자연어조건(n)) or any(_조건_평가(c, 입력) is None for c in _구조화조건(n))


def _발췌(n: dict, 칸: str | None = None) -> str | None:
    근거 = n.get("근거") or []
    if isinstance(근거, dict):                                  # 원천 JSON 모양도 받는다
        근거 = [{**g, "칸": k} for k, v in 근거.items() for g in v or []]
    후보 = [g for g in 근거 if not 칸 or g.get("칸") == 칸] or list(근거)
    # 한 규범에 근거가 여럿이면 뜻이 가장 잘 드러나는 긴 발췌를 쓴다("1. 재료비" 보다 "사용 가능한 비목은 …")
    return max((g.get("발췌") or "" for g in 후보), key=len, default=None) or None


def _규범문장(n: dict) -> str:
    결과 = n.get("결과") or {}
    # 조건 조각("공유 사무공간 외")은 그것만으로 뜻이 통하지 않아 화면에는 원문 발췌를 먼저 쓴다
    글 = (결과.get("문장") or 결과.get("조건문장") or _발췌(n) or _자연어조건(n) or "").strip()
    칸 = [x.strip() for x in 글.split(" | ")]                  # 표 행 발췌는 앞 두 칸(품목 · 효과)만. 구분자를 화면에 내지 않는다
    return f"{칸[0]} — {칸[1]}" if len(칸) > 1 and 칸[1] else 칸[0]


_서술어_끝 = re.compile(r"[다요][.!]?$")
_조각_머리 = re.compile(r"^(?:단|다만)[,\s]")     # 앞 문장에 이어지는 조각(「단, 해당 기술자료를 … 불가하다.」)


def _문장_꼴(글: str | None) -> bool:
    """표 구분자가 없고 서술어로 끝나는 글. 조건 문장으로 내도 뜻이 통한다. 끝의 괄호 꼬리(`…않습니다.(현장 결제 포함).`)는 보지 않는다."""
    글 = re.sub(r"\s*\([^()]*\)\s*[.]?$", "", (글 or "").strip())
    return bool(글) and "|" not in 글 and not 글.endswith("?") and not _조각_머리.match(글) and bool(_서술어_끝.search(글))


def _근거_말(n: dict, 문서표시: dict[str, str] | None) -> str:
    """규범의 근거를 사용자가 찾아갈 수 있는 말로(`안내 책자 29쪽`). 내부 문서키는 내지 않는다. 모르면 「관련 규정」."""
    근거 = n.get("근거") or []
    첫 = 근거[0] if isinstance(근거, list) and 근거 else {}
    이름 = (문서표시 or {}).get(첫.get("doc_id"))
    if not 이름:
        return "관련 규정"
    항호 = str(첫.get("항호") or "")
    return " ".join(x for x in (이름, 조번호_표시(첫.get("조번호")), 항호.replace("/", " ").strip() if 항호.startswith("/") else None) if x)


def _화면문장(n: dict, 문서표시: dict[str, str] | None = None) -> str:
    """조건 칸에 낼 문장. 원천 문장이 없으면 뜻이 통하는 발췌만 쓰고, 표 행 · 조각뿐이면 근거 위치를 말로 적어 원문을 보게 한다(5-5 "의무는 조건 문장", 진단 3-7).
    `결과.제재`(어기면 따르는 제재, 5-5)는 문장에 같은 말이 없을 때만 「(어기면 …)」 로 붙인다. 어길 수 있는 종류(`_제재종류`)에만 붙이고,
    근거 위치만 말하는 대체 문장에는 어길 기준이 없어 붙이지 않는다(리뷰 3c R6)."""
    결과 = n.get("결과") or {}
    제재 = 결과.get("제재") if n.get("종류") in _제재종류 else None
    if str(결과.get("문장") or "").strip():
        return _제재_붙이기(str(결과["문장"]).strip(), 제재)
    후보 = [str(결과.get("조건문장") or "").strip(), (_발췌(n) or "").strip(), _자연어조건(n).strip()]
    if not any(후보):
        return ""
    글 = next((x for x in 후보 if _문장_꼴(x)), None)
    if 글:
        return _제재_붙이기(글, 제재)
    if n.get("종류") == "금지":
        return f"{_근거_말(n, 문서표시)}에 이 지출에 걸릴 수 있는 금지 기준이 있어요. 원문을 확인해 주세요"
    return f"{_근거_말(n, 문서표시)}의 기준을 원문에서 확인해 주세요"


_제재종류 = ("금지", "의무", "요건", "한도")     # 어기거나 못 채울 수 있는 규범. 허용 · 임계값 · 우선순위의 제재는 그 문장의 결과 자체다


# 제재 말 → 문장에서 같은 뜻으로 쓰는 말(띄어쓰기 없이 비교). 문장이 이미 말한 제재는 다시 붙이지 않는다
_제재말 = (("환수", ("환수", "돌려줘야", "돌려주", "돌려줍", "반환")), ("반납", ("반납", "돌려줘야", "돌려주", "돌려줍", "반환")),
         ("참여제한", ("참여제한", "참여가제한", "참여를제한", "지원제한", "지원이제한", "참여할수없")), ("지원제한", ("참여제한", "참여가제한", "참여를제한", "지원제한", "지원이제한")),
         ("불인정", ("불인정", "인정되지않", "인정하지않", "인정받지못")), ("부정집행", ("부정집행", "부정사용", "부정하게")),
         ("선정취소", ("선정취소", "선정을취소", "선정이취소")), ("경고", ("경고",)), ("주의", ("주의를받", "주의처분", "주의를")),
         ("감점", ("감점",)), ("중단", ("중단",)), ("공표", ("공표",)), ("징역", ("징역",)), ("벌금", ("벌금",)),
         ("제재부가금", ("제재부가금",)), ("부적정", ("부적정",)), ("사용불가", ("사용불가", "쓸수없")),
         # 문장이 제재를 결과로 말하는 꼴(「…내지 않으면 강제로 징수될 수 있습니다」)을 알아보는 말(리뷰 3c R6)
         ("가산금", ("가산금",)), ("징수", ("징수",)), ("반환", ("반환", "돌려주", "돌려줘", "돌려줍")), ("배제", ("배제",)),
         ("감액", ("감액", "줄일수", "줄수있")), ("보전", ("보전", "남겨두")), ("제재", ("제재",)), ("조치", ("조치",)), ("반영", ("반영",)),
         ("지급제한", ("지급제한", "지급을제한", "지급이제한")))


def _제재_붙이기(문장: str, 제재: Any) -> str:
    제재 = str(제재 or "").strip()
    if not 문장 or not 제재 or 제재 == "미확인":
        return 문장
    붙임 = re.sub(r"\s+", "", 문장)
    if _문장_꼴(제재):                       # 제재가 그 자체로 문장이면 통째로 본다
        부분들 = [제재]
    else:
        부분들 = [x.strip() for x in re.split(r"\s*·\s*", 제재) if x.strip()]

    def 말함(부분: str) -> bool:
        글 = re.sub(r"\s+", "", 부분)
        if 글 in 붙임:
            return True
        뜻들 = [같은말 for 말, 같은말 in _제재말 if 말 in 글]
        if 뜻들 and all(any(x in 붙임 for x in 같은말) for 같은말 in 뜻들):
            return True
        낱말 = [x for x in re.split(r"[\s()·,]+", 부분) if len(x) >= 2]     # 「다음 라운드 진출평가 반영」 ⊂ 「…다음 라운드 진출평가에 반영됩니다」
        return bool(낱말) and all(x in 붙임 for x in 낱말)
    남음 = [x for x in 부분들 if not 말함(x)]
    if not 남음:
        return 문장
    if 남음 == [제재] and _문장_꼴(제재):
        return f"{문장}{'' if re.search(r'[.!?。]$', 문장) else '.'} {제재}"     # 문장 둘을 마침표로 가른다
    return f"{문장} (어기면 {' · '.join(남음)})"


_표제목 = re.compile(r"^\s*\[[^\]]*\]")


def _표_제목(글: str) -> bool:
    """표 제목 발췌(`[참고 1] 창업활동자금 클린카드 제한 업종`). 금지 문장 자체는 아니지만 그 표에 든 품목이라는 사실은 말할 수 있다."""
    글 = (글 or "").strip()
    return bool(_표제목.match(글)) and not 글.endswith("?") and not re.search(r"(?:다|요)[.]?$", 글)


def _문장_아님(글: str) -> bool:
    """비었거나 질문형 FAQ 발췌. 이것만으로는 사용자에게 보일 사실 문장이 아니다."""
    글 = (글 or "").strip()
    return not 글 or 글.endswith("?")


def 경로밖금지_구분(n: dict, 품목들: list[str] | None) -> str:
    """경로 밖 품목에 닿은 금지가 어떤 문장인가. 문맥 · 화면이 같은 기준을 쓴다.

    `표` 발췌가 표 제목이다. 그 표에 든 품목이라고 말한다(클린카드 제한 업종) / `문장아님` 질문형. 화면에 내지 않는다 /
    `행위` 조건(구조화 또는 자연어)이 있어 그 조건에 해당할 때만 막는다 /
    `품목` 대상이 일치한 품목 노드 자체다 / `묶음` 품목이 속한 상위 묶음의 금지다(하위 노드는 상속한다, 5-5).
    """
    if _표_제목(_규범문장(n)):
        return "표"
    if _문장_아님(_규범문장(n)):
        return "문장아님"
    if _구조화조건(n) or _자연어조건(n):
        return "행위"
    return "품목" if set(n.get("대상") or []) & set(품목들 or []) else "묶음"


def _품목금지_문장(n: dict, 품목들: list[str] | None, 문서표시: dict[str, str] | None = None) -> str | None:
    """확정 경로 밖 품목에 걸린 금지의 조건 문장. 구입 자체를 막는 금지만 품목 틀을 쓴다. 틀에 넣지 못하는 문장은 있는 그대로, 사실 문장이 아니면 None. 표 행 · 조각은 인용하지 않고 근거 위치를 말로 적는다."""
    구분, 글 = 경로밖금지_구분(n, 품목들), _규범문장(n)
    if 구분 == "문장아님":
        return None
    if 구분 == "표":
        return f"이 품목은 「{_표제목.sub('', 글).strip()}」에 들어 있어요. 이 지출이 해당하면 집행할 수 없어요"
    if (n.get("결과") or {}).get("문장") or 구분 == "행위":
        return _화면문장(n, 문서표시)
    앞 = "이 품목은 집행 금지 규정에 해당할 수 있어요" if 구분 == "품목" else "이 품목이 속한 묶음에 집행 금지 규정이 있어요"
    if not _문장_꼴(_발췌(n)):
        return f"{앞}. {_근거_말(n, 문서표시)} 원문을 확인해 주세요"
    return f"{앞}: 「{글}」. 이 지출이 해당하는지 확인해 주세요"


def _자연어조건(n: dict) -> str:
    조건 = n.get("조건") or {}
    목록 = 조건.get("자연어") if isinstance(조건, dict) else None
    return " ".join(str(x.get("발췌") if isinstance(x, dict) else x) for x in 목록 or [])


def _구조화조건(n: dict) -> list[dict]:
    조건 = n.get("조건") or {}
    return list((조건.get("구조화") if isinstance(조건, dict) else None) or [])


_크기연산 = {"이상": (1, True), "초과": (1, False), "이하": (-1, True), "미만": (-1, False), "이후": (1, True), "이전": (-1, True)}
_수와단위 = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*([^\d\s]*)\s*$")


def _수_단위(v: Any) -> tuple[float, str] | None:
    """`3` · `"3회"` · `"2주"` → (3.0, "회"). 수가 아니면 None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v), ""
    m = _수와단위.match(str(v))
    return (float(m.group(1).replace(",", "")), m.group(2)) if m else None


def _크기_평가(항목: str | None, 값: Any, 연산: str, 기준: Any, 입력: dict) -> bool | None:
    """이상 · 초과 · 이하 · 미만(수) · 이전 · 이후(날짜, 그날 포함). 단위가 다르거나 부가세 기준에 따라 결과가 갈리면 None."""
    방향, 같음 = _크기연산[연산]
    if 연산 in ("이전", "이후"):
        a, b = _날짜(값), _날짜(기준)
    elif 항목 == "금액":                   # 조건 금액은 부가세 기준이 없다(정의없음). 두 기준의 결과가 같을 때만 쓴다
        집합 = {float(기준)} if isinstance(기준, (int, float)) and not isinstance(기준, bool) else 금액집합(str(기준 or ""))
        a = _수(값)
        if a is None or len(집합) != 1:
            return None
        b = next(iter(집합))
        결과들 = {(v > b if 방향 > 0 else v < b) or (같음 and v == b) for v in _후보금액(a, "정의없음", 입력.get("부가세구분"))}
        return 결과들.pop() if len(결과들) == 1 else None
    else:
        x, y = _수_단위(값), _수_단위(기준)
        if not x or not y or x[1] != y[1]:     # 「2주」와 입력 3 처럼 단위가 다르거나 빠지면 비교하지 않는다
            return None
        a, b = x[0], y[0]
    if a is None or b is None:
        return None
    return (a > b if 방향 > 0 else a < b) or (같음 and a == b)


def _조건_평가(c: dict, 입력: dict) -> bool | None:
    """구조화 조건 하나를 입력과 비교한다. 입력이 없으면 None(모른다). 확인 질문 답(`입력["진술"]`)은 여기서 읽지 않는다(`_진술_평가`)."""
    값 = 입력.get(c.get("항목"))
    if 값 in (None, ""):
        return None
    연산, 기준 = c.get("연산") or "=", c.get("값")
    if 연산 in ("=", "일치"):
        return 값 == 기준
    if 연산 in ("!=", "≠", "불일치"):
        return 값 != 기준
    if 연산 in ("in", "포함"):
        return 값 in (기준 or [])
    if 연산 in ("not in", "제외"):
        return 값 not in (기준 or [])
    if 연산 in _크기연산:
        return _크기_평가(c.get("항목"), 값, 연산, 기준, 입력)
    return None                     # 모르는 연산은 비교하지 않는다(발동시키지 않는다)


# 확인 질문 답으로 받는 조건 항목 → 화면 이름 · 칸 라벨. 코드가 `AI검증` 이상 금지의 구조화 조건에서 선택 칸을 만든다(5-6 「거래처 유형 · 특수관계 → 확인 질문」, 5-6b ③ 추가입력).
# 답은 진술이라 코드 불가 · 상한 해제에 쓰지 않는다(수정 3c, 진행상황 E 표). 이 표에 없는 항목(고의성 · 확인시점 …)은 묻지 않고 지금처럼 조건으로 둔다.
진술항목 = {"거래처유형": ("거래 상대", "거래 상대가 해당하는 것"), "지급대상": ("지급받는 사람", "지급받는 사람이 해당하는 것"),
         "결제유형": ("결제 방식", "결제 방식이 해당하는 것"), "결제수단": ("결제 수단", "결제 수단이 해당하는 것")}
해당없음 = "해당 없음"


def _조건값들(c: dict) -> list[str]:
    """`=` · `in` 조건의 값을 화면 말로. 주체 노드 ID 의 머리(`주체:`)는 뗀다."""
    값 = c.get("값")
    return [str(v).removeprefix("주체:").strip() for v in (값 if isinstance(값, list) else [값]) if str(v or "").strip()]


def _진술_대상(n: dict) -> bool:
    return n.get("종류") == "금지" and _사용가능(n)


def 진술_선택지(규범들: list[dict]) -> dict[str, list[str]]:
    """항목별 선택지(규범 순서대로, 겹침 없이). 칸 만들기와 게이트 평가가 같은 값을 쓴다."""
    표: dict[str, list[str]] = {}
    for n in 규범들:
        if not _진술_대상(n):
            continue
        for c in _구조화조건(n):
            if c.get("항목") in 진술항목 and (c.get("연산") or "=") in ("=", "일치", "in", "포함"):
                표.setdefault(c["항목"], [])
                표[c["항목"]].extend(v for v in _조건값들(c) if v not in 표[c["항목"]] and v != 해당없음)
    return {k: v for k, v in 표.items() if v}


def _진술_평가(c: dict, 입력: dict, 선택지: dict[str, list[str]]) -> bool | None:
    """확인 질문 답(진술)으로 본 조건. `해당 없음` 은 그 칸에 보인 값만 아니라고 본다. 하나를 고르면 그 값의 조건만 참이고 다른 값은 모른다(둘에 해당할 수 있다)."""
    항목 = c.get("항목")
    답 = ((입력.get("진술") or {}).get(항목) or "").strip()
    보인값 = 선택지.get(항목) or []
    값들 = _조건값들(c)
    if not 답 or not 값들 or (c.get("연산") or "=") not in ("=", "일치", "in", "포함") or (답 != 해당없음 and 답 not in 보인값):
        return None
    if 답 == 해당없음:
        return False if set(값들) <= set(보인값) else None
    return True if 답 in 값들 else None


def _후보금액(금액: float, 규범부가세: str | None, 입력부가세: str | None) -> list[float]:
    """부가세 기준을 맞춘 비교 금액 후보. 후보가 둘 이상이면 결과가 모두 같을 때만 쓴다(5-5 "비교 절차").

    규범 포함 · 공급가액은 입력 구분이 있으면 한 값이다. 입력이 모름이거나 규범이 정의없음이면 가능한 값을 전부 낸다.
    """
    구분 = 입력부가세 if 입력부가세 in ("포함", "공급가액") else "모름"
    # 환산 값의 부동소수 오차를 없앤다(137500 / 1.1 = 124999.99999999999). 경계 비교가 오차로 뒤집히지 않게 한다
    더함, 뺌 = round(금액 * 1.1, 6), round(금액 / 1.1, 6)
    if 규범부가세 == "포함":
        if 구분 == "포함":
            return [금액]
        if 구분 == "공급가액":
            return [더함]
        return [금액, 더함]
    if 규범부가세 == "공급가액":
        if 구분 == "공급가액":
            return [금액]
        if 구분 == "포함":
            return [뺌]
        return [금액, 뺌]
    # 정의없음: 입력을 포함 · 공급가액 두 기준으로 본 값을 다 비교한다
    if 구분 == "포함":
        return [금액, 뺌]
    if 구분 == "공급가액":
        return [금액, 더함]
    return [금액, 더함, 뺌]


def _비교(값: float, 기준: float, 연산: str) -> bool:
    return 값 >= 기준 if 연산 == "이상" else 값 > 기준        # 그 밖은 "초과"


def _원표시(v: float) -> str:
    return f"{round(v):,}원"


def _필요입력(n: dict) -> list[str]:
    """이 규범을 비교하는 데 필요한 입력 키(금액 · 부가세 구분 제외)."""
    수치 = n.get("수치") or {}
    키 = [_분모표[d][0] for d in 수치.get("분모") or [] if d in _분모표]
    if 수치.get("단위") in ("개", "대"):
        키.append("수량")              # 대수 한도는 금액이 아니라 수량을 비교한다(_비교값들). 비면 수량을 묻는다
    if 수치.get("지역"):
        키.append("출장지구분")
    if 수치.get("누적범위") in _누적표:
        키.append(_누적표[수치["누적범위"]][0])
    return list(dict.fromkeys(키))


def _모자란입력(n: dict, 입력: dict) -> list[str]:
    """비교에 필요한데 비었거나 쓸 수 없는 값(0 이하 · 무한대 · 숫자 아님)인 입력 키. 금액은 값이 있을 때만 본다(없으면 호출부가 안내한다). 출장지 구분은 글자라 비었는지만 본다."""
    def 못씀(v) -> bool:
        try:
            값 = float(v)
        except (TypeError, ValueError):
            return True
        return not math.isfinite(값) or 값 <= 0

    나쁨 = [k for k in _필요입력(n) if 입력.get(k) in (None, "") or (k != "출장지구분" and 못씀(입력.get(k)))]
    금액 = 입력.get("금액")
    if "금액" not in 나쁨 and 금액 not in (None, "") and 못씀(금액):
        나쁨.append("금액")
    return 나쁨


def _분모값(n: dict, 입력: dict) -> float | None:
    """분모 곱. 비율 분모(계약총액 · 용역비총액)는 빼고 곱한다. 입력이 0 이하이면 None."""
    곱 = 1.0
    for d in (n.get("수치") or {}).get("분모") or []:
        if d in _비율분모 or d not in _분모표:
            continue
        try:
            v = float(입력.get(_분모표[d][0]))
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        곱 *= v
    return 곱


def _분자금액(n: dict, 입력: dict) -> float | None:
    수치 = n.get("수치") or {}
    if 수치.get("누적범위") in _누적표:
        키 = _누적표[수치["누적범위"]][0]
        v = 입력.get(키)
    else:
        v = 입력.get("금액")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _비교값들(n: dict, 입력: dict) -> tuple[list[float] | None, float]:
    """(비교값 후보, 기준값). 입력이 모자라면 (None, 기준)."""
    수치 = n.get("수치") or {}
    기준 = float(수치.get("값"))
    단위 = 수치.get("단위") or "원"
    if 단위 == "%":
        분모키 = next((d for d in 수치.get("분모") or [] if d in _비율분모), None)
        금액 = _분자금액(n, 입력)
        총액 = 입력.get(_분모표[분모키][0]) if 분모키 else None
        try:
            총액 = float(총액) if 총액 is not None else None
        except (TypeError, ValueError):
            총액 = None
        if 금액 is None or not 총액 or 총액 <= 0:
            return None, 기준
        return [금액 / 총액 * 100], 기준                   # 비율은 같은 기준의 금액끼리라 부가세를 맞추지 않는다
    if 단위 in ("개", "대"):
        try:
            수량 = float(입력.get("수량"))
        except (TypeError, ValueError):
            return None, 기준
        분모 = _분모값(n, 입력)                       # 1인당 1대: 수량을 인원으로 나눠 비교한다. 인원이 없으면 비교하지 않는다
        return ([수량 / 분모], 기준) if 분모 is not None else (None, 기준)
    금액 = _분자금액(n, 입력)
    분모 = _분모값(n, 입력)
    if 금액 is None or 분모 is None:
        return None, 기준
    return [x / 분모 for x in _후보금액(금액, 수치.get("부가세"), 입력.get("부가세구분"))], 기준


def _분모표시(n: dict) -> str:
    분모 = (n.get("수치") or {}).get("분모") or []
    글 = {"인": "1인", "일": "1일", "박": "1박", "시간": "시간당", "건": "건당", "월": "월"}
    return " ".join(글.get(d, "") for d in 분모 if d not in _비율분모).strip()


def _수치표시(n: dict, v: float) -> str:
    단위 = (n.get("수치") or {}).get("단위") or "원"
    if 단위 == "%":
        return f"{v:g}%"
    if 단위 in ("개", "대"):
        return f"{_분모표시(n)} {v:g}{단위}".strip()
    return f"{_분모표시(n)} {_원표시(v)}".strip()


def _을를(말: str) -> str:
    """받침에 맞는 목적격 조사."""
    끝 = 말.strip()[-1:] or "가"
    if not "가" <= 끝 <= "힣":
        return "을"
    return "을" if (ord(끝) - 0xAC00) % 28 else "를"


def _필요문구(키: str, n: dict, 세목표시: str | None) -> str:
    """입력을 비워 비교하지 못했을 때 결과 조건에 붙는 문장."""
    종류말 = "한도" if n.get("종류") == "한도" else "기준"
    표시 = _분모표시(n)
    이름 = {"시간": f"{세목표시 or ''} 시간".strip(), "인원": "인원", "일수": "일수", "박수": "박수", "건수": "건수",
          "월수": "개월 수", "계약총액": "계약 총액", "용역비총액": "용역비 총액", "출장지구분": "출장지 구분",
          "수량": "수량", "금액": "금액", "동일업체누적": "같은 거래처와의 누적 거래금액", "누적금액": "누적 금액"}.get(키, 키)
    return f"{이름}{_을를(이름)} 알려주시면 {표시 + ' ' if 표시 else ''}{종류말}{_을를(종류말)} 확인해요"


def _지역_맞음(n: dict, 입력: dict) -> bool | None:
    지역 = (n.get("수치") or {}).get("지역")
    if not 지역:
        return True
    값 = 입력.get("출장지구분")
    if 값 in (None, ""):
        return None
    return 값 == 지역


def 추가입력_칸(규범들: list[dict], 세목표시: str | None = None) -> list[dict]:
    """확정 세목의 한도 · 임계값 분모에서 화면 숫자 칸을, 금지의 구조화 조건(거래 상대 …)에서 선택 칸을 만든다(LLM 아님). `AI검증` 이상 규범만.
    선택 칸의 답은 진술이다. 게이트는 `입력["진술"]` 로 따로 읽는다(`_진술_평가`)."""
    return [*_숫자_칸(규범들, 세목표시),
            *({"키": k, "라벨": 진술항목[k][1], "단위": None, "형식": "선택", "선택지": [해당없음, *v]} for k, v in 진술_선택지(규범들).items())]


def _숫자_칸(규범들: list[dict], 세목표시: str | None = None) -> list[dict]:
    칸: dict[str, dict] = {}
    for n in 규범들:
        if n.get("종류") not in ("한도", "임계값") or not _사용가능(n):
            continue
        수치 = n.get("수치") or {}
        for d in 수치.get("분모") or []:
            if d in _분모표:
                키, 라벨, 단위 = _분모표[d]
                if 키 == "시간" and 세목표시:
                    라벨 = f"{세목표시} 시간"
                칸.setdefault(키, {"키": 키, "라벨": 라벨, "단위": 단위, "형식": "숫자"})
        if 수치.get("단위") in ("개", "대"):
            칸.setdefault("수량", {"키": "수량", "라벨": "수량", "단위": 수치["단위"], "형식": "숫자"})
        if 수치.get("지역"):
            칸.setdefault("출장지구분", {"키": "출장지구분", "라벨": "출장지 구분", "단위": None, "형식": "선택",
                                       "선택지": list(_지역값)})
        if 수치.get("누적범위") in _누적표:
            키, 라벨 = _누적표[수치["누적범위"]]
            칸.setdefault(키, {"키": 키, "라벨": 라벨, "단위": "원", "형식": "숫자"})
    return list(칸.values())


def 부가세_질문_필요(규범들: list[dict], 입력: dict[str, Any]) -> bool:
    """입력 구분이 모름이고 두 기준의 비교 결과가 갈릴 때만 코드가 부가세 질문을 더한다(5-6b ③ "비교에 필요하면").

    금액이나 칸이 비어 지금 비교하지 못하는 규범은 칸을 채우면 갈릴 수 있어 묻는다(칸과 질문은 한 화면이다).
    다만 비어 있는 분모를 1로 채운 최대 비교값도 기준에 못 미치면 묻지 않는다(1만원과 1인 1박 7만원). 누적 금액은 이번 금액과 무관하게 갈릴 수 있어 그대로 묻는다.
    예외가 붙은 한도와 다른 지역 행은 게이트가 비교하지 않으므로 세지 않는다.
    """
    if 입력.get("부가세구분") in ("포함", "공급가액"):
        return False
    모름 = {**입력, "부가세구분": "모름"}
    for n in 규범들:
        종류, 수치 = n.get("종류"), n.get("수치") or {}
        if 종류 not in ("한도", "임계값") or not _사용가능(n) or 수치.get("값") is None or (수치.get("단위") or "원") != "원":
            continue
        if (종류 == "한도" and n.get("예외")) or _지역_맞음(n, 입력) is False:
            continue
        연산 = 수치.get("위반연산" if 종류 == "한도" else "발동연산") or "초과"
        값들, 기준 = _비교값들(n, 모름)
        비움 = [k for k in _필요입력(n) if 모름.get(k) in (None, "")]
        if 비움 or 값들 is None:
            if 수치.get("누적범위") not in _누적표:
                분모1 = {**모름, **{k: 1 for k in 비움}}
                최대값들, _ = _비교값들(n, 분모1)
                if 최대값들 and not any(_비교(v, 기준, 연산) for v in 최대값들):
                    continue
            return True
        if len({_비교(v, 기준, 연산) for v in 값들}) > 1:
            return True
    return False


_전체기준_머리 = "이 자금의 나머지 사용 기준은"


def _전체기준_문서(규범들: list[dict], 문서표시: dict[str, str] | None) -> str:
    """자금 전체 기준이 가장 많이 나온 문서 이름. 모르면 "관련 규정"."""
    세기: dict[str, int] = {}
    for n in 규범들:
        근거 = n.get("근거") or []
        이름 = (문서표시 or {}).get(근거[0].get("doc_id")) if 근거 else None
        if 이름:
            세기[이름] = 세기.get(이름, 0) + 1
    return max(세기, key=lambda k: 세기[k]) if 세기 else "관련 규정"


def _미확정한도_비교(n: dict, 입력: dict) -> tuple[set, float] | None:
    """코드 불가에 쓰지 못하는 원 단위 불가 한도와 입력 금액을 비교한다. (비교 결과들, 기준). 비교할 수 없으면 None."""
    수치, 결과 = n.get("수치") or {}, n.get("결과") or {}
    if (수치.get("단위") or "원") != "원" or 수치.get("값") is None or (결과.get("종류") or "불가") != "불가" or n.get("예외"):
        return None
    채운 = {"인원": 1, **입력}
    if _지역_맞음(n, 채운) is not True or [k for k in _필요입력(n) if 채운.get(k) in (None, "")]:
        return None
    값들, 기준 = _비교값들(n, 채운)
    if not 값들:
        return None
    return {_비교(v, 기준, 수치.get("위반연산") or "초과") for v in 값들}, 기준


def _미확정한도_안내(n: dict, 입력: dict, 문서표시: dict[str, str] | None, 문의처: str) -> str | None:
    """`AI검증` 미만이라 코드 판정에 못 쓰는 한도를 금액이 넘으면 사용자가 볼 문장. 넘지 않거나 비교할 수 없으면 None.

    코드 불가는 내지 않는다(5-6 "AI검증 미만은 ④ 를 발동시키지 못한다"). 넘으면 판정을 판단불가로 낮춘다(9b 설계 변경 제안, 사용자 결정 대기).
    다른 주체의 한도 · 해석미결 · 이 지출에 맞는지 확인되지 않은 조건이 붙은 한도는 「넘습니다」로 말하지 않는다. 규범 문장은 `미확정규범` 조건으로 따로 나간다.
    인원 칸은 미검증 규범에 만들지 않으므로 1명으로 보고 그렇게 쓴다. 분모가 없는 한도는 입력이 합계인지 1개 값인지 모르므로 단정하지 않는다(책자 참고2-p31-1 「개당 단가로 판단」).
    """
    넘음결과 = _미확정한도_비교(n, 입력)
    if not 넘음결과 or True not in 넘음결과[0]:
        return None
    if n.get("해석미결") or _다른_수범자(n) or _자연어조건(n) or any(_조건_평가(c, 입력) is not True for c in _구조화조건(n)):
        return None
    결과들, 기준 = 넘음결과
    수치 = n.get("수치") or {}
    이름 = _전체기준_문서([n], 문서표시)
    분모 = 수치.get("분모") or []
    앞 = "입력한 금액을 1명이 쓴다고 보면 " if "인" in 분모 and 입력.get("인원") in (None, "") else "1개 기준으로 " if not 분모 else ""
    if len(결과들) > 1:
        넘음 = f"{'부가세 포함 여부에 따라' if 입력.get('부가세구분') not in ('포함', '공급가액') else '규정에 부가세 기준이 적혀 있지 않아'} 넘을 수 있습니다"
    else:
        넘음 = "넘을 수 있습니다" if not 분모 else "넘습니다"
    return f"{앞}{이름}의 한도({_수치표시(n, 기준)})를 {넘음}. 쓰기 전에 {문의처} 확인해 주세요"


def _공통_규범(n: dict) -> bool:
    """대상이 전부 자금 전체(`비목:창업/*` · `비목:창업활동자금/*@…`)다. 어느 지출에나 같은 문장이라 이번 지출의 조건이 아니라 사업 전체 공통 사항이다."""
    대상 = n.get("대상") or []
    return bool(대상) and all(str(v).split("@")[0].endswith("/*") for v in 대상)


def _임계값_발동(t: dict, 입력: dict) -> bool | None:
    """임계값을 입력과 비교한다. 입력이 모자라면 None, 부가세 기준 하나로라도 넘으면 True(52만원의 「50만원이 넘으면」을 접힌 칸에 두지 않는다).
    AI검증 미만 규범에도 쓰므로 화면 칸을 가르는 데만 쓴다(판정을 만들지 않는다)."""
    if (t.get("수치") or {}).get("값") is None:
        return None
    없음 = _모자란입력(t, 입력)
    값들, 기준 = _비교값들(t, 입력)
    if 없음 or not 값들:
        return None
    return any(_비교(v, 기준, (t.get("수치") or {}).get("발동연산") or "초과") for v in 값들)


def _날짜(값: Any) -> date | None:
    try:
        return date.fromisoformat(str(값)[:10])
    except (TypeError, ValueError):
        return None


def _기한_지남(n: dict, 입력: dict) -> bool:
    """사용일자가 규범의 기한일(`결과.기한일`, YYYY-MM-DD)을 지났다. 어느 쪽이든 없거나 날짜가 아니면 지나지 않은 것으로 본다(빼지 않는다)."""
    기한, 사용 = _날짜((n.get("결과") or {}).get("기한일")), _날짜(입력.get("사용일자"))
    return bool(기한 and 사용 and 사용 > 기한)


def 게이트(규범들: list[dict], 입력: dict[str, Any], *, 확정: bool, 세목표시: str | None = None,
         우선순위_적용: list[dict] | None = None, 위임만: bool = False, 세목미정: bool = False,
         문서표시: dict[str, str] | None = None, 경로밖_품목규범: list[dict] | None = None,
         경로밖_품목: list[str] | None = None, 공통미정: list[dict] | None = None, 문의처: str = "주관기관에",
         가정세목: str | None = None) -> dict:
    """규범 + 입력 → 코드 결과.

    반환: {종류: 금지|한도초과|부가세기준미정|None, 코드판정, 검증코드[], 조건[{문장, 구분}], 상한(조건부|None),
          활성의무[규범 dict], 미검증만(bool), 쓴규범[norm_id], 발동임계값[norm_id], 미확정초과(bool), 초과규범[규범 dict]}.
    미확정초과 = `AI검증` 미만 한도를 입력이 넘는다. 합치기가 가능 · 조건부를 판단불가로 낮춘다. 초과규범 = 넘었지만 코드 불가가 아닌 한도(미확정 · 초과분 자부담). 요약 금액 검사의 차액은 이 규범에서만 허용한다.
    확정=False(LLM 추천만)이면 금지 · 한도 · 임계값을 발동시키지 않는다.
    세목미정 = 비목만 확정했고 그 아래 세목에 한도 규범이 있다. 상한 조건부 + 안내.
    가정세목 = 사용자가 고르지 않았는데 확인 질문 LLM 의 「동일」 노드로 채운 세목의 정식명(5-14 A). 세목미정 안내 문장만 바꾼다(상한은 그대로).
    경로밖_품목규범 = 품목이 확정 세목 밖이라 코드 불가를 내지 않는 규범. 금지는 조건 문장으로, 의무는 해당할 때의 조건으로 알리고 상한을 조건부로 둔다.
    경로밖_품목 = 그때 일치한 품목 노드. 금지가 그 품목 자체의 것인지 가른다.
    공통미정 = 이 자금에 미치는지 정해지지 않은 공통 규범(ontology_fund). 조건 · 비교에 쓰지 않고 상한 조건부만 남겨 '가능' 이 나오지 않게 한다.
    구분 `다른수범자` 조건은 상한과 판정 문맥에만 쓰고 화면 조건 · 할 일에는 내지 않는다. 조건의 `해당시` = 이 지출에서 확인되지 않은 조건이 붙어 해당할 때만 걸린다.
    조건의 `공통` = 사업 전체 공통 사항이다: 대상이 자금 전체이거나, 금액으로 켜지는 미확정 의무인데 이 금액에서 켜진다고 코드가 확인하지 못했다. 칸만 가른다. 상한은 그대로 건다.
    입력 `사용일자` 가 규범의 `결과.기한일` 을 지났으면 그 의무를 조건에서 뺀다. 상한은 그대로 건다(기한이 지났다고 판정을 올리지 않는다).
    """
    r: dict[str, Any] = {"종류": None, "코드판정": None, "검증코드": [], "조건": [], "상한": None,
                         "활성의무": [], "미검증만": False, "쓴규범": [], "제외규범": [], "발동임계값": [], "미확정초과": False, "초과규범": [],
                         "진술평가": {}, "답변": {}}

    def 조건추가(문장: str, 구분: str = "의무", 시점: str | None = None, 증빙: list | None = None, 해당시: bool = False, 금지: bool = False, 공통: bool = False,
              승인주체: list | None = None, 답변항목: list | None = None, 진술: str | None = None) -> None:
        # 승인주체 → 할 일 설명(orchestrate._승인_안내) · 답변항목 → 확인한 내용 묶음 · 진술 = 답으로 해당한다고 본 금지(해당)
        문장 = (문장 or "").strip()
        증빙 = list(증빙 or [])
        있음 = next((c for c in r["조건"] if c["문장"] == 문장), None)
        # 같은 증빙 목록을 두 조문이 다른 말로 요구하면 한 줄만 낸다(지침 표-10 · 별지2). 더 넓은 목록이 그 줄을 맡는다
        겹침 = [c for c in r["조건"] if 구분 == "의무" and 증빙 and c["증빙"] and c["구분"] == "의무" and c["시점"] == 시점 and c["해당시"] == 해당시 and c["공통"] == 공통
              and (set(증빙) <= set(c["증빙"]) or set(c["증빙"]) <= set(증빙))]
        if 문장 and 있음 is None and 겹침:
            if set(증빙) > set(겹침[0]["증빙"]):
                겹침[0].update(문장=문장, 증빙=증빙)
        elif 문장 and 있음 is None:
            r["조건"].append({"문장": 문장, "구분": 구분, "시점": 시점, "증빙": 증빙, "해당시": 해당시, "금지": 금지 or 구분 == "금지조건", "공통": 공통,
                            "승인주체": list(승인주체 or []), "답변항목": list(답변항목 or []), "진술": 진술})
        elif 있음 and 있음["구분"] == "답변확인" and 구분 != "답변확인":
            # 같은 문장을 답이 닿지 않은 규범도 말하면 그 줄은 아직 확인되지 않았다. 원래 칸으로 되돌린다
            있음.update(구분=구분, 시점=시점, 증빙=증빙, 해당시=해당시, 금지=금지 or 구분 == "금지조건", 공통=공통, 승인주체=list(승인주체 or []), 답변항목=[], 진술=진술)
        elif 있음 and 있음["구분"] == "다른수범자" and 구분 != "다른수범자":
            있음.update(구분=구분, 시점=시점, 증빙=증빙, 해당시=해당시, 공통=공통)     # 같은 문장을 사용자 쪽 의무도 말하면 화면에 낸다
        elif 있음 and 있음["공통"] and not 공통:
            있음["공통"] = False            # 같은 문장이 이번 지출에도 걸리면 이번 지출 칸에 한 번만 낸다
        r["상한"] = "조건부"

    실효 = [n for n in 규범들 if n.get("종류") != "우선순위"]
    지난기한 = {n.get("norm_id") for n in [*규범들, *(경로밖_품목규범 or [])] if n.get("종류") == "의무" and _기한_지남(n, 입력)}
    if 지난기한:
        r["상한"] = "조건부"
    r["미검증만"] = bool(실효) and not any(_사용가능(n) for n in 실효)
    if r["미검증만"] or any(n.get("종류") in _제약종류 for n in 공통미정 or []):
        r["상한"] = "조건부"
    if 위임만:
        조건추가("전문기관 집행 가이드를 확인해 주세요", "위임")
    if not 확정:
        # 확정 전 노드는 화면이 막는다. 화면 밖에서 온 요청도 '가능' 을 내지 않는다(백로그 결정)
        조건추가("비목을 확정하면 금지 항목과 한도를 확인해요", "미확정")
        return r
    if 세목미정:
        조건추가(f"세목을 고르지 않아 「{가정세목}」 기준으로 봤어요. 다른 세목이면 세목을 골라 다시 확인해 주세요" if 가정세목
                 else "세목을 고르면 한도를 확인해요", "세목미정")
    for n in [*규범들, *(경로밖_품목규범 or [])]:
        안내 = _미확정한도_안내(n, 입력, 문서표시, 문의처) if n.get("종류") == "한도" and not _사용가능(n) else None
        if 안내:                            # 코드 불가는 아니다. 다른 조건보다 먼저 보이게 앞에 넣는다
            조건추가(안내, "미확정한도")
            r["미확정초과"] = True
            r["초과규범"].append(n)
    for n in 경로밖_품목규범 or []:
        결과 = n.get("결과") or {}
        if n.get("종류") == "금지":         # 확정 경로 밖이라 코드 불가는 내지 않는다(5-6 ③). 판정은 LLM 이 원문으로 한다
            조건추가(_품목금지_문장(n, 경로밖_품목, 문서표시) or "", "금지조건")
        elif n.get("종류") == "의무":       # 자산 보유 서약서처럼 그 품목을 사면 생기는 일. 품목이 확정 경로 밖이라 해당할 때의 조건으로 둔다
            if n.get("norm_id") in 지난기한:
                continue
            문장, 구분 = _의무_문장(n, 문서표시)
            조건추가(문장, 구분, 결과.get("시점"), 결과.get("증빙"), 해당시=True, 승인주체=n.get("승인주체"))
        elif n.get("종류") == "한도" and not _사용가능(n) and n not in r["초과규범"]:
            비교 = _미확정한도_비교(n, 입력)   # 「넘습니다」로 단정할 수 없는 한도(조건 · 수범자 미확인)라도 넘을 수 있으면 '가능' 을 내지 않는다
            if 비교 and True in 비교[0]:
                조건추가(_화면문장(n, 문서표시), "다른수범자" if _다른_수범자(n) else "미확정규범", 결과.get("시점"), 결과.get("증빙"), 해당시=True)
    # 금지 · 한도 · 임계값 · 의무인데 코드가 쓸 수 없는 규범: 문맥에는 들어간다. 다른 규범과 섞여 있어도 '가능' 을 내지 않는다
    전체기준: list[dict] = []
    # 미확정 임계값에 묶인 의무: 코드 판정은 하지 않지만 금액으로 켜지는지는 볼 수 있다. 켜진다고 확인되면 이번 지출, 아니면 공통 사항이다(복사용지 3만원의 「50만원이 넘으면」)
    미확정_묶임: dict[str, list[dict]] = {}
    for t in 실효:
        if t.get("종류") == "임계값" and not _사용가능(t):
            for m in (t.get("결과") or {}).get("활성화") or []:
                미확정_묶임.setdefault(m, []).append(t)
    for n in 실효:
        if n.get("종류") in _제약종류 and not _사용가능(n):
            결과 = n.get("결과") or {}
            if n.get("종류") == "임계값" and 결과.get("활성화"):
                continue                    # 조건 조각("500만원 이상 구입할 경우")이다. 할 일은 묶인 의무 문장이 말한다
            if n.get("norm_id") in 지난기한:
                continue
            묶임 = 미확정_묶임.get(n.get("norm_id"))
            발동 = any(_임계값_발동(t, 입력) is True for t in 묶임 or [])
            공통 = not 발동 if 묶임 else _공통_규범(n)
            if n.get("종류") == "의무" and _다른_수범자(n):
                문장, 구분 = _의무_문장(n, 문서표시)
                조건추가(문장, 구분, 결과.get("시점"), 해당시=_해당시(n, 입력), 공통=공통, 승인주체=n.get("승인주체"))
                continue
            시점 = str(결과.get("시점") or "")
            자금전체 = any(str(v).split("@")[0].endswith("/*") for v in n.get("대상") or [])
            if 자금전체 and not 발동 and not (n.get("종류") == "의무" and (시점.startswith("집행 전") or 시점.startswith("기한"))):
                전체기준.append(n)          # 자금 전체에 걸린 기준은 수십 건이라 쓰기 전에 할 일만 따로 보인다. 판정 LLM 문맥에는 다 들어간다
            else:
                조건추가(_화면문장(n, 문서표시), "미확정규범", 결과.get("시점"), 결과.get("증빙"), 해당시=_해당시(n, 입력), 금지=n.get("종류") == "금지", 공통=공통,
                         승인주체=n.get("승인주체"))
    if 전체기준:
        조건추가(f"{_전체기준_머리} {_전체기준_문서(전체기준, 문서표시)}에서 확인해 주세요", "미확정규범", 공통=True)

    # ③ 금지: 예외 없음 + 조건이 없거나 구조화 조건이 전부 입력으로 확인될 때만 발동한다
    # 확인 질문 답(진술)은 코드 불가도 상한 해제도 만들지 않는다(5-6 ③ 은 입력 확인만 말한다. 더 안전한 쪽, 진행상황 E 표):
    # 해당 없음 → 「확인한 내용」 으로 옮기고 상한 조건부 유지 / 해당 → 금지 칸에 답을 앞세워 남기고 판정은 LLM / 모름 → 지금처럼
    선택지 = 진술_선택지(규범들)
    for n in 규범들:
        if n.get("종류") != "금지" or not _사용가능(n):
            continue
        자연어 = _자연어조건(n)
        구조화 = _구조화조건(n)
        평가 = [_조건_평가(c, 입력) for c in 구조화]
        답 = {i: v for i, c in enumerate(구조화) if 평가[i] is None and (v := _진술_평가(c, 입력, 선택지)) is not None}
        for i in 답:
            k = 구조화[i]["항목"]
            r["답변"][k] = {"항목": 진술항목[k][0], "답": (입력.get("진술") or {}).get(k)}
        합 = [평가[i] if 평가[i] is not None else 답.get(i) for i in range(len(구조화))]
        if 답 and False in 답.values():
            r["진술평가"][n.get("norm_id")] = False
            조건추가(_화면문장(n, 문서표시), "답변확인", 답변항목=[구조화[i]["항목"] for i in 답])
            continue
        if 답 and all(v is True for v in 합):
            r["진술평가"][n.get("norm_id")] = True
            if n.get("예외") or 자연어:
                # 5-6 ③ 은 예외 없음일 때만 금지를 확정한다. 예외 · 답으로 묻지 않은 조건이 남아 「이 기준에 해당해요」 로 단정하지 않는다(리뷰 3c R1).
                # 화면은 답하기 전과 같은 금지 문장(답으로 해당한다고 했으니 자금 전체 금지도 접지 않는다, E66), 판정 문맥(B4)은 답과 예외를 함께 준다
                조건추가(_화면문장(n, 문서표시), "금지조건")
                continue
            말 =" · ".join(f"{진술항목[구조화[i]['항목']][0]}: {(입력.get('진술') or {}).get(구조화[i]['항목'])}" for i in 답)
            # 자금 전체 금지라도 답으로 해당한다고 했으면 접힌 공통 사항이 아니라 「안 돼요」 칸에 보인다
            조건추가(f"답하신 내용({말})대로면 이 기준에 해당해요. {_화면문장(n, 문서표시)}", "금지조건",
                     답변항목=[구조화[i]["항목"] for i in 답], 진술="해당")
            continue
        if n.get("예외") or 자연어 or any(v is None for v in 평가):
            조건추가(_화면문장(n, 문서표시), "금지조건", 공통=_공통_규범(n))     # 조건을 코드로 확정하지 못한 금지는 조건으로 알리고 LLM 이 판단한다
            continue
        if all(v is True for v in 평가):
            r.update(종류="금지", 검증코드=["ONTOLOGY_BAN"],
                     코드판정={"종류": "금지", "비교값": None, "한도": None, "발췌": _발췌(n, "대상") or _발췌(n)})
            r["쓴규범"].append(n.get("norm_id"))
            r["발동규범"] = n
            return r

    # ④ 한도: 입력이 다 있고 예외가 없을 때만 비교한다. 부가세 기준 갈림은 기록만 하고 나머지 한도를 돈 뒤, 확정 위반이 없을 때 판단불가를 낸다
    한도갈림: tuple[dict, list[float], float] | None = None
    for n in 규범들:
        if n.get("종류") != "한도" or not _사용가능(n) or (n.get("수치") or {}).get("값") is None:
            continue
        if n.get("예외"):
            조건추가(_화면문장(n, 문서표시), "한도예외")
            continue
        지역맞음 = _지역_맞음(n, 입력)
        if 지역맞음 is False:
            r["제외규범"].append(n.get("norm_id"))     # 다른 지역 행이다. 판정 문맥의 "비교하지 못했다" 에서도 뺀다
            continue
        없음 = _모자란입력(n, 입력)
        값들, 기준 = _비교값들(n, 입력)
        if 없음 or 값들 is None:
            빠짐 = 없음 or ["금액"]
            for k in 빠짐:
                조건추가(_필요문구(k, n, 세목표시), "입력비움")
            continue
        연산 = (n.get("수치") or {}).get("위반연산") or "초과"
        결과들 = {_비교(v, 기준, 연산) for v in 값들}
        if ((n.get("결과") or {}).get("종류") or "불가") != "불가":
            # 초과분 자부담처럼 넘어도 집행 자체는 되는 한도: 불가를 내지 않고 원문으로 알린다
            r["쓴규범"].append(n.get("norm_id"))
            if True in 결과들:
                발췌 = _발췌(n, "수치")
                조건추가(발췌 if _문장_꼴(발췌) else _화면문장(n, 문서표시), "한도예외")
                r["초과규범"].append(n)
            continue
        r["쓴규범"].append(n.get("norm_id"))
        if len(결과들) > 1:
            한도갈림 = 한도갈림 or (n, 값들, 기준)
            continue
        if 결과들 == {True}:
            r.update(종류="한도초과", 검증코드=["LIMIT_EXCEEDED"],
                     코드판정={"종류": "한도초과", "비교값": _수치표시(n, 값들[0]), "한도": _수치표시(n, 기준),
                             "발췌": _발췌(n, "수치")})
            r["발동규범"] = n
            return r
    if 한도갈림:
        n, 값들, 기준 = 한도갈림
        r.update(종류="부가세기준미정", 검증코드=["VAT_BASIS_UNKNOWN"],
                 코드판정={"종류": "부가세기준미정", "비교값": _수치표시(n, 값들[0]), "한도": _수치표시(n, 기준),
                         "발췌": _발췌(n, "수치")})
        r["발동규범"] = n
        return r

    # ⑤ 임계값: 발동이면 의무 규범 활성화
    활성: set[str] = set()
    표 = {n.get("norm_id"): n for n in 규범들}
    for n in 규범들:
        if n.get("종류") != "임계값" or not _사용가능(n) or (n.get("수치") or {}).get("값") is None:
            continue
        없음 = _모자란입력(n, 입력)
        값들, 기준 = _비교값들(n, 입력)
        if 없음 or 값들 is None:
            for k in 없음 or ["금액"]:
                조건추가(_필요문구(k, n, 세목표시), "입력비움")
            continue
        연산 = (n.get("수치") or {}).get("발동연산") or "초과"
        결과들 = {_비교(v, 기준, 연산) for v in 값들}
        if len(결과들) > 1:
            r.update(종류="부가세기준미정", 검증코드=["VAT_BASIS_UNKNOWN"],
                     코드판정={"종류": "부가세기준미정", "비교값": _수치표시(n, 값들[0]), "한도": _수치표시(n, 기준),
                             "발췌": _발췌(n, "수치")})
            r["쓴규범"].append(n.get("norm_id"))
            r["발동규범"] = n
            return r
        r["쓴규범"].append(n.get("norm_id"))
        if 결과들 == {True}:
            r["발동임계값"].append(n.get("norm_id"))
            for m in (n.get("결과") or {}).get("활성화") or []:
                활성.add(m)

    # 우선순위로 제외된 상위 기준(지침 "이상")과 채택 기준(관리기준 "초과")의 결과가 갈리는 금액: 조건부 + 두 기준 표시(5-5 충돌 예)
    제외_임계값: list[dict] = []
    for u in 우선순위_적용 or []:
        제외, 채택 = u.get("제외규범"), u.get("채택규범")
        if not (isinstance(제외, dict) and isinstance(채택, dict)):
            continue
        if 제외.get("종류") != "임계값" or 채택.get("종류") != "임계값":
            continue
        제외_임계값.append(제외)
        제외수치, 채택수치 = 제외.get("수치") or {}, 채택.get("수치") or {}
        # 제외 기준이 부가세를 정의하지 않으면 채택 기준의 부가세로 맞춰 같은 금액을 본다. 정의없음 그대로 두면 정확히 그 금액이 아닌 곳에서도 갈린다
        맞춘제외 = {**제외, "수치": {**제외수치, "부가세": 채택수치.get("부가세")}} if 제외수치.get("부가세") == "정의없음" else 제외
        값1, 기준1 = _비교값들(맞춘제외, 입력)
        값2, 기준2 = _비교값들(채택, 입력)
        if not 값1 or not 값2:
            continue
        발1 = {_비교(v, 기준1, 제외수치.get("발동연산") or "초과") for v in 값1}
        발2 = {_비교(v, 기준2, 채택수치.get("발동연산") or "초과") for v in 값2}
        if 발1 != 발2:
            조건추가(f"이 금액은 두 기준의 결과가 달라요: 관리기준 「{_발췌(채택, '수치') or ''}」, "
                     f"지침 「{_발췌(제외, '수치') or ''}」. 관리기준을 우선 적용해요", "두기준")

    # ⑥ 의무: 임계값에 묶인 의무는 활성화된 것만, 독립 의무는 항상 조건이 된다
    # 대체로 제외된 임계값의 의무도 묶인 채로 둔다. 풀리면 금액과 상관없이 항상 조건이 된다
    묶인 = {m for n in [*규범들, *제외_임계값] if n.get("종류") == "임계값" for m in (n.get("결과") or {}).get("활성화") or []}
    for n in 규범들:
        if n.get("종류") != "의무" or not _사용가능(n):
            continue
        if n.get("norm_id") in 묶인 and n.get("norm_id") not in 활성:
            continue
        r["활성의무"].append(n)
        r["쓴규범"].append(n.get("norm_id"))
        if n.get("norm_id") in 지난기한:
            continue
        문장, 구분 = _의무_문장(n, 문서표시)
        # 임계값이 켠 의무는 그 조건을 코드가 확인한 것이다. 첫 줄에 둔다
        조건추가(문장, 구분, (n.get("결과") or {}).get("시점"), (n.get("결과") or {}).get("증빙"),
                 해당시=_해당시(n, 입력) and n.get("norm_id") not in 활성, 공통=_공통_규범(n) and n.get("norm_id") not in 활성, 승인주체=n.get("승인주체"))
    for m in 활성 - {n.get("norm_id") for n in r["활성의무"]}:
        if m in 표 and m not in 지난기한:               # 활성화 대상이 AI검증 미만이면 위 반복에서 빠진다. 조건으로만 알린다
            문장, 구분 = _의무_문장(표[m], 문서표시)
            조건추가(문장, 구분, (표[m].get("결과") or {}).get("시점"), 승인주체=표[m].get("승인주체"))
    return r


_한글자릿수 = dict(zip("일이삼사오육칠팔구", range(1, 10)))
_작은자리 = {"십": 10, "백": 100, "천": 1000}
_큰자리 = {"만": 10**4, "억": 10**8}


def _금액값(표현: str) -> float:
    """`3천5백만` · `12만5천` · `2,500,000` 을 값으로 푼다. 천 · 백 · 십은 뒤의 만 · 억에 묶인다."""
    합 = 구간 = 0.0
    for 수, 자리 in re.findall(r"(\d[\d,]*(?:\.\d+)?)?([억만천백십])?", 표현):
        값 = float(수.replace(",", "")) if 수 else 0.0
        if 자리 in _작은자리:
            구간 += (값 or 1) * _작은자리[자리]
        elif 자리:
            합 += ((구간 + 값) or 1) * _큰자리[자리]
            구간 = 0.0
        else:
            구간 += 값
    return 합 + 구간


def _한글수_값(글: str) -> int:
    합 = 구간 = 현재 = 0
    for 글자 in 글:
        if 글자 in _한글자릿수:
            현재 = _한글자릿수[글자]
        elif 글자 in _작은자리:
            구간, 현재 = 구간 + (현재 or 1) * _작은자리[글자], 0
        else:
            합, 구간, 현재 = 합 + ((구간 + 현재) or 1) * _큰자리[글자], 0, 0
    return 합 + 구간 + 현재


def _만억표기(값: int) -> str:
    """`20000000` 은 날짜 모양이라 금액으로 읽히도록 `2000만` 으로 쓴다."""
    return f"{값 // 10**8}억" if 값 % 10**8 == 0 else f"{값 // 10**4}만" if 값 % 10**4 == 0 else str(값)


_수자 = "일이삼사오육칠팔구십백천만억"
_비교말 = r"\s*(?:[을를은는이가]\s*)?(?:초과|이상|이하|미만|이내|까지|한도|넘|규모)"
_한글금액 = re.compile(rf"(?<![\d수몇])(?:[{_수자}]*[십백천만억]\s+)?[{_수자}]*[십백천만억](?=\s*원|{_비교말})")
# 값이 아닌 말: 수백만원 · 몇 십만 원, 만 원 단위 · 10만원대, 표 머리 (단위: 천원)
_값아닌말 = re.compile(r"(?<![가-힣\d])(?:수|몇|십수)\s*[십백천]*\s*[만억천백십]\s*원?|(?<![가-힣\d.,])[만천백]\s*원\s*(?:단위|대|짜리|권)|\d[\d,]*[만천백]?\s*원\s*(?:단위|대(?![가-힣]))|단위\s*[:：]\s*[십백천만억]*\s*원")
# 띄어 쓴 금액을 붙인다. 뒤 자리가 더 작을 때만 붙여 `3천만 1인` · `2천만 3천만원` · `5백 3백만원` 을 한 값으로 읽지 않는다
_금액_띄어쓰기 = re.compile(
    r"(?<=\d)\s+(?=(?:천만|백만|십만|억|만|천|백|십)(?:\s*원|[^가-힣]|$|[억만천백십]))"
    r"|(?<=억)\s+(?=\d[\d,.]*(?:천만|백만|십만|만|천|백|십|\s*원))"
    r"|(?<=만)\s+(?=\d[\d,.]*(?:[천백십](?![만억])|\s*원))"
    r"|(?<=천)\s+(?=\d[\d,.]*(?:백|십|\s*원))"
    r"|(?<=백)\s+(?=\d[\d,.]*(?:십|\s*원))"
    r"|(?<=십)\s+(?=\d[\d,.]*\s*원)"
    r"|(?<=[천백십])\s+(?=[만억])"
    r"|(?<=[\d억만천백십])\s+(?=원)")
_뒤_셈말 = re.compile(r"\s*(?:여\s*)?(?:명|건|개|대|회|번|호|조|항|쪽|면|년|월|일|주|시간|장|권|곳|차|종|배|세|살|가구|가지|%|퍼센트|km|kg|mAh|달러|유로|엔|위안)"
                    r"(?:[을를이가은는의에도로와과만]|까지|부터|에게|에서|이상|이하|이내|미만|초과|씩|당)*(?![가-힣])")
_뒤_비교말 = re.compile(_비교말)
_앞_범위 = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*[~∼～–-]\s*$")
_뒤_자리 = re.compile(r"(?:천만|백만|십만|억|만|천|백|십)$")
_앞_번호말 = re.compile(r"번호|코드|분류|우편|업종|전화|호수")
_원_조사 = "은는이가을를의에도로와과만입으며부까"     # `3원을` 의 원은 돈, `3원칙` · `2원화` 의 원은 다른 말이다


def _금액_찾기(글: str | None) -> tuple[str, list[tuple[float, int, int]]]:
    """(금액을 정리한 글, [(값, 시작, 끝)]). 금액집합 · 금액_빼기 가 쓴다."""
    글 = _값아닌말.sub(" ", 글 or "")

    def 한글(m: re.Match) -> str:
        접힘 = re.sub(r"\s+", "", m.group(0))
        뒤말 = m.string[m.end():].lstrip()[:1]
        if 뒤말 != "원" and (len(접힘) < 2 or 접힘[-1] not in "만억" or 접힘 == "이만"):      # `원` 없이는 두 글자 이상 + 만 · 억 끝만 읽는다. `이만 이상` 의 이만은 수가 아니다
            return m.group(0)
        앞말 = m.string[m.start() - 1] if m.start() else ""
        if 접힘[0] in "십백천만억" and "가" <= (앞말 or " ") <= "힣" and 앞말 not in "총약도":   # `비만원` 의 `만`, `인천 만원` 의 `천`
            return m.group(0)
        조각 = m.group(0).split()
        if len(조각) == 2 and 조각[0][-1] in "만억" and (조각[1][-1] == "억" or ("만" in 조각[1] and 조각[0][-1] == "만")):    # `이천만 삼천만원` 은 두 값이다
            return " ".join(_만억표기(_한글수_값(x)) for x in 조각)
        return str(_한글수_값(접힘))
    글 = _금액_띄어쓰기.sub("", 글)
    글 = _금액_띄어쓰기.sub("", _한글금액.sub(한글, 글))
    찾음: list[tuple[float, int, int]] = []
    for m in _금액표현.finditer(글):
        표현, 값 = m.group(0).rstrip(","), _금액값(m.group(0))
        앞글, 뒤글 = 글[max(m.start() - 1, 0):m.start()], 글[m.end():]
        if re.search(r"억\d+[천백]$", 표현) and 뒤글[:1] != "원":      # `1억5천` 은 1억 5천만이다
            값 = _금액값(표현 + "만")
        자리 = _뒤_자리.search(표현)
        원붙음 = 뒤글[:1] == "원" and not (값 < 100 and 표현.isdigit() and "가" <= (뒤글[1:2] or " ") <= "힣" and 뒤글[1:2] not in _원_조사)
        큰자리 = bool(re.search(r"[억만]", 표현)) and not 뒤글.startswith("큼")
        if not 값 or ((앞글 == "제" or (앞글.isascii() and 앞글.isalpha())) and not 원붙음 and not 큰자리):     # `제3조` · `B2`. `VAT240만원` 은 금액이다
            continue
        맨수 = (표현.isdigit() and 값 >= 10000 and not 표현.startswith("0") and not re.fullmatch(r"(?:19|20)\d{6}", 표현)
              and 앞글 != "-" and 뒤글[:1] != "-" and not _앞_번호말.search(글[max(m.start() - 20, 0):m.start()]))     # 업종 코드 · 우편번호 · 사업자번호는 금액이 아니다
        작은자리 = bool(자리) and bool(_뒤_비교말.match(뒤글))
        if 원붙음 or ((큰자리 or 작은자리 or 맨수 or ("," in 표현 and 값 >= 10000)) and not _뒤_셈말.match(뒤글)):
            찾음.append((값, m.start(), m.end()))
            범위 = _앞_범위.search(글[:m.start()])
            범위값 = _금액값(범위.group(1) + (자리.group(0) if 자리 else "")) if 범위 else None
            if 범위값 and 범위값 < 값:         # `100~300만원` 의 100 은 뒤 자리를 받는다. `2025-100만원` 의 2025 는 범위가 아니다
                찾음.append((범위값, 범위.start(), 범위.end()))
    return 글, 찾음


def 금액집합(글: str | None) -> set[float]:
    """글 안의 돈 금액을 원 단위 값으로 모은다. `원` 이 붙거나, 만 · 억이 들거나, 쉼표로 묶였거나 단위 없이 쓴 1만 이상인 수만 금액으로 본다(`1인` · `3회` · `2026년` · `1만 명` 은 뺀다).
    천 · 백 · 십으로 끝나면 `이하` 같은 비교말이 뒤따를 때만, 한글로 쓴 수는 `원` 이나 비교말이 붙을 때만 읽는다. `수백만원` 은 값이 아니라 읽지 않는다."""
    return {값 for 값, _, _ in _금액_찾기(글)[1]}


def 금액_빼기(글: str | None) -> str:
    """글에서 금액 표현을 지운다. 질문 검사가 금액이 아닌 수(`3회`)를 따로 볼 때 쓴다."""
    정리, 찾음 = _금액_찾기(글)
    for _, 시작, 끝 in sorted(찾음, key=lambda x: -x[1]):
        정리 = 정리[:시작] + " " + 정리[끝:].lstrip("원")
    return 정리


def _수(v) -> float | None:
    try:
        값 = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return 값 if 값 is not None and math.isfinite(값) else None


def 요약_금액_허용(입력: dict, 규범들: list[dict], 글들: list[str | None], 초과규범: list[dict] | None = None) -> tuple[set[float], set[float]]:
    """요약에 써도 되는 금액 (그대로 맞아야 하는 값, 반올림을 허용하는 부가세 환산 값).

    그대로: 입력 금액 · 입력 칸 · 글들(입력 문장 · 인용 원문) · 규범의 수치 · 발췌 · 조건 문장 · 기준 × 입력 분모(1인 30만원 × 2명).
    환산: 입력 부가세 구분에 맞는 공급가액 · 부가세액과 그 값을 분모로 나눈 값. 입력이 포함이면 ×1.1 을 허용하지 않는다.
    차액: 초과규범(넘었지만 코드 불가가 아닌 한도)과 입력 부가세 기준 하나로만 계산한다. 그 밖의 계산 금액은 허용하지 않는다(9b 사용자 결정 대기).
    """
    금액, 구분 = _수(입력.get("금액")), 입력.get("부가세구분")
    그대로: set[float] = set()
    환산: set[float] = set()
    if 금액 is not None:
        그대로.add(금액)
        if 구분 != "공급가액":                # 입력을 포함 금액으로 본다
            환산 |= {금액 / 1.1, 금액 / 11}
        if 구분 != "포함":                    # 입력을 공급가액으로 본다
            환산 |= {금액 * 1.1, 금액 * 0.1}
    for k in 입력키:
        값 = _수(입력.get(k))
        if 값 is not None:
            그대로.add(값)
    for 글 in 글들:
        그대로 |= 금액집합(글)
    for n in 규범들:
        수치 = n.get("수치") or {}
        if 수치.get("값") is not None and (수치.get("단위") or "원") == "원":
            기준 = float(수치["값"])
            그대로.add(기준)
            분모 = _분모값(n, 입력)
            if 분모 is not None:
                그대로.add(기준 * 분모)
                for v in (x / 분모 for x in _후보금액(금액, 수치.get("부가세"), 구분)) if 금액 is not None else ():
                    (그대로 if 금액 is not None and abs(v * 분모 - 금액) <= 1 else 환산).add(v)
        for g in (n.get("근거") or []):
            그대로 |= 금액집합(g.get("발췌"))
        결과 = n.get("결과") or {}
        그대로 |= 금액집합(결과.get("문장")) | 금액집합(결과.get("조건문장")) | 금액집합(_자연어조건(n))
    for n in 초과규범 or []:
        수치 = n.get("수치") or {}
        분모 = _분모값(n, {"인원": 1, **입력})
        if 금액 is not None and 분모 is not None and 수치.get("값") is not None:
            기준값 = _후보금액(금액, 수치.get("부가세"), 구분)[0]      # 입력 구분 그대로 본 금액
            (그대로 if 기준값 == 금액 else 환산).add(abs(기준값 - float(수치["값"]) * 분모))
    return 그대로, 환산


def 요약_금액_불일치(요약: str | None, 입력: dict, 규범들: list[dict], 글들: list[str | None], 초과규범: list[dict] | None = None) -> set[float]:
    """글의 금액 중 입력 · 규범 · 인용 원문 값과 맞지 않는 것. 반올림 차이(0.5%)는 부가세 환산 값에만 인정한다. 요약과 전제 사실에 쓴다."""
    그대로, 환산 = 요약_금액_허용(입력, 규범들, 글들, 초과규범)
    return {v for v in 금액집합(요약) if not any(abs(v - h) <= 1.0 for h in 그대로) and not any(abs(v - h) <= max(1.0, h * 0.005) for h in 환산)}


def 화면_조건(g: dict) -> list[dict]:
    """게이트 조건 중 화면에 내는 것. 다른 수범자의 의무는 판정 문맥에만 남는다."""
    return [c for c in g.get("조건") or [] if c.get("구분") != "다른수범자"]


def 화면_조건_나누기(조건들: list[dict]) -> dict[str, list[dict]]:
    """화면 칸별로 나눈다(리뷰 F6 · F7, 진단 3-1 · 3-8). `조건` 이번 지출에 영향 있는 것 / `기한조건` 그중 기한이 있는 일 / `금지조건` 해당하면 집행할 수 없는 것 /
    `해당시조건` 해당할 때만 필요한 것 / `공통사항` 어느 지출에나 같은 사업 전체 공통 사항(금지 먼저) /
    `확인한조건` 확인 질문 답으로 해당하지 않는다고 본 금지(진술이라 상한은 그대로, 화면 「확인한 내용」)."""
    칸: dict[str, list[dict]] = {"조건": [], "기한조건": [], "금지조건": [], "해당시조건": [], "공통사항": [], "확인한조건": []}
    for c in 조건들:
        금지 = c.get("금지") or c.get("구분") == "금지조건"
        이름 = ("확인한조건" if c.get("구분") == "답변확인" else "공통사항" if c.get("공통") else "금지조건" if 금지 else "해당시조건" if c.get("해당시")
              else "기한조건" if 기한_조건(c) else "조건")
        칸[이름].append(c)
    칸["공통사항"].sort(key=lambda c: 2 if c["문장"].startswith(_전체기준_머리) else 0 if c.get("금지") or c.get("구분") == "금지조건" else 1)
    return 칸


def 기한_조건(c: dict) -> bool:
    """규범 시점이 기한이다(`기한: 취득한 날로부터 1개월 이내`). 결제 전 · 결제 후 어느 할 일 칸에도 넣지 않는다."""
    return str(c.get("시점") or "").startswith("기한")


def 합치기(g: dict, llm판정: str | None) -> tuple[str, bool]:
    """5-6 "합치는 규칙". 코드 결과는 판정의 상한이다. 반환 (최종 판정, 코드가 낮췄는가)."""
    if g.get("종류") in ("금지", "한도초과"):
        return "불가", False
    if g.get("종류") == "부가세기준미정":
        return "판단불가", False
    if g.get("미확정초과") and llm판정 in ("가능", "조건부"):      # 검수 전 한도를 넘는 금액에 집행 안내를 내지 않는다(9b 사용자 결정 대기). 낮추기만 한다
        return "판단불가", True
    if llm판정 == "가능" and g.get("상한") == "조건부":
        return "조건부", True
    return llm판정 or "판단불가", False


# ---------------------------------------------------------------- self-test

def _규범(id_, 종류, **k) -> dict:
    return {"norm_id": id_, "종류": 종류, "verified": k.pop("verified", True), "해석미결": k.pop("해석미결", False),
            "예외": k.pop("예외", []), "조건": k.pop("조건", {"구조화": [], "자연어": []}),
            "수치": k.pop("수치", None), "결과": k.pop("결과", {}),
            "근거": k.pop("근거", [{"칸": "수치", "발췌": "1인 1일 최대 30만원 한도"}]), **k}


def _자체시험() -> int:
    일일 = _규범("한도1", "한도", 수치={"값": 300000, "단위": "원", "위반연산": "초과", "분모": ["인", "일"], "부가세": "정의없음"})
    시간당 = _규범("한도2", "한도", 수치={"값": 100000, "단위": "원", "위반연산": "초과", "분모": ["시간"], "부가세": "정의없음"})
    공제의무 = _규범("의무1", "의무", 결과={"문장": "기타소득세 등을 공제한 뒤 집행해요"})
    임계 = _규범("임계1", "임계값", 수치={"값": 125000, "단위": "원", "발동연산": "초과", "분모": ["인", "일"], "부가세": "정의없음"},
               결과={"활성화": ["의무1"]})
    포함한도 = _규범("한도3", "한도", 수치={"값": 500000, "단위": "원", "위반연산": "초과", "분모": [], "부가세": "포함"})
    심의 = _규범("임계2", "임계값", 수치={"값": 5000000, "단위": "원", "발동연산": "이상", "분모": [], "부가세": "포함"},
               결과={"활성화": ["의무2"]})
    심의의무 = _규범("의무2", "의무", 결과={"문장": "사업운영위원회 사전심의를 받아야 해요"})
    금지 = _규범("금지1", "금지", 근거=[{"칸": "대상", "발췌": "국외 여비는 집행할 수 없다"}])
    조건금지 = _규범("금지2", "금지", 조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "주관기관소속"}], "자연어": []},
                  결과={"문장": "주관기관 소속 인력에게는 집행할 수 없어요"})
    미검증 = _규범("금지3", "금지", verified=False)
    미결 = _규범("금지4", "금지", 해석미결=True)
    예외한도 = _규범("한도4", "한도", 예외=["규범:x"], 수치={"값": 100, "단위": "원", "위반연산": "초과", "분모": [], "부가세": "포함"})
    비율 = _규범("한도5", "한도", 수치={"값": 50, "단위": "%", "위반연산": "초과", "분모": ["계약총액"], "부가세": "포함"})

    def g(규범, **입력):
        확정 = 입력.pop("확정", True)
        return 게이트(규범, 입력, 확정=확정, 세목표시="멘토링")

    t: list[tuple[str, bool]] = []
    a = g([일일, 시간당], 금액=400000, 부가세구분="포함", 인원=1, 일수=1, 시간=8)
    t.append(("1일 40만원 → 불가 · LIMIT_EXCEEDED", a["종류"] == "한도초과" and a["검증코드"] == ["LIMIT_EXCEEDED"]))
    t.append(("불가 화면 값 '1인 1일 400,000원' · 한도 '1인 1일 300,000원'",
              a["코드판정"]["비교값"] == "1인 1일 400,000원" and a["코드판정"]["한도"] == "1인 1일 300,000원"
              and "30만원" in a["코드판정"]["발췌"]))
    t.append(("합치기: 한도 초과는 LLM 없이 불가", 합치기(a, None) == ("불가", False)))
    인당대 = _규범("한도대", "한도", 수치={"값": 1, "단위": "대", "위반연산": "초과", "분모": ["인"]})
    t.append(("1인당 1대: 2명 2대는 통과", g([인당대], 수량=2, 인원=2)["종류"] != "한도초과"))
    x대 = g([인당대], 수량=3, 인원=2)
    t.append(("1인당 1대: 2명 3대는 불가 · 화면 값 '1인 1.5대'", x대["종류"] == "한도초과" and x대["코드판정"]["비교값"] == "1인 1.5대"
              and x대["코드판정"]["한도"] == "1인 1대"))
    t.append(("1인당 1대: 인원이 없으면 비교하지 않고 묻는다", g([인당대], 수량=2)["종류"] != "한도초과"
              and any("인원" in str(c) for c in g([인당대], 수량=2)["조건"])))
    t.append(("분모 없는 대수 한도는 수량만 본다", g([_규범("한도대2", "한도", 수치={"값": 1, "단위": "대", "위반연산": "초과", "분모": []})], 수량=2)["종류"] == "한도초과"))
    수량없음 = g([인당대], 인원=2, 금액=3000000)["조건"]
    t.append(("1인당 1대: 수량이 없으면 수량을 묻는다(금액이 아니다)", any("수량을 알려주시면 1인 한도" in c["문장"] for c in 수량없음)
              and not any("금액을 알려" in c["문장"] for c in 수량없음)))
    b = g([일일], 금액=320000, 부가세구분="포함", 인원=1, 일수=1)
    t.append(("정의없음 32만원: 32만 > 30만 · 29.09만 ≤ 30만 → 판단불가 VAT", b["종류"] == "부가세기준미정"
              and b["검증코드"] == ["VAT_BASIS_UNKNOWN"]))
    t.append(("합치기: 부가세 미정은 판단불가", 합치기(b, None) == ("판단불가", False)))
    c = g([일일, 시간당], 금액=200000, 부가세구분="포함", 인원=1, 일수=1)
    t.append(("시간 비움 → 비교 안 함 · 조건 문구 · 상한 조건부", c["종류"] is None and c["상한"] == "조건부"
              and any("멘토링 시간을 알려주시면 시간당 한도" in x["문장"] for x in c["조건"])))
    t.append(("합치기: 입력 비움 + LLM 가능 → 조건부", 합치기(c, "가능") == ("조건부", True)))
    t.append(("합치기: 입력 비움 + LLM 불가 → LLM 판정", 합치기(c, "불가") == ("불가", False)))
    d = g([일일, 임계, 공제의무], 금액=200000, 부가세구분="포함", 인원=1, 일수=1)
    t.append(("20만원 > 12.5만 임계값 발동 → 공제 의무 조건", d["상한"] == "조건부"
              and any("공제" in x["문장"] for x in d["조건"]) and d["활성의무"][0]["norm_id"] == "의무1"))
    d2 = g([임계, 공제의무], 금액=100000, 부가세구분="포함", 인원=1, 일수=1)
    t.append(("10만원 임계값 미발동 → 의무 없음 · 상한 없음", d2["상한"] is None and not d2["활성의무"]))
    t.append(("발동한 임계값만 발동임계값에 적는다", d["발동임계값"] and not d2["발동임계값"]))
    자연어금지 = _규범("자연어금지", "금지", 대상=["품목:창업활동자금/구독료"], 조건={"구조화": [], "자연어": ["AI 솔루션 지원으로 결제"]}, 근거=[{"칸": "조건", "발췌": "그 구독료는 AI 솔루션 지원으로 결제할 수 없습니다."}])
    t.append(("자연어 조건이 있는 품목 금지는 품목 틀을 쓰지 않는다", 경로밖금지_구분(자연어금지, ["품목:창업활동자금/구독료"]) == "행위"
              and _품목금지_문장(자연어금지, ["품목:창업활동자금/구독료"]) == "그 구독료는 AI 솔루션 지원으로 결제할 수 없습니다."))
    e = g([심의, 심의의무], 금액=5000000, 부가세구분="포함")
    t.append(("500만원 '이상' 경계값 발동", any("사전심의" in x["문장"] for x in e["조건"])))
    e2 = g([심의, 심의의무], 금액=4999999, 부가세구분="포함")
    t.append(("499만9999원 미발동", not e2["조건"]))
    e3 = g([심의, 심의의무], 금액=4600000, 부가세구분="공급가액")
    t.append(("공급가액 460만 ×1.1 = 506만 → 포함 기준 규범 발동", any("사전심의" in x["문장"] for x in e3["조건"])))
    f = g([포함한도], 금액=490000, 부가세구분="모름")
    t.append(("부가세 모름: 49만 · 53.9만 갈림 → 판단불가", f["종류"] == "부가세기준미정"))
    f2 = g([포함한도], 금액=100000, 부가세구분="모름")
    t.append(("부가세 모름: 10만 · 11만 같음 → 통과", f2["종류"] is None))
    h = g([금지], 금액=1)
    t.append(("예외 없는 금지 + 확정 노드 → 불가 ONTOLOGY_BAN", h["종류"] == "금지" and h["검증코드"] == ["ONTOLOGY_BAN"]))
    h2 = g([금지], 금액=1, 확정=False)
    t.append(("LLM 추천만(확정 아님) → 금지 미발동 · 상한 조건부", h2["종류"] is None and h2["상한"] == "조건부"))
    섞임 = g([미검증, 일일], 금액=100000, 부가세구분="포함", 인원=1, 일수=1)
    t.append(("AI검증 + 미확정 금지 섞임 → 불가 아님 · 상한 조건부", 섞임["종류"] is None and 섞임["상한"] == "조건부"))
    세목 = 게이트([], {"금액": 1}, 확정=True, 세목미정=True)
    t.append(("비목만 확정 + 세목 한도 있음 → 상한 조건부", 세목["상한"] == "조건부" and "세목" in 세목["조건"][0]["문장"]))
    가정 = 게이트([], {"금액": 1}, 확정=True, 세목미정=True, 가정세목="멘토링비")
    t.append(("5-14 A: LLM 이 채운 세목은 안내 문장만 바꾼다 · 구분 · 상한 그대로 · 세목미정 아니면 문장 없음",
              가정["상한"] == "조건부" and [c["구분"] for c in 가정["조건"]] == ["세목미정"] and "「멘토링비」 기준" in 가정["조건"][0]["문장"]
              and 합치기(가정, "가능") == ("조건부", True) and not 게이트([], {"금액": 1}, 확정=True, 가정세목="멘토링비")["조건"]))
    자부담 = _규범("숙박", "한도", 수치={"값": 100000, "단위": "원", "위반연산": "초과", "분모": ["일"], "부가세": "정의없음"},
                결과={"종류": "초과분 자부담"})
    넘음 = 게이트([자부담], {"금액": 150000, "일수": 1, "부가세구분": "포함"}, 확정=True)
    t.append(("초과분 자부담 한도는 넘어도 불가가 아니다 · 조건으로 알린다", 넘음["종류"] is None and 넘음["상한"] == "조건부" and 넘음["조건"]))
    안넘음 = 게이트([자부담], {"금액": 50000, "일수": 1, "부가세구분": "포함"}, 확정=True)
    t.append(("초과분 자부담 한도 안이면 조건 없음", 안넘음["종류"] is None and not 안넘음["조건"]))
    전체 = [_규범(f"전체{i}", "의무", verified=False, 대상=["비목:창업활동자금/*@모두의창업"], 결과={"문장": f"문장{i}"}) for i in range(5)]
    묶음 = 게이트(전체, {"금액": 1}, 확정=True)
    t.append(("자금 전체 미확정 기준 5건은 조건 한 줄", 묶음["상한"] == "조건부" and len(묶음["조건"]) == 1 and "나머지" in 묶음["조건"][0]["문장"]))
    앞일 = _규범("앞일", "의무", verified=False, 대상=["비목:창업활동자금/*@모두의창업"], 결과={"문장": "선불카드로 결제하세요", "시점": "집행 전"})
    묶음2 = 게이트([앞일, *전체], {"금액": 1}, 확정=True)
    t.append(("자금 전체라도 쓰기 전 의무는 한 줄씩", [c["문장"] for c in 묶음2["조건"]][0] == "선불카드로 결제하세요" and len(묶음2["조건"]) == 2))
    문서이름 = _규범("문서", "금지", verified=False, 대상=["비목:창업활동자금/*@모두의창업"], 근거=[{"doc_id": "책자"}])
    묶음3 = 게이트([문서이름], {"금액": 1}, 확정=True, 문서표시={"책자": "안내 책자"})
    t.append(("자금 전체 기준은 문서 이름으로 안내", "안내 책자에서" in 묶음3["조건"][0]["문장"]))
    t.append(("입력 안내 조사", _을를("기준") == "을" and _을를("한도") == "를" and _을를("일수") == "를" and _을를("인원") == "을"))
    묶인미확정 = 게이트([_규범("문턱", "임계값", verified=False, 수치={"값": 5000000, "단위": "원", "발동연산": "이상"}, 결과={"활성화": ["뒤의무"]},
                      근거={"수치": [{"발췌": "500만원 이상 구입할 경우"}]}),
                       _규범("뒤의무", "의무", verified=False, 결과={"문장": "500만원 이상이면 심의를 받으세요"})], {"금액": 1}, 확정=True)
    t.append(("미확정 임계값은 조건 조각을 내지 않고 묶인 의무 문장만", [c["문장"] for c in 묶인미확정["조건"]] == ["500만원 이상이면 심의를 받으세요"]))
    예외관계 = 게이트([공제의무], {"금액": 1}, 확정=True, 우선순위_적용=[{"채택": "단서", "제외": "의무1", "조건": {"종류": "예외"}}])
    t.append(("예외 관계의 원칙 의무는 빠지지 않는다", 예외관계["활성의무"] and 예외관계["활성의무"][0]["norm_id"] == "의무1"))
    h3 = g([미검증], 금액=1)
    t.append(("AI검증 미만 금지 미발동 · 미검증만 → 상한 조건부", h3["종류"] is None and h3["미검증만"] and h3["상한"] == "조건부"))
    h4 = g([미결], 금액=1)
    t.append(("해석미결 금지 미발동", h4["종류"] is None and h4["미검증만"]))
    i1 = g([조건금지], 금액=1)
    t.append(("거래처 유형 모름 → 금지 미발동 · 조건으로 알림", i1["종류"] is None and i1["상한"] == "조건부"))
    i2 = g([조건금지], 금액=1, 거래처유형="주관기관소속")
    t.append(("거래처 유형 일치 → 금지 발동", i2["종류"] == "금지"))
    i3 = g([조건금지], 금액=1, 거래처유형="일반사업자")
    t.append(("거래처 유형 불일치 → 금지 미발동 · 상한 없음", i3["종류"] is None and i3["상한"] is None))
    j = g([예외한도], 금액=1000, 부가세구분="포함")
    t.append(("예외 붙은 한도 → 즉시 불가 아님", j["종류"] is None and j["상한"] == "조건부"))
    k = g([비율], 금액=600, 계약총액=1000, 부가세구분="포함")
    t.append(("선금 60% > 50% → 불가", k["종류"] == "한도초과" and k["코드판정"]["비교값"] == "60%"))
    k2 = g([비율], 금액=600, 부가세구분="포함")
    t.append(("계약총액 비움 → 조건", k2["종류"] is None and any("계약 총액" in x["문장"] for x in k2["조건"])))
    m = 게이트([], {"금액": 1}, 확정=True, 위임만=True)
    t.append(("위임만 → 상한 조건부 · 가이드 확인", m["상한"] == "조건부" and "가이드" in m["조건"][0]["문장"]))
    t.append(("규범 없음 → 상한 없음", 게이트([], {"금액": 1}, 확정=True)["상한"] is None))
    칸 = 추가입력_칸([일일, 시간당], 세목표시="멘토링")
    t.append(("추가 입력 칸: 인원 · 일수 · 멘토링 시간", [c["키"] for c in 칸] == ["인원", "일수", "시간"] and 칸[2]["라벨"] == "멘토링 시간"))
    t.append(("미검증 규범은 칸을 안 만든다", 추가입력_칸([_규범("x", "한도", verified=False, 수치={"값": 1, "분모": ["시간"]})]) == []))
    포함임계 = _규범("임계3", "임계값", 수치={"값": 20000000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"})
    t.append(("부가세 질문: 이미 고르면 묻지 않는다", not 부가세_질문_필요([포함임계], {"금액": 19000000, "부가세구분": "포함"})))
    t.append(("부가세 질문: 어느 기준이든 같은 쪽이면 묻지 않는다(100만 vs 2천만)", not 부가세_질문_필요([포함임계], {"금액": 1000000, "부가세구분": "모름"})))
    t.append(("부가세 질문: 두 기준이 갈리면 묻는다(1900만 ×1.1 > 2천만)", 부가세_질문_필요([포함임계], {"금액": 19000000, "부가세구분": "모름"})))
    t.append(("부가세 질문: 칸이 비어도 분모 1 최대값이 기준 아래면 묻지 않는다", not 부가세_질문_필요([일일], {"금액": 1000, "부가세구분": "모름"})))
    t.append(("부가세 질문: 칸이 비고 최대값이 기준을 넘으면 묻는다", 부가세_질문_필요([일일], {"금액": 400000, "부가세구분": "모름"})))
    누적임계 = _규범("누적", "임계값", 수치={"값": 20000000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함", "누적범위": "동일업체합계"})
    t.append(("부가세 질문: 누적 칸이 비면 금액이 작아도 묻는다", 부가세_질문_필요([누적임계], {"금액": 1000, "부가세구분": "모름"})))
    t.append(("부가세 질문: 칸이 있으면 비교한다", not 부가세_질문_필요([일일], {"금액": 1000, "부가세구분": "모름", "인원": 1, "일수": 1})))
    t.append(("부가세 질문: 금액이 없으면 묻는다", 부가세_질문_필요([포함임계], {"부가세구분": "모름"})))
    t.append(("부가세 질문: 미검증 규범은 세지 않는다", not 부가세_질문_필요([_규범("x", "임계값", verified=False, 수치=포함임계["수치"])], {"부가세구분": "모름"})))
    # 2-6: 앞 한도의 갈림이 뒤 한도의 확정 초과를 가리지 않는다
    t.append(("앞 한도 갈림 + 뒤 한도 확정 초과 → 불가", g([일일, 시간당], 금액=320000, 부가세구분="포함", 인원=1, 일수=1, 시간=1)["종류"] == "한도초과"))
    갈림만 = g([일일, 시간당], 금액=320000, 부가세구분="포함", 인원=1, 일수=1, 시간=4)
    t.append(("앞 한도 갈림 + 뒤 한도 통과 → 판단불가, 갈린 규범을 보인다", 갈림만["종류"] == "부가세기준미정" and 갈림만["발동규범"]["norm_id"] == "한도1"
              and 갈림만["검증코드"] == ["VAT_BASIS_UNKNOWN"]))
    # 3-2: 다른 수범자의 의무는 화면에 내지 않고 문맥 · 상한에는 남긴다
    기관의무 = _규범("기관", "의무", 수범자=["주체:주관기관"], 결과={"문장": "주관기관은 확인합니다"})
    함께의무 = _규범("함께", "의무", 수범자=["주체:주관기관", "주체:후속지원자"], 결과={"문장": "주관기관과 후속 지원자는 냅니다"})
    빈의무 = _규범("빈칸", "의무", 수범자=[], 결과={"문장": "수범자 없는 의무"})
    미검증기관 = _규범("미기관", "의무", verified=False, 수범자=["주체:멘토기관"], 결과={"문장": "멘토기관이 검토합니다"})
    수 = g([기관의무, 함께의무, 빈의무, 미검증기관], 금액=1)
    화면 = [c["문장"] for c in 화면_조건(수)]
    t.append(("다른 수범자 의무는 화면 조건에서 빠지고 사용자 쪽 · 빈 칸은 남는다", 화면 == ["주관기관과 후속 지원자는 냅니다", "수범자 없는 의무"]))
    t.append(("다른 수범자 의무도 게이트 조건(판정 문맥) · 상한에는 남는다", 수["상한"] == "조건부"
              and {c["문장"] for c in 수["조건"]} >= {"주관기관은 확인합니다", "멘토기관이 검토합니다"}))
    둘 = g([기관의무, _규범("같은말", "의무", 수범자=["주체:진출자"], 결과={"문장": "주관기관은 확인합니다"})], 금액=1)
    t.append(("같은 문장을 사용자 쪽 의무도 말하면 화면에 낸다", [c["문장"] for c in 화면_조건(둘)] == ["주관기관은 확인합니다"]))
    # F12: 모르는 주체 값은 사용자 쪽으로 본다
    새주체 = g([_규범("새", "의무", 수범자=["주체:새로만든주체"], 결과={"문장": "새 주체 의무"})], 금액=1)
    t.append(("모르는 수범자 값은 화면에서 빠지지 않는다", [c["문장"] for c in 화면_조건(새주체)] == ["새 주체 의무"]))
    # F5: 다른 주체의 집행 전 절차는 사용자 행동 문장과 함께 화면에 낸다
    사전 = g([_규범("사전", "의무", 수범자=["주체:주관기관"], 결과={"문장": "주관기관 담당자가 집행 전에 공간을 확인해야 합니다.", "시점": "집행 전"})], 금액=1)
    t.append(("다른 주체의 집행 전 절차: 화면에 낸다 · 결제 전 요청 문장", [c["문장"] for c in 화면_조건(사전)] == ["주관기관 담당자가 집행 전에 공간을 확인해야 합니다. 결제 전에 주관기관에 요청해 주세요"]))
    사후 = g([_규범("사후", "의무", 수범자=["주체:주관기관의장"], 결과={"문장": "기관장이 보고합니다", "시점": "집행 후"})], 금액=1)
    t.append(("다른 주체의 집행 후 의무는 여전히 판정 문맥에만", not 화면_조건(사후) and 사후["상한"] == "조건부"))
    장 = g([_규범("장", "의무", 수범자=["주체:주관기관의장"], 결과={"문장": "기관장이 집행 사유서를 갖춥니다", "시점": "집행 전"})], 금액=1)
    t.append(("기관장 내부 서류는 집행 전이어도 판정 문맥에만", not 화면_조건(장)))
    정함 = g([_규범("정함", "의무", 수범자=["주체:멘토기관"], 결과={"문장": "멘토기관이 책임멘토를 정합니다", "시점": "집행 전"})], 금액=1)
    t.append(("집행을 말하지 않는 다른 주체 절차는 판정 문맥에만", not 화면_조건(정함)))
    # F6 · F7: 화면 칸 나누기
    조건의무 = _규범("해외", "의무", 조건={"구조화": [], "자연어": ["해외구매"]}, 결과={"문장": "해외구매는 카드로 결제하세요"})
    거래처금지 = _규범("거래처", "금지", 조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "배우자"}], "자연어": []}, 결과={"문장": "배우자에게는 집행할 수 없습니다"})
    칸들 = 화면_조건_나누기(화면_조건(g([조건의무, 공제의무, 거래처금지], 금액=1)))
    t.append(("칸 나누기: 조건 없는 의무 · 금지 · 해당할 때", [[c["문장"] for c in 칸들[k]] for k in ("조건", "금지조건", "해당시조건")]
              == [["기타소득세 등을 공제한 뒤 집행해요"], ["배우자에게는 집행할 수 없습니다"], ["해외구매는 카드로 결제하세요"]]))
    t.append(("임계값이 켠 의무는 조건이 있어도 첫 칸", [c["문장"] for c in 화면_조건_나누기(화면_조건(g([_규범("문턱", "임계값", 수치={"값": 100, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"}, 결과={"활성화": ["켜짐"]}),
                                                                                  _규범("켜짐", "의무", 조건={"구조화": [], "자연어": ["2천만원 초과"]}, 결과={"문장": "사전심의"})], 금액=200, 부가세구분="포함")))["조건"]] == ["사전심의"]))
    미확정금지 = 화면_조건_나누기(화면_조건(g([_규범("미금", "금지", verified=False, 근거=[{"칸": "대상", "발췌": "키보드 | 불가"}])], 금액=1)))
    t.append(("미확정 금지도 금지 칸이다(채우는 조건이 아니다)", [c["문장"] for c in 미확정금지["금지조건"]] == ["관련 규정에 이 지출에 걸릴 수 있는 금지 기준이 있어요. 원문을 확인해 주세요"] and not 미확정금지["조건"]))
    증빙1 = _규범("증1", "의무", 결과={"문장": "세금계산서를 내세요", "시점": "집행 후", "증빙": ["증빙:세금계산서", "증빙:견적서"]})
    증빙2 = _규범("증2", "의무", 결과={"문장": "증빙으로 세금계산서와 견적서를 제출하세요", "시점": "집행 후", "증빙": ["증빙:견적서", "증빙:세금계산서"]})
    t.append(("같은 증빙 목록을 요구하는 두 의무는 한 줄", [c["문장"] for c in g([증빙1, 증빙2], 금액=1)["조건"]] == ["세금계산서를 내세요"]))
    증빙3 = _규범("증3", "의무", 결과={"문장": "보증보험도 내세요", "시점": "집행 후", "증빙": ["증빙:견적서", "증빙:세금계산서", "증빙:보증보험"]})
    증빙없음 = _규범("증0", "의무", 결과={"문장": "외부 업체와 계약하세요", "시점": "집행 후"})
    t.append(("증빙 없는 의무는 증빙 의무와 합치지 않는다(순서 무관)", [c["문장"] for c in g([증빙없음, 증빙1], 금액=1)["조건"]] == ["외부 업체와 계약하세요", "세금계산서를 내세요"]
              and [c["문장"] for c in g([증빙1, 증빙없음], 금액=1)["조건"]] == ["세금계산서를 내세요", "외부 업체와 계약하세요"]))
    t.append(("더 넓은 증빙 목록의 문장이 그 줄을 맡는다", [c["문장"] for c in g([증빙1, 증빙3], 금액=1)["조건"]] == ["보증보험도 내세요"]))
    # F13: 쓸 수 없는 입력값은 그 칸을 다시 묻는다. 부동소수 오차로 경계가 뒤집히지 않는다
    for 값 in (0, -1, "abc", float("inf"), float("nan")):
        나쁜칸 = g([일일], 금액=100000, 부가세구분="포함", 인원=값, 일수=1)
        t.append((f"인원 {값}: 인원을 묻는다(금액이 아니다)", any("인원을 알려주시면" in c["문장"] for c in 나쁜칸["조건"]) and not any("금액을 알려" in c["문장"] for c in 나쁜칸["조건"])))
    t.append(("금액 0 · 무한대는 비교하지 않고 금액을 묻는다", all(any("금액을 알려" in c["문장"] for c in g([일일], 금액=v, 부가세구분="포함", 인원=1, 일수=1)["조건"]) for v in (0, float("inf")))))
    t.append(("공급가액 환산 부동소수: 137500 포함 ÷ 1.1 = 125000 '초과' 아님", _후보금액(137500, "공급가액", "포함") == [125000.0]))
    # 1-1 · 2-5: 경로 밖 품목 규범은 조건 문장 + 상한만, 코드 불가를 내지 않는다
    표시문서 = {"책자": "안내 책자"}
    표행금지 = _규범("표행", "금지", 근거=[{"칸": "대상", "발췌": "키보드·마우스 | 불가 | 일상 사용 가치가 큼"}])
    문장금지 = _규범("문장", "금지", 결과={"문장": "귀금속은 살 수 없습니다."})
    밖 = 게이트([], {"금액": 30000}, 확정=True, 경로밖_품목규범=[표행금지, 문장금지, _규범("허용", "허용", 결과={"문장": "허용 문장"})])
    t.append(("경로 밖 품목 금지 → 코드 불가 아님 · 상한 조건부 · 조건 문장", 밖["종류"] is None and 밖["상한"] == "조건부"
              and [c["구분"] for c in 밖["조건"]] == ["금지조건", "금지조건"]))
    t.append(("표 행은 인용하지 않고 원문을 보게 한다", "원문을 확인해 주세요" in 밖["조건"][0]["문장"] and not any(x in 밖["조건"][0]["문장"] for x in ("|", "—", "키보드"))))
    t.append(("분류 · 문맥용 문장은 표 행을 구분자 없이 편다", _규범문장(표행금지) == "키보드·마우스 — 불가"))
    # 진단 3-7: 원천 문장이 없는 규범의 화면 문장. 숨겨도 상한은 그대로
    표금지 = _규범("표금지", "금지", verified=False, 근거=[{"칸": "대상", "doc_id": "책자", "조번호": "참고2-p29-1", "발췌": "미니PC·NUC | 불가 | 렌탈 가능"}])
    조각금지 = _규범("조각", "금지", verified=False, 근거=[{"칸": "대상", "doc_id": "책자", "조번호": "p4-2", "발췌": "유사 시설 및 동일 지역"}])
    문장발췌 = _규범("문발", "금지", verified=False, 근거=[{"칸": "대상", "doc_id": "책자", "조번호": "p11-2", "발췌": "온라인 결제는 승인되지 않습니다.(현장 결제 포함)."}])
    원천문장 = _규범("원천", "금지", verified=False, 결과={"문장": "원천 문장입니다 | 그대로"}, 근거=[{"칸": "대상", "발췌": "다른 발췌"}])
    표게이트 = 게이트([표금지, 조각금지, 문장발췌, 원천문장], {"금액": 1}, 확정=True, 문서표시=표시문서)
    표문장 = [c["문장"] for c in 표게이트["조건"]]
    t.append(("3-7: 표 행 금지는 근거 문서와 쪽을 말로 적는다", "안내 책자 참고2 29쪽에 이 지출에 걸릴 수 있는 금지 기준이 있어요. 원문을 확인해 주세요" in 표문장))
    t.append(("3-7: 서술어로 끝나지 않는 조각도 인용하지 않는다", "안내 책자 4쪽에 이 지출에 걸릴 수 있는 금지 기준이 있어요. 원문을 확인해 주세요" in 표문장 and not any("유사 시설" in s for s in 표문장)))
    t.append(("3-7: 괄호 꼬리가 붙은 문장 발췌는 그대로 쓴다", "온라인 결제는 승인되지 않습니다.(현장 결제 포함)." in 표문장))
    t.append(("3-7: 원천 문장이 있으면 건드리지 않는다", "원천 문장입니다 | 그대로" in 표문장))
    t.append(("3-7: 대체 문장이어도 상한은 그대로", 표게이트["상한"] == "조건부" and 합치기(표게이트, "가능") == ("조건부", True)))
    t.append(("3-7: 조각만 있는 금지 하나도 상한을 건다", 합치기(게이트([조각금지], {"금액": 1}, 확정=True), "가능") == ("조건부", True)))
    t.append(("3-7: 같은 쪽의 조각 금지 둘은 한 줄", len(게이트([조각금지, {**조각금지, "norm_id": "조각2", "근거": [{"칸": "대상", "doc_id": "책자", "조번호": "p4-2", "발췌": "기타 시설"}]}], {"금액": 1}, 확정=True, 문서표시=표시문서)["조건"]) == 1))
    표끝문장 = _규범("표끝", "금지", verified=False, 근거=[{"칸": "대상", "doc_id": "책자", "조번호": "p5-1", "발췌": "노트북은 살 수 없습니다 | 렌탈은 됩니다"}])
    t.append(("3-7: 서술어로 끝나도 표 행이면 인용하지 않는다", [c["문장"] for c in 게이트([표끝문장], {"금액": 1}, 확정=True, 문서표시=표시문서)["조건"]] == ["안내 책자 5쪽에 이 지출에 걸릴 수 있는 금지 기준이 있어요. 원문을 확인해 주세요"]))
    검증표금지 = _규범("검표", "금지", 조건={"구조화": [], "자연어": ["개인 용도"]}, 근거=[{"칸": "대상", "doc_id": "책자", "조번호": "p5-1", "발췌": "키보드 | 불가"}])
    t.append(("3-7: AI검증 금지의 조건 줄도 표 행을 인용하지 않는다 · 상한 유지", [c["문장"] for c in 게이트([검증표금지], {"금액": 1}, 확정=True, 문서표시=표시문서)["조건"]] == ["안내 책자 5쪽에 이 지출에 걸릴 수 있는 금지 기준이 있어요. 원문을 확인해 주세요"]
              and 게이트([검증표금지], {"금액": 1}, 확정=True, 문서표시=표시문서)["상한"] == "조건부"))
    단조각 = _규범("단조각", "금지", verified=False, 근거=[{"칸": "대상", "doc_id": "책자", "조번호": "p5-1", "발췌": "단, 해당 기술자료를 제공하면 집행이 불가하다."}])
    t.append(("3-7: 앞 문장에 이어지는 조각(단, …)은 서술어로 끝나도 인용하지 않는다", [c["문장"] for c in 게이트([단조각], {"금액": 1}, 확정=True, 문서표시=표시문서)["조건"]] == ["안내 책자 5쪽에 이 지출에 걸릴 수 있는 금지 기준이 있어요. 원문을 확인해 주세요"]))
    # 진단 3-1: 이번 지출 · 공통 사항. 칸만 가르고 상한은 그대로
    자금금지 = _규범("자금금지", "금지", 대상=["비목:창업/*"], 조건={"구조화": [], "자연어": ["현금 인출"]}, 결과={"문장": "현금으로 인출할 수 없습니다"})
    품목금지2 = _규범("품금", "금지", 대상=["비목:재료비"], 조건={"구조화": [], "자연어": ["개인 용도"]}, 결과={"문장": "개인 용도로 쓸 수 없습니다"})
    자금의무 = _규범("자금의무", "의무", 대상=["비목:창업활동자금/*@모두의창업"], 결과={"문장": "서약서를 제출해 주세요", "시점": "집행 전"})
    세목의무 = _규범("세목의무", "의무", 대상=["비목:재료비"], 결과={"문장": "견적서를 받아 주세요", "시점": "집행 전"})
    갈 = 화면_조건_나누기(화면_조건(g([자금금지, 품목금지2, 자금의무, 세목의무], 금액=1)))
    t.append(("3-1: 자금 전체 대상은 공통 사항, 세목 대상은 이번 지출", [c["문장"] for c in 갈["조건"]] == ["견적서를 받아 주세요"] and [c["문장"] for c in 갈["금지조건"]] == ["개인 용도로 쓸 수 없습니다"]
              and [c["문장"] for c in 갈["공통사항"]] == ["현금으로 인출할 수 없습니다", "서약서를 제출해 주세요"]))
    t.append(("3-1: 공통 사항은 금지 · 그 밖 · 나머지 기준 안내 순서", [c["문장"][:2] for c in 화면_조건_나누기([{"문장": "이 자금의 나머지 사용 기준은 어디", "공통": True}, {"문장": "의무", "공통": True}, {"문장": "금지", "공통": True, "금지": True}])["공통사항"]] == ["금지", "의무", "이 "]))
    검증문턱 = _규범("검문턱", "임계값", 대상=["비목:창업/*"], 수치={"값": 100, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"}, 결과={"활성화": ["켜진공통"]})
    켜진공통 = _규범("켜진공통", "의무", 대상=["비목:창업/*"], 결과={"문장": "넘으면 심의를 받으세요"})
    t.append(("3-1: 자금 전체 대상이어도 임계값이 켠 의무는 이번 지출이다", [c["문장"] for c in 화면_조건_나누기(화면_조건(g([검증문턱, 켜진공통], 금액=200, 부가세구분="포함")))["조건"]] == ["넘으면 심의를 받으세요"]))
    갈림문턱 = _규범("갈림문턱", "임계값", 대상=["비목:창업/*"], verified=False, 수치={"값": 500000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "정의없음"}, 결과={"활성화": ["갈림의무"]})
    낮은문턱 = _규범("낮은문턱", "임계값", 대상=["비목:창업/*"], verified=False, 수치={"값": 100, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"}, 결과={"활성화": ["갈림의무"]})
    높은문턱 = _규범("높은문턱", "임계값", 대상=["비목:창업/*"], verified=False, 수치={"값": 99999999, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"}, 결과={"활성화": ["갈림의무"]})
    갈림의무 = _규범("갈림의무", "의무", 대상=["비목:창업/*"], verified=False, 결과={"문장": "넘으면 승인을 받으세요"})
    칸_볼 = lambda 규범들, 금액, 구분="포함": {칸: [c["문장"] for c in 줄] for 칸, 줄 in 화면_조건_나누기(화면_조건(g(규범들, 금액=금액, 부가세구분=구분))).items()}
    t.append(("3-1: 부가세 기준으로 갈려도 넘을 수 있으면 이번 지출이다 (52만원 · 50만원 기준)", 칸_볼([갈림문턱, 갈림의무], 520000)["조건"] == [갈림의무["결과"]["문장"]]))
    t.append(("3-1: 부가세 모름도 넘을 수 있으면 이번 지출이다", 칸_볼([갈림문턱, 갈림의무], 480000, "모름")["조건"] == [갈림의무["결과"]["문장"]]))
    t.append(("3-1: 어느 기준으로도 안 넘으면 공통 사항이다", (lambda 칸: not 칸["조건"] and 칸["공통사항"] and 갈림의무["결과"]["문장"] not in 칸["조건"])(칸_볼([갈림문턱, 갈림의무], 400000))))
    t.append(("3-1: 의무 하나에 임계값이 여럿이면 하나라도 켜면 이번 지출이다 (순서 무관)", all(칸_볼([*문턱들, 갈림의무], 1000)["조건"] == [갈림의무["결과"]["문장"]] for 문턱들 in ([낮은문턱, 높은문턱], [높은문턱, 낮은문턱]))))
    공통만 = g([자금의무], 금액=1)
    t.append(("3-1: 공통 사항만 있어도 상한은 그대로", 공통만["상한"] == "조건부" and 합치기(공통만, "가능") == ("조건부", True)))
    t.append(("3-1: 같은 문장이 세목에도 걸리면 이번 지출에 한 번만", [[c["문장"] for c in 화면_조건_나누기(화면_조건(g(순서, 금액=1)))[k]] for 순서 in ([자금의무, {**세목의무, "결과": 자금의무["결과"]}], [{**세목의무, "결과": 자금의무["결과"]}, 자금의무]) for k in ("조건", "공통사항")]
              == [["서약서를 제출해 주세요"], [], ["서약서를 제출해 주세요"], []]))
    문턱미 = _규범("문턱미", "임계값", verified=False, 대상=["비목:창업/*"], 수치={"값": 500000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"}, 결과={"활성화": ["묶인의무"]})
    묶인의무 = _규범("묶인의무", "의무", verified=False, 대상=["비목:창업/*"], 결과={"문장": "한 건에 50만원이 넘으면 승인을 받아 주세요", "시점": "집행 후"})

    def 문턱(금액, **k):
        return 화면_조건_나누기(화면_조건(g([문턱미, 묶인의무], 금액=금액, 부가세구분="포함", **k)))
    t.append(("3-1: 복사용지 3만원에 50만원 문장은 공통 사항으로 간다", [c["문장"] for c in 문턱(30000)["공통사항"]] == ["이 자금의 나머지 사용 기준은 관련 규정에서 확인해 주세요"] and not 문턱(30000)["조건"]))
    t.append(("3-1: 넘는 금액이면 이번 지출 칸에 그 문장이 간다", [c["문장"] for c in 문턱(600000)["조건"]] == ["한 건에 50만원이 넘으면 승인을 받아 주세요"] and not 문턱(600000)["공통사항"]))
    금액모름 = 화면_조건_나누기(화면_조건(게이트([문턱미, 묶인의무], {}, 확정=True)))
    t.append(("3-1: 금액을 모르면 못 가르니 공통으로 보낸다", not 금액모름["조건"] and 금액모름["공통사항"]))
    t.append(("3-1: 상한은 금액과 상관없이 그대로", all(합치기(g([문턱미, 묶인의무], 금액=v, 부가세구분="포함"), "가능") == ("조건부", True) for v in (30000, 600000))))
    # 진단 3-8: 기한이 지난 의무는 빼고 판정을 올리지 않는다
    기한의무 = _규범("기한의무", "의무", 대상=["비목:재료비"], 결과={"문장": "10월 27일까지 서약서를 내세요", "시점": "기한: ~ 10.27(화)", "기한일": "2026-10-27"})
    기한글 = _규범("기한글", "의무", 대상=["비목:재료비"], 결과={"문장": "기한일 없는 기한 문장", "시점": "기한: ~ 11.30(월)"})

    def 기한(n=기한의무, **k):
        return g([n], 금액=1, **k)
    t.append(("3-8: 사용일자가 기한을 지나면 조건에서 뺀다 · 상한은 그대로", not 기한(사용일자="2026-10-28")["조건"] and 기한(사용일자="2026-10-28")["상한"] == "조건부" and 합치기(기한(사용일자="2026-10-28"), "가능") == ("조건부", True)))
    t.append(("3-8: 기한 당일 · 전이면 남긴다", all([c["문장"] for c in 기한(사용일자=d)["조건"]] == ["10월 27일까지 서약서를 내세요"] for d in ("2026-10-27", "2026-10-01"))))
    t.append(("3-8: 사용일자가 없거나 날짜가 아니면 빼지 않는다", all(기한(**k)["조건"] for k in ({}, {"사용일자": None}, {"사용일자": ""}, {"사용일자": "내일"}))))
    t.append(("3-8: 기한일 칸이 없으면 빼지 않는다", 기한(기한글, 사용일자="2030-01-01")["조건"]))
    t.append(("3-8: 기한일이 날짜가 아니면 빼지 않는다", 기한({**기한의무, "결과": {**기한의무["결과"], "기한일": "10.27"}}, 사용일자="2030-01-01")["조건"]))
    t.append(("3-8: 지난 기한의 미확정 의무 · 공통 · 경로 밖도 뺀다", not g([{**기한의무, "verified": False}], 금액=1, 사용일자="2026-11-01")["조건"]
              and not g([{**기한의무, "대상": ["비목:창업/*"]}], 금액=1, 사용일자="2026-11-01")["조건"] and not 게이트([], {"금액": 1, "사용일자": "2026-11-01"}, 확정=True, 경로밖_품목규범=[기한의무])["조건"]))
    t.append(("3-8: 경로 밖 의무만 지나도 상한은 그대로", 게이트([], {"금액": 1, "사용일자": "2026-11-01"}, 확정=True, 경로밖_품목규범=[기한의무])["상한"] == "조건부"))
    t.append(("3-8: 지난 기한도 판정 문맥(활성의무)에는 남는다", 기한(사용일자="2026-10-28")["활성의무"] == [기한의무]))
    t.append(("3-8: 시점이 기한인 줄은 기한 칸으로 간다", [c["문장"] for c in 화면_조건_나누기(화면_조건(기한()))["기한조건"]] == ["10월 27일까지 서약서를 내세요"]))
    t.append(("3-8: 기한인 공통 의무는 공통 칸이 앞선다", [c["문장"] for c in 화면_조건_나누기(화면_조건(기한({**기한의무, "대상": ["비목:창업/*"]})))["공통사항"]] == ["10월 27일까지 서약서를 내세요"]))
    # F4: 품목 금지가 아닌 문장을 「이 품목은」 틀에 넣지 않는다
    쪼개기 = _규범("쪼개기", "금지", 조건={"구조화": [{"항목": "결제유형", "연산": "=", "값": "분할"}], "자연어": []}, 근거=[{"칸": "대상", "발췌": "기준을 피하려 나누어 결제하면 부정 집행입니다."}])
    표제목 = _규범("제목", "금지", 조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "제한"}], "자연어": []}, 근거=[{"칸": "대상", "발췌": "[참고 1] 클린카드 제한 업종"}])
    질문형 = _규범("질문", "금지", 근거=[{"칸": "대상", "발췌": "노트북을 사도 되나요?"}])
    묶음금지 = _규범("묶음", "금지", 대상=["품목:묶음"], 근거=[{"칸": "대상", "발췌": "개인용 범용 전자기기 — 노트북"}])
    자기금지 = _규범("자기", "금지", 대상=["품목:키보드"], 근거=[{"칸": "대상", "발췌": "키보드 | 불가 | 사유"}])
    밖2 = 게이트([], {"금액": 1}, 확정=True, 경로밖_품목규범=[쪼개기, 표제목, 질문형, 묶음금지, 자기금지], 경로밖_품목=["품목:키보드"])
    문장2 = [c["문장"] for c in 밖2["조건"]]
    t.append(("행위 금지는 품목 틀 없이 원문 그대로", "기준을 피하려 나누어 결제하면 부정 집행입니다." in 문장2))
    t.append(("표 제목은 그 표에 든 품목이라고 쓴다(`[참고` 는 내지 않는다) · 질문형은 내지 않는다", not any("[참고" in s or "되나요" in s for s in 문장2)
              and any(s.startswith("이 품목은 「클린카드 제한 업종」에 들어 있어요") for s in 문장2) and 밖2["상한"] == "조건부"))
    t.append(("상위 묶음 금지는 묶음이라고 쓴다", any(s.startswith("이 품목이 속한 묶음") and "개인용 범용" not in s and "원문을 확인" in s for s in 문장2)))
    t.append(("품목 자체 금지만 「이 품목은 집행 금지」", [s for s in 문장2 if s.startswith("이 품목은 집행")] == ["이 품목은 집행 금지 규정에 해당할 수 있어요. 관련 규정 원문을 확인해 주세요"]))
    밖3 = 게이트([], {"금액": 1}, 확정=True, 경로밖_품목규범=[질문형])
    t.append(("문장이 아닌 금지만 있어도 상한은 조건부 · 화면 줄 없음", 밖3["상한"] == "조건부" and not 밖3["조건"]))
    서약 = _규범("서약", "의무", verified=False, 조건={"구조화": [], "자연어": ["50만원 이하 자산성 물품 구입"]}, 결과={"문장": "자산 보유 서약서를 내세요", "시점": "집행 후"})
    밖4 = 게이트([], {"금액": 1}, 확정=True, 경로밖_품목규범=[서약])
    t.append(("경로 밖 품목의 의무는 해당할 때의 조건으로 화면에 낸다", 화면_조건_나누기(밖4["조건"])["해당시조건"][0]["문장"] == "자산 보유 서약서를 내세요"))
    t.append(("합치기: 경로 밖 금지 + LLM 가능 → 조건부", 합치기(밖, "가능") == ("조건부", True)))
    # 2-1: 대체 관계가 있으면 정확히 그 금액만 두 기준. 제외 임계값의 의무는 풀리지 않는다
    지침 = _규범("지침", "임계값", 수치={"값": 20000000, "단위": "원", "발동연산": "이상", "분모": [], "부가세": "정의없음"},
               결과={"활성화": ["지침의무"]}, 근거=[{"칸": "수치", "발췌": "2천만원 이상"}])
    관리 = _규범("관리", "임계값", 수치={"값": 20000000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"},
               결과={"활성화": ["관리의무"]}, 근거=[{"칸": "수치", "발췌": "2천만원을 초과"}])
    지침의무 = _규범("지침의무", "의무", 결과={"문장": "지침 비교견적"})
    관리의무 = _규범("관리의무", "의무", 결과={"문장": "관리기준 비교견적"})

    def 대체(금액, 구분):
        return 게이트([관리, 지침의무, 관리의무], {"금액": 금액, "부가세구분": 구분}, 확정=True,
                    우선순위_적용=[{"제외규범": 지침, "채택규범": 관리}])
    a2 = 대체(20000000, "포함")
    t.append(("대체: 정확히 2천만원 → 판단불가 아님 · 두 기준 · 의무 미발동", a2["종류"] is None and [c["구분"] for c in a2["조건"]] == ["두기준"]))
    a3 = 대체(20000001, "포함")
    t.append(("대체: 2천만원 초과 → 관리기준 의무만, 두 기준 없음", [c["문장"] for c in a3["조건"]] == ["관리기준 비교견적"]))
    a4 = 대체(19000000, "공급가액")
    t.append(("대체: 공급가액 1900만(2090만) → 관리기준 의무만, 정의없음 제외 기준으로 갈리지 않는다", [c["문장"] for c in a4["조건"]] == ["관리기준 비교견적"]))
    a5 = 대체(10000000, "포함")
    t.append(("대체: 1천만원 → 조건 없음, 제외 임계값의 의무가 풀리지 않는다", not a5["조건"]))
    a6 = 게이트([지침, 관리, 지침의무, 관리의무], {"금액": 20000000, "부가세구분": "포함"}, 확정=True)
    t.append(("대체 데이터가 없으면 5-5:63 대로 판단불가", a6["종류"] == "부가세기준미정"))
    갈림 = 게이트([], {"금액": 20000000, "부가세구분": "포함"}, 확정=True, 우선순위_적용=[{
        "제외규범": _규범("지침", "임계값", 수치={"값": 20000000, "단위": "원", "발동연산": "이상", "분모": [], "부가세": "포함"},
                     근거=[{"칸": "수치", "발췌": "2천만원 이상"}]),
        "채택규범": _규범("관리", "임계값", 수치={"값": 20000000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"},
                     근거=[{"칸": "수치", "발췌": "2천만원을 초과"}])}])
    t.append(("정확히 2천만원: 두 기준 갈림 → 조건부 + 두 기준 표시", 갈림["상한"] == "조건부"
              and "2천만원 이상" in 갈림["조건"][0]["문장"] and "2천만원을 초과" in 갈림["조건"][0]["문장"]))
    t.append(("코드는 '가능' 을 만들지 않는다", 합치기({"상한": None}, "가능") == ("가능", False) and 합치기({"상한": None}, None)[0] == "판단불가"))
    # 2-3: 자금 적용 미정 공통 규범은 조건 · 비교에 쓰지 않고 상한만 남긴다
    공통 = _규범("공통", "의무", 결과={"문장": "온라인시스템으로 집행하세요"})
    공통금지 = _규범("공통금지", "금지")
    미정 = 게이트([], {"금액": 30000, "부가세구분": "포함"}, 확정=True, 공통미정=[공통, 공통금지])
    t.append(("공통 미정: 조건 없음 · 코드 불가 없음 · 상한 조건부", 미정["종류"] is None and not 미정["조건"] and 미정["상한"] == "조건부"))
    t.append(("합치기: 공통 미정 + LLM 가능 → 조건부", 합치기(미정, "가능") == ("조건부", True)))
    t.append(("공통 미정이 허용 · 우선순위뿐이면 상한을 걸지 않는다", 게이트([], {"금액": 1}, 확정=True, 공통미정=[_규범("허용", "허용")])["상한"] is None))
    # 1-2: 미검증 한도를 넘으면 조건 문장으로 알리고 판정은 바꾸지 않는다
    책자한도 = _규범("책자한도", "한도", verified=False, 수치={"값": 2000000, "단위": "원", "위반연산": "초과", "분모": ["인"], "부가세": "포함"},
                 결과={"종류": "불가"}, 근거=[{"칸": "수치", "doc_id": "책자", "발췌": "진출자 1인당 200만원(부가가치세 포함)"}])
    표시 = {"책자": "안내 책자"}

    def 책자게이트(금액, 구분, **k):
        return 게이트([책자한도], {"금액": 금액, "부가세구분": 구분, **k}, 확정=True, 문서표시=표시, 문의처="멘토기관 담당자에게")
    넘 = 책자게이트(2500000, "포함")
    t.append(("미검증 한도 초과: 코드 판정 아님 · 조건부와 가능은 판단불가로 낮춘다 · 불가는 그대로 · 문장", 넘["종류"] is None and 넘["상한"] == "조건부" and 넘["미확정초과"]
              and 합치기(넘, "조건부") == ("판단불가", True) and 합치기(넘, "가능") == ("판단불가", True) and 합치기(넘, "불가") == ("불가", False) and 합치기(넘, "판단불가") == ("판단불가", False)
              and 넘["조건"][0]["문장"] == "입력한 금액을 1명이 쓴다고 보면 안내 책자의 한도(1인 2,000,000원)를 넘습니다. 쓰기 전에 멘토기관 담당자에게 확인해 주세요" and 넘["조건"][0]["구분"] == "미확정한도"
              and 넘["초과규범"] == [책자한도]))
    t.append(("미검증 한도 안이면 미확정초과가 아니다 · 조건부 그대로", not 책자게이트(2000000, "포함")["미확정초과"] and 합치기(책자게이트(2000000, "포함"), "조건부") == ("조건부", False)))
    t.append(("미검증 한도: 경계 200만원은 안 넘는다", not [c for c in 책자게이트(2000000, "포함")["조건"] if c["구분"] == "미확정한도"]))
    t.append(("미검증 한도: 공급가액 190만 ×1.1 → 넘음, 포함 190만 → 안 넘음", [c for c in 책자게이트(1900000, "공급가액")["조건"] if c["구분"] == "미확정한도"]
              and not [c for c in 책자게이트(1900000, "포함")["조건"] if c["구분"] == "미확정한도"]))
    t.append(("미검증 한도: 부가세 모름으로 갈리면 넘을 수 있다고 쓴다", "부가세 포함 여부에 따라 넘을 수 있습니다" in 책자게이트(1900000, "모름")["조건"][0]["문장"]))
    t.append(("미검증 한도: 인원을 넣고 넘으면 단정한다", 책자게이트(5000000, "포함", 인원=2)["조건"][0]["문장"].startswith("안내 책자의 한도(1인 2,000,000원)를 넘습니다")))

    def 미검증(값=2000000, 분모=(), 부가세="포함", **k):
        return _규범("미검", "한도", verified=k.pop("verified", False), 수치={"값": 값, "단위": "원", "위반연산": k.pop("연산", "초과"), "분모": list(분모), "부가세": 부가세}, 결과={"종류": "불가"},
                   근거=[{"칸": "수치", "doc_id": "책자", "발췌": "한도"}], **k)

    def 안내(n, 금액=2100000, 구분="포함", **입력):
        return _미확정한도_안내(n, {"금액": 금액, "부가세구분": 구분, **입력}, 표시, "멘토기관 담당자에게")
    t.append(("리뷰 6: 분모 없는 한도는 합계인지 몰라 단정하지 않는다", 안내(미검증(500000), 900000) == "1개 기준으로 안내 책자의 한도(500,000원)를 넘을 수 있습니다. 쓰기 전에 멘토기관 담당자에게 확인해 주세요"))
    t.append(("리뷰 15: 다른 수범자 · 자연어 조건 · 평가 못 한 구조화 조건 · 해석미결은 안내하지 않는다", all(안내(x) is None for x in (
        미검증(수범자=["주체:주관기관"]), 미검증(조건={"구조화": [], "자연어": ["국외 출장인 경우"]}),
        미검증(조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "배우자"}], "자연어": []}), 미검증(해석미결=True)))))
    t.append(("리뷰 15: 구조화 조건이 맞다고 확인되면 안내한다", 안내(미검증(조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "배우자"}], "자연어": []}), 거래처유형="배우자") is not None))
    t.append(("리뷰 15: 규범 정의없음 + 입력 포함은 입력을 다시 묻지 않고 규정 사정을 쓴다", "규정에 부가세 기준이 적혀 있지 않아 넘을 수 있습니다" in 안내(미검증(부가세="정의없음", 분모=["인"]), 인원=1)))
    t.append(("안내: 예외 · 지역 다름 · 초과분 자부담은 내지 않는다", 안내(미검증(예외=["규범:x"])) is None and 안내({**미검증(), "수치": {**미검증()["수치"], "지역": "특별시"}}, 출장지구분="광역시") is None
              and 안내({**미검증(), "결과": {"종류": "초과분 자부담"}}) is None))
    t.append(("안내: 위반연산 이상을 지킨다", 안내(미검증(연산="이상"), 2000000) is not None and 안내(미검증(), 2000000) is None))
    t.append(("안내: 게이트는 검증된 규범에 안내를 쓰지 않고 경로 밖 미검증 한도에는 쓴다", not 게이트([미검증(verified=True)], {"금액": 2100000, "부가세구분": "포함"}, 확정=True)["미확정초과"]
              and 게이트([], {"금액": 2100000, "부가세구분": "포함"}, 확정=True, 문서표시=표시, 경로밖_품목규범=[미검증()])["미확정초과"]))
    t.append(("미검증 한도: 인원을 주면 나눠 비교한다", not [c for c in 책자게이트(2500000, "포함", 인원=2)["조건"] if c["구분"] == "미확정한도"]))
    자부담 = _규범("숙박책자", "한도", verified=False, 수치={"값": 100000, "단위": "원", "위반연산": "초과", "분모": ["일"], "지역": "특별시", "부가세": "정의없음"}, 결과={"종류": "초과분 자부담"})
    t.append(("미검증 한도: 초과분 자부담은 '넘습니다' 를 쓰지 않는다", not [c for c in 게이트([자부담], {"금액": 500000, "일수": 1, "출장지구분": "특별시", "부가세구분": "포함"}, 확정=True)["조건"] if c["구분"] == "미확정한도"]))
    t.append(("미검증 한도: AI검증 규범에는 쓰지 않는다(코드 불가 경로)", 게이트([포함한도], {"금액": 600000, "부가세구분": "포함"}, 확정=True)["종류"] == "한도초과"))
    # 1-2 (a): 요약의 금액을 입력 · 규범 값과 맞춘다
    t.append(("금액집합: 만 단위 · 원 · 띄어쓰기 · 1인 3회는 뺀다", 금액집합("1인 3회 2천만원을 12만 5천 원 2,500,000원 200만 초과, 2026년") == {20000000, 125000, 2500000, 2000000}))
    입 = {"금액": 2500000, "부가세구분": "포함"}
    t.append(("금액집합: 3천5백만 · 1백50만 · 3억5천만은 앞 자리를 만 · 억에 묶는다", 금액집합("3천5백만원 1백50만원 3억5천만원") == {35000000, 1500000, 350000000}))
    t.append(("금액집합: 한글 수 · 쉼표 수", 금액집합("이백만원 천만원 오십만 원 2,000,000 1,000명") == {2000000, 10000000, 500000}))
    t.append(("금액집합: 수백만원은 값이 아니다", 금액집합("수백만원") == set()))
    for 글, 기대 in (("3천만 1인 기준입니다", {30000000}), ("삼천만 초과 외주입니다", {30000000}), ("12만5천 이하", {125000}), ("1억5천 이하", {150000000}), ("총삼백오십만원까지", {3500000}),
                   ("5십만원까지", {500000}), ("한도 500000 입니다", {500000}), ("100~300만원", {1000000, 3000000}), ("10~20만원", {100000, 200000}), ("1,000~3,000원", {1000, 3000}),
                   ("수십 만원 단위", set()), ("오십 만원 이하", {500000}), ("만 원 단위로", set()), ("1만 명 규모", set()), ("10,000명", set()), ("20261107", set()), ("2026.11.07", set()),
                   ("2천만 3천만원", {20000000, 30000000}), ("이 백만원은", {1000000}), ("이백 만원", {2000000}), ("이만 줄입니다", set()), ("3.5백만원", {3500000}), ("30만원짜리 중고", {300000}),
                   ("12만 5,000원", {125000}), ("200만 일반 지출", {2000000}), ("이백만 초과", {2000000}), ("2 백만원", {2000000}), ("1억 5천만원", {150000000}), ("5천 이하", {5000}), ("5천 명", set())):
        t.append((f"금액집합: {글}", 금액집합(글) == 기대))
    for 글, 기대 in (("3원칙에 따라", set()), ("2원화", set()), ("3원을 냅니다", {3}), ("50원이", {50}), ("1만 명까지", set()), ("3만여 건", set()), ("10,000건까지", set()), ("30,000개까지", set()), ("1만 명에게", set()),
                   ("12만km 이상", set()), ("2만 달러", set()), ("10,000 mAh", set()), ("산업분류 56211 업종", set()), ("우편번호 34141", set()), ("123-45-67890", set()),
                   ("이만 이상", set()), ("이만 한도에서", set()), ("이만 원", {20000}), ("구만 구천 원", {99000}), ("일억 오천만 원", {150000000}), ("이천만 삼천만원", {20000000, 30000000}), ("인천 만원", set()),
                   ("5백 3백만원", {3000000}), ("3천 2만원", {20000}), ("3천 5백만원", {35000000}), ("3천 5백 원", {3500}), ("2025-100만원", {1000000}), ("개정 2018.3.13,2025.12.30", set()), ("214-2353,2356", set()),
                   ("KRW2,400,000원", {2400000}), ("VAT240만원", {2400000}), ("PC240만원", {2400000}), ("공제240만원", {2400000}), ("경제240만원", {2400000}), ("제3조", set()), ("제3조의2", set()), ("B2", set()),
                   ("삼천만을 초과", {30000000}), ("삼천만 넘는 외주", {30000000}), ("3천 넘는 외주", {3000}), ("2천4백 규모", {2400}), ("5백을 넘으면", {500}),
                   ("(단위:천원)", set()), ("(단위: 백만원)", set()), ("주빈 250 (단위:천원)", set())):
        t.append((f"금액집합: {글}", 금액집합(글) == 기대))
    t.append(("금액_빼기: 금액을 지우고 다른 수는 남긴다", "3회" in 금액_빼기("3회 30만원") and "30" not in 금액_빼기("3회 30만원")))
    # 독립 검토 A: 경로 밖 미검증 한도를 단정할 수 없어도 '가능' 을 내지 않는다
    for 이름, n in (("자연어 조건", 미검증(조건={"구조화": [], "자연어": ["국외 출장인 경우"]})), ("구조화 미확인", 미검증(조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "배우자"}], "자연어": []})),
                  ("해석미결", 미검증(해석미결=True)), ("다른 수범자", 미검증(수범자=["주체:주관기관"]))):
        밖 = 게이트([], {"금액": 2100000, "부가세구분": "포함"}, 확정=True, 문서표시=표시, 경로밖_품목규범=[n])
        t.append((f"경로 밖 미검증 한도({이름}): 넘으면 상한 조건부 · 넘는다고 단정하지 않는다 · 판단불가로 낮추지 않는다", 밖["상한"] == "조건부" and not 밖["미확정초과"] and 합치기(밖, "가능")[0] == "조건부"
                  and not any("넘습니다" in c["문장"] for c in 밖["조건"])))
        안넘음 = 게이트([], {"금액": 1000000, "부가세구분": "포함"}, 확정=True, 문서표시=표시, 경로밖_품목규범=[n])
        t.append((f"경로 밖 미검증 한도({이름}): 안 넘으면 조건을 내지 않는다", not 안넘음["조건"] and 안넘음["상한"] is None))
    t.append(("요약 금액: 인용 원문의 금액은 일치", not 요약_금액_불일치("숙박 8만원 이내", 입, [], ["숙박비 한도 80,000원"])))
    t.append(("요약 금액: 없는 2천만원은 불일치", 요약_금액_불일치("2천만원을 초과하는 외주용역비로", 입, [책자한도], []) == {20000000}))
    t.append(("요약 금액: 입력 · 규범 값 · 초과 규범의 차액은 일치", not 요약_금액_불일치("250만원은 200만원을 넘고 초과분 50만원은 쓸 수 없습니다", 입, [책자한도], [], [책자한도])))
    t.append(("요약 금액: 넘지 않은 규범의 차액은 불일치", 요약_금액_불일치("초과분 50만원", 입, [책자한도], []) == {500000}))
    작은한도 = _규범("작은", "임계값", 수치={"값": 100000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"})
    t.append(("리뷰 2: 다른 규범과의 차액 · 포함 입력의 ×1.1 차액은 불일치", 요약_금액_불일치("240만원 75만원 225만원 275만원 265만원", 입, [책자한도, 작은한도], [], [책자한도]) == {2400000, 750000, 2250000, 2750000, 2650000}))
    t.append(("리뷰 2: 외주 2천만 임계 잔액은 불일치", 요약_금액_불일치("1,750만원까지 더 계약", 입, [_규범("외", "임계값", 수치={"값": 20000000, "단위": "원", "발동연산": "초과", "분모": [], "부가세": "포함"})], []) == {17500000}))
    공급입력 = {"금액": 1000000, "부가세구분": "공급가액"}
    포함입력 = {"금액": 1100000, "부가세구분": "포함"}
    t.append(("리뷰 4: 부가세액 · 공급가액 합계는 입력 구분대로 일치", not 요약_금액_불일치("공급가액 100만원에 부가세 10만원을 더한 110만원", 공급입력, [], []) and not 요약_금액_불일치("공급가액 100만원, 부가세 10만원", 포함입력, [], [])))
    t.append(("리뷰 2: 포함 입력에 ×1.1 은 불일치", 요약_금액_불일치("121만원", 포함입력, [], []) == {1210000} and 요약_금액_불일치("90만원", 공급입력, [], []) == {900000}))
    t.append(("입력 구분 모름이면 양쪽 환산이 일치", not 요약_금액_불일치("110만원 또는 약 91만원, 부가세 10만원", {"금액": 1000000, "부가세구분": "모름"}, [], [])))
    t.append(("리뷰 4: 기준 × 입력 분모는 일치 · 분모 입력 없으면 불일치", not 요약_금액_불일치("하루 60만원까지", {"금액": 500000, "부가세구분": "포함", "인원": 2, "일수": 1}, [일일], [])
              and 요약_금액_불일치("하루 60만원까지", {"금액": 500000, "부가세구분": "포함"}, [일일], []) == {600000}))
    t.append(("계산한 세액은 불일치(사용자 결정 대기)", 요약_금액_불일치("기타소득세 22,000원", 입, [], []) == {22000}))
    t.append(("리뷰 3: 오차는 부가세 환산에만: 규범 값 근처 · 입력 근처는 불일치, 환산 근처는 일치", 요약_금액_불일치("한도 201만원", {"금액": 1000000, "부가세구분": "포함"}, [책자한도], []) == {2010000}
              and 요약_금액_불일치("한도 2천10만원", 입, [], ["기준 20,000,000원"]) == {20100000} and 요약_금액_불일치("251만원", 입, [], []) == {2510000} and not 요약_금액_불일치("공급가액 227만원", 입, [], [])))
    t.append(("분모로 나눈 값은 일치", not 요약_금액_불일치("1인 125만원", {"금액": 2500000, "부가세구분": "포함", "인원": 2}, [책자한도], [])))
    t.append(("입력 칸 값은 일치", not 요약_금액_불일치("계약 총액 1,000만원", {"금액": 1000, "부가세구분": "포함", "계약총액": 10000000}, [], [])))
    t.append(("규범 결과 문장 금액은 일치", not 요약_금액_불일치("50만원 이상", 입, [_규범("문", "의무", 근거=[], 결과={"문장": "50만원 이상이면 승인"})], [])))
    t.append(("요약 금액: 규범 발췌 금액은 일치", not 요약_금액_불일치("200만원 안에서", {"금액": 1000}, [_규범("x", "허용", 근거=[{"칸": "대상", "발췌": "200만원"}])], [])))
    t.append(("요약 금액: 부가세 환산 · 입력 문장 금액은 일치", not 요약_금액_불일치("공급가액 227만원, 답한 대로 총 30만원", {"금액": 2500000}, [], ["총 30만원 쓰려고요"])))
    t.append(("요약 금액: 금액이 없으면 불일치 없음", not 요약_금액_불일치("쓸 수 있습니다", 입, [], [])))
    t.append(("요약 금액: 0.5% 를 넘는 차이는 불일치", 요약_금액_불일치("240만원입니다", 입, [], []) == {2400000}))

    # 수정 3c 4-2: 확인 질문 답(진술) → 항목값. 코드 불가 · 상한 해제 없음
    def 진(규범, 답=None, **입력):
        return 게이트(규범, {"금액": 1, **입력, **({"진술": {"거래처유형": 답}} if 답 else {})}, 확정=True)
    자금금지 = _규범("자금금지", "금지", 대상=["비목:창업/*"], 조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "주체:배우자"}], "자연어": []},
                  결과={"문장": "배우자에게는 집행할 수 없습니다"})
    둘값 = _규범("둘값", "금지", 조건={"구조화": [{"항목": "거래처유형", "연산": "in", "값": ["계열사", "임직원 업체"]}], "자연어": []}, 결과={"문장": "계열사 · 임직원 업체와 거래할 수 없습니다"})
    칸들 = 추가입력_칸([조건금지, 자금금지, 둘값, _규범("미조건", "금지", verified=False, 조건=조건금지["조건"]),
                    _규범("의무조건", "의무", 조건={"구조화": [{"항목": "거래처유형", "연산": "=", "값": "프리랜서"}], "자연어": []}),
                    _규범("고의", "금지", 조건={"구조화": [{"항목": "고의성", "연산": "=", "값": "있음"}], "자연어": []})])
    t.append(("3c 칸: 사용가능 금지의 거래처유형만 선택 칸 하나 · 해당 없음 먼저 · 주체 머리 뗌 · 의무 · 미검증 · 표 밖 항목은 칸 없음",
              칸들 == [{"키": "거래처유형", "라벨": "거래 상대가 해당하는 것", "단위": None, "형식": "선택",
                        "선택지": ["해당 없음", "주관기관소속", "배우자", "계열사", "임직원 업체"]}]))
    t.append(("3c 칸: 숫자 칸 앞 · 선택 칸 뒤", [c["키"] for c in 추가입력_칸([일일, 조건금지])] == ["인원", "일수", "거래처유형"]))
    해당 = 진([조건금지], "주관기관소속")
    t.append(("3c 해당: 코드 불가 아님 · 상한 조건부 · 금지 칸에 답을 앞세움", 해당["종류"] is None and 해당["상한"] == "조건부" and 합치기(해당, "가능") == ("조건부", True)
              and [c["문장"] for c in 화면_조건_나누기(화면_조건(해당))["금지조건"]] == ["답하신 내용(거래 상대: 주관기관소속)대로면 이 기준에 해당해요. 주관기관 소속 인력에게는 집행할 수 없어요"]
              and 해당["진술평가"] == {"금지2": True} and 해당["답변"] == {"거래처유형": {"항목": "거래 상대", "답": "주관기관소속"}}))
    자금해당 = 화면_조건_나누기(화면_조건(진([자금금지], "배우자")))
    t.append(("3c 해당: 자금 전체 금지라도 접힌 공통 사항이 아니라 금지 칸", len(자금해당["금지조건"]) == 1 and not 자금해당["공통사항"]))
    없음 = 진([조건금지], 해당없음)
    없음칸 = 화면_조건_나누기(화면_조건(없음))
    t.append(("3c 해당 없음: 줄이 확인한 조건으로 · 상한 조건부 유지(입력 값 불일치와 다름) · 가능 → 조건부", 없음["종류"] is None and 없음["상한"] == "조건부"
              and [c["문장"] for c in 없음칸["확인한조건"]] == ["주관기관 소속 인력에게는 집행할 수 없어요"] and not 없음칸["금지조건"]
              and 합치기(없음, "가능") == ("조건부", True) and 없음["진술평가"] == {"금지2": False}))
    t.append(("3c 대조: 같은 값을 입력 키로 넣으면 옛 동작(발동 · 상한 없음). 화면 경로는 진술로만 넣는다", i2["종류"] == "금지" and i3["상한"] is None))
    밖값 = 진([조건금지], "아무 말")
    t.append(("3c 선택지 밖 답은 버림: 지금처럼 금지 칸 · 답변 없음", [c["구분"] for c in 밖값["조건"]] == ["금지조건"] and not 밖값["답변"] and not 밖값["진술평가"]))
    t.append(("3c in 조건: 한 값을 고르면 해당 · 해당 없음이면 확인한 조건", 진([둘값], "임직원 업체")["진술평가"] == {"둘값": True} and 진([둘값], 해당없음)["진술평가"] == {"둘값": False}))
    다른값 = 진([조건금지, 자금금지], "배우자")
    t.append(("3c 하나를 고르면 다른 값의 조건은 모른다(둘에 해당할 수 있다)", 다른값["진술평가"] == {"자금금지": True}
              and any(c["문장"] == "주관기관 소속 인력에게는 집행할 수 없어요" and c["구분"] == "금지조건" for c in 다른값["조건"])))
    같은문장 = _규범("같은문장", "금지", 조건={"구조화": [], "자연어": ["주관기관 소속이면"]}, 결과={"문장": "주관기관 소속 인력에게는 집행할 수 없어요"})
    t.append(("3c 해당 없음이어도 같은 문장을 답이 닿지 않은 규범이 말하면 금지 칸에 남는다", [c["구분"] for c in 진([조건금지, 같은문장], 해당없음)["조건"]] == ["금지조건"]))
    미답 = 진([_규범("미금", "금지", verified=False, 조건=조건금지["조건"], 결과={"문장": "미검증 금지"}), 조건금지], 해당없음)
    t.append(("3c 미검증 금지는 답으로 옮기지 않는다", any(c["문장"] == "미검증 금지" and c["구분"] == "미확정규범" for c in 미답["조건"]) and list(미답["진술평가"]) == ["금지2"]))
    예외금지 = _규범("예외금지", "금지", 예외=["규범:x"], 조건=조건금지["조건"], 결과={"문장": "예외 있는 금지"})
    t.append(("3c 예외 붙은 금지: 해당 없음 → 확인한 조건 · 해당 → 금지 칸(불가 아님)", [c["구분"] for c in 진([예외금지], 해당없음)["조건"]] == ["답변확인"]
              and 진([예외금지], "주관기관소속")["종류"] is None))
    # 리뷰 3c R1: 예외 · 묻지 않은 자연어 조건이 남은 금지는 「해당」 답이어도 「이 기준에 해당해요」 로 단정하지 않는다(5-6 ③ 예외 없음)
    자연어금지 = _규범("자연어금지", "금지", 조건={"구조화": 조건금지["조건"]["구조화"], "자연어": ["재직 중일 때"]}, 결과={"문장": "재직 기업에 집행할 수 없어요"})
    예외해당, 자연어해당 = 진([예외금지], "주관기관소속"), 진([자연어금지], "주관기관소속")
    t.append(("3c 예외 · 자연어 조건 붙은 금지의 해당: 답하기 전과 같은 금지 문장 · 진술 표시 없음 · 진술평가는 남김(B4 가 예외를 함께 줌)",
              [(c["문장"], c["구분"], c.get("진술")) for c in 예외해당["조건"]] == [("예외 있는 금지", "금지조건", None)]
              and [(c["문장"], c.get("진술")) for c in 자연어해당["조건"]] == [("재직 기업에 집행할 수 없어요", None)]
              and 예외해당["진술평가"] == {"예외금지": True} and 자연어해당["진술평가"] == {"자연어금지": True} and 예외해당["상한"] == "조건부"
              and len(화면_조건_나누기(화면_조건(진([{**자금금지, "예외": ["규범:x"]}], "배우자")))["금지조건"]) == 1))
    # 연산: 이상 · 미만 · 이하 · 이전 · 이후
    def 평(항목, 연산, 값, **입력):
        return _조건_평가({"항목": 항목, "연산": 연산, "값": 값}, 입력)
    t.append(("3c 연산 금액 이상: 부가세 두 기준이 갈리면 모름 · 같으면 판정", 평("금액", "이상", 1000000, 금액=1000000, 부가세구분="포함") is None
              and 평("금액", "이상", 1000000, 금액=1000000, 부가세구분="공급가액") is True and 평("금액", "이상", "100만원", 금액=1200000, 부가세구분="포함") is True
              and 평("금액", "이상", 1000000, 금액=500000) is False))
    t.append(("3c 연산 미만 · 이하 · 초과", 평("금액", "미만", 1000000, 금액=500000, 부가세구분="포함") is True and 평("견적서수령업체수", "이하", 2, 견적서수령업체수=2) is True
              and 평("견적서수령업체수", "이하", 2, 견적서수령업체수=3) is False and 평("선금지급횟수", "초과", "2회", 선금지급횟수="2회") is False))
    t.append(("3c 연산 단위가 다르거나 없으면 모름", 평("지체기간", "이상", "2주", 지체기간=3) is None and 평("지체기간", "이상", "2주", 지체기간="3주") is True
              and 평("지체기간", "이상", "2주", 지체기간="15일") is None))
    t.append(("3c 연산 이전 · 이후는 그날 포함 · 날짜가 아니면 모름", 평("출원일", "이후", "2026-01-01", 출원일="2026-01-01") is True
              and 평("출원완료일", "이전", "2025-12-31", 출원완료일="2026-01-02") is False and 평("출원일", "이후", "협약 이후", 출원일="2026-01-01") is None))
    금액금지 = _규범("금액금지", "금지", 조건={"구조화": [{"항목": "금액", "연산": "미만", "값": 10000}], "자연어": []}, 결과={"문장": "1만원 미만은 집행할 수 없습니다"})
    t.append(("3c 금액 조건 금지: 입력 금액으로 확인되면 발동 · 아니면 미발동", 게이트([금액금지], {"금액": 5000, "부가세구분": "포함"}, 확정=True)["종류"] == "금지"
              and 게이트([금액금지], {"금액": 20000, "부가세구분": "포함"}, 확정=True)["종류"] is None))
    # 수정 3c 4-1: 제재는 조건 문구에, 승인주체는 할 일 설명으로
    t.append(("3c 제재: 문장에 없는 말만 「(어기면 …)」", _제재_붙이기("서약서를 내세요.", "지원 제한 가능") == "서약서를 내세요. (어기면 지원 제한 가능)"
              and _제재_붙이기("내지 않으면 돌려줘야 합니다.", "환수") == "내지 않으면 돌려줘야 합니다."
              and _제재_붙이기("어기면 전액 환수됩니다.", "참여제한 1년 · 전액 환수") == "어기면 전액 환수됩니다. (어기면 참여제한 1년)"
              and _제재_붙이기("x를 하세요.", "미확인") == "x를 하세요."
              and _제재_붙이기("부적정으로 판정되어 다음 라운드 진출평가에 반영됩니다.", "적정성 검토 부적정 · 다음 라운드 진출평가 반영") == "부적정으로 판정되어 다음 라운드 진출평가에 반영됩니다."))
    # 리뷰 3c R6: 어길 수 없는 종류 · 근거 위치만 말하는 대체 문장에는 붙이지 않는다. 결과로 이미 말한 제재는 다시 붙이지 않는다. 문장 꼴 제재는 마침표로 가른다
    t.append(("3c 제재: 허용 · 대체 문장에는 안 붙음 · 결과로 말한 제재 안 붙음 · 문장 꼴은 마침표로 가름",
              _화면문장(_규범("허용제재", "허용", 결과={"문장": "정당한 사유로 포기하면 참여 제한 없이 끝낼 수 있습니다.", "제재": "잔액 환수"})) == "정당한 사유로 포기하면 참여 제한 없이 끝낼 수 있습니다."
              and _제재_붙이기("정당한 사유로 포기하면 남은 잔액만 돌려줍니다.", "잔액 환수") == "정당한 사유로 포기하면 남은 잔액만 돌려줍니다."
              and "어기면" not in _화면문장(_규범("대체제재", "요건", 근거=[{"칸": "대상", "doc_id": "책자", "조번호": "p5-1", "발췌": "선정 요건"}], 결과={"제재": "선정 취소"}))
              and _제재_붙이기("환수금을 내지 않으면 강제로 징수될 수 있습니다.", "국세강제징수의 예에 따른 징수") == "환수금을 내지 않으면 강제로 징수될 수 있습니다."
              and _제재_붙이기("관련 기준을 지키세요", "선정이 취소될 수 있습니다.") == "관련 기준을 지키세요. 선정이 취소될 수 있습니다."
              and _화면문장(_규범("의무제재", "의무", 결과={"문장": "서약서를 내세요.", "제재": "지원 제한 가능"})) == "서약서를 내세요. (어기면 지원 제한 가능)"))
    제재의무 = _규범("제재의무", "의무", 승인주체=["주체:사업운영위원회"], 결과={"문장": "보고서를 내세요.", "제재": "환수"})
    제재g = g([제재의무], 금액=1)
    t.append(("3c 게이트 조건 문장에 제재 · 조건에 승인주체가 실린다", [(c["문장"], c["승인주체"]) for c in 제재g["조건"]] == [("보고서를 내세요. (어기면 환수)", ["주체:사업운영위원회"])]))
    실패 = [x for x, ok in t if not ok]
    for x, ok in t:
        print("ok  " if ok else "FAIL", x)
    print(f"selftest: {len(t) - len(실패)}/{len(t)} 통과")
    return 1 if 실패 else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_자체시험())
    print(__doc__)
