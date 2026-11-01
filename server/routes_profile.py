# -*- coding: utf-8 -*-
"""GET/PUT /api/profile — 기관의 F 프로필(F1 협약 금액·기간). f3·f4 는 항상 빈 목록이다.

GET 은 tenant.f_profile 을 읽는다(RLS 정책이 org 를 거른다). 프로필이 없거나 게스트면 빈 프로필을 준다 —
프로필 없이도 판정은 되고 전제가 늘어날 뿐이다. PUT 은 저장하지 않고 받은 값을 그대로 돌려준다.
PMS 원문·실명·과제번호는 스키마에 자리가 없어 받지 않는다.
"""
from __future__ import annotations

from fastapi import APIRouter, Body

from server._common import MOCK, _질의
from server.models import F1, 프로필

router = APIRouter()

_빈_프로필 = {"f1": F1().model_dump(), "f3": [], "f4": []}


@router.get("/api/profile")
def profile_get(org_id: str | None = None) -> dict:
    if MOCK or not org_id:
        return _빈_프로필
    행 = _질의('SELECT "정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일" '
              'FROM tenant.f_profile WHERE org_id = %s LIMIT 1', (org_id,))
    if not 행:
        return _빈_프로필
    a, b, c, d = 행[0]
    return {"f1": {"정부지원_현금": float(a or 0), "자기부담_현금": float(b or 0),
                   "협약시작일": str(c) if c else None, "협약종료일": str(d) if d else None},
            "f3": [], "f4": []}


@router.put("/api/profile")
def profile_put(body: 프로필 = Body(...)) -> dict:
    return {"저장": False, "이유": "f_profile 저장은 아직 지원하지 않습니다",
            "받은값": body.model_dump()}
