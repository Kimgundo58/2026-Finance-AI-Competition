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
    """앱에서 한 겹(폴백), DB 에서 한 겹(RLS) — 지금은 DB 가 막는다.

    🔴 **상태코드까지 못 박는다.** 전엔 여기가 503 「DB 연결 실패」였다 — DB 는 멀쩡한데
    RLS 가 문 것이었고, `_질의` 가 예외를 삼켜 «빈 리스트» 로 만들어 `if not 행:` 이
    접속 실패와 같은 문구를 냈다. 실서버에서 그걸로 한 번 헛짚었다(중앙 실측).
    `!= 201` 만 재면 그 뭉갬이 그대로 돌아와도 초록이다.
    """
    생성, _, A, _ = 판
    r = 생성(org_id=A, 토큰org=None)
    assert r.status_code == 401, (
        f"게스트 저장 거절이 401 이 아니다 ({r.status_code}) — 사유가 다시 뭉개졌다. "
        f"본문: {r.text[:200]}")
    사유 = r.json().get("detail", "")
    assert "DB" not in 사유, f"RLS 차단인데 DB 문제로 읽힌다: {사유!r}"
    for 조각 in ("42501", "sqlstate", "psycopg", "row-level", "expense_plans"):
        assert 조각 not in str(r.text), f"응답에 DB 내부 정보 «{조각}» 이 샌다"


def test_저장_실패_사유가_원인별로_갈린다(판):
    """🔴 ⓐ DB 다운 · ⓑ 권한/RLS · ⓒ 그 밖 — 셋이 한 문구면 아무도 원인을 못 찾는다.
    `auth._계정조회()` 가 「죽은 DB」와 「없는 계정」을 가른 것과 같은 축이다.
    여기서는 «접속 실패» 쪽만 잰다 (RLS 쪽은 위 게스트 테스트가 401 로 잡는다).
    """
    from server import _common

    생성, _, A, _ = 판
    옛 = _common.DSN
    _common.DSN = "postgresql://postgres:devpw@localhost:59999/suddoe"   # 아무도 안 듣는다
    try:
        r = 생성(org_id=None, 토큰org=A)
    finally:
        _common.DSN = 옛
    assert r.status_code == 503, f"접속 실패가 503 이 아니다 ({r.status_code})"
    for 조각 in ("59999", "devpw", "localhost", "psycopg", "timeout"):
        assert 조각 not in r.text, (
            f"503 사유에 접속정보 «{조각}» 이 실려 나간다 — 인증 «전» 응답이라 아무나 본다")


def test_본문에_org_id_가_없어도_토큰만으로_저장된다(판):
    """🔴 B2 회귀. 예전엔 여기가 **503** 이었다 — `_실_생성` 이 `body.org_id` 를 그대로
    INSERT 해서, 프론트가 본문에 org_id 를 안 실으면 `org_id=NULL` 로 들어가고
    GUC 가 A 여도 `NULL = A` 는 거짓이라 RLS 가 막았다. 토큰을 제대로 냈는데도
    저장이 안 되는 상태였다. 이제 주인은 본문이 아니라 «검증된 주체» 에서 온다.
    """
    생성, 조회, A, _ = 판
    r = 생성(org_id=None, 토큰org=A)
    assert r.status_code == 201, (
        f"본문 org_id 없이 저장이 안 된다 ({r.status_code}) — `_계획_주인` 이 주체를 "
        f"못 읽었거나 GUC 와 값이 어긋났다. 본문: {r.text[:200]}")
    assert 조회(r.json()["plan_id"], 토큰org=A).status_code == 200


def test_본문에_남의_org_를_실어도_내_org_로_저장된다(판):
    """🔴 `body.org_id` 축. 예전엔 「RLS 가 막아서 != 201」이었는데, 이제는
    **앱이 본문을 버려서 내 org 로 저장된다** — 막는 대신 «못 쓰게» 만든 것이다.
    그래서 상태코드만 보면 안 되고 «어느 기관 행이 됐는지» 를 조회로 되짚는다
    (201 만 재면 본문이 그대로 들어가도 초록이다).
    """
    생성, 조회, A, B = 판
    r = 생성(org_id=B, 토큰org=A)
    assert r.status_code == 201, f"토큰이 멀쩡한데 저장이 안 된다 ({r.status_code})"
    pid = r.json()["plan_id"]
    assert 조회(pid, 토큰org=A).status_code == 200, "본문 org 가 이겨서 남의 행이 됐다"
    assert 조회(pid, 토큰org=B).status_code == 404, (
        "본문에 실은 org_id 로 저장됐다 — 기관 사칭이 열려 있다 (TENANT_LEAK)")


# ── 읽기 격리 ───────────────────────────────────────────────────────

def test_남의_기관_계획은_404_다(판):
    생성, 조회, A, B = 판
    pid = 생성(org_id=A, 토큰org=A).json()["plan_id"]
    assert 조회(pid, 토큰org=A).status_code == 200
    assert 조회(pid, 토큰org=B).status_code == 404, "남의 기관 계획이 열렸다 — TENANT_LEAK"
    assert 조회(pid, 토큰org=None).status_code == 404
