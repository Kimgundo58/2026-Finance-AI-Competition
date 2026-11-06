# -*- coding: utf-8 -*-
"""POST /api/questions — 판정 전에 사용자에게 물을 확인 질문을 규정 원문에서 만든다.

question_generate.질문생성() 을 부르고 질문_공개() 로 화면 값만 내보낸다. 충족답·할일·근거조문은 서버에 남긴다.
응답의 질문세트는 응답 캐시 키다. 화면이 이 값을 /api/judge 로 보내면 판정이 저장해 둔 질문으로 답을 맞춘다(24시간 보관).
어떤 실패도 오류로 보이지 않는다. DB·LLM 오류와 요청 한도는 200 과 상태 '실패' 로 답하고 화면은 바로 결과로 넘어간다.
비목이 10종 밖이거나 등록되지 않은 사업명만 422 다. 설계: docs/LLM_판정아키텍쳐/05_API.md.
로그에는 상태·질문 수·버린 사유·지연만 남긴다. 사용자 입력과 질문 문장은 남기지 않는다.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request

from server import auth, response_cache
from server._common import MOCK, 답변_반영, 비목_ENUM, 클라이언트_ip
from server.catalog import _비목_표준화, _사업명_표준화
from server.models import 질문요청
from server.rate_limit import 제한
from server.selftest import mock_질문

_log = logging.getLogger("uvicorn.error")

router = APIRouter()

# 질문 화면을 띄워 놓고 오래 있어도 답변이 사라지지 않게 일반 응답 캐시보다 길게 둔다.
_질문세트_TTL초 = 24 * 3600

_실패_응답 = {"상태": "실패", "질문세트": None, "질문": [], "추가입력": [], "용어풀이": []}
# LLM 이 실패해 코드 항목(추가 입력 · 부가세 질문)만 남은 결과. 일시적 장애를 오래 고정하지 않는다.
_임시_TTL초 = 1800


def _로그(상태: str, 질문수: int, 버린사유: list[str], 캐시: bool, t0: float) -> None:
    _log.info("확인 질문 상태=%s 질문수=%d 버린사유=%s 캐시=%s 지연ms=%d",
              상태, 질문수, 버린사유, 캐시, int((time.time() - t0) * 1000))


@router.post("/api/questions")
def questions(req: Request, body: 질문요청) -> dict:
    """응답: {상태: 있음|없음|바로판정|실패, 질문세트, 질문: [{id, 질문문, 이유, 선택지, 근거}], 추가입력, 용어풀이}."""
    t0 = time.time()
    body.확정비목 = _비목_표준화(body.확정비목)
    if body.확정비목 not in 비목_ENUM:
        raise HTTPException(422, f"비목은 다음 10종 중 하나여야 합니다: {비목_ENUM}")
    try:
        body.사업명 = _사업명_표준화(body.사업명)
    except HTTPException as e:
        if e.status_code < 500:
            raise
        _로그("실패", 0, ["사업목록"], False, t0)      # 사업 목록을 못 읽었다(DB 장애)
        return dict(_실패_응답)
    if MOCK:
        return mock_질문
    if not 답변_반영():                                    # 답을 쓰지 않을 때는 묻지 않는다
        return {"상태": "없음", "질문세트": None, "질문": [], "추가입력": [], "용어풀이": []}

    org, account = auth.검증된_org(req), auth.검증된_account(req)
    try:
        return _live_질문(body, org, account, 클라이언트_ip(req), t0)
    except Exception as e:                                    # noqa: BLE001
        _log.warning("확인 질문 실패: %s", type(e).__name__)
        _로그("실패", 0, ["예외"], False, t0)
        return dict(_실패_응답)


def _live_질문(body: 질문요청, org: str | None, account: str | None, ip: str, t0: float) -> dict:
    import question_generate

    # 캐시 행은 계정 단위라(RLS) 키에도 org·계정을 넣는다. L3 를 새로 올리면 키가 바뀐다.
    k = response_cache.캐시키("questions", org, account, body.사업명, body.확정비목, body.확정세목, body.부가세구분,
                            body.세부유형, body.품목, body.금액, body.용도, body.질문원문,
                            response_cache.l3_해시(org))

    def 응답(저장값: dict) -> dict:
        return {"상태": 저장값["상태"], "질문세트": k,
                "질문": [question_generate.질문_공개(q) for q in 저장값["질문"]],
                "추가입력": 저장값.get("추가입력") or [], "용어풀이": 저장값.get("용어풀이") or []}

    # 조회() 를 쓰지 않는다. 아직 카운트를 올리지 않았으므로 적중해도 되돌릴 것이 없다.
    저장값 = response_cache.질문세트_조회(k, org, account)
    if isinstance(저장값, dict):
        _로그(저장값["상태"], len(저장값["질문"]), [], True, t0)
        return 응답(저장값)

    ok, _사유 = 제한.통과(ip)
    if not ok:
        _로그("실패", 0, ["요청한도"], False, t0)      # 판정 요청이 자기 한도 오류를 낸다
        return dict(_실패_응답)

    데이터버전해시 = response_cache.요청_해시()
    r = question_generate.질문생성(
        {"사업명": body.사업명, "비목": body.확정비목, "세부유형": body.확정세목 or body.세부유형, "품목": body.품목,
         "용도": body.용도, "금액": body.금액, "질문": body.질문원문,
         "확정세목": body.확정세목, "부가세구분": body.부가세구분},
        org_id=org, account_id=account)
    메타 = r.get("메타") or {}
    if not 메타.get("LLM호출"):                         # LLM 을 부르지 않았으면 일일 카운트를 되돌린다
        제한.카운트_복원()
    # 버린질문의 키는 질문 문장이라 사유 코드만 남긴다
    버린사유 = sorted({사유 for 항목 in (메타.get("버린질문") or {}).values() for 사유 in 항목["사유"]})
    _로그(r["상태"], len(r["질문"]), 버린사유 or ([메타["사유"]] if 메타.get("사유") else []), False, t0)
    if r["상태"] == "실패":                             # 일시적 장애를 24시간 고정하지 않는다
        return dict(_실패_응답)
    저장값 = {"상태": r["상태"], "질문": r["질문"], "추가입력": r.get("추가입력") or [], "용어풀이": r.get("용어풀이") or []}
    저장됨 = response_cache.저장(k, 저장값, 종류="questions", org=org, account=account, 데이터버전_해시=데이터버전해시,
                              ttl초=_임시_TTL초 if 메타.get("사유") == "LLM실패" else _질문세트_TTL초)
    if not 저장됨 and 저장값["질문"]:                    # 답을 받아도 판정에서 질문을 찾지 못하니 묻지 않는다
        _로그("실패", 0, ["저장실패"], False, t0)
        return dict(_실패_응답)
    return 응답(저장값)
