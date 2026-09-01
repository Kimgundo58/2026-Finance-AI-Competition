# -*- coding: utf-8 -*-
"""레인 A — 지출계획 `_실_*` 네 함수 테스트.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_plans.py -q

DB 가 없으면 전부 skip 된다 — `_common._질의` 는 접속 실패 시 빈 리스트를 주므로
그걸 «0건» 으로 오해하지 않으려고 스키마 존재 여부로 별도 확인한다.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from server import routes_plans
from server._common import _질의, _실행
from server.models import 계획생성


def _db_있음() -> bool:
    행 = _질의("SELECT to_regclass('tenant.expense_plans')")
    return bool(행) and 행[0][0] is not None


pytestmark = pytest.mark.skipif(not _db_있음(), reason="tenant DB 미기동 — 실 경로 테스트 스킵")


@pytest.fixture
def 정리():
    """테스트가 만든 plan_id·decision_id 를 끝에 지운다."""
    plan_ids: list[int] = []
    decision_ids: list[int] = []
    yield plan_ids, decision_ids
    for did in decision_ids:
        _실행("DELETE FROM tenant.decisions WHERE decision_id = %s", (did,))
    for pid in plan_ids:
        _실행("DELETE FROM tenant.expense_plans WHERE plan_id = %s", (pid,))


def _계획생성(**override) -> 계획생성:
    기본 = dict(사업명="초기창업패키지", 품목="테스트 품목", 금액=100000.0, 용도="테스트 용도")
    기본.update(override)
    return 계획생성(**기본)


def test_생성_게스트_그리고_상세(정리):
    plan_ids, _ = 정리
    body = _계획생성(제목=f"레인A-{uuid.uuid4().hex[:8]}")
    상세 = routes_plans._실_생성(body)
    plan_ids.append(상세.plan_id)

    # 방금 만든 계획이라 아직 판정이 없다 — 4-way 가 아니라 None
    assert 상세.판정 is None
    assert 상세.상태 == "draft"
    assert 상세.사업명 == "초기창업패키지"
    assert 상세.용도 == "테스트 용도"
    assert 상세.할일 == []
    assert 상세.판정상세 is None
    assert 상세.질문원문                      # 서버가 합성했다 (NOT NULL 컬럼)

    다시조회 = routes_plans._실_상세(상세.plan_id, None)
    assert 다시조회.plan_id == 상세.plan_id
    assert 다시조회.제목 == body.제목
    assert 다시조회.질문원문 == 상세.질문원문


def test_상세_없는_계획은_404():
    with pytest.raises(HTTPException) as e:
        routes_plans._실_상세(-1, None)
    assert e.value.status_code == 404


def test_목록_통계_판정있는_계획_반영(정리):
    plan_ids, decision_ids = 정리
    body = _계획생성(제목=f"레인A판정-{uuid.uuid4().hex[:8]}", 확정비목="기계장치")
    상세 = routes_plans._실_생성(body)
    plan_ids.append(상세.plan_id)

    decision_id = _질의(
        """
        INSERT INTO tenant.decisions (org_id, 사업명, 질문원문, 비목, 금액, 판정, 요약,
                                       신뢰등급, plan_id)
        VALUES (NULL, %s, %s, %s, %s, '조건부', '테스트 요약', 'B', %s)
        RETURNING decision_id
        """,
        (상세.사업명, 상세.질문원문, 상세.확정비목, 상세.금액, 상세.plan_id),
    )[0][0]
    decision_ids.append(decision_id)
    assert _실행(
        "UPDATE tenant.expense_plans SET latest_decision_id = %s, 상태 = 'judged' "
        "WHERE plan_id = %s",
        (decision_id, 상세.plan_id),
    ) == 1

    행 = next(r for r in routes_plans._실_목록(None) if r["plan_id"] == 상세.plan_id)
    assert 행["판정"] == "조건부"
    assert 행["상태"] == "judged"

    통계 = routes_plans._실_통계(None)
    assert 통계["확인필요"] >= 1                 # 조건부·판단불가 합산 카운트
    assert 통계["금액합계"] >= 상세.금액

    상세2 = routes_plans._실_상세(상세.plan_id, None)
    assert 상세2.판정 == "조건부"
    assert 상세2.판정상세["요약"] == "테스트 요약"
    assert 상세2.판정상세["신뢰등급"] == "B"


def test_org_격리_TENANT_LEAK(정리):
    plan_ids, _ = 정리
    기관들 = _질의("SELECT org_id FROM tenant.orgs LIMIT 1")
    if not 기관들:
        pytest.skip("tenant.orgs 에 등록된 기관이 없다")
    org_id = str(기관들[0][0])

    게스트 = routes_plans._실_생성(_계획생성(제목=f"게스트-{uuid.uuid4().hex[:8]}"))
    plan_ids.append(게스트.plan_id)
    기관 = routes_plans._실_생성(_계획생성(제목=f"기관-{uuid.uuid4().hex[:8]}", org_id=org_id))
    plan_ids.append(기관.plan_id)

    게스트목록 = {r["plan_id"] for r in routes_plans._실_목록(None)}
    기관목록 = {r["plan_id"] for r in routes_plans._실_목록(org_id)}

    assert 게스트.plan_id in 게스트목록
    assert 게스트.plan_id not in 기관목록, "TENANT_LEAK: 게스트 계획이 기관 목록에 샜다"
    assert 기관.plan_id in 기관목록
    assert 기관.plan_id not in 게스트목록, "TENANT_LEAK: 기관 계획이 게스트 목록에 샜다"

    with pytest.raises(HTTPException):
        routes_plans._실_상세(기관.plan_id, None)   # 게스트로 남의 기관 상세 조회 — 404 여야 한다
