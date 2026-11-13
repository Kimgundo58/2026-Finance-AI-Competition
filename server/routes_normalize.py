# -*- coding: utf-8 -*-
"""POST /api/normalize — ① 입력 정리. 지출 입력을 판정 입력(품목·금액·용도)으로 정리한다. SSE.

온톨로지 사업(재설계 11-11): 폼은 LLM 0회, 품목 별칭 일치만 `품목확인` 으로 알린다. 자연어는 질의변환 LLM 1회(분류 없음).
옛 경로(온톨로지 밖 사업): 자연어는 normalize_run.정규화(), 폼은 비목후보_추출() 이 비목후보를 채운다(LLM 1회).
고정 심층질문은 없앴다(빈 목록). 결과는 response_cache 에 캐시하되 LLM 오류로 나온 결과는 캐시하지 않는다.
이벤트 순서: 진행 → 필드* → 결과 → 완료 (실패 시 진행 → 오류 → 완료). 이벤트 이름과 순서는 프론트 계약이다.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from server import auth, response_cache, research_sessions, ontology_readiness
from server._common import MOCK, _sse, _sse응답, 비목_ENUM, 온톨로지_사업, 클라이언트_ip
from server.catalog import _사업_비목, _사업명_표준화
from server.models import 정규화요청
from server.rate_limit import 제한
from server import mock_flow

router = APIRouter()


@router.post("/api/normalize")
def normalize(req: Request, body: 정규화요청):
    """프론트가 결과만 쓰고 싶으면 결과 이벤트 하나만 읽어도 된다."""
    ok, 사유 = 제한.통과(클라이언트_ip(req))
    if not ok:
        raise HTTPException(429, 사유)

    # 캐시 키를 만들기 전에 표준화한다. 표기만 다른 같은 사업이 다른 키로 나뉘지 않게 한다.
    body.사업명 = _사업명_표준화(body.사업명)

    # 특수관계 값은 판정 후 폐기하는 값이라 키에 해시로만 들어간다. 추가설명은 용도에 합쳐져 LLM 입력이
    # 되므로 키에 넣는다. 거래처는 정규화에 쓰이지 않아 넣지 않는다. 집행예정일은 온톨로지 폼 경로 결과의 사용일자가 되므로 넣는다.
    org = auth.검증된_org(req)
    account = auth.검증된_account(req)
    # 세션을 못 만들어도 정규화는 한다. 조사ID 없이 답하면 화면은 질문 단계에서 새 조사를 연다(리뷰 28-7)
    try:
        조사ID = research_sessions.생성(org, account)
        조사세션 = research_sessions.조회(조사ID, org, account)
    except Exception:  # noqa: BLE001
        조사ID, 조사세션 = None, None
    # 캐시 행은 계정 단위라(RLS) 키에도 org·계정을 넣는다. 키가 같으면 다른 계정 행과 PRIMARY KEY 가 겹친다.
    k = response_cache.캐시키("normalize", org, account, (body.질문 or "").strip(),
                            body.품목, body.금액, body.용도, body.추가설명, body.사업명,
                            body.특수관계.친족거래, body.특수관계.전직임직원업체,
                            # 온톨로지 경로는 응답 모양이 달라 옛 경로의 저장값을 쓰지 않는다
                            body.부가세구분, 온톨로지_사업(body.사업명), body.집행예정일)
    캐시 = response_cache.조회(k, org, account)

    def gen():
        try:
            if not MOCK:
                ontology_readiness.확인(body.사업명)
        except Exception:  # noqa: BLE001
            yield _sse("오류", {"메시지": "선택한 사업의 규정을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요."})
            yield _sse("완료", {"실패": True})
            return
        if 캐시 is not None:
            yield _sse("진행", {"단계": "정규화", "상태": "완료", "문장": "저장된 입력 분석 결과를 불러왔어요.", "설명": "저장된 분석 결과 사용"})
            yield _sse("결과", {"거래처": None, **캐시, "조사ID": 조사ID})   # 거래처 칸 전에 저장한 값
            yield _sse("완료", {"캐시": True})
            return
        yield _sse("진행", {"단계": "정규화", "상태": "진행중", "문장": "입력에서 지출 정보를 추출하고 있어요." if not MOCK else "연결 확인용 분석 결과를 준비하고 있어요.", "설명": "입력 분석"})
        try:
            import research_runtime
            with research_runtime.activate(조사세션.budget if 조사세션 else research_runtime.Budget()):
                out, 실패 = (mock_flow.정규화(body), False) if MOCK else _live_정규화(body)
        except Exception as e:                                # noqa: BLE001
            # 500 을 내지 않고 오류 이벤트로 끝낸다.
            yield _sse("오류", {"메시지": "정규화에 실패했습니다", "종류": type(e).__name__})
            yield _sse("완료", {"실패": True})
            return
        out.setdefault("질문원문", _합성_질문(body))
        # 거래처: 한 문장에 적혀 있을 때만 값(스펙 8-1). live 문장 정리는 아직 뽑지 않아 null 이다. 폼 값은 화면이 이미 갖고 있다
        out.setdefault("거래처", None)
        if not 실패:                  # 일시적 LLM 오류로 빈 비목후보가 캐시에 남지 않게 한다
            response_cache.저장(k, out, 종류="normalize", org=org, account=account)
        for 필드 in ("품목", "금액", "금액_추정여부", "용도", "거래처", "비목후보", "분류후보", "수치", "품목확인"):
            if 필드 in out:
                yield _sse("필드", {필드: out[필드]})
        yield _sse("결과", {**out, "조사ID": 조사ID})
        yield _sse("완료", {"캐시": False})

    return _sse응답(gen())


def _live_정규화(body: 정규화요청) -> tuple[dict, bool]:
    """live 정규화. (결과, 실패) — 실패는 폼 경로 LLM 오류로 비목후보가 비었다는 뜻이다."""
    if 온톨로지_사업(body.사업명):
        결과 = _온톨로지_정규화(body)
        if 결과 is None:
            raise RuntimeError('선택한 사업의 규정을 확인할 수 없습니다')
        return 결과
    실패 = False
    # 사업에 없는 비목을 후보로 받으면 화면에 고를 칸이 없고, 심층질문도 그 비목 기준으로 걸러진다.
    비목들 = _사업_비목(body.사업명)
    if body.질문 and body.질문.strip():
        from normalize_run import 정규화
        out, _메타 = 정규화(body.질문, 비목목록=비목들)
    else:
        # 폼 경로: 품목·금액·용도는 이미 구조화돼 있어 비목후보만 LLM 으로 채운다.
        # 합성 문장(_합성_질문)을 LLM 에 다시 넣지 않는다. 필드→문장→필드 왕복은 정보를 잃는다.
        from normalize_run import 비목후보_추출
        용도 = body.용도 or ""
        if body.추가설명:
            용도 = f"{용도} ({body.추가설명})".strip()
        후보, 실패 = 비목후보_추출(body.품목, 용도, body.금액, body.사업명, 비목목록=비목들)
        out = {"품목": body.품목, "금액": body.금액, "금액_추정여부": False,
               "용도": 용도, "비목후보": 후보}
    # 두 경로의 응답 키를 맞춘다. 하위항목은 자연어 경로만 뽑을 수 있다.
    out.setdefault("결제수단", None)
    out.setdefault("구매명의", None)
    out.setdefault("신청일", None)
    out.setdefault("비교견적", None)
    out.setdefault("하위항목", None)
    # 누락필드·_출처 는 normalize_run 내부 산출물이고 API 계약이 아니다.
    out.pop("누락필드", None)
    out.pop("_출처", None)
    # 스키마를 지키지 않는 응답도 사업에 없는 비목을 넘기지 못하게 한다.
    후보 = out.get("비목후보")
    out["비목후보"] = ([c for c in 후보 if isinstance(c, dict) and c.get("비목") in 비목들]
                    if isinstance(후보, list) else [])
    out["심층질문"] = []      # 고정 심층질문(check_items) 제거(재설계 11-11). 질문은 규정 원문 기반 LLM 질문만
    return out, 실패


def _온톨로지_정규화(body: 정규화요청) -> tuple[dict, bool] | None:
    """① 입력 정리(재설계 11-11). 대상 사업이 아니면 None.

    폼: LLM 0회. 폼 값을 그대로 판정 입력으로 쓰고, 지출 항목이 이 사업의 품목 이름 · 별칭과 글자 그대로 같은지만 본다.
        같으면 응답 `품목확인` 에 분류를 실어 화면이 ② 조사를 건너뛴다(③-2 는 /api/questions 가 같은 별칭으로 다시 정한다).
    자연어(골든셋 평가용): 질의변환 LLM 으로 품목 · 금액 · 용도만 뽑는다. 분류는 고르지 않는다.
    분류 선택 화면 · 고정 심층질문(check_items)은 없앴다. 응답 키 분류후보 · 비목후보 · 심층질문은 빈 값으로 둔다(옛 화면 호환).
    """
    import item_classify
    import ontology_lookup
    from _lib import db
    from normalize_run import 질의변환

    with db.connect(connect_timeout=5, autocommit=True) as c:
        cur = c.cursor()
        if not ontology_lookup.새경로_대상(cur, body.사업명):
            return None
        레이블 = [x for x in ontology_lookup.레이블_목록(cur, body.사업명) if x["비목"] in 비목_ENUM]
        폼 = not (body.질문 and body.질문.strip())
        용도 = body.용도 or ""
        if 폼 and body.추가설명:
            용도 = f"{용도} ({body.추가설명})".strip()
        if 폼:
            # 사용일자 = 화면의 예상 지출일(스펙 8-1). 판정은 날짜로 읽히는 값만 쓴다(orchestrate.유효_날짜)
            out = {"품목": body.품목, "금액": body.금액, "금액_추정여부": False, "용도": 용도,
                   "질의문": f"{용도} {body.품목 or ''}".strip(), "수치": {}, "거래처유형": None,
                   "사용일자": (body.집행예정일 or "").strip() or None}
        else:
            out, _메타 = 질의변환({"질문": body.질문, "부가세구분": body.부가세구분, "사업명": body.사업명}, 레이블)
        # 별칭 정확 일치만(LLM 없음). 같으면 ② · ③-1 을 건너뛴다
        별칭 = item_classify.분류(cur, body.사업명, {"품목": out.get("품목")}, 레이블, llm=None)
    수치 = {k: v for k, v in (out.get("수치") or {}).items() if v not in (None, "")}
    수치["부가세구분"] = body.부가세구분 or 수치.get("부가세구분") or "모름"
    out.update(분류후보=[], 비목후보=[], 수치=수치, 심층질문=[],
               품목확인={"별칭일치": bool(별칭["분류"]), "분류": 별칭["분류"],
                       "안내": item_classify.안내문(별칭["분류"]) if 별칭["분류"] else None})
    for k in ("결제수단", "구매명의", "신청일", "비교견적", "하위항목"):
        out.setdefault(k, None)
    out.pop("누락필드", None)
    out.pop("_출처", None)
    return out, False


def _합성_질문(body: 정규화요청) -> str:
    """폼 값을 문장으로 합성한다(routes_plans._합성 과 같은 문장 규칙). 저장·검색·표시 전용이다."""
    if body.질문 and body.질문.strip():
        return body.질문.strip()
    사업 = f"{body.사업명}에서 " if body.사업명 else ""
    금액문 = f"{int(body.금액):,}원" if body.금액 is not None else ""
    return f"{사업}{body.용도 or ''} {body.품목 or ''} {금액문}을 사도 되나요?".strip()
