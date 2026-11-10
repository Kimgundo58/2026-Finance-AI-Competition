# -*- coding: utf-8 -*-
"""관리 엔드포인트: /admin/status(요청 제한·캐시 상태), /admin/queue(재검수 큐), /admin/feedback(틀렸어요 평가),
/admin/warmup(모델 워밍업).

모든 라우트는 X-Admin-Token 헤더를 auth.관리자_확인 으로 검사한다(SUDDOE_ADMIN_TOKEN 미설정이면 전부 403).
모델_워밍업() 은 main 의 기동 훅도 같이 쓴다. 읽기 전용이다. 재검수 큐를 승인해 corpus.rules 로
올리는 경로는 두지 않는다(사람의 검수 없이 룰이 바뀌는 길이 생긴다).
이름을 바꾼 경로·응답 키·필터 값은 한 릴리스 동안 이전 이름도 받거나 함께 보낸다(파일 끝 호환 블록).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from server import auth, response_cache
from server._common import MOCK, _질의, 구키_추가
from server.rate_limit import 제한

router = APIRouter(dependencies=[Depends(auth.관리자_확인)])

_워밍업시각: str | None = None


def 모델_워밍업() -> dict:
    """검색 임베딩 모델과 비목 후보 조회 모델을 미리 로드한다. 단계별 소요 시간 또는 실패 사유를 돌려준다."""
    결과 = {}
    t = time.time()
    try:
        from retrieve import 워밍업
        워밍업()
        결과["임베딩"] = f"{time.time() - t:.1f}초"
    except Exception as e:                                    # noqa: BLE001
        결과["임베딩"] = f"실패 {type(e).__name__}"
    t = time.time()
    try:
        from rule_lookup import 워밍업 as _룰_워밍업
        _룰_워밍업()
        결과["비목후보조회"] = f"{time.time() - t:.1f}초"
    except Exception as e:                                    # noqa: BLE001
        결과["비목후보조회"] = f"실패 {type(e).__name__}"
    return 결과


@router.get("/admin/status")
@router.get("/admin/cost", include_in_schema=False)  # 구 이름 호환(제거 예정: 다음 릴리스)
def admin_status() -> dict:
    out = {"모드": "mock" if MOCK else "live", **제한.상태(),
           "캐시": response_cache.상태(), "워밍업": _워밍업시각}
    return _구_상태키_포함(out)


@router.get("/admin/feedback")
def admin_feedback(limit: int = 50, offset: int = 0) -> dict:
    """사용자가 「틀렸어요」로 표시한 판정(최신순, 최대 200건). 기관을 넘어 읽어야 해서 DB 함수를 거친다."""
    if MOCK:
        return {"건수": 0, "항목": []}
    행 = _질의("SELECT * FROM tenant.틀림_피드백목록(%s, %s)", (limit, offset), 예외전파=True)
    키 = ("decision_id", "plan_id", "기관명", "이유", "의견", "질문원문", "판정", "요약", "저장시각")
    항목 = [dict(zip(키, r)) for r in 행]
    return {"건수": len(항목), "항목": [{**h, "저장시각": h["저장시각"].isoformat()} for h in 항목]}


@router.get("/admin/queue")
def admin_queue(종류: str | None = None, 사유코드: str | None = None,
                사업명: str | None = None, 상태: str = "대기",
                limit: int = 50, offset: int = 0) -> dict:
    """corpus.recheck_queue 의 요약·상태별 건수·항목(최신순, 최대 200건)."""
    종류 = _구_종류값.get(종류, 종류) if 종류 else 종류  # 구 이름 호환(제거 예정: 다음 릴리스)
    where, 인자 = ['"상태" = %s'], [상태]
    for 컬럼, 값 in (('"종류"', 종류), ('"사유코드"', 사유코드), ('"사업명"', 사업명)):
        if 값:
            where.append(f"{컬럼} = %s")
            인자.append(값)
    조건 = " AND ".join(where)

    요약 = _질의(f'SELECT "종류", "사유코드", count(*) FROM corpus.recheck_queue '
                f'WHERE {조건} GROUP BY 1,2 ORDER BY 1, 3 DESC', tuple(인자))
    전체 = _질의(f'SELECT count(*) FROM corpus.recheck_queue WHERE {조건}', tuple(인자))
    상태별 = _질의('SELECT "상태", count(*) FROM corpus.recheck_queue GROUP BY 1 ORDER BY 1')
    행 = _질의(f"""
        SELECT queue_id, "종류", "사유코드", "대상종류", "대상id", "사업명", "비목",
               doc_id, "조번호", "이전판_doc_id", "이전판_조번호", "변경유형", "유사도",
               "요약", "상태", "발견일"
        FROM corpus.recheck_queue WHERE {조건}
        ORDER BY "발견일" DESC, queue_id LIMIT %s OFFSET %s
    """, tuple(인자) + (min(limit, 200), offset))
    키 = ("queue_id", "종류", "사유코드", "대상종류", "대상id", "사업명", "비목",
          "doc_id", "조번호", "이전판_doc_id", "이전판_조번호", "변경유형", "유사도",
          "요약", "상태", "발견일")
    return {
        "전체": 전체[0][0] if 전체 else 0,
        "요약": [{"종류": a, "사유코드": b, "건수": c} for a, b, c in 요약],
        "상태별": {a: b for a, b in 상태별},
        "항목": [구키_추가(dict(zip(키, r)), _구_큐키) for r in 행],
        "필터": {"종류": 종류, "사유코드": 사유코드, "사업명": 사업명, "상태": 상태},
        "비고": "읽기 전용. 승인·반영은 사람이 검수 절차로 한다",
    }


@router.post("/admin/warmup")
def admin_warmup() -> dict:
    global _워밍업시각
    결과 = 모델_워밍업()
    _워밍업시각 = datetime.now(timezone.utc).isoformat()
    결과 = 구키_추가(dict(결과), {"비목후보조회": "비목확정"})  # 구 이름 호환(제거 예정: 다음 릴리스)
    return {"워밍업": 결과, "시각": _워밍업시각}


# ── 구 이름 호환(제거 예정: 다음 릴리스) ─────────────────────────────
# 운영 점검 스크립트가 새 이름으로 바뀌면 이 블록과 위의 호환 줄을 함께 지운다. 표는 {새 이름: 이전 이름}.
_구_큐키 = {"대상id": "대상ID", "이전판_doc_id": "구doc_id", "이전판_조번호": "구조번호"}  # 구 이름 호환(제거 예정: 다음 릴리스)
# /admin/queue?종류= 필터는 이전 값으로 와도 새 값으로 바꿔 조회한다. 표는 {이전 값: 새 값}.
_구_종류값 = {"A4개정": "개정감지", "A2엄격조항": "엄격조항", "A5기관답변": "기관답변", "H4정산리허설": "정산점검"}  # 구 이름 호환(제거 예정: 다음 릴리스)


def _구_상태키_포함(out: dict) -> dict:
    """/admin/status 응답에 이전 키 이름(요청 제한 한도·운영 기간·차단 집계)을 덧붙인다."""
    구키_추가(out, {"일일한도": "일일캡"})  # 구 이름 호환(제거 예정: 다음 릴리스)
    if isinstance(out.get("운영기간"), dict):
        out["개방"] = 구키_추가(dict(out["운영기간"]), {"운영중": "열림"})  # 구 이름 호환(제거 예정: 다음 릴리스)
    if isinstance(out.get("차단"), dict):
        out["차단"] = 구키_추가(dict(out["차단"]), {"운영기간외": "개방창", "일일한도": "일일캡"})  # 구 이름 호환(제거 예정: 다음 릴리스)
    return out
