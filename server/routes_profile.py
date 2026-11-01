# -*- coding: utf-8 -*-
"""GET/PUT /api/profile — 기관의 기업 프로필(협약 금액·기간). 집행내역·참여인력은 항상 빈 목록이다.

GET 은 토큰으로 검증된 org 의 tenant.profile_agreements 를 읽는다(코드 조건 + RLS). ?org_id= 쿼리 값은 믿지 않는다.
프로필이 없거나 게스트면 빈 프로필을 준다. 프로필 없이도 판정은 되고 전제가 늘어날 뿐이다.
PUT 은 저장하지 않고 받은 값을 그대로 돌려준다.
PMS 원문·실명·과제번호는 스키마에 필드가 없어 받지 않는다.
응답 키는 한 릴리스 동안 이전 이름으로도 함께 보낸다(_구키_포함).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

from server import auth
from server._common import MOCK, _질의, 구키_추가
from server.models import 프로필, 협약정보

router = APIRouter()

_구_프로필키 = {"협약": "f1", "집행내역": "f3", "참여인력": "f4"}  # 구 이름 호환(제거 예정: 다음 릴리스)


def _구키_포함(d: dict) -> dict:
    """프로필 dict 에 이전 키 이름을 덧붙인다."""
    return 구키_추가(d, _구_프로필키)


def _빈_프로필() -> dict:
    return _구키_포함({"협약": 협약정보().model_dump(), "집행내역": [], "참여인력": []})


@router.get("/api/profile")
def profile_get(req: Request) -> dict:
    # 특권 DSN(로컬 postgres 등)에서는 RLS 가 꺼지므로 쿼리 값을 쓰면 다른 기관 협약 정보가 보인다.
    org_id = auth.검증된_org(req)
    if MOCK or not org_id:
        return _빈_프로필()
    행 = _질의('SELECT "정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일" '
              'FROM tenant.profile_agreements WHERE org_id = %s LIMIT 1', (org_id,))
    if not 행:
        return _빈_프로필()
    a, b, c, d = 행[0]
    return _구키_포함({"협약": {"정부지원_현금": float(a or 0), "자기부담_현금": float(b or 0),
                               "협약시작일": str(c) if c else None,
                               "협약종료일": str(d) if d else None},
                      "집행내역": [], "참여인력": []})


@router.put("/api/profile")
def profile_put(body: 프로필 = Body(...)) -> dict:
    out = {"저장": False, "사유": "기업 프로필 저장은 아직 지원하지 않습니다",
           "받은값": _구키_포함(body.model_dump())}
    return 구키_추가(out, {"사유": "이유"})  # 구 이름 호환(제거 예정: 다음 릴리스)
