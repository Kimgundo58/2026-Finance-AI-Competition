# -*- coding: utf-8 -*-
"""판정 파이프라인 결과를 /api/judge 응답 계약으로 바꾼다.

routes_judge 가 orchestrate.판정() 의 결과를 파이프라인결과_매핑(r, body) 로 바꿔 SSE 로 내보낸다.
인용·요약 내용은 새로 만들지 않는다. 요약의 문장 ID 를 인용 목록의 조문 표기로 바꾸고, 해야할일에
check_items 의 구분·유형을 붙이고, 판단불가면 server/inquiry.py 로 문의 초안을 만든다(LLM 호출 없음).
_사용자프로필값_구성 은 심층질문 답을 orchestrate.판정(사용자프로필값=) 입력으로 바꾼다.
사용자답변 은 질문문·답만 내보낸다. 충족답·근거조문·할일은 서버 안에서만 쓴다(사용자답변_공개).
이름을 바꾼 응답 키를 이전 이름으로도 보내는 호환 처리는 파일 끝 블록(구_판정키_포함)에 모아 둔다.
"""
from __future__ import annotations

import re

from server import inquiry
from server._common import 구키_추가
from server.catalog import _비목_표준화, _심층질문마스터, _체크항목_표
from server.models import 판정요청


def 파이프라인결과_매핑(r: dict, body: 판정요청) -> dict:
    """orchestrate.판정() 결과 → 응답 dict.

    응답 계약 키 외에 내부 키 세 개를 함께 담는다. routes_judge 가 SSE 로 내보내기 전에 뺀다:
    증빙목록(persist 가 plan_tasks 에 넣는다), decision_id(계획 연결용), _실패(실패 경로 여부).
    """
    전제 = r.get("전제목록") or []
    # 정규화 후보에 없는 비목을 사용자가 고르면 그 비목으로 본 판정이 나온다. 그대로 내보내면
    # 시스템이 그 비목에 동의한 것으로 읽히므로 전제 맨 앞에 그 사실을 넣는다(새 SSE 이벤트는 만들지 않는다).
    후보 = [n for n in (_비목_표준화(c.get("비목")) if isinstance(c, dict) else None
                      for c in (body.정규화.get("비목후보") or [])) if n]
    if body.확정비목 and 후보 and body.확정비목 not in 후보:
        # 조사는 '로' 로 고정한다. 비목 이름이 모두 모음으로 끝난다.
        # 온톨로지 경로 화면에는 내부 단계 이름(정규화)을 쓰지 않는다. 옛 경로 문구는 그대로 둔다
        후보이름 = "추천" if r.get("경로") == "온톨로지" else "정규화가 제시한"
        전제 = [{"사실": f"비목을 「{body.확정비목}」로 보고 판정했습니다 — {후보이름} "
                      f"후보({' · '.join(후보)})에 없는 비목을 직접 고르셨습니다",
                "근거조항": None, "매핑": [], "미충족시": "판단불가"}, *전제]
    인용 = r.get("인용목록") or []
    # 판정 파이프라인이 문의 초안을 주면 그것을 쓰고, 없으면 이미 가진 값으로 작성한다.
    문의초안 = r.get("문의초안") or inquiry.문의초안(
        body.정규화 or {}, r.get("판정", "판단불가"), 인용, 전제, body.사업명)

    return {
        "판정": r.get("판정", "판단불가"),
        "요약": _요약_나열이면_대체(_요약_문장ID_치환(r.get("요약", ""), 인용),
                                 r.get("판정", ""), 온톨로지경로=r.get("경로") == "온톨로지"),
        "해야할일": _할일_보강(_할일_중복제거(r.get("해야할일", []))),
        "인용": 인용,
        "전제": 전제,
        "신뢰등급": r.get("신뢰등급"),
        "규정버전": r.get("규정버전"),
        "참조관계": r.get("참조관계", []),
        "증빙목록": r.get("증빙목록") or [],
        "사용자답변": 사용자답변_공개(r.get("사용자답변")),
        # 온톨로지 경로(옛 경로는 빈 값). 노드 ID · 규범 ID · 문장 번호를 넣지 않는다
        "분류": r.get("분류"), "근거경로": r.get("근거경로") or [], "코드판정": r.get("코드판정"),
        "조건": r.get("조건") or [], "기한조건": r.get("기한조건") or [], "금지조건": r.get("금지조건") or [],
        "해당시조건": r.get("해당시조건") or [], "공통사항": r.get("공통사항") or [],
        "용어풀이": r.get("용어풀이") or [], "문의처": r.get("문의처"),
        **({"문의초안": 문의초안} if 문의초안 else {}),
        **({"decision_id": r["decision_id"]} if r.get("decision_id") else {}),
        # 판정 파이프라인의 실패 경로(DB·LLM·검색 장애). 캐시 저장과 계획 연결을 막는다.
        "_실패": bool(r.get("실패경로")),
    }


# 요약 정리

_문장ID_패턴 = re.compile(r"S\d+(?!\d)")


def _요약_문장ID_치환(요약: str, 인용들: list) -> str:
    """요약 안의 문장 ID(S01 등)를 사람이 읽는 조문 표기로 바꾼다.

    문장 ID 는 프롬프트 안에서만 쓰는 인용 표기라 화면에 그대로 내보내지 않는다. 인용을 새로
    만들지 않고 인용 목록에서 그 문장 ID 가 가리키는 조의 이름으로만 바꾼다. 매칭되지 않는
    문장 ID 는 그대로 둔다.
    """
    if not 요약 or not 인용들:
        return 요약
    표: dict[str, str] = {}
    for c in 인용들:
        if not isinstance(c, dict):
            continue
        문장ID = c.get("문장ID")
        if not 문장ID:
            continue
        조 = " ".join(str(x) for x in (c.get("조번호"), c.get("조제목")) if x)
        항 = c.get("항호")
        이름 = (조 + (f" {항}" if 항 else "")).strip()
        if 이름:
            표[str(문장ID)] = 이름
    if not 표:
        return 요약
    # 뒤에 숫자가 더 붙지 않는 문장 ID 만 바꾼다. S1 을 바꾸면서 S10 을 깨뜨리지 않게 한다.
    return _문장ID_패턴.sub(lambda m: 표.get(m.group(0), m.group(0)), 요약)


# 요약이 문장이 아니라 인용 표기 나열뿐인지 판별한다. 인용 표기가 있다는 것만으로 판별하면
# "제38조에 따라 …필요합니다." 같은 정상 문장까지 잡히므로, 인용 표기가 있고 종결어미로
# 끝나지 않을 때만 나열로 본다. 인용 표기가 없으면 짧아도 사람이 읽을 수 있는 요약이다.
_인용표기_패턴 = re.compile(r"제\d+조|S\d{1,2}")
_문장끝 = re.compile(r"(?:습니다|합니다|됩니다|입니다|세요|해요|어요|죠)[.!?]?\s*$|[.!?]\s*$")


def _요약_나열이면_대체(요약: str, 판정: str, *, 온톨로지경로: bool = False) -> str:
    """요약이 인용 표기 나열뿐이면 판정별 정형 문구로 바꾼다. 근거는 인용란이 보여 준다."""
    if not 요약:
        return 요약
    if not _인용표기_패턴.search(요약) or _문장끝.search(요약):
        return 요약
    기본 = {
        "가능": "규정상 집행할 수 있습니다. 아래 근거 조항을 확인해 주세요.",
        # 온톨로지 경로의 조건부에는 채울 조건만 있는 것이 아니다(해당하면 안 되는 경우 · 해당할 때만 필요한 것)
        "조건부": "아래 항목을 확인한 뒤 집행하세요." if 온톨로지경로 else "조건을 채우면 집행할 수 있습니다. 아래 확인 항목을 먼저 챙겨 주세요.",
        "불가": "규정상 집행할 수 없습니다. 아래 근거 조항을 확인해 주세요.",
        "판단불가": "주어진 근거만으로는 판단할 수 없습니다. 담당자에게 확인해 주세요.",
    }
    return 기본.get(판정 or "", "아래 근거 조항과 확인 항목을 살펴봐 주세요.")


# 해야할일 정리

def _할일_중복제거(할일: list) -> list:
    """같은 (code, 항목, 설명) 항목을 순서를 지키며 한 번만 남긴다.

    같은 code 에 다른 항목이 오는 것은 정상이라 세 필드 전부를 키로 쓴다.
    """
    남김, 처리한_키 = [], set()
    for h in 할일 or []:
        if not isinstance(h, dict):
            continue
        k = (h.get("code"), h.get("항목"), h.get("설명"))
        if k in 처리한_키:
            continue
        처리한_키.add(k)
        남김.append(h)
    return 남김


def _할일_보강(할일: list) -> list:
    """code 로 check_items 를 찾아 구분·유형을 붙인다.

    판정 생성 스키마의 해야할일은 {code, 항목, 설명} 뿐이고 구분·유형의 기준값은 check_items 다.
    check_items 에 없는 code 거나 DB 를 못 읽으면 붙이지 않고 그대로 둔다.
    """
    표 = _체크항목_표()
    if not 표:
        return 할일
    보강 = []
    for h in 할일 or []:
        m = 표.get(h.get("code")) if isinstance(h, dict) else None
        보강.append({**h, "구분": m[0], "유형": m[1]} if m else h)
    return 보강


def 할일_공개(할일: list | None) -> list:
    """해야할일 항목에서 내부 키 코드추정_제외 를 뺀다. 저장(persist)에는 원래 값을 쓰고 화면으로 나갈 때만 부른다."""
    return [{k: v for k, v in h.items() if k != "코드추정_제외"} if isinstance(h, dict) else h
            for h in 할일 or []]


def 사용자답변_공개(사용자답변: list | None) -> list[dict]:
    """화면으로 나가는 값만 남긴다. 판정 응답과 GET /api/plans/{id} 가 같이 쓴다."""
    return [{"질문문": a.get("질문문"), "답": a.get("답")}
            for a in 사용자답변 or [] if isinstance(a, dict)]


# 심층질문 답

def _사용자프로필값_구성(답변: list) -> dict | None:
    """심층질문 답 → orchestrate.판정(사용자프로필값=) 입력 {컬럼명: 값}. 답이 없으면 None.

    프로필 경로 접두사를 붙인 경로 키 변환은 orchestrate.프로필값_경로키() 가 한 번만 한다.
    check_items 에 없는 code 의 답은 버린다.
    """
    if not 답변:
        return None
    표 = {q["code"]: q.get("필요프로필필드") or [] for q in _심층질문마스터()}
    out: dict = {}
    for a in 답변:
        for 컬럼 in 표.get(a.code, []):
            out[컬럼] = a.값
    return out or None


# ── 구 이름 호환(제거 예정: 다음 릴리스) ─────────────────────────────
# 응답 키 이름을 바꾼 뒤 한 릴리스 동안 이전 이름도 같이 보낸다. 프론트 전환이 끝나면 이 블록과
# 호출부(routes_judge·routes_plans)의 호환 줄을 함께 지운다. 표는 {새 키: 이전 키} 다.
_구_판정키 = {"규정버전": "버전스탬프", "참조관계": "참조사슬", "검증사유": "강등사유"}  # 구 이름 호환(제거 예정: 다음 릴리스)
_구_인용키 = {"문장ID": "s번호"}  # 구 이름 호환(제거 예정: 다음 릴리스)
_구_참조관계키 = {"참조문자열": "표기", "해결근거": "보정"}  # 구 이름 호환(제거 예정: 다음 릴리스)
구_참조관계_이벤트 = "참조사슬"  # 구 이름 호환(제거 예정: 다음 릴리스). SSE 이벤트 이름


def 구_판정키_포함(out: dict) -> dict:
    """판정 응답 dict 에 이전 키 이름을 덧붙인다. 새 키와 값은 그대로 두고 out 을 돌려준다.

    인용·참조관계 항목 안의 키를 먼저 덧붙인 뒤 최상위 키를 복사한다. 항목 dict 는 새로 만들어
    원본 목록을 바꾸지 않는다.
    """
    if isinstance(out.get("인용"), list):
        out["인용"] = [구키_추가(dict(c), _구_인용키) if isinstance(c, dict) else c
                      for c in out["인용"]]
    if isinstance(out.get("참조관계"), list):
        out["참조관계"] = [구키_추가(dict(x), _구_참조관계키) if isinstance(x, dict) else x
                        for x in out["참조관계"]]
    return 구키_추가(out, _구_판정키)
