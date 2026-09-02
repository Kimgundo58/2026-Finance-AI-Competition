# -*- coding: utf-8 -*-
"""판정 영속화 회귀 테스트 — `server/persist.py::판정_저장` 대조.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_persist.py -q

MOCK 분기는 `persist.MOCK` 을 직접 세워 DB 없이 돈다 (conftest 의 모듈 스위치 목록에
`server.persist` 가 없다 — 새 모듈이었다). 실 DB 분기는
`_실_저장()` 을 직접 불러 `판정_저장()` 의 MOCK 분기를 아예 거치지 않는다
(`test_plans.py` 가 `_실_생성()` 을 직접 부르는 것과 같은 관례).
"""
from __future__ import annotations

import pytest

from server import persist
from server._common import _질의, _실행
from server import routes_plans
from server.models import 계획생성


# ════════════════════════════════════════════════════════════════════
# MOCK 분기 — DB 없이 돈다
# ════════════════════════════════════════════════════════════════════

def test_MOCK_plan_id_있으면_저장True():
    persist.MOCK = True
    out = persist.판정_저장(plan_id=45, body=None, out={"해야할일": []})
    assert out["저장"] is True
    assert out["decision_id"] == 9001
    assert out["plan_id"] == 45
    assert out["할일"] == {"생성": 2, "갱신": 0, "보존_user": 0,
                          "보존_날짜수정": 0, "코드매칭": 2, "코드미상": 0}


def test_MOCK_plan_id_없으면_저장False():
    persist.MOCK = True
    out = persist.판정_저장(plan_id=None, body=None, out={"해야할일": []})
    assert out == {"저장": False, "사유": "plan_id 없음"}


# ════════════════════════════════════════════════════════════════════
# 실 DB 분기 — `_실_저장()` 직접 호출. DB 없으면 skip.
# ════════════════════════════════════════════════════════════════════

def _db_있음() -> bool:
    행 = _질의("SELECT to_regclass('tenant.expense_plans')")
    return bool(행) and 행[0][0] is not None


pytestmark_db = pytest.mark.skipif(not _db_있음(), reason="tenant DB 미기동 — 실 경로 테스트 스킵")


@pytest.fixture
def 정리():
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


def _decision_만들기(상세) -> int:
    return _질의(
        """
        INSERT INTO tenant.decisions (org_id, 사업명, 질문원문, 비목, 금액, 판정, 요약, 신뢰등급)
        VALUES (NULL, %s, %s, %s, %s, '조건부', '테스트 요약', 'B')
        RETURNING decision_id
        """,
        (상세.사업명, 상세.질문원문, 상세.확정비목, 상세.금액),
    )[0][0]


@pytestmark_db
def test_decision_id_없으면_저장False(정리):
    plan_ids, _ = 정리
    상세 = routes_plans._실_생성(_계획생성())
    plan_ids.append(상세.plan_id)

    out = persist._실_저장(상세.plan_id, None, {"해야할일": []}, None, None)
    assert out == {"저장": False, "사유": "decision_id 없음 — 판정이 기록되지 않았다"}


@pytestmark_db
def test_없는_plan_id_는_저장False_decisions_안건드림(정리):
    plan_ids, decision_ids = 정리
    # 🔴 2026-09-01 감사에서 잡힌 버그: 아래 _실_생성() 이 expense_plans 행을 실제로
    #    만드는데(주석과 달리 "만들어지지 않는다"가 아니었다) plan_ids 에 안 담아 17건이
    #    샜다. 존재확인용 decisions 행을 만들려고 만든 계획이지 -1 테스트 대상은 아니다
    #    (아래 _실_저장 은 plan_id=-1 을 쓴다) — 그래도 정리 대상에는 반드시 넣는다.
    상세 = routes_plans._실_생성(_계획생성())
    plan_ids.append(상세.plan_id)
    decision_id = _decision_만들기(상세)
    decision_ids.append(decision_id)
    # persist._실_저장 은 plan_id=-1(존재하지 않음)로 부른다 — 위 상세.plan_id 는 안 쓴다
    out = persist._실_저장(-1, None, {"해야할일": []}, None, decision_id)
    assert out["저장"] is False

    남은 = _질의("SELECT plan_id FROM tenant.decisions WHERE decision_id = %s", (decision_id,))
    assert 남은[0][0] is None, "plan_id 를 못 찾았는데 decisions.plan_id 가 채워졌다"


@pytestmark_db
def test_잇기_그리고_두번_저장해도_decisions_행_안늘어난다(정리):
    plan_ids, decision_ids = 정리
    상세 = routes_plans._실_생성(_계획생성(제목="레인A판정저장"))
    plan_ids.append(상세.plan_id)
    decision_id = _decision_만들기(상세)
    decision_ids.append(decision_id)

    # 🔴 2026-09-02 — 여기는 `count(*)` 전역이었다. **WHERE 가 없었다.**
    #    그러면 이 assert 는 「전 세션이 조용히 있어야만」 참이다 — 같은 `suddoe` 를
    #    여러 세션이 동시에 쓰는 지금은 게이트가 아니라 동전던지기다.
    #    실제로 전체 스위트를 3번 돌려 **2회차에서만** 이 테스트가 빨개지는 걸 봤다.
    #    이 테스트가 재려는 건 「이 판정 행이 안 늘었나」이지 「테이블 총량」이 아니다 —
    #    `WHERE plan_id = %s` 로 좁히면 회귀 방지 의도(INSERT 로 되돌아감)는 그대로 지켜지고
    #    남의 행에 흔들리지 않는다.
    def _내판정행수() -> int:
        return _질의("SELECT count(*) FROM tenant.decisions WHERE plan_id = %s",
                    (상세.plan_id,))[0][0]

    out1 = persist._실_저장(상세.plan_id, None, {"해야할일": [{"항목": "증빙 준비"}]},
                           None, decision_id)
    assert out1 == {
        "저장": True, "decision_id": decision_id, "plan_id": 상세.plan_id,
        "할일": out1["할일"],   # 아래서 별도 확인
    }
    assert out1["할일"]["생성"] == 1

    # 🔴 «1차 저장 후» 를 기준선으로 잡는다. 저장 «전» 은 아직 `plan_id` 가 NULL 이라
    #    이 계획에 붙은 판정이 0건이고, 1차 저장이 그걸 1로 만드는 게 **정상 동작**이다.
    #    (처음에 저장 전을 기준선으로 잡았다가 `1 == 0` 으로 빨개져서 잡았다 —
    #     전역 count 를 좁힐 때 «무엇을 재는 시점인가» 가 같이 바뀐 것이다)
    행수_1차 = _내판정행수()
    assert 행수_1차 == 1, f"1차 저장 후 이 계획에 붙은 판정이 {행수_1차}건이다 (1이어야)"

    # 🔴 같은 판정을 두 번 저장해도 decisions 행이 늘면 안 된다 — INSERT 로 되돌아간 회귀 방지
    out2 = persist._실_저장(상세.plan_id, None, {"해야할일": [{"항목": "증빙 준비"}]},
                           None, decision_id)
    assert out2["저장"] is True

    행수_2차 = _내판정행수()
    assert 행수_2차 == 행수_1차, (
        f"같은 판정을 두 번 저장했더니 이 계획의 decisions 행이 "
        f"{행수_1차} → {행수_2차} 로 늘었다 (INSERT 로 되돌아간 회귀)")

    plan_row = _질의(
        "SELECT latest_decision_id, 상태 FROM tenant.expense_plans WHERE plan_id = %s",
        (상세.plan_id,),
    )[0]
    assert plan_row[0] == decision_id
    assert plan_row[1] == "judged"

    decision_row = _질의(
        "SELECT plan_id FROM tenant.decisions WHERE decision_id = %s", (decision_id,)
    )[0]
    assert decision_row[0] == 상세.plan_id
