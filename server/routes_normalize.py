# -*- coding: utf-8 -*-
"""POST /api/normalize — 지출 입력을 판정 입력(품목·금액·용도·비목후보)으로 정규화한다. SSE.

자연어 질문은 normalize_run.정규화() 가, 폼 입력은 normalize_run.비목후보_추출() 이 비목후보만 채운다(LLM 1회).
심층질문은 check_items 에서 고른다(LLM 호출 없음). 결과는 response_cache 에 캐시하되 LLM 오류로 나온 결과는 캐시하지 않는다.
이벤트 순서: 진행 → 필드* → 결과 → 완료 (실패 시 진행 → 오류 → 완료). 이벤트 이름과 순서는 프론트 계약이다.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from server import auth, response_cache
from server._common import MOCK, _sse, _sse응답, 구키_추가, 클라이언트_ip
from server.catalog import _사업_비목, _사업명_표준화, _심층질문마스터
from server.models import 정규화요청
from server.rate_limit import 제한
from server.selftest import mock_정규화

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
    # 되므로 키에 넣는다. 집행예정일·거래처는 정규화에 쓰이지 않아 넣지 않는다.
    org = auth.검증된_org(req)
    account = auth.검증된_account(req)
    # 캐시 행은 계정 단위라(RLS) 키에도 org·계정을 넣는다. 키가 같으면 다른 계정 행과 PRIMARY KEY 가 겹친다.
    k = response_cache.캐시키("normalize", org, account, (body.질문 or "").strip(),
                            body.품목, body.금액, body.용도, body.추가설명, body.사업명,
                            body.특수관계.친족거래, body.특수관계.전직임직원업체)
    캐시 = response_cache.조회(k, org, account)

    def gen():
        yield _sse("진행", {"단계": "정규화", "설명": "입력에서 정보를 추출하는 중"})
        if 캐시 is not None:
            yield _sse("결과", 캐시)
            yield _sse("완료", {"캐시": True})
            return
        try:
            out, 실패 = (dict(mock_정규화), False) if MOCK else _live_정규화(body)
        except Exception as e:                                # noqa: BLE001
            # 500 을 내지 않고 오류 이벤트로 끝낸다.
            yield _sse("오류", {"메시지": "정규화에 실패했습니다", "종류": type(e).__name__})
            yield _sse("완료", {"실패": True})
            return
        out.setdefault("질문원문", _합성_질문(body))
        if not 실패:                  # 일시적 LLM 오류로 빈 비목후보가 캐시에 남지 않게 한다
            response_cache.저장(k, out, 종류="normalize", org=org, account=account)
        for 필드 in ("품목", "금액", "금액_추정여부", "용도", "비목후보"):
            if 필드 in out:
                yield _sse("필드", {필드: out[필드]})
        yield _sse("결과", out)
        yield _sse("완료", {"캐시": False})

    return _sse응답(gen())


def _live_정규화(body: 정규화요청) -> tuple[dict, bool]:
    """live 정규화. (결과, 실패) — 실패는 폼 경로 LLM 오류로 비목후보가 비었다는 뜻이다."""
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
    out["심층질문"] = _심층질문_선별(body.사업명, out.get("비목후보"))
    return out, 실패


def _합성_질문(body: 정규화요청) -> str:
    """폼 값을 문장으로 합성한다(routes_plans._합성 과 같은 문장 규칙). 저장·검색·표시 전용이다."""
    if body.질문 and body.질문.strip():
        return body.질문.strip()
    사업 = f"{body.사업명}에서 " if body.사업명 else ""
    금액문 = f"{int(body.금액):,}원" if body.금액 is not None else ""
    return f"{사업}{body.용도 or ''} {body.품목 or ''} {금액문}을 사도 되나요?".strip()


def _심층질문_선별(사업명: str | None, 비목후보: list[dict] | None) -> list[dict]:
    """사업명과 신뢰도 1위 비목후보로 심층질문을 거른다.

    정규화 시점에는 비목이 후보뿐이라 거칠게만 거른다. 사업명이 없으면 전부 준다. 서버가
    추측으로 줄이면 '해당 없음' 과 '아직 모름' 을 구분할 수 없다.
    필요프로필필드는 한 릴리스 동안 이전 키 이름으로도 함께 보낸다.
    """
    표 = _심층질문마스터()
    if not 표:
        return []
    최우선비목 = None
    if 비목후보:
        try:
            최우선비목 = max(비목후보, key=lambda c: c.get("신뢰도", 0)).get("비목")
        except (TypeError, ValueError):
            최우선비목 = None
    out = []
    for q in 표:
        if q["사업명"] and 사업명 and q["사업명"] != 사업명:
            continue
        if q["비목"] and 최우선비목 and q["비목"] != 최우선비목:
            continue
        # 비목은 남긴다. 후보가 없거나 사용자가 다른 비목을 고르면 화면이 고른 비목으로 다시 거른다.
        항목 = {k: v for k, v in q.items() if k != "사업명"}
        out.append(구키_추가(항목, {"필요프로필필드": "필요F필드"}))  # 구 이름 호환(제거 예정: 다음 릴리스)
    return out
