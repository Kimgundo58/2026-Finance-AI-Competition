# -*- coding: utf-8 -*-
"""E2E — 계획 생성 → 판정 저장 → 화면(목록·상세·탭·통계) 반영. **[레인 A]**

ai-14 배정 (2026-09-01). 오늘 이전엔 `tenant.decisions` 73행 / `expense_plans` 0행 —
두 테이블이 한 번도 이어진 적이 없어 목록 탭 3개(확인필요·위험·특이사항없음)가
구조적으로 영원히 빈 화면이었다. `persist.py` 로 이어졌다는 걸 이 테스트가 증명한다.
`test_contract.py`(레인 D)는 계약 표면(엔드포인트·이벤트·스키마)만 본다 — 겹치지 않는다.

🔴 LLM 을 부르지 않는다 — `persist.판정_저장()` 을 직접 호출하고 `out` 은 실제 판정
   출력 모양(해야할일[].code 포함, 2026-09-01 실측 146/146 전부 code 를 싣는다)으로 준다.
🔴 픽스처가 만든 plan_id·decision_id 만 지운다. `tenant.decisions` 기존 행은 안 건드린다
   (골든셋 평가가 그 테이블을 분모로 쓴다).
🔴 DB 가 없으면 skip — 실패로 만들지 않는다 (다른 사람 로컬에서 빨간 줄이 뜨면 안 된다).

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_e2e_flow.py -v
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

os.environ["SUDDOE_MOCK"] = "0"          # conftest 가 이 표식으로 실 DB 모드를 잡는다

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import pytest
from fastapi.testclient import TestClient  # noqa: E402

from server import persist                 # noqa: E402
from server._common import _질의, _실행, 할일유형_ENUM, 탭_판정  # noqa: E402
from server.main import app                 # noqa: E402

client = TestClient(app)


def _db_있음() -> bool:
    행 = _질의("SELECT to_regclass('tenant.expense_plans')")
    return bool(행) and 행[0][0] is not None


pytestmark = pytest.mark.skipif(not _db_있음(), reason="tenant DB 미기동 — E2E 스킵")


@pytest.fixture
def 정리():
    plan_ids: list[int] = []
    decision_ids: list[int] = []
    yield plan_ids, decision_ids
    if plan_ids:
        # plan_tasks 는 expense_plans ON DELETE CASCADE, decisions.plan_id 는 SET NULL —
        # 명시적으로도 지운다(교차 세션이 동시에 도는 동안 순서를 눈으로 보이게).
        _실행("DELETE FROM tenant.plan_tasks WHERE plan_id = ANY(%s)", (plan_ids,))
        _실행("DELETE FROM tenant.expense_plans WHERE plan_id = ANY(%s)", (plan_ids,))
    if decision_ids:
        _실행("DELETE FROM tenant.decisions WHERE decision_id = ANY(%s)", (decision_ids,))


def _계획_만들기(**override) -> dict:
    기본 = dict(사업명="초기창업패키지", 품목=f"e2e-{uuid.uuid4().hex[:8]}",
                금액=1_000_000.0, 용도="테스트 용도", 확정비목="기계장치")
    기본.update(override)
    r = client.post("/api/plans", json=기본)
    assert r.status_code == 201, r.text
    return r.json()


def _decision_만들기(계획: dict, 판정: str, 해야할일: list[dict]) -> int:
    """orchestrate.decisions_적재() 가 판정 시점에 만드는 행을 흉내낸다. plan_id 는 안 넣는다
    — 그게 `persist._실_저장` 이 잇는 대상이다."""
    from psycopg.types.json import Json
    행 = _질의(
        """
        INSERT INTO tenant.decisions
            (org_id, 사업명, 질문원문, 비목, 금액, 판정, 요약, 신뢰등급, 해야할일)
        VALUES (NULL, %s, %s, %s, %s, %s, %s, 'B', %s)
        RETURNING decision_id
        """,
        (계획["사업명"], 계획["질문원문"], 계획["확정비목"], 계획["금액"], 판정,
         f"{판정} 테스트 요약", Json(해야할일)),
    )
    assert 행
    return 행[0][0]


def _판정출력(판정: str, 해야할일: list[dict]) -> dict:
    """`main.py::_실_판정()` 이 orchestrate 반환을 옮겨 담는 모양과 동일하게."""
    return {
        "판정": 판정, "요약": f"{판정} 테스트 요약", "해야할일": 해야할일,
        "인용": [], "전제": [], "신뢰등급": "B", "버전스탬프": "e2e-test-v1",
        "참조사슬": [],
    }


_해야할일_고정 = [
    {"항목": "비교견적 3곳 이상 확보하세요", "설명": None, "code": "비교견적준비"},
    {"항목": "사무실이 전대차 계약이 아닌지 확인하세요", "설명": None, "code": "전대차아님확인"},
    {"항목": "거래처 사업자등록증과 통장사본을 받아두세요", "설명": None, "code": "거래처증빙수취"},
]

# 4-way → 배지 탭 (`_common.탭_판정` 과 같은 매핑을 여기서 다시 명시해 회귀를 잡는다)
_기대_탭 = {"가능": "특이사항없음", "조건부": "확인필요", "불가": "위험", "판단불가": "확인필요"}


def test_판정4way_저장_그리고_탭에_반영(정리):
    plan_ids, decision_ids = 정리
    plan_id별_판정: dict[int, str] = {}

    for 판정 in ("가능", "조건부", "불가", "판단불가"):
        # ① POST /api/plans → 201, draft, latest_decision_id 없음
        계획 = _계획_만들기()
        plan_ids.append(계획["plan_id"])
        assert 계획["상태"] == "draft"
        assert 계획["latest_decision_id"] is None
        assert 계획["판정"] is None

        # (orchestrate 가 이미 만들어 둔) decisions 행을 흉내낸다
        decision_id = _decision_만들기(계획, 판정, _해야할일_고정)
        decision_ids.append(decision_id)

        # ② 판정 → 저장. LLM 안 부른다 — persist.판정_저장() 직접 호출
        out = _판정출력(판정, _해야할일_고정)
        저장 = persist.판정_저장(계획["plan_id"], body=None, out=out, org_id=None,
                               decision_id=decision_id)

        # ③ 반환 모양이 계약 §6-1 과 같은지
        assert 저장["저장"] is True
        assert 저장["decision_id"] == decision_id
        assert 저장["plan_id"] == 계획["plan_id"]
        assert set(저장["할일"]) == {"생성", "갱신", "보존_user", "보존_날짜수정",
                                    "코드매칭", "코드미상"}
        assert 저장["할일"]["생성"] == len(_해야할일_고정)
        assert 저장["할일"]["코드매칭"] == len(_해야할일_고정)   # 전부 code 를 실었다

        # ④ DB 를 직접 조회 — API 응답만 믿지 않는다
        plan_row = _질의(
            "SELECT 상태, latest_decision_id FROM tenant.expense_plans WHERE plan_id = %s",
            (계획["plan_id"],),
        )[0]
        assert plan_row[0] == "judged"
        assert plan_row[1] == decision_id
        decision_row = _질의(
            "SELECT plan_id FROM tenant.decisions WHERE decision_id = %s", (decision_id,)
        )[0]
        assert decision_row[0] == 계획["plan_id"]

        # ⑦ 코드가 실린 해야할일의 유형이 `할일유형_ENUM` 안인지 (레인 B 가 지금 유형 소스를
        #    바꾸는 중이라 값 자체는 단정하지 않는다)
        유형들 = _질의(
            'SELECT "유형" FROM tenant.plan_tasks WHERE plan_id = %s', (계획["plan_id"],)
        )
        assert 유형들, "plan_tasks 가 하나도 안 생겼다"
        for (유형,) in 유형들:
            assert 유형 in 할일유형_ENUM, f"할일유형_ENUM 밖 값: {유형}"

        # ⑤ GET /api/plans/{id} 에 판정이 실렸는지
        상세 = client.get(f"/api/plans/{계획['plan_id']}").json()
        assert 상세["판정"] == 판정
        assert 상세["판정상세"] is not None
        assert 상세["판정상세"]["판정"] == 판정

        plan_id별_판정[계획["plan_id"]] = 판정

        # 멱등성 — 같은 decision_id 로 두 번째 저장해도 plan_tasks 가 안 늘어난다
        저장2 = persist.판정_저장(계획["plan_id"], body=None, out=out, org_id=None,
                                decision_id=decision_id)
        assert 저장2["저장"] is True
        개수 = _질의(
            "SELECT count(*) FROM tenant.plan_tasks WHERE plan_id = %s", (계획["plan_id"],)
        )[0][0]
        assert 개수 == len(_해야할일_고정), "같은 판정을 두 번 저장했는데 plan_tasks 가 늘었다"

    # ⑥ 탭 배지 매핑 — 4-way 전부가 기대한 탭에서만 보인다
    표: dict[str, str] = {}
    for plan_id, 판정 in plan_id별_판정.items():
        기대탭 = _기대_탭[판정]
        표[판정] = 기대탭
        for 탭 in 탭_판정:
            항목목록 = client.get("/api/plans", params={"탭": 탭}).json()["항목"]
            보임 = plan_id in {p["plan_id"] for p in 항목목록}
            if 탭 == "전체":
                assert 보임, f"plan_id={plan_id}({판정}) 이 «전체» 탭에 없다"
            elif 탭 == "점검전":
                assert not 보임, f"plan_id={plan_id}({판정}) 이 판정이 있는데 «점검전» 에 떴다"
            elif 탭 == 기대탭:
                assert 보임, f"plan_id={plan_id}({판정}) 이 기대한 탭 «{기대탭}» 에 없다"
            else:
                assert not 보임, f"plan_id={plan_id}({판정}) 이 엉뚱한 탭 «{탭}» 에도 떴다"

    # 알 수 없는 탭은 422 (오늘 고친 것 — 회귀 방지)
    assert client.get("/api/plans", params={"탭": "없는탭"}).status_code == 422

    print("\n4-way → 탭 매핑:", 표)

    # ⑧ 통계는 필터(탭) 적용 전 전체 기준 — 탭을 바꿔도 숫자가 안 흔들린다
    통계들 = [client.get("/api/plans", params={"탭": t}).json()["통계"] for t in 탭_판정]
    assert all(t == 통계들[0] for t in 통계들), "통계가 탭에 따라 흔들렸다 — 필터전 전체 기준이 아니다"
    assert 통계들[0]["확인필요"] >= 2   # 조건부 1 + 판단불가 1 (이 테스트가 만든 것만으로도)
    assert 통계들[0]["위험"] >= 1
    assert 통계들[0]["특이사항없음"] >= 1


if __name__ == "__main__":
    if not _db_있음():
        print("tenant DB 미기동 — 스킵")
    else:
        pids: list[int] = []
        dids: list[int] = []
        try:
            test_판정4way_저장_그리고_탭에_반영((pids, dids))
            print("  ok  test_판정4way_저장_그리고_탭에_반영")
        finally:
            if pids:
                _실행("DELETE FROM tenant.plan_tasks WHERE plan_id = ANY(%s)", (pids,))
                _실행("DELETE FROM tenant.expense_plans WHERE plan_id = ANY(%s)", (pids,))
            if dids:
                _실행("DELETE FROM tenant.decisions WHERE decision_id = ANY(%s)", (dids,))
        print("전부 통과")
