# -*- coding: utf-8 -*-
"""POST /api/judge — 정규화된 지출 하나를 판정한다(가능·조건부·불가·판단불가). SSE.

orchestrate.판정() 을 워커 스레드에서 실행하고 judge_response 로 응답 계약에 맞춘 뒤, plan_id 가 있으면
persist.판정_저장() 으로 계획에 연결한다. 응답은 response_cache 에 캐시한다.
이벤트 순서: 진행*3 → 판정 → 해야할일 → 인용 → 전제 → 참조사슬 → (문의초안) → 결과 → 저장 → 완료.
이벤트 이름과 순서는 프론트 계약이다. 예외·기한 초과·파이프라인 실패는 모두 판단불가로 반환하고,
그 결과는 캐시하지 않으며 계획에도 연결하지 않는다. 환경변수: SUDDOE_JUDGE_HEARTBEAT_SEC(12), SUDDOE_JUDGE_DEADLINE_SEC(180).
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from server import auth, inquiry, response_cache
from server._common import MOCK, _int환경, _sse, _sse응답, 클라이언트_ip, 비목_ENUM
from server.catalog import _비목_표준화, _사업명_표준화
from server.judge_response import _사용자F값_조립, 파이프라인결과_매핑
from server.models import 판정요청
from server.persist import 판정_저장
from server.rate_limit import 제한
from server.selftest import 목_판정

_log = logging.getLogger("suddoe.judge")

router = APIRouter()

# 판정을 기다리는 동안 SSE 주석(: keep-alive)을 보내는 주기(초). 프록시 idle timeout 에 끊기지 않게 한다.
_하트비트_주기초 = _int환경("SUDDOE_JUDGE_HEARTBEAT_SEC", 12)
# 판정 한 건의 전체 기한(초). 넘기면 워커를 기다리지 않고 판단불가로 반환한다.
_판정_기한초 = _int환경("SUDDOE_JUDGE_DEADLINE_SEC", 180)


@router.post("/api/judge")
def judge(req: Request, body: 판정요청, 목: str | None = None):
    """mock 모드에서는 ?목=가능|조건부|불가|판단불가 로 네 판정의 고정 응답을 받는다.

    판단불가는 오류가 아니라 정상 응답이다. 저장은 부수 효과라 실패해도 스트림은 완료까지 간다.
    """
    ok, 사유 = 제한.통과(클라이언트_ip(req))
    if not ok:
        raise HTTPException(429, 사유)
    # 별칭을 먼저 표준 표기로 바꾼다 — 프론트 표기가 표준 표기와 다른 비목이 있다.
    body.확정비목 = _비목_표준화(body.확정비목)
    if body.확정비목 and body.확정비목 not in 비목_ENUM:
        raise HTTPException(422, f"비목은 enum 10종 뿐입니다: {비목_ENUM}")
    # 모르는 사업명을 통과시키면 비목계통() 이 '창업' 으로 잘못 분류한다.
    body.사업명 = _사업명_표준화(body.사업명)
    # 본문의 org_id 는 클라이언트가 보낸 검증되지 않은 값이라 버리고, 토큰으로 검증된 org 만
    # 쓴다(게스트는 None). 본문 값을 믿으면 남의 기관 L3·F 프로필을 읽고 그 기관에 판정 행을 쓴다.
    body.org_id = auth.검증된_org(req)

    k = response_cache.캐시키(
        "judge", body.org_id, body.사업명, body.확정비목,
        json.dumps(body.정규화, ensure_ascii=False, sort_keys=True),
        body.f5.친족거래, body.f5.전직임직원업체,
        json.dumps([a.model_dump() for a in body.답변], ensure_ascii=False, sort_keys=True),
        # ?목= 은 mock 모드에서만 응답을 바꾼다. live 에서 키에 넣으면 캐시를 우회할 수 있다.
        *([목] if MOCK else []))
    캐시 = response_cache.꺼내기(k, body.org_id)

    def gen():
        for 단계, 설명 in (("검색", "관련 조항을 찾는 중"),
                          ("룰조회", "비목별 한도·증빙을 확인하는 중"),
                          ("조립", "판정을 작성하는 중")):
            yield _sse("진행", {"단계": 단계, "설명": 설명})
        실패 = False
        if 캐시 is not None:
            # 캐시 값에는 decision_id 가 없다(넣기 전에 뺀다). 남의 판정 행을 이 계획에 붙이지 않는다.
            out = {k: v for k, v in 캐시.items() if k != "decision_id"}
            decision_id = None
        elif MOCK:
            out = dict(목_판정.get(목 or "가능", 목_판정["가능"]))
            decision_id = None
            response_cache.넣기(k, out, 종류="judge", org=body.org_id)
        else:
            out, 실패 = yield from _판정_실행(body)
            # decision_id 를 캐시에 넣으면 다른 요청이 남의 판정 행을 자기 계획에 연결한다.
            decision_id = out.pop("decision_id", None)
            if not 실패:              # 일시적 장애의 판단불가를 캐시에 고정하지 않는다
                response_cache.넣기(k, out, 종류="judge", org=body.org_id)
        yield from _판정_이벤트(out)
        yield _sse("저장", _판정_저장_시도(body, out, decision_id,
                                          캐시적중=캐시 is not None, 실패=실패))
        yield _sse("완료", {"캐시": 캐시 is not None})

    return _sse응답(gen())


def _판정_이벤트(out: dict):
    """판정 → 해야할일 → 인용 → 전제 → 참조사슬 → (문의초안) → 결과 이벤트를 고정 순서로 yield 한다."""
    yield _sse("판정", {키: out.get(키) for 키 in ("판정", "요약", "신뢰등급", "버전스탬프")})
    yield _sse("해야할일", out.get("해야할일", []))
    yield _sse("인용", out.get("인용", []))
    yield _sse("전제", out.get("전제", []))
    yield _sse("참조사슬", out.get("참조사슬", []))
    if out.get("판정") == "판단불가" and out.get("문의초안"):
        yield _sse("문의초안", out["문의초안"])
    # 증빙목록은 persist 가 plan_tasks 에 넣는 내부 키라 결과 이벤트에서만 뺀다(저장에는 out 을 그대로 쓴다).
    yield _sse("결과", {k: v for k, v in out.items() if k != "증빙목록"})


def _판정_실행(body: 판정요청):
    """_실_판정 을 워커 스레드에서 실행하고, 기다리는 동안 SSE keep-alive 주석을 yield 한다.

    `out, 실패 = yield from _판정_실행(body)` 로 쓴다. 예외나 기한 초과는 판단불가 결과와
    실패=True 를 돌려준다. 기한을 넘긴 워커는 기다리지 않고 백그라운드에서 끝나게 둔다
    (판정 파이프라인은 그대로 decisions 행을 기록한다).
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    # submit 은 컨텍스트 변수를 복사하지 않는다. 미들웨어가 설정한 현재_org 를 워커에 넘긴다.
    future = ex.submit(contextvars.copy_context().run, _실_판정, body)
    기한 = time.monotonic() + _판정_기한초
    try:
        while True:
            남은 = 기한 - time.monotonic()
            if 남은 <= 0:
                _log.error("judge 기한 초과(%s초) — 판단불가로 반환 (plan_id=%s)",
                           _판정_기한초, body.plan_id)
                ex.shutdown(wait=False, cancel_futures=True)
                return _실패_결과(body), True
            완료, _ = concurrent.futures.wait([future], timeout=min(_하트비트_주기초, 남은))
            if not 완료:
                yield ": keep-alive\n\n"
                continue
            try:
                out = future.result()
            except Exception:                                 # noqa: BLE001
                # 실패해도 네 판정 밖으로 나가지 않는다. 원인은 서버 로그에만 남긴다.
                _log.exception("judge 실패 — 판단불가로 반환 (plan_id=%s)", body.plan_id)
                return _실패_결과(body), True
            return out, bool(out.pop("_실패", False))
    finally:
        ex.shutdown(wait=False)


def _실패_결과(body: 판정요청) -> dict:
    """예외·기한 초과 때의 판단불가 결과. 이 요청의 정규화 값으로만 문의 초안을 만든다."""
    out = {"판정": "판단불가",
           "요약": "일시적인 오류로 판정하지 못했습니다. 잠시 후 다시 시도해 주세요.",
           "해야할일": [], "인용": [], "전제": [], "신뢰등급": None, "버전스탬프": None,
           "참조사슬": []}
    초안 = inquiry.문의초안(body.정규화, "판단불가", [], [], body.사업명)
    if 초안:
        out["문의초안"] = 초안
    return out


def _실_판정(body: 판정요청) -> dict:
    """orchestrate.판정() 을 호출하고 응답 계약으로 바꾼다. 워커 스레드에서 실행된다."""
    import orchestrate
    # 질문 원문이 없을 때만 품목·용도·금액으로 문장을 재구성한다. 합성 문장(_합성_질문)을 다시
    # 정규화에 넣지 않는다 — 필드→문장→필드 왕복은 사용자가 확정한 값을 잃는다.
    질문 = (body.정규화.get("_원문") or body.정규화.get("질문")
          or body.정규화.get("질문원문") or "")
    if not 질문:
        조각 = [str(body.정규화.get(k)) for k in ("품목", "용도") if body.정규화.get(k)]
        금액 = body.정규화.get("금액")
        질문 = " ".join(조각) + (f" {금액}원" if 금액 else "")
    # 정규화결과를 넘겨 정규화 LLM 호출을 다시 하지 않는다. 확정비목이 있으면 그 비목으로 판정한다.
    # plan_id 를 넘기면 decisions 행이 처음부터 plan_id 를 갖는다.
    r = orchestrate.판정(질문, 사업명=body.사업명, org_id=body.org_id, plan_id=body.plan_id,
                       _비목고정=body.확정비목 or None,
                       정규화결과=body.정규화,
                       사용자F값=_사용자F값_조립(body.답변))
    return 파이프라인결과_매핑(r, body)


def _판정_저장_시도(body: 판정요청, out: dict, decision_id: int | None, *,
                   캐시적중: bool, 실패: bool) -> dict:
    """plan_id 가 있으면 판정을 계획에 연결한다. 저장 오류는 SSE 를 끊지 않고 사유로 돌려준다."""
    if body.plan_id is None:
        return {"저장": False, "사유": "plan_id 없음"}
    try:
        return 판정_저장(body.plan_id, body, out, body.org_id, decision_id=decision_id,
                     캐시적중=캐시적중, 실패=실패)
    except Exception as e:                                    # noqa: BLE001
        return {"저장": False, "사유": f"저장 실패 ({type(e).__name__})"}
