# -*- coding: utf-8 -*-
"""사업·비목·체크항목 참조 데이터와 /api/vocab, /api/programs.

판정·정규화 라우트는 요청을 받는 시점에 _사업명_표준화()·_비목_표준화() 로 사용자 표기를 표준 표기로 바꾸고,
judge_response·routes_normalize 는 _체크항목_표()·_심층질문마스터() 로 해야할일·심층질문을 채운다.
읽는 표: corpus.programs, corpus.item_vocab, corpus.check_items, corpus.precedence_rules (org 경계 없는 공통 데이터).
표는 TTL 캐시한다(SUDDOE_PROGRAM_TTL·SUDDOE_CHECK_ITEM_TTL, 기본 300초). DB 를 못 읽은 빈 결과는 캐시하지 않는다.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException

from server._common import MOCK, _질의, ttl_캐시, 비목_ENUM, 창업활동비_사업
from server.models import 사업목록응답, 사업정보, 우선순위규칙

_log = logging.getLogger("suddoe.catalog")

router = APIRouter()


# 사업명 표준화
# 요청 입력(쿼리 파라미터·body.사업명)에서만 검사한다. 다운스트림 쿼리는 파라미터 바인딩이라
# 등록되지 않은 표기가 경고 없이 0행으로 끝나기 때문이다. 등록되지 않은 표기는 422 로 거부한다.
# 그대로 통과시키면 rule_lookup 이 사업의 비목체계를 알 수 없어 룰 없이 검색 근거만으로 판정한다.
# 연도 접두사·공백·가운뎃점(・→·) 차이만 흡수한다. 이름 자체가 다른 표기는
# corpus.programs.별칭 에 등록한다.

def _사업명_키(값: str) -> str:
    """비교 전용 키. 화면 표시에 쓰지 않는다."""
    s = re.sub(r"^\s*20\d{2}\s+", "", (값 or "").strip())   # '2026 ' 접두사
    return re.sub(r"\s+", "", s.replace("・", "·"))          # 공백 제거 · 가운뎃점 통일


@ttl_캐시("SUDDOE_PROGRAM_TTL", 300)
def _사업명_표() -> dict[str, str]:
    """비교 키 → 표준 사업명. DB 를 못 읽으면 빈 dict."""
    표: dict[str, str] = {}
    for 표준, 별칭들 in _질의('SELECT "사업명", "별칭" FROM corpus.programs WHERE "활성"'):
        for v in [표준, *(별칭들 or [])]:
            표[_사업명_키(v)] = 표준
    return 표


def _사업명_표준화(값: str | None) -> str | None:
    """표준 사업명을 돌려준다. 등록되지 않은 표기는 422, 사업 목록을 못 읽으면 503.

    mock 모드는 DB 가 없는 것이 정상이라 검사하지 않는다. live 모드에서 빈 표는
    '등록되지 않은 사업' 이 아니라 'DB 장애' 다(_질의 는 오류도 빈 결과로 돌려준다).
    """
    if not 값:
        return 값
    if MOCK:
        return 값
    표 = _사업명_표()
    if not 표:
        raise HTTPException(503, "사업 목록을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.")
    표준 = 표.get(_사업명_키(값))
    if 표준 is None:
        raise HTTPException(422, f"등록되지 않은 사업명입니다: {값!r}. "
                                 f"등록된 사업: {sorted(set(표.values()))}")
    return 표준


# 비목 표준화
# 프론트 표기는 corpus.item_vocab.별칭 에 등록돼 있다. 표준 표기가 원문과 다른 경우
# (예: 기계장치/기계장치비, 무형자산 취득비의 공백)도 별칭으로 흡수하고, 공백을 지운 키를
# 보조 조회 키로 함께 둔다.

def _비목_키(값: str) -> str:
    return re.sub(r"\s+", "", (값 or "").strip())


@ttl_캐시("SUDDOE_PROGRAM_TTL", 300)
def _비목_표() -> dict[str, str]:
    """표기 → 표준 비목. 원문 표기와 공백을 지운 키를 함께 담는다."""
    표: dict[str, str] = {}
    for 표준, 별칭들 in _질의(
            'SELECT "비목", "별칭" FROM corpus.item_vocab WHERE "비목체계"=\'창업\''):
        for v in [표준, *(별칭들 or [])]:
            표[v] = 표준
            표.setdefault(_비목_키(v), 표준)
    return 표


def _비목_표준화(값: str | None) -> str | None:
    """별칭·공백 차이를 흡수해 표준 비목으로 바꾼다. 모르면 원래 값을 그대로 돌려준다.

    여기서 422 를 내지 않는다 — 호출부의 비목_ENUM 검사가 허용 목록을 담은 메시지로 거부한다.
    DB 를 못 읽으면(mock 모드 포함) 표가 비어 원래 값이 그대로 간다.
    """
    if not 값:
        return 값
    표 = _비목_표()
    return 표.get(값) or 표.get(_비목_키(값)) or 값


# 체크항목·심층질문

@ttl_캐시("SUDDOE_CHECK_ITEM_TTL", 300, 구이름="SUDDOE_CHECKITEM_TTL")  # 구 이름 호환(제거 예정: 다음 릴리스)
def _체크항목_표() -> dict[str, tuple[str, str]]:
    """check_items.code → (구분, 유형). DB 를 못 읽으면 빈 dict."""
    return {c: (구분, 유형) for c, 구분, 유형 in
            _질의('SELECT "code", "구분", "유형" FROM corpus.check_items')}


@ttl_캐시("SUDDOE_CHECK_ITEM_TTL", 300, 구이름="SUDDOE_CHECKITEM_TTL")  # 구 이름 호환(제거 예정: 다음 릴리스)
def _심층질문마스터() -> list[dict]:
    """check_items 에서 질문문과 근거가 모두 있는 행을 심층질문 후보로 읽는다.

    근거가 없는 행은 내지 않는다. 조회가 실패하면(심층질문 컬럼이 없는 DB 의 UndefinedColumn 등)
    오류 로그를 남기고 빈 목록을 돌려준다 — 심층질문 없이도 판정은 진행된다.
    """
    # 컬럼 '질문유형' 은 할일.유형 과 겹치지 않게 지은 이름이다.
    try:
        행 = _질의('SELECT "code", "사업명", "비목", "질문문", "질문유형", "선택지", "근거", '
                  '"필요프로필필드" FROM corpus.check_items', 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        _log.error("심층질문 조회 실패 — 심층질문 없이 진행한다 (%s: %s)", type(e).__name__, e)
        return []
    표 = []
    for code, 사업명, 비목, 질문문, 유형, 선택지, 근거, 필요프로필필드 in 행:
        if not 질문문 or not 근거:
            continue
        근거들 = 근거 if isinstance(근거, list) else [근거]
        if not 근거들 or not isinstance(근거들[0], dict):
            continue
        표.append({
            "code": code, "사업명": 사업명, "비목": 비목,
            "질문문": 질문문, "유형": 유형 or "예아니오",
            "선택지": 선택지 or [],
            "근거": {"doc_id": 근거들[0].get("doc_id"), "조번호": 근거들[0].get("조번호")},
            "필요프로필필드": list(필요프로필필드 or []),
        })
    return 표


# 라우트

@router.get("/api/vocab")
def vocab(사업명: str | None = None) -> dict:
    """비목 목록과 별칭. 창업활동비는 예비창업패키지에만 있다."""
    사업명 = _사업명_표준화(사업명)          # 창업활동비_사업 비교가 표준 표기 기준이라 먼저 바꾼다
    목록 = [b for b in 비목_ENUM
            if b != "창업활동비" or 사업명 is None or 사업명 in 창업활동비_사업]
    별칭: dict[str, list[str]] = {}
    for r in _질의("SELECT \"비목\", \"별칭\" FROM corpus.item_vocab WHERE \"비목체계\"='창업'"):
        별칭[r[0]] = list(r[1] or [])
    return {"비목": 목록, "별칭": 별칭 or None, "사업명": 사업명,
            "비고": "프론트 라벨을 이 문자열 그대로 맞춘다. 별칭 매핑은 서버가 한다"}


def _우선순위규칙맵() -> dict[str, list[우선순위규칙]]:
    """corpus.precedence_rules 의 검수된 행 → {사업명: [우선순위규칙, ...]}."""
    행 = _질의('SELECT "사업명", "우선계층", "후순위계층", "범위", "우선규범", "해석" '
              'FROM corpus.precedence_rules WHERE "verified" ORDER BY prec_id')
    맵: dict[str, list[우선순위규칙]] = {}
    for 사업명, 우선, 후순위, 범위, 우선규범, 해석 in 행:
        맵.setdefault(사업명, []).append(
            우선순위규칙(우선계층=우선, 후순위계층=후순위, 범위=범위, 우선규범=우선규범, 해석=해석))
    return 맵


@router.get("/api/programs", response_model=사업목록응답)
def programs() -> 사업목록응답:
    """사업 목록과 사업별 L1/L2/L3 우선순위 규칙.

    룰 조회 키가 사업 × 비목이라 게스트에게도 사업을 묻는다. DB 를 못 읽으면 코드에 둔
    사업 이름 목록으로 답하고 비고에 그 사실을 적는다.
    """
    행 = _질의('SELECT "사업명", "별칭", "비목체계", "트랙범위" FROM corpus.programs '
              'WHERE "활성" ORDER BY "사업명"')
    우선순위맵 = _우선순위규칙맵()
    if 행:
        return 사업목록응답(사업=[
            사업정보(사업명=a, 별칭=list(b or []), 비목체계=c, 트랙범위=d,
                    우선순위규칙목록=우선순위맵.get(a, []))
            for a, b, c, d in 행])
    return 사업목록응답(
        사업=[사업정보(사업명=n, 별칭=[], 비목체계="창업", 트랙범위=None,
                    우선순위규칙목록=우선순위맵.get(n, [])) for n in (
            "예비창업패키지", "초기창업패키지", "재도전성공패키지", "창업도약패키지",
            "창업중심대학", "초격차 스타트업 프로젝트", "모두의 창업 프로젝트", "TIPS")],
        비고="corpus.programs 를 못 읽어 코드 상수로 답했다")
