# -*- coding: utf-8 -*-
"""회귀 — **심사위원 데모 진입이 비특권 롤로 실제로 도는가.**

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_데모세션_비특권.py -q

🔴 이 파일이 막는 두 가지는 «증상이 정반대» 다.

    ① `POST /api/demo/session` 이 503      → 시끄럽다. 프론트가 바로 막힌다
    ② `_낡은데모정리()` 가 0건을 지운다     → **조용하다.** 예외도 없고 rowcount 0 이
                                             정상으로 돌아온다. 상한(200)이 찰 때까지
                                             아무도 모르고, 찬 뒤엔 **전부 429** 다

둘 다 뿌리가 같다 — `tenant.orgs` 정책이 `org_id = tenant.current_org()` 인데
로컬 `postgres` 는 superuser 라 **RLS 를 통째로 우회한다.** 그래서 이 파일은
`suddoe_app` 으로만 돈다. superuser 로 돌면 넷 다 그냥 통과한다.

🔴 격리는 **개수로 재지 않는다.** 두 세션 모두 「5건」이라, 공용 버킷이어도 개수는
   같다. `plan_id` 교집합을 본다 — 「개수 검산은 통과하는데 내용이 틀린다」.

■ 로컬 DB 에만 쓴다. 이 파일이 만든 `[데모]` org 만 골라 지운다(앞뒤 차집합).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

psycopg = pytest.importorskip("psycopg")

from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402

from server import _common, auth, routes_orgs, routes_plans   # noqa: E402

실DB = True                                       # 🔴 conftest 에 실 경로를 요구한다

관리DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"
앱DSN = "postgresql://suddoe_app:devpw@localhost:5432/suddoe"


def _데모org들() -> set[str]:
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        return {str(r[0]) for r in c.execute(
            "SELECT org_id FROM tenant.orgs WHERE 기관명 LIKE %s",
            (f"{routes_orgs.데모접두}%",)).fetchall()}


@pytest.fixture
def 판():
    if "@localhost" not in 관리DSN:
        pytest.skip("로컬 DB 가 아니다 — 이 파일은 운영에 쓰지 않는다")
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        assert c.execute("SELECT 1 FROM pg_roles WHERE rolname='suddoe_app'").fetchone(), (
            "suddoe_app 롤이 없다 — db/init/10_rls_guc.sql 을 로컬에 적용할 것. "
            "superuser 로 돌면 이 파일은 넷 다 «안 돌면서 통과» 한다")
    앞 = _데모org들()
    옛 = (_common.DSN, routes_plans.MOCK, routes_orgs.데모_상한)
    _common.DSN, routes_plans.MOCK = 앱DSN, False

    app = FastAPI()
    app.include_router(routes_orgs.router)
    app.include_router(routes_plans.router)
    app.add_middleware(auth.OrgId주입)
    try:
        yield TestClient(app)
    finally:
        _common.DSN, routes_plans.MOCK, routes_orgs.데모_상한 = 옛
        생긴 = _데모org들() - 앞                  # 🔴 내가 만든 것만 지운다
        if 생긴:
            with psycopg.connect(관리DSN, connect_timeout=5) as c:
                c.execute("DELETE FROM tenant.orgs WHERE org_id = ANY(%s)", (list(생긴),))
                c.commit()


def _계획(cl, 토큰) -> set[int]:
    j = cl.get("/api/plans", headers={"Authorization": f"Bearer {토큰}"}).json()
    return {x["plan_id"] for x in (j.get("항목") or [])}


# ── ① 열리는가 ──────────────────────────────────────────────────────

def test_비특권_롤로_데모세션이_열리고_홈이_차_있다(판):
    """🔴 전엔 503 이었다. `tenant.orgs` 는 org 를 «만드는» 요청인데 정책이
    `org_id = current_org()` 라 GUC 없이는 42501 이고, `_실행` 이 -1 로 삼켜
    「DB 연결 실패」로 나갔다. uuid 를 먼저 뽑아 GUC 에 세우고 «같은» uuid 로 INSERT 한다.
    """
    r = 판.post("/api/demo/session")
    assert r.status_code == 200, f"데모 세션이 안 열린다 ({r.status_code}): {r.text[:200]}"
    계획 = _계획(판, r.json()["access_token"])
    assert len(계획) == 5, f"홈이 비었다 — 샘플 계획 {len(계획)}건 (5건이어야 한다)"


def test_샘플에_판정이_안_붙어_있다(판):
    """심사위원이 직접 눌러 보는 게 시연 동선이다 — 이미 판정된 화면을 주면 그게 사라진다."""
    판.post("/api/demo/session")
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        n = c.execute(
            "SELECT count(*) FROM tenant.expense_plans p JOIN tenant.orgs o USING (org_id) "
            "WHERE o.기관명 LIKE %s AND (p.latest_decision_id IS NOT NULL OR p.상태 <> 'draft')",
            (f"{routes_orgs.데모접두}%",)).fetchone()[0]
    assert n == 0, f"판정이 붙었거나 상태가 draft 가 아닌 샘플이 {n}건 있다"


# ── ② 격리 — 🔴 개수로는 안 갈린다 ──────────────────────────────────

def test_두_데모세션은_서로의_계획을_못_본다(판):
    """🔴 **개수를 세면 못 잡는다.** 공용 버킷이어도 양쪽 다 5건이다.
    `데모세션()` 이 클릭마다 새 org 를 발급하는 설계(=(b)안)가 실제로 사는지는
    `plan_id` 교집합으로만 확인된다."""
    a = _계획(판, 판.post("/api/demo/session").json()["access_token"])
    b = _계획(판, 판.post("/api/demo/session").json()["access_token"])
    assert len(a) == len(b) == 5
    assert not (a & b), (
        f"두 데모 세션이 같은 계획을 본다 (교집합 {sorted(a & b)}) — "
        f"«게스트 공용 버킷» 이 이름만 바꿔 돌아왔다")
    assert not _계획(판, ""), "게스트가 데모 계획을 본다"


# ── ③ 🔴 조용한 실패 — 이 파일의 핵심 ───────────────────────────────

def test_낡은데모정리가_실제로_지운다(판):
    """🔴 **「예외가 안 났다」로 통과시키면 안 되는 자리.**

    전엔 `LIKE '[데모] %'` 로 한 방에 지웠는데, 정책이 `org_id = current_org()` 라
    비특권 롤에서는 **0행이 지워지고 예외도 안 난다.** `_실행` 은 rowcount 0 을
    정상으로 돌려준다. 실측 「TTL 지난 2건 → 정리 후 2건」.
    그대로 두면 `데모_상한` 이 차고 그 뒤 **모든 데모 세션이 429** 다.
    """
    판.post("/api/demo/session")
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        c.execute("UPDATE tenant.orgs SET created_at = now() - interval '48 hours' "
                  "WHERE 기관명 LIKE %s", (f"{routes_orgs.데모접두}%",))
        c.commit()
    전 = len(_데모org들())
    assert 전 >= 1
    routes_orgs._낡은데모정리()
    후 = len(_데모org들())
    assert 후 == 0, (
        f"TTL 지난 데모 org 를 {전 - 후}/{전} 건만 지웠다 — RLS 가 DELETE 를 막고 있다. "
        f"조용해서 상한이 찰 때까지 안 보인다")


def test_상한이_이미_차_있어도_TTL_지난_것이면_스스로_풀린다(판):
    """🔴 배포 직후 시나리오. 고치기 «전» 에 조용히 안 지워진 것들이 쌓여 있으면
    고친 코드를 올려도 첫 요청부터 429 인가? — TTL 이 지난 것이면 아니다(실측).
    아직 살아 있는 것들이면 429 가 맞다 (자원 상한이 하는 일이다)."""
    routes_orgs.데모_상한 = 3
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        for _ in range(3):
            o = str(uuid.uuid4())
            c.execute("INSERT INTO tenant.orgs (org_id, 기관명, 사업명, created_at) "
                      "VALUES (%s, %s, %s, now() - make_interval(hours => 48))",
                      (o, f"{routes_orgs.데모접두}{o[:8]}", ["예비창업패키지"]))
        c.commit()
    assert 판.post("/api/demo/session").status_code == 200, (
        "쌓인 데모 org 가 상한을 채웠고 전부 TTL 이 지났는데도 429 다 — "
        "정리가 자리를 못 비운다")
