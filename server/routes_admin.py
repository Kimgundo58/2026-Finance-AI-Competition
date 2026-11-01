# -*- coding: utf-8 -*-
"""관리 엔드포인트: /admin/cost(요청 제한·캐시 상태), /admin/queue(재검수 큐), /admin/warmup(모델 워밍업).

모든 라우트는 X-Admin-Token 헤더를 auth.관리자_확인 으로 검사한다(SUDDOE_ADMIN_TOKEN 미설정이면 전부 403).
모델_워밍업() 은 main 의 기동 훅도 같이 쓴다. 읽기 전용이다 — 재검수 큐를 승인해 corpus.rules 로
올리는 경로는 두지 않는다(사람의 검수 없이 룰이 바뀌는 길이 생긴다).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from server import auth, response_cache
from server._common import MOCK, _질의
from server.rate_limit import 제한

router = APIRouter(dependencies=[Depends(auth.관리자_확인)])

_워밍업시각: str | None = None


def 모델_워밍업() -> dict:
    """검색 임베딩 모델과 비목확정 모델을 미리 로드한다. 단계별 소요 시간 또는 실패 사유를 돌려준다."""
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
        from rule_lookup import warmup as _룰워밍업
        _룰워밍업()
        결과["비목확정"] = f"{time.time() - t:.1f}초"
    except Exception as e:                                    # noqa: BLE001
        결과["비목확정"] = f"실패 {type(e).__name__}"
    return 결과


@router.get("/admin/cost")
def admin_cost() -> dict:
    return {"모드": "mock" if MOCK else "live", **제한.상태(),
            "캐시": response_cache.상태(), "워밍업": _워밍업시각}


@router.get("/admin/queue")
def admin_queue(종류: str | None = None, 사유코드: str | None = None,
                사업명: str | None = None, 상태: str = "대기",
                limit: int = 50, offset: int = 0) -> dict:
    """corpus.recheck_queue 의 요약·상태별 건수·항목(최신순, 최대 200건)."""
    where, 인자 = ['"상태" = %s'], [상태]
    for 칼럼, 값 in (('"종류"', 종류), ('"사유코드"', 사유코드), ('"사업명"', 사업명)):
        if 값:
            where.append(f"{칼럼} = %s")
            인자.append(값)
    조건 = " AND ".join(where)

    요약 = _질의(f'SELECT "종류", "사유코드", count(*) FROM corpus.recheck_queue '
                f'WHERE {조건} GROUP BY 1,2 ORDER BY 1, 3 DESC', tuple(인자))
    전체 = _질의(f'SELECT count(*) FROM corpus.recheck_queue WHERE {조건}', tuple(인자))
    상태별 = _질의('SELECT "상태", count(*) FROM corpus.recheck_queue GROUP BY 1 ORDER BY 1')
    행 = _질의(f"""
        SELECT queue_id, "종류", "사유코드", "대상종류", "대상id", "사업명", "비목",
               doc_id, "조번호", "구doc_id", "구조번호", "변경유형", "유사도",
               "요약", "상태", "발견일"
        FROM corpus.recheck_queue WHERE {조건}
        ORDER BY "발견일" DESC, queue_id LIMIT %s OFFSET %s
    """, tuple(인자) + (min(limit, 200), offset))
    키 = ("queue_id", "종류", "사유코드", "대상종류", "대상ID", "사업명", "비목",
          "doc_id", "조번호", "구doc_id", "구조번호", "변경유형", "유사도",
          "요약", "상태", "발견일")
    return {
        "전체": 전체[0][0] if 전체 else 0,
        "요약": [{"종류": a, "사유코드": b, "건수": c} for a, b, c in 요약],
        "상태별": {a: b for a, b in 상태별},
        "항목": [dict(zip(키, r)) for r in 행],
        "필터": {"종류": 종류, "사유코드": 사유코드, "사업명": 사업명, "상태": 상태},
        "비고": "읽기 전용. 승인·반영은 사람이 검수 절차로 한다",
    }


@router.post("/admin/warmup")
def admin_warmup() -> dict:
    global _워밍업시각
    결과 = 모델_워밍업()
    _워밍업시각 = datetime.now(timezone.utc).isoformat()
    return {"워밍업": 결과, "시각": _워밍업시각}
