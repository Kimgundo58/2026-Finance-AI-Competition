# -*- coding: utf-8 -*-
"""POST /api/judge — 정규화된 지출 하나를 판정한다(가능·조건부·불가·판단불가). SSE.

orchestrate.판정() 을 워커 스레드에서 실행하고 judge_response 로 응답 계약에 맞춘 뒤, plan_id 가 있으면
persist.판정_저장() 으로 계획에 연결한다. 응답은 response_cache 에 캐시한다.
질문세트(POST /api/questions 의 키)와 답변이 오면 서버에 저장한 질문 문장과 답 값으로 사용자답변을 만들어 판정에 넣는다.
이벤트 순서: 진행*3 → 판정 → 해야할일 → 인용 → 전제 → 참조관계 → (문의초안) → 결과 → 저장 → 완료.
이벤트 이름과 순서는 프론트 계약이다. 참조관계 이벤트 바로 뒤에 같은 내용을 이전 이벤트 이름으로 한 번
더 보낸다(한 릴리스 동안, judge_response 호환 블록). 예외·타임아웃·파이프라인 실패는 모두 판단불가로
반환하고, 그 결과는 캐시하지 않으며 계획에도 연결하지 않는다.
환경변수: SUDDOE_JUDGE_HEARTBEAT_SEC(12), SUDDOE_JUDGE_DEADLINE_SEC(180), SUDDOE_JUDGE_ANSWERS(1, 0 이면 확인 질문·답변 미반영).
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request

from server import auth, inquiry, response_cache
from server._common import MOCK, _정수_환경변수, _sse, _sse응답, 답변_반영, 클라이언트_ip, 비목_ENUM, 온톨로지_사업
from server.catalog import _비목_표준화, _사업명_표준화
from server.judge_response import (_사용자프로필값_구성, 구_참조관계_이벤트, 구_판정키_포함,
                                   파이프라인결과_매핑, 할일_공개)
from server.models import 판정요청
from server.persist import 판정_저장
from server.rate_limit import 제한
from server import mock_data
from server.selftest import mock_판정

_log = logging.getLogger("suddoe.judge")

router = APIRouter()

# 판정을 기다리는 동안 SSE 주석(: keep-alive)을 보내는 주기(초). 프록시 idle timeout 에 끊기지 않게 한다.
_하트비트_주기초 = _정수_환경변수("SUDDOE_JUDGE_HEARTBEAT_SEC", 12)
# 판정 한 건의 전체 타임아웃(초). 넘기면 워커를 기다리지 않고 판단불가로 반환한다.
_판정_타임아웃초 = _정수_환경변수("SUDDOE_JUDGE_DEADLINE_SEC", 180)


@router.post("/api/judge")
def judge(req: Request, body: 판정요청, mock: str | None = None,
          목: str | None = Query(None, include_in_schema=False)):  # 구 이름 호환(제거 예정: 다음 릴리스)
    """mock 모드에서는 ?mock=가능|조건부|불가|판단불가 로 네 판정의 고정 응답을 받는다.

    판단불가는 오류가 아니라 정상 응답이다. 저장은 부수 효과라 실패해도 스트림은 완료까지 간다.
    """
    mock = mock or 목  # 구 이름 호환(제거 예정: 다음 릴리스)
    ok, 사유 = 제한.통과(클라이언트_ip(req))
    if not ok:
        raise HTTPException(429, 사유)
    # 별칭을 먼저 표준 표기로 바꾼다 — 프론트 표기가 표준 표기와 다른 비목이 있다.
    body.확정비목 = _비목_표준화(body.확정비목)
    if body.확정비목 and body.확정비목 not in 비목_ENUM:
        raise HTTPException(422, f"비목은 다음 10종 중 하나여야 합니다: {비목_ENUM}")
    # 등록되지 않은 사업명을 통과시키면 rule_lookup 이 사업의 비목체계를 알 수 없어 룰 없이 판정한다.
    body.사업명 = _사업명_표준화(body.사업명)
    # 본문의 org_id 는 클라이언트가 보낸 검증되지 않은 값이라 버리고, 토큰으로 검증된 org 만
    # 쓴다(게스트는 None). 본문 값을 믿으면 남의 기관 L3·기업 프로필을 읽고 그 기관에 판정 행을 쓴다.
    body.org_id = auth.검증된_org(req)
    # 계정도 토큰에서만 정한다. 계획·판정·프로필·캐시는 계정 단위다(L3 는 org 단위).
    account_id = auth.검증된_account(req)
    # 저장 질문 목록이 판정 프롬프트 B6 에 들어가 확인 질문 답변과 섞이지 않게 뺀다. 표시 전용 값이라 키에서도 뺀다.
    body.정규화 = {k: v for k, v in body.정규화.items() if k != "심층질문"}
    반영 = 답변_반영()

    k = response_cache.캐시키(
        # 계정을 키에 넣는다. 다른 계정의 응답(그 계정의 계획·판정 id·프로필 전제)이 나가면 안 된다.
        "judge", body.org_id, account_id, body.사업명, body.확정비목,
        json.dumps(body.정규화, ensure_ascii=False, sort_keys=True),
        body.특수관계.친족거래, body.특수관계.전직임직원업체,
        json.dumps([a.model_dump() for a in body.답변], ensure_ascii=False, sort_keys=True),
        # 답변을 판정에 넣지 않는 동안(SUDDOE_JUDGE_ANSWERS=0)에는 답변을 넣은 옛 결과가 적중하지 않게 한다.
        body.질문세트 if 반영 else None,
        # L3 를 새로 올리거나 파싱이 끝나면 키가 바뀐다. 파싱 커밋 직전에 시작한 판정이 옛 L3 결과를
        # 캐시에 남겨도 이후 요청에는 적중하지 않는다(routes_l3 의 캐시 삭제와 함께 쓴다).
        response_cache.l3_해시(body.org_id),
        # 협약 프로필(B5)을 PUT /api/profile 로 바꾸면 키가 바뀐다(routes_profile 의 캐시 삭제와 함께 쓴다).
        response_cache.프로필_해시(body.org_id, account_id),
        # 온톨로지 경로의 입력. 값이 달라지면 코드 판정이 달라진다.
        body.확정세목, json.dumps(body.추가입력값, ensure_ascii=False, sort_keys=True), body.부가세구분,
        # 사용일자는 새 경로의 지난 기한 문장에만 쓴다. 옛 경로는 날짜가 달라도 같은 결과라 키를 나누지 않는다.
        body.사용일자 if 온톨로지_사업(body.사업명) else None,
        # ?mock= 은 mock 모드에서만 응답을 바꾼다. live 에서 키에 넣으면 캐시를 우회할 수 있다.
        *([mock] if MOCK else []))
    캐시 = response_cache.조회(k, body.org_id, account_id)
    # 검색 전에 계산한다. 판정 도중 규정이 활성화되면 결과는 옛 해시로 저장되어 이후 요청에 적중하지 않는다.
    데이터버전해시 = response_cache.요청_해시()
    # 미들웨어 컨텍스트(RLS GUC)가 있는 여기서 읽는다. 저장 질문을 읽지 못하면 판정에서 「가능」을 내지 않는다.
    사용자답변, 답변유실, 부가세답 = (_사용자답변_읽기(body, account_id)
                                if 반영 and 캐시 is None and not MOCK else (None, False, None))
    if body.부가세구분 in (None, "모름") and 부가세답:      # 입력에서 모름이면 코드가 만든 확인 질문의 답을 쓴다
        body.부가세구분 = 부가세답

    def gen():
        for 단계, 설명 in (("검색", "관련 조항을 찾는 중"),
                          ("룰조회", "비목별 한도·증빙을 확인하는 중"),
                          ("판정생성", "판정을 작성하는 중")):
            yield _sse("진행", {"단계": 단계, "설명": 설명})
        실패 = False
        if 캐시 is not None:
            # 캐시 값에는 decision_id 가 없다(저장 전에 뺀다). 남의 판정 행을 이 계획에 붙이지 않는다.
            out = {k: v for k, v in 캐시.items() if k != "decision_id"}
            decision_id = None
        elif MOCK:
            out = {"사용자답변": [], "기준문서": mock_data.mock_기준문서(),
                   **mock_판정.get(mock or "가능", mock_판정["가능"])}
            decision_id = None
            response_cache.저장(k, out, 종류="judge", org=body.org_id, account=account_id,
                               데이터버전_해시=데이터버전해시)
        else:
            out, 실패 = yield from _판정_실행(body, account_id, 사용자답변, 답변유실)
            # decision_id 를 캐시에 넣으면 다른 요청이 남의 판정 행을 자기 계획에 연결한다.
            decision_id = out.pop("decision_id", None)
            if not 실패:              # 일시적 장애의 판단불가를 캐시에 고정하지 않는다
                response_cache.저장(k, out, 종류="judge", org=body.org_id, account=account_id,
                                   데이터버전_해시=데이터버전해시)
        yield from _판정_이벤트(out)
        yield _sse("저장", _판정_저장_시도(body, account_id, out, decision_id,
                                          캐시적중=캐시 is not None, 실패=실패))
        yield _sse("완료", {"캐시": 캐시 is not None})

    return _sse응답(gen())


def _사용자답변_읽기(body: 판정요청, account_id: str | None) -> tuple[list[dict] | None, bool, str | None]:
    """저장한 질문세트와 화면 답변으로 (사용자답변, 답변유실, 부가세 답) 을 만든다. 프론트가 보낸 글은 쓰지 않는다.

    질문세트가 왔는데 저장값을 못 읽으면(만료·재시작·다른 계정 키·DB 오류) 답변유실이다. 사용자가
    「아니오」라고 답했을 수 있으므로 판정이 「가능」을 내지 못하게 한다. 화면에는 오류를 내지 않는다.
    """
    if not body.질문세트:
        return None, False, None
    try:
        저장값 = response_cache.질문세트_조회(body.질문세트, body.org_id, account_id)
        if isinstance(저장값, dict) and isinstance(저장값.get("질문"), list):
            import question_generate
            답변들 = [a.model_dump() for a in body.답변]
            return question_generate.답변_구성(저장값, 답변들) or None, False, question_generate.부가세_답(저장값, 답변들)
    except Exception as e:                                    # noqa: BLE001
        _log.warning("질문세트 읽기 실패: %s", type(e).__name__)
    return None, True, None


def _판정_이벤트(out: dict):
    """판정 → 해야할일 → 인용 → 전제 → 참조관계 → (문의초안) → 결과 이벤트를 고정 순서로 yield 한다.

    이름을 바꾼 응답 키는 한 릴리스 동안 이전 이름으로도 보낸다. 사본에만 덧붙이므로 저장·캐시에
    쓰는 out 은 새 키만 가진다.
    """
    out = 구_판정키_포함(dict(out))  # 구 이름 호환(제거 예정: 다음 릴리스)
    out["해야할일"] = 할일_공개(out.get("해야할일"))
    판정값 = {키: out.get(키) for 키 in ("판정", "요약", "신뢰등급", "규정버전")}
    판정값["버전스탬프"] = out.get("규정버전")  # 구 이름 호환(제거 예정: 다음 릴리스)
    yield _sse("판정", 판정값)
    yield _sse("해야할일", out.get("해야할일", []))
    yield _sse("인용", out.get("인용", []))
    yield _sse("전제", out.get("전제", []))
    yield _sse("참조관계", out.get("참조관계", []))
    yield _sse(구_참조관계_이벤트, out.get("참조관계", []))  # 구 이름 호환(제거 예정: 다음 릴리스)
    if out.get("판정") == "판단불가" and out.get("문의초안"):
        yield _sse("문의초안", out["문의초안"])
    # 증빙목록은 persist 가 plan_tasks 에 넣는 내부 키라 결과 이벤트에서만 뺀다(저장에는 out 을 그대로 쓴다).
    yield _sse("결과", {k: v for k, v in out.items() if k != "증빙목록"})


def _판정_실행(body: 판정요청, account_id: str | None, 사용자답변: list[dict] | None = None,
            답변유실: bool = False):
    """_live_판정 을 워커 스레드에서 실행하고, 기다리는 동안 SSE keep-alive 주석을 yield 한다.

    `out, 실패 = yield from _판정_실행(body, account_id, 사용자답변, 답변유실)` 로 쓴다. 예외나 타임아웃은 판단불가 결과와
    실패=True 를 돌려준다. 타임아웃이 지난 워커는 기다리지 않고 백그라운드에서 끝나게 둔다
    (판정 파이프라인은 그대로 decisions 행을 기록한다).
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    # submit 은 컨텍스트 변수를 복사하지 않는다. 미들웨어가 설정한 현재_org 를 워커에 넘긴다.
    future = ex.submit(contextvars.copy_context().run, _live_판정, body, account_id, 사용자답변, 답변유실)
    만료시각 = time.monotonic() + _판정_타임아웃초
    try:
        while True:
            남은 = 만료시각 - time.monotonic()
            if 남은 <= 0:
                _log.error("judge 타임아웃(%s초) — 판단불가로 반환 (plan_id=%s)",
                           _판정_타임아웃초, body.plan_id)
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
    """예외·타임아웃 때의 판단불가 결과. 이 요청의 정규화 값으로만 문의 초안을 만든다."""
    out = {"판정": "판단불가",
           "요약": "일시적인 오류로 판정하지 못했습니다. 잠시 후 다시 시도해 주세요.",
           "해야할일": [], "인용": [], "전제": [], "신뢰등급": None, "규정버전": None,
           "참조관계": [], "사용자답변": []}
    try:
        초안 = inquiry.문의초안(body.정규화, "판단불가", [], [], body.사업명)
    except Exception:                                         # noqa: BLE001
        # 실패 경로의 실패다. 초안 없이라도 판단불가 결과는 반드시 돌려준다.
        _log.exception("실패 결과의 문의 초안을 만들지 못했다 (plan_id=%s)", body.plan_id)
        초안 = None
    if 초안:
        out["문의초안"] = 초안
    return out


def _live_판정(body: 판정요청, account_id: str | None = None, 사용자답변: list[dict] | None = None,
             답변유실: bool = False) -> dict:
    """orchestrate.판정() 을 호출하고 응답 계약으로 바꾼다. 워커 스레드에서 실행된다."""
    import orchestrate
    # 질문 원문이 없을 때만 품목·용도·금액으로 문장을 재구성한다. 합성 문장(_합성_질문)을 다시
    # 정규화에 넣지 않는다. 필드→문장→필드 왕복은 사용자가 확정한 값을 잃는다.
    # 저장된 계획의 정규화 값에는 이전 키로 원문이 들어 있을 수 있어 그 키도 읽는다.
    질문 = (body.정규화.get("질문원문") or body.정규화.get("질문")
          or body.정규화.get("_원문") or "")  # 구 이름 호환(제거 예정: 다음 릴리스)
    if not 질문:
        조각 = [str(body.정규화.get(k)) for k in ("품목", "용도") if body.정규화.get(k)]
        금액 = body.정규화.get("금액")
        질문 = " ".join(조각) + (f" {금액}원" if 금액 else "")
    # 정규화결과를 넘겨 정규화 LLM 호출을 다시 하지 않는다. 확정비목이 있으면 그 비목으로 판정한다.
    # plan_id 를 넘기면 decisions 행이 처음부터 plan_id 를 갖는다.
    r = orchestrate.판정(질문, 사업명=body.사업명, org_id=body.org_id, account_id=account_id,
                       plan_id=body.plan_id,
                       확정비목=body.확정비목 or None,
                       정규화결과=body.정규화,
                       사용자프로필값=_사용자프로필값_구성(body.답변),
                       사용자답변=사용자답변, 답변유실=답변유실,
                       확정세목=body.확정세목, 추가입력값=body.추가입력값, 부가세구분=body.부가세구분, 사용일자=body.사용일자)
    return 파이프라인결과_매핑(r, body)


def _판정_저장_시도(body: 판정요청, account_id: str | None, out: dict, decision_id: int | None, *,
                   캐시적중: bool, 실패: bool) -> dict:
    """plan_id 가 있으면 판정을 계획에 연결한다. 저장 오류는 SSE 를 끊지 않고 사유로 돌려준다."""
    if body.plan_id is None:
        return {"저장": False, "사유": "plan_id 없음"}
    try:
        return 판정_저장(body.plan_id, body, out, body.org_id, decision_id=decision_id,
                     account_id=account_id, 캐시적중=캐시적중, 실패=실패)
    except Exception as e:                                    # noqa: BLE001
        return {"저장": False, "사유": f"저장 실패 ({type(e).__name__})"}
