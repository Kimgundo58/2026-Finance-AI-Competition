# -*- coding: utf-8 -*-
"""할일 「확인필요」 실 DB 경로 테스트.   **[할일 계통]**

🔴 `SUDDOE_MOCK=0` 을 강제한다 — 목 경로는 이미 완성돼 있고, 이 세션이 채운 건
   `server/routes_tasks.py::_실_*` 뿐이라 목으로 돌리면 아무것도 검증하지 못한다.
   실행에는 로컬 postgres(`SUDDOE_DSN`, 기본 localhost:5432/suddoe)가 떠 있어야 한다.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_tasks.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SUDDOE_MOCK"] = "0"        # import 전에 고정 — server._common.MOCK 이 여기서 확정된다

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
import pytest
from fastapi.testclient import TestClient

from server._common import DSN
from server.main import app

client = TestClient(app)


def _conn():
    return psycopg.connect(DSN, connect_timeout=3)


@pytest.fixture
def plan():
    """org_id=NULL(게스트) 지출계획 1건. 끝나면 plan_tasks 까지 CASCADE 로 지운다."""
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO tenant.expense_plans (질문원문, 확정비목, 금액, 집행예정일) "
            "VALUES ('테스트용 지출계획입니다', '재료비', 100000, '2026-09-20') "
            "RETURNING plan_id"
        ).fetchone()
    plan_id = row[0]
    yield plan_id
    with _conn() as conn:
        conn.execute("DELETE FROM tenant.expense_plans WHERE plan_id = %s", (plan_id,))


def _sync(plan_id: int, 해야할일: list[dict], decision_id: int | None = None) -> dict:
    r = client.post(
        f"/api/plans/{plan_id}/tasks:sync",
        json={"decision_id": decision_id, "해야할일": 해야할일},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _tasks_of(plan_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT task_id, 출처, 코드, 항목, due_date, 날짜_사용자수정, 상태 "
            "FROM tenant.plan_tasks WHERE plan_id = %s ORDER BY task_id", (plan_id,)
        ).fetchall()
    return [
        dict(zip(("task_id", "출처", "코드", "항목", "due_date", "날짜_사용자수정", "상태"), r))
        for r in rows
    ]


# ════════════════════════════════════════════════════════════════════
# 동기화 — 신규 생성
# ════════════════════════════════════════════════════════════════════

def test_동기화_신규_생성(plan):
    out = _sync(plan, [
        {"code": "비교견적준비", "항목": "비교견적 확보", "설명": "3곳 이상 받아두세요"},
        {"code": "부가세제외", "항목": "부가세 제외", "설명": "부가세는 빼고 신청하세요"},
    ])
    assert out["생성"] == 2 and out["갱신"] == 0
    assert out["코드매칭"] == 2 and out["코드미상"] == 0
    행 = _tasks_of(plan)
    assert len(행) == 2
    assert all(t["출처"] == "ai" and t["상태"] == "준비필요" for t in 행)
    # 🔴 2026-09-06 — 여기는 «바뀐 동작» 이다. 예전엔 2026-09-13 이 붙었다.
    #    비교견적준비의 `기본_오프셋일=-7` 은 `기한근거='운영기본값'` — 규정에 근거가
    #    «없는» 우리 관행이다(52행 중 45행이 그렇다). 그걸 날짜로 띄우면 사용자는
    #    규정상 기한으로 읽는다. 그래서 근거가 없으면 날짜를 «만들지 않는다».
    #    🔴 할일이 사라지는 게 아니다 — 체크리스트에는 그대로 남고 캘린더에만 안 뜬다
    #    (`models.py:169` — 같은 행이고 due_date 유무로만 갈린다).
    비교 = next(t for t in 행 if t["코드"] == "비교견적준비")
    assert 비교["due_date"] is None, "근거 없는 오프셋이 날짜로 새어나갔다"


def test_동기화_규정근거는_날짜가_붙는다(plan):
    """근거가 있으면 «반드시» 붙어야 한다 — 안 붙는 쪽으로만 기울면 기능이 죽는다.

    🔴 이 테스트가 없으면 `_due계산()` 이 항상 None 을 줘도 위 테스트가 통과한다.
       `자산등록` 은 `기한근거='규정근거'` · `기본_오프셋일=+30` 인 52행 중 2행 중 하나다.
    """
    _sync(plan, [{"code": "자산등록", "항목": "자산관리대장에 등록",
                  "설명": "취득가액 500만원 초과면 등록하세요"}])
    행 = _tasks_of(plan)
    자산 = next(t for t in 행 if t["코드"] == "자산등록")
    # 집행예정일 2026-09-20 + 30일
    assert str(자산["due_date"]) == "2026-10-20"


# ════════════════════════════════════════════════════════════════════
# 재판정 규칙 4개 — 여기가 제일 틀리기 쉽다
# ════════════════════════════════════════════════════════════════════

def test_재판정_규칙_전부(plan):
    # 1차: ai 행 둘
    _sync(plan, [
        {"code": "비교견적준비", "항목": "비교견적 확보", "설명": "1차 설명"},
        {"code": "부가세제외", "항목": "부가세 제외", "설명": "부가세는 빼고"},
    ])
    ai행 = {t["코드"]: t for t in _tasks_of(plan)}
    # 사용자가 직접 추가한 행 — 절대 안 건드려질 대상
    add = client.post(f"/api/plans/{plan}/tasks", json={
        "항목": "산단 담당자 확인", "설명": "직접 물어보기",
    })
    assert add.status_code == 201, add.text
    user_task_id = add.json()["task_id"]

    # 사용자가 비교견적 행의 날짜를 직접 고정
    patch = client.patch(
        f"/api/plans/{plan}/tasks/{ai행['비교견적준비']['task_id']}",
        json={"due_date": "2026-09-01"},
    )
    assert patch.status_code == 200
    assert patch.json()["날짜_사용자수정"] is True

    # 2차 재판정: 비교견적준비(설명 갱신 기대) + 부가세제외 소멸 + 새 코드 하나
    out = _sync(plan, [
        {"code": "비교견적준비", "항목": "비교견적 확보", "설명": "2차 설명"},
        {"code": "자산등록", "항목": "자산관리대장 등록", "설명": "취득가액 확인"},
    ])
    assert out["보존_user"] == 1
    assert out["보존_날짜수정"] == 1
    assert out["갱신"] == 1          # 비교견적준비
    assert out["생성"] == 1          # 자산등록

    행 = {t["코드"] or t["항목"]: t for t in _tasks_of(plan)}

    # ① user 행이 그대로 살아 있다
    assert any(t["task_id"] == user_task_id and t["출처"] == "user" for t in 행.values())

    # ② 날짜_사용자수정=true 행의 due_date 가 안 덮였다
    비교 = 행["비교견적준비"]
    assert str(비교["due_date"]) == "2026-09-01"
    assert 비교["날짜_사용자수정"] is True

    # ③ 코드 소멸(부가세제외)은 지워지고, 새 코드(자산등록)는 생겼다
    assert "부가세제외" not in 행
    assert "자산등록" in 행

    # 3차 재판정: 한 번 더 돌려도 user 행과 사용자수정 날짜가 그대로다
    out2 = _sync(plan, [
        {"code": "비교견적준비", "항목": "비교견적 확보", "설명": "3차 설명"},
        {"code": "자산등록", "항목": "자산관리대장 등록", "설명": "취득가액 확인"},
    ])
    assert out2["보존_user"] == 1
    assert out2["보존_날짜수정"] == 1
    행2 = {t["코드"] or t["항목"]: t for t in _tasks_of(plan)}
    assert any(t["task_id"] == user_task_id and t["출처"] == "user" for t in 행2.values())
    assert str(행2["비교견적준비"]["due_date"]) == "2026-09-01"


def test_상태_완료는_재판정이_되돌리지_않는다(plan):
    _sync(plan, [{"code": "부가세제외", "항목": "부가세 제외", "설명": "설명1"}])
    task_id = _tasks_of(plan)[0]["task_id"]
    client.patch(f"/api/plans/{plan}/tasks/{task_id}", json={"상태": "완료"})

    _sync(plan, [{"code": "부가세제외", "항목": "부가세 제외", "설명": "설명2(갱신)"}])
    행 = _tasks_of(plan)[0]
    assert 행["상태"] == "완료"


# ════════════════════════════════════════════════════════════════════
# 코드_매칭 — code 필드가 없을 때의 텍스트 대조 폴백
# ════════════════════════════════════════════════════════════════════

def test_코드_매칭_텍스트_폴백(plan):
    out = _sync(plan, [{"항목": "부가세 제외", "설명": "code 없이 항목만 왔다"}])
    assert out["코드매칭"] == 1
    행 = _tasks_of(plan)[0]
    assert 행["코드"] == "부가세제외"


# ════════════════════════════════════════════════════════════════════
# 사용자 직접 추가 · PATCH 목록 필터
# ════════════════════════════════════════════════════════════════════

def test_사용자_직접_추가는_출처가_강제된다(plan):
    r = client.post(f"/api/plans/{plan}/tasks", json={"항목": "회의비 단가 확인"})
    assert r.status_code == 201
    assert r.json()["출처"] == "user"
    assert r.json()["코드"] is None


def test_목록_plan_id_필터(plan):
    _sync(plan, [{"code": "부가세제외", "항목": "부가세 제외", "설명": "설명"}])
    r = client.get("/api/tasks", params={"plan_id": plan})
    assert r.status_code == 200
    out = r.json()
    assert out["건수"] == 1
    assert out["항목"][0]["plan_id"] == plan
