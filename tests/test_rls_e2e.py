# -*- coding: utf-8 -*-
"""회귀 — **비특권 롤로 지출계획 저장이 실제로 도는가.** (e2e · 실 DB)

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_rls_e2e.py -q

🔴 이 파일만이 증명하는 것

`test_rls_guc.py` 는 «DB 가 무엇을 막는가» 를 SQL 로 재고, `test_rls_plumbing.py` 는
«GUC 가 어디서 오는가» 를 읽기로 잰다. 둘 다 통과해도 **HTTP 요청이 실제로 저장되는지는
말해주지 않는다** — 미들웨어·라우터·`_질의` 가 한 줄로 이어져야 비로소 도는데,
그 이음매는 태워 봐야 안다. 실제로 A/B 로 확인한 값이 이렇다:

    같은 요청 · 같은 롤 · 같은 DB
      `_org_세우기` 켜짐  → 201
      `_org_세우기` 끔    → 503     ← 배관을 빼면 즉시 죽는다

■ 🔴 **로컬 DB 에만 쓴다** (오너 승인 2026-09-03). 만든 행은 fixture 가 지운다.
  운영(Cloud SQL)은 읽기 전용이다 — 이 파일은 `localhost` 가 아니면 돌지 않는다.
■ 앱은 `main.py` 가 아니라 «복제» 로 세운다. `main.py` 의 미들웨어 배선은 import
  시점의 MOCK 값에 걸려 있어 파일 실행 순서에 결과가 달라진다 (실측 — `test_auth_cors`
  가 그걸로 한 번 속았다). 복제하면 이 파일의 답이 순서와 무관해진다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

psycopg = pytest.importorskip("psycopg")

from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402

from server import _common, auth, routes_plans    # noqa: E402

# 🔴 **`conftest` 에 실 경로를 요구한다.** 이걸 빠뜨렸다가 한 번 속았다 —
#    conftest 의 autouse fixture 가 매 테스트 직전에 `routes_plans.MOCK` 을 True 로
#    되돌려서 요청이 «목» 으로 흘렀고, 목은 무조건 201 이라 **격리 테스트 4건이
#    「막혀야 하는데 저장됐다」로 빨개졌다.** RLS 는 구경도 안 한 상태였다.
#    (fixture 안에서 MOCK 을 덮는 것만으로는 부족하다 — conftest 가 뒤에 다시 덮는다)
실DB = True

관리DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"
앱DSN = "postgresql://suddoe_app:devpw@localhost:5432/suddoe"


def _로컬인가(dsn: str) -> bool:
    return "@localhost" in dsn or "@127.0.0.1" in dsn


@pytest.fixture(scope="module")
def 판():
    """비특권 롤로 도는 복제 앱 + 정리. 🔴 만든 행은 반드시 지운다."""
    if not _로컬인가(관리DSN):
        pytest.skip("로컬 DB 가 아니다 — 이 파일은 운영에 쓰지 않는다")
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        있나 = c.execute("SELECT 1 FROM pg_roles WHERE rolname='suddoe_app'").fetchone()
        # 🔴 skip 이 아니라 fail 이다. 이 롤이 없다는 건 「환경이 다르다」가 아니라
        #    `db/init/10_rls_guc.sql` 이 안 돌았다는 뜻이고, 그 상태로 초록을 보면
        #    RLS 회귀가 통째로 «안 돌면서 통과» 한다. 그게 이 파일이 막으려는 사고다.
        assert 있나, ("suddoe_app 롤이 없다 — db/init/10_rls_guc.sql 을 로컬에 적용할 것. "
                      "superuser 로만 돌면 RLS 가 우회되어 이 파일이 아무것도 못 잡는다")
        orgs = c.execute(
            "SELECT org_id FROM tenant.orgs ORDER BY 기관명 LIMIT 2").fetchall()
    if len(orgs) < 2:
        pytest.skip("tenant.orgs 에 기관이 2개 미만이라 격리를 잴 수 없다")
    A, B = str(orgs[0][0]), str(orgs[1][0])

    옛DSN, 옛MOCK = _common.DSN, routes_plans.MOCK
    _common.DSN, routes_plans.MOCK = 앱DSN, False

    app = FastAPI()
    app.include_router(routes_plans.router)
    app.add_middleware(auth.OrgId주입)
    만든: list[int] = []

    def 계획생성(org_id=None, 토큰org=None):
        body = {"사업명": "예비창업패키지", "품목": "노트북", "금액": 1500000, "용도": "개발용"}
        if org_id:
            body["org_id"] = org_id
        머리 = ({"Authorization": f"Bearer {auth.데모토큰_발급(토큰org)[0]}"}
                if 토큰org else {})
        r = TestClient(app).post("/api/plans", json=body, headers=머리)
        if r.status_code == 201:
            만든.append(r.json()["plan_id"])
        return r

    def 계획조회(plan_id, 토큰org=None):
        머리 = ({"Authorization": f"Bearer {auth.데모토큰_발급(토큰org)[0]}"}
                if 토큰org else {})
        return TestClient(app).get(f"/api/plans/{plan_id}", headers=머리)

    try:
        yield 계획생성, 계획조회, A, B
    finally:
        _common.DSN, routes_plans.MOCK = 옛DSN, 옛MOCK
        if 만든:
            with psycopg.connect(관리DSN, connect_timeout=5) as c:
                c.execute("DELETE FROM tenant.expense_plans WHERE plan_id = ANY(%s)",
                          (만든,))
                c.commit()


# ── 쓰기 ────────────────────────────────────────────────────────────

def test_검증된_토큰이면_지출계획이_저장된다(판):
    """🔴 이 파일의 핵심. 이 줄이 빨개지면 **실서버가 읽기만 도는 서비스**가 된다."""
    생성, _, A, _ = 판
    r = 생성(org_id=A, 토큰org=A)
    assert r.status_code == 201, (
        f"비특권 롤로 저장이 안 된다 ({r.status_code}) — GUC 배관이 끊겼다. "
        f"본문: {r.text[:200]}")
    assert r.json()["plan_id"]


def test_게스트는_저장하지_못한다(판):
    """앱에서 한 겹(폴백), DB 에서 한 겹(RLS) — 지금은 DB 가 막는다."""
    생성, _, A, _ = 판
    assert 생성(org_id=A, 토큰org=None).status_code != 201


def test_토큰이_있어도_남의_org_로는_저장하지_못한다(판):
    """🔴 `body.org_id` 축이다. 미들웨어는 쿼리스트링만 갈아끼우므로 본문에는 못 닿는데,
    **RLS 가 DB 층에서 막는다** — 층을 달리한 두 번째 방어선이 실제로 무는지 잰다."""
    생성, _, A, B = 판
    assert 생성(org_id=B, 토큰org=A).status_code != 201


# ── 읽기 격리 ───────────────────────────────────────────────────────

def test_남의_기관_계획은_404_다(판):
    생성, 조회, A, B = 판
    pid = 생성(org_id=A, 토큰org=A).json()["plan_id"]
    assert 조회(pid, 토큰org=A).status_code == 200
    assert 조회(pid, 토큰org=B).status_code == 404, "남의 기관 계획이 열렸다 — TENANT_LEAK"
    assert 조회(pid, 토큰org=None).status_code == 404


# ── 🔴 지금 «안 되는» 것 — 라우터 소유 세션에 넘긴다 ────────────────

def test_본문에_org_id_가_없으면_토큰이_있어도_저장이_안_된다(판):
    """🔴 «막는다» 가 아니라 «지금 이렇다» 를 기록하는 줄이다.

    `routes_plans._실_생성` 이 `body.org_id` 를 그대로 INSERT 한다. 프론트가 본문에
    org_id 를 안 실으면 org_id=NULL 로 들어가고, GUC 가 A 여도 `NULL = A` 는 거짓이라
    RLS 가 막는다 → 503. **토큰을 제대로 냈는데도 저장이 안 된다.**

    고칠 자리는 `routes_plans.py`(다른 세션 소유)다 — `body.org_id` 를 버리고
    `scope["suddoe_주체"]` 를 쓰면 닫힌다 (`routes_l3._업로드_주인` 과 같은 처방).
    🔴 그쪽이 고쳐지면 이 테스트는 빨개진다. 그때 이 함수를 지우고 위
       `test_검증된_토큰이면...` 에 「본문 org_id 없이도 201」을 더해라.
    """
    생성, _, A, _ = 판
    r = 생성(org_id=None, 토큰org=A)
    assert r.status_code != 201, (
        "본문 org_id 없이 저장이 됐다 — 라우터가 주체를 쓰도록 고쳐진 것 같다. "
        "docstring 대로 이 테스트를 정리할 것")
