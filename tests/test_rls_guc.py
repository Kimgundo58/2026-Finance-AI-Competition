# -*- coding: utf-8 -*-
"""회귀 — **RLS 가 실제로 무는 롤로 tenant 쓰기가 도는가.**

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_rls_guc.py -q

🔴 왜 이 파일이 필요한가 (2026-09-03)

`tenant.*` 는 전부 RLS 가 켜져 있는데 **로컬에서 한 번도 물린 적이 없다.**
로컬 `postgres` 가 superuser(`rolbypassrls=True`)라 정책을 통째로 우회하기 때문이다.
Cloud SQL 의 앱 계정(`suddoe_app`, rolsuper=False rolbypassrls=False)으로 바꾸는
순간 처음 물렸고, **쓰기가 전부 죽었다.**

🔴 **개수 검산으로는 절대 안 잡힌다.** 12개 테이블 행수가 다 맞아도 「누가 읽느냐」가
   다르면 답이 다르다. 닻이 달라야 잡히고, 그 닻이 «롤» 이다 — 그래서 이 파일은
   테스트용 비특권 롤을 만들어 그걸로 잰다.

■ **DB 에 아무것도 안 남긴다.** 롤 생성·GRANT·INSERT 를 전부 «한 트랜잭션» 안에서
  하고 마지막에 ROLLBACK 한다 (Postgres 는 CREATE ROLE 도 트랜잭션 대상이다).
  마지막 테스트가 잔여물 0을 검산한다.
■ 이 파일이 재는 것은 「DB 층이 무엇을 막는가」다. 앱 배관(contextvar → set_config)은
  `test_rls_plumbing.py` 가 잰다 — 축이 다르다.
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

from server._common import DSN                    # noqa: E402

_롤 = "suddoe_rls_test_tmp"

_준비 = f"""
CREATE ROLE {_롤} NOLOGIN NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA tenant, corpus TO {_롤};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA tenant TO {_롤};
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA tenant TO {_롤};
GRANT SELECT ON ALL TABLES IN SCHEMA corpus TO {_롤};
"""


class 판:
    """비특권 롤로 한 문장을 태우고 «통과했는가» 를 돌려준다. 실패해도 트랜잭션이
    안 죽게 SAVEPOINT 로 감싼다 — 한 트랜잭션 안에서 여러 건을 재야 하기 때문이다."""

    def __init__(self, cur):
        self.cur = cur

    def 태움(self, sql, 인자=()):
        self.cur.execute("SAVEPOINT sp")
        try:
            self.cur.execute(sql, 인자)
            값 = self.cur.fetchone() if self.cur.description else None
        except psycopg.errors.InsufficientPrivilege:
            self.cur.execute("ROLLBACK TO SAVEPOINT sp")
            return False, None
        self.cur.execute("ROLLBACK TO SAVEPOINT sp")   # 🔴 성공해도 되돌린다
        return True, 값

    def org세움(self, org_id):
        self.cur.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id or ""),))

    @property
    def current_org(self):
        return self.cur.execute("SELECT tenant.current_org()").fetchone()[0]


@pytest.fixture(scope="module")
def 판정판():
    conn = psycopg.connect(DSN, connect_timeout=5)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(_준비)
        orgs = cur.execute(
            "SELECT org_id FROM tenant.orgs ORDER BY 기관명 LIMIT 2").fetchall()
        if len(orgs) < 2:
            pytest.skip("tenant.orgs 에 기관이 2개 미만이라 격리를 잴 수 없다")
        cur.execute(f"SET LOCAL ROLE {_롤}")
        yield 판(cur), str(orgs[0][0]), str(orgs[1][0])
    finally:
        conn.rollback()          # 🔴 롤·GRANT·INSERT 전부 되돌린다
        conn.close()


# ── GUC 없음 — 지금 실서버가 처한 상태 ──────────────────────────────

def test_GUC_없으면_current_org_는_NULL(판정판):
    판, _, _ = 판정판
    판.org세움(None)
    assert 판.current_org is None


def test_GUC_없으면_지출계획_INSERT_가_막힌다(판정판):
    """🔴 이 파일의 핵심. `POST /api/plans` 가 여기 걸려 실서버에서 죽는다."""
    판, org, _ = 판정판
    판.org세움(None)
    통과, _ = 판.태움(
        "INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (%s, 't')", (org,))
    assert not 통과, "GUC 없이 INSERT 가 통과했다 — 정책이 사라졌거나 롤이 RLS 를 우회한다"


def test_GUC_없으면_읽기는_통과하는데_0행이다(판정판):
    """🔴 쓰기보다 이쪽이 위험하다. 예외가 안 나서 «데이터가 없다» 로 읽힌다 —
    `_질의` 가 실패를 빈 리스트로 삼키는 것과 정확히 같은 함정이 DB 층에도 있다."""
    판, _, _ = 판정판
    판.org세움(None)
    통과, 값 = 판.태움("SELECT count(*) FROM tenant.expense_plans")
    assert 통과 and 값[0] == 0


# ── GUC 세움 — 배관이 붙은 뒤 ───────────────────────────────────────

def test_GUC_를_세우면_내_org_로는_쓸_수_있다(판정판):
    판, org, _ = 판정판
    판.org세움(org)
    assert 판.current_org is not None
    통과, _ = 판.태움(
        "INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (%s, 't')", (org,))
    assert 통과, "GUC 를 세웠는데도 INSERT 가 막혔다 — 배관이 무의미해진다"


def test_GUC_를_세워도_남의_org_로는_못_쓴다(판정판):
    """🔴 DB 가 격리를 «문다». 앱에서 org 를 사칭해도 여기서 한 번 더 걸린다 —
    `routes_l3._업로드_주인` 이 막는 것과 같은 사고를 층을 달리해 막는다."""
    판, org, 남 = 판정판
    판.org세움(org)
    통과, _ = 판.태움(
        "INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (%s, 't')", (남,))
    assert not 통과, "남의 org 로 INSERT 가 통과했다 — RLS 가 쓰기를 안 막는다"


def test_corpus_는_GUC_없이도_읽힌다(판정판):
    """판정 검색(L1·L2)은 기관 축이 없다. 여기까지 막히면 판정 자체가 안 돈다."""
    판, _, _ = 판정판
    판.org세움(None)
    통과, 값 = 판.태움("SELECT count(*) FROM corpus.chunks")
    assert 통과 and 값[0] > 0


# ── 🔴 닭-달걀 두 건 — «지금 이렇다» 를 기록한다 (고치는 건 오너 결정) ──

def test_닭달걀_a_데모세션이_org_를_못_만든다(판정판):
    """`POST /api/demo/session` 은 `tenant.orgs` 를 만드는 요청인데 정책이
    `org_id = current_org()` 라 org 가 이미 있어야 통과한다.
    🔴 이 줄이 초록인 동안 심사용 데모 세션은 실서버에서 안 열린다."""
    판, _, _ = 판정판
    판.org세움(None)
    통과, _ = 판.태움("INSERT INTO tenant.orgs (기관명) VALUES ('테스트기관')")
    assert not 통과, "닭-달걀 ①이 풀렸다 — 정책이 바뀌었으면 이 테스트를 지워라"


def test_닭달걀_b_게스트_행은_개념이_성립하지_않는다(판정판):
    """`NULL = NULL` 은 참이 아니라 NULL 이고 RLS 는 참이 아닌 것을 안 통과시킨다.
    🔴 그래서 「게스트 행(org_id IS NULL)」은 GUC 를 안 세워도 «안 보인다» —
    `routes_plans._org조건` 이 게스트에게 `org_id IS NULL` 을 주는 것과 어긋난다."""
    판, _, _ = 판정판
    판.org세움(None)
    통과, 값 = 판.태움(
        "SELECT count(*) FROM tenant.expense_plans WHERE org_id IS NULL")
    assert 통과 and 값[0] == 0


def test_잔여물이_없다():
    """🔴 이 파일은 DB 에 아무것도 안 남긴다. 남으면 다음 실행이 다른 조건에서 돈다."""
    with psycopg.connect(DSN, connect_timeout=5) as c:
        남은롤 = c.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s",
                           (_롤,)).fetchone()[0]
    assert 남은롤 == 0, f"테스트 롤 {_롤} 이 DB 에 남았다"
