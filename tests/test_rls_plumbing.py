# -*- coding: utf-8 -*-
"""회귀 — **`app.org_id` 가 «검증된 주체» 에서만 오는가.**

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_rls_plumbing.py -q

🔴 이 배관에서 틀릴 수 있는 방향은 둘인데 무게가 다르다.

    안 세운다   → 쓰기가 죽는다. **시끄럽게** 죽는다. 바로 안다
    잘못 세운다 → 클라이언트가 말한 org 를 DB 에 도장 찍는다. **조용하다.**
                  감사에는 「RLS 켜져 있음」으로 통과하는데 실제로는 아무것도 안 막는다

두 번째가 훨씬 나쁘고, 하필 **배선하기 더 쉬운 방향**이다 (`?org_id=` 는 이미 손에
있고 `주체` 는 미들웨어까지 가야 있다). 그래서 이 파일의 절반은 「안 세우는 것을
확인하는」 테스트다 — 통과가 «기능이 도는 것» 이 아니라 «값이 안 흘러든 것» 을 뜻한다.

■ **읽기만 한다.** `current_setting('app.org_id', true)` 을 되읽어 GUC 를 확인한다.
  INSERT 를 안 태우므로 DB 에 아무것도 안 남는다. 「무엇이 막히는가」는
  `test_rls_guc.py` 가 비특권 롤로 잰다 — 축이 다르다.
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

from server import _common, auth                  # noqa: E402
from server._common import DSN, _질의, org_고정    # noqa: E402

A = "426162ba-437b-57d0-be60-a492c64e4f57"
B = "0148ccca-dab8-5fc5-b961-bf5ffde23e85"

_읽기 = "SELECT current_setting('app.org_id', true)"


def _GUC() -> str | None:
    """`_질의` 를 «그대로» 태워서 GUC 를 되읽는다 — 배관을 우회하지 않는다."""
    행 = _질의(_읽기)
    return (행[0][0] or None) if 행 else None


# ── ① 배관 자체 ─────────────────────────────────────────────────────

def test_contextvar_가_없으면_GUC_도_없다():
    assert _GUC() is None


def test_org_고정하면_같은_트랜잭션에_GUC_가_걸린다():
    with org_고정(A):
        assert _GUC() == A


def test_org_고정을_빠져나오면_다시_없다():
    with org_고정(A):
        pass
    assert _GUC() is None


# ── ② HTTP 경로 — 여기가 「출처」를 정하는 자리다 ────────────────────

@pytest.fixture
def 앱():
    app = FastAPI()

    @app.get("/api/guc")
    def _g():                       # 🔴 동기 def — 스레드풀로 간다. contextvar 가
        return {"org": _GUC()}      #    거기까지 따라가는지도 같이 재는 셈이다

    app.add_middleware(auth.OrgId주입)
    return TestClient(app)


def _토큰(org):
    return {"Authorization": f"Bearer {auth.데모토큰_발급(org)[0]}"}


def test_검증된_토큰이면_GUC_가_그_org_로_선다(앱):
    assert 앱.get("/api/guc", headers=_토큰(A)).json()["org"] == A


def test_자기신고_org_id_는_GUC_를_못_세운다(앱):
    """🔴 **이 파일에서 가장 중요한 줄.** 여기가 빨개지면 RLS 가 장식이 된다 —
    클라이언트가 `?org_id=` 로 말한 값이 그대로 DB 정책의 기준이 된다."""
    r = 앱.get(f"/api/guc?org_id={B}")
    assert r.status_code == 200
    assert r.json()["org"] is None, (
        "자기신고 org_id 가 GUC 로 흘러들었다 — RLS 가 「클라이언트가 말한 대로」 "
        "판단하게 되어 아무것도 막지 못한다"
    )


def test_토큰이_있으면_자기신고를_같이_보내도_토큰이_이긴다(앱):
    assert 앱.get(f"/api/guc?org_id={B}", headers=_토큰(A)).json()["org"] == A


def test_게스트는_GUC_가_없다(앱):
    assert 앱.get("/api/guc").json()["org"] is None


def test_요청_사이에_GUC_가_새지_않는다(앱):
    """앞 요청이 세운 값이 다음 요청에 남으면 그 자체가 TENANT_LEAK 이다."""
    assert 앱.get("/api/guc", headers=_토큰(A)).json()["org"] == A
    assert 앱.get("/api/guc").json()["org"] is None
    assert 앱.get("/api/guc", headers=_토큰(B)).json()["org"] == B


def test_동시_요청이_서로_섞이지_않는다(앱):
    """동기 라우터는 AnyIO 워커 «스레드» 에서 돈다 — 스레드를 재사용하므로
    contextvar 가 스레드에 붙어 있으면 여기서 섞인다."""
    from concurrent.futures import ThreadPoolExecutor

    org들 = [A, B] * 4
    with ThreadPoolExecutor(8) as ex:
        결과 = list(ex.map(
            lambda o: 앱.get("/api/guc", headers=_토큰(o)).json()["org"], org들))
    assert 결과 == org들, f"동시 요청에서 org 가 섞였다: {결과}"


# ── ③ 커넥션 재사용 누수 — 이 과제의 절반 ───────────────────────────

def test_트랜잭션_한정_GUC_는_커밋_후_사라진다():
    """🔴 `set_config(..., true)` 를 고른 근거. 같은 커넥션을 다음 트랜잭션에서
    다시 써도 앞 값이 안 남는다 — 풀을 넣는 날 이게 참이어야 한다."""
    with psycopg.connect(DSN, connect_timeout=5) as c:
        c.execute("SELECT set_config('app.org_id', %s, true)", (A,))
        assert c.execute(_읽기).fetchone()[0] == A
        c.commit()
        assert (c.execute(_읽기).fetchone()[0] or None) is None
        c.rollback()


def test_세션_GUC_였다면_샜을_것이다():
    """🔴 반대쪽 증거. `local=false` 로 하면 «실제로» 샌다는 것을 보여 둔다 —
    안 그러면 위 테스트가 「그냥 통과하는 줄」로 보인다."""
    with psycopg.connect(DSN, connect_timeout=5) as c:
        c.execute("SELECT set_config('app.org_id', %s, false)", (A,))
        c.commit()
        assert c.execute(_읽기).fetchone()[0] == A, (
            "local=false 가 안 샜다 — 그러면 위 테스트가 무엇을 증명하는지 다시 봐야 한다")
        c.rollback()


def test_autocommit_이면_GUC_가_다음_문장에_이미_없다():
    """🔴 함정 기록. autocommit 을 켜면 트랜잭션 한정 GUC 가 즉시 사라져
    **격리가 아니라 «전부 차단»** 이 된다 (읽기는 조용히 0행). `_질의`/`_실행` 이
    autocommit 을 안 켜는 이유다."""
    with psycopg.connect(DSN, connect_timeout=5) as c:
        c.autocommit = True
        c.execute("SELECT set_config('app.org_id', %s, true)", (A,))
        assert (c.execute(_읽기).fetchone()[0] or None) is None


def test_현재는_커넥션을_재사용하지_않는다():
    """지금 누수가 «구조적으로» 없는 근거 — 호출마다 새 커넥션이다.
    🔴 이 줄이 빨개지면 풀이 들어온 것이다. 그때 위 두 테스트를 다시 읽어라."""
    본문 = (ROOT / "server" / "_common.py").read_text(encoding="utf-8")
    assert "ConnectionPool" not in 본문
