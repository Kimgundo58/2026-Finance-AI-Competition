# -*- coding: utf-8 -*-
"""판정 결과의 「맞았어요 / 틀렸어요」.

PUT /api/decisions/{decision_id}/feedback 이 내 평가를 저장하고, 계획 상세(`내피드백`)가 화면에 되돌려 준다.
틀렸어요 목록은 GET /admin/feedback(routes_admin)이 읽는다. 판정 코드는 이 표를 읽지 않는다.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ._common import MOCK, 작성자표시, 현재_본인, 현재_org, _계정조건, _질의
from .models import 판정피드백, 피드백입력
from . import mock_data

router = APIRouter(tags=["판정 피드백"])

_찾을수없음 = "점검 결과를 찾을 수 없습니다"


def _계정값() -> str | None:
    # 평가는 팀이 아니라 사람별이다. 데이터 계정이 아니라 로그인한 본인을 쓴다
    계정 = 현재_본인.get()
    return str(계정) if 계정 else None


def 내피드백_조회(decision_id: int | None) -> dict | None:
    """지금 계정이 이 판정에 남긴 평가. 재점검하면 decision_id 가 바뀌어 비어 있다."""
    if decision_id is None:
        return None
    if MOCK:
        return mock_data.mock_피드백.get(decision_id)
    조건, 인자 = _계정조건(현재_org.get(), "f")
    행 = _질의(
        f"SELECT f.평가, f.이유, f.의견 FROM tenant.decision_feedback f "
        f"WHERE f.decision_id = %s AND {조건} AND f.작성_account_id IS NOT DISTINCT FROM %s::uuid",
        (decision_id, *인자, _계정값()))
    return dict(zip(("평가", "이유", "의견"), 행[0])) if 행 else None


def 팀평가_조회(decision_id: int | None) -> list[dict]:
    """같은 판정에 다른 구성원이 남긴 평가. 팀이 없으면 빈 목록이다."""
    if decision_id is None or MOCK:
        return []
    조건, 인자 = _계정조건(현재_org.get(), "f")
    행 = _질의(
        "SELECT f.작성_account_id, w.email, f.평가, f.이유, f.의견 FROM tenant.decision_feedback f "
        "JOIN tenant.accounts w ON w.account_id = f.작성_account_id "
        f"WHERE f.decision_id = %s AND {조건} ORDER BY f.created_at",
        (decision_id, *인자))
    return [{"작성자": 작성자표시(r[0], r[1]), "평가": r[2], "이유": r[3], "의견": r[4]}
            for r in 행 if 작성자표시(r[0], r[1])]


@router.put("/api/decisions/{decision_id}/feedback", response_model=판정피드백)
def 피드백_저장(decision_id: int, body: 피드백입력) -> 판정피드백:
    """내 평가를 저장한다. 같은 판정에 다시 보내면 바꾼다."""
    if MOCK:
        # mock 은 판정을 저장하지 않아 판정 직후 번호(9001)도 받는다(persist.판정_저장).
        if decision_id not in {p["latest_decision_id"] for p in mock_data.mock_계획} | {9001}:
            raise HTTPException(404, _찾을수없음)
        값 = 판정피드백(평가=body.평가, 이유=body.이유, 의견=body.의견)
        mock_data.mock_피드백[decision_id] = 값.model_dump()
        return 값
    org = 현재_org.get()
    if org is None:
        raise HTTPException(401, "평가를 저장하려면 로그인이 필요합니다")
    조건, 인자 = _계정조건(org, "d")
    행 = _질의(
        "INSERT INTO tenant.decision_feedback "
        "(decision_id, org_id, account_id, 작성_account_id, 평가, 이유, 의견) "
        "SELECT d.decision_id, d.org_id, d.account_id, %s::uuid, %s, %s, %s "
        f"FROM tenant.decisions d WHERE d.decision_id = %s AND {조건} "
        "ON CONFLICT (decision_id, 작성_account_id) DO UPDATE "
        "SET 평가 = EXCLUDED.평가, 이유 = EXCLUDED.이유, 의견 = EXCLUDED.의견, updated_at = now() "
        "RETURNING 평가, 이유, 의견",
        (_계정값(), body.평가, body.이유, body.의견, decision_id, *인자), 예외전파=True)
    if not 행:
        raise HTTPException(404, _찾을수없음)
    return 판정피드백(평가=행[0][0], 이유=행[0][1], 의견=행[0][2])
