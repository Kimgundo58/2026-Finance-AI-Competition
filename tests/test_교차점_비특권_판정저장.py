# -*- coding: utf-8 -*-
"""회귀 — **비특권 롤 × 판정 경로**. 오늘 결함 여섯 건이 전부 이 교차점에 있었다.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_교차점_비특권_판정저장.py -q

🔴 왜 254건이 오늘 결함을 하나도 못 잡았나 (검증 ai-a3 진단)

    판정 경로를 태우는 테스트   → postgres(superuser) 로 돈다 → RLS 를 우회한다
    비특권 롤로 도는 RLS 테스트 → 판정 경로를 안 탄다
                              두 축이 **한 번도 안 만난다**

`test_rls_guc.py` 는 「DB 가 무엇을 막는가」, `test_rls_plumbing.py` 는 「GUC 가 어디서
오는가」, `test_rls_e2e.py` 는 「지출계획 저장이 도는가」를 잰다. 셋 다 판정기를 안 탄다.
이 파일이 그 교차점 하나를 잡는다.

🔴 **`rowcount` 나 「예외가 안 났다」로 통과시키면 안 된다. 그걸로 뚫린다.**

    abort 된 트랜잭션의 `COMMIT` 은 **조용히 ROLLBACK 으로 처리되고 예외를 안 던진다.**
    그래서 `orchestrate.py:778` 의 `conn.commit()` 이 «정상 반환» 하고, 코드는 저장했다고
    믿는다. 실측(비특권 롤):

        1) expense_plans INSERT (GUC 세움)   ✅ plan_id 나옴
        2) unmapped_premise INSERT           🔴 42501
        3) 앞선 INSERT 재조회                🔴 InFailedSqlTransaction
        4) conn.commit()                     ✅ «성공» — 예외 없음
        5) 🔴 새 트랜잭션에서 되읽기          → **0행**

    → **본체는 5) 다.** 1~4 만 보면 전부 초록이다. 「201·200 이 떠도 어느 기관 행이
      됐는지는 따로 재야 한다」(`test_rls_e2e`)와 같은 판단이다.

■ `tenant.unmapped_premise` 는 RLS 가 켜져 있는데 **정책이 하나도 없다**(실측: 정책 0개).
  정책이 없으면 비특권 롤에는 아무것도 안 통과한다 — superuser 는 우회하니 안 보인다.
■ LLM·GPU 를 안 태운다. `전제해소(기록=True)` 만으로 같은 자리에 닿는다.
■ 로컬 DB 에만 쓴다. 만든 행은 지운다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

psycopg = pytest.importorskip("psycopg")

import orchestrate                                  # noqa: E402

# 🔴 conftest 에 실 경로를 요구한다. 어제 이걸 빠뜨려 격리 테스트 4건이 «목» 으로 흘렀고,
#    목은 무조건 성공이라 RLS 를 구경도 안 한 채 초록이었다.
실DB = True

관리DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"
앱DSN = "postgresql://suddoe_app:devpw@localhost:5432/suddoe"

_미매핑전제 = [{"사실": "교차점 회귀용 전제 — 매핑 없음", "매핑": []}]


@pytest.fixture(scope="module")
def 판():
    if "@localhost" not in 관리DSN:
        pytest.skip("로컬 DB 가 아니다 — 이 파일은 운영에 쓰지 않는다")
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        # 🔴 skip 이 아니라 fail 이다. 롤이 없으면 이 파일은 «안 돌면서 통과» 한다
        assert c.execute("SELECT 1 FROM pg_roles WHERE rolname='suddoe_app'").fetchone(), (
            "suddoe_app 롤이 없다 — db/init/10_rls_guc.sql 을 로컬에 적용할 것. "
            "superuser 로만 돌면 RLS 가 우회되어 이 파일이 아무것도 못 잡는다")
        org = str(c.execute("SELECT org_id FROM tenant.orgs ORDER BY 기관명 LIMIT 1")
                  .fetchone()[0])
    만든: list[int] = []
    try:
        yield org, 만든
    finally:
        if 만든:
            with psycopg.connect(관리DSN, connect_timeout=5) as c:
                c.execute("DELETE FROM tenant.expense_plans WHERE plan_id = ANY(%s)", (만든,))
                c.commit()


def _판정_한건(org: str, 만든: list[int]) -> tuple[int, bool]:
    """비특권 롤로 「계획 저장 → 전제해소(기록) → commit」. (plan_id, commit이_예외를_던졌나)."""
    conn = psycopg.connect(앱DSN, connect_timeout=5)
    conn.autocommit = False            # 🔴 켜면 GUC 가 다음 문장에 이미 없다
    try:
        cur = conn.cursor()
        cur.execute("SELECT set_config('app.org_id', %s, true)", (org,))
        pid = cur.execute(
            "INSERT INTO tenant.expense_plans (org_id, 질문원문) "
            "VALUES (%s, '교차점 회귀') RETURNING plan_id", (org,)).fetchone()[0]
        만든.append(pid)
        try:
            orchestrate.전제해소(cur, _미매핑전제, org_id=org, 사업명="예비창업패키지",
                              비목="재료비", 기록=True)
        except Exception:                                     # noqa: BLE001
            # 🔴 삼키는 게 «이 테스트의 재현» 이다. 실 호출부(orchestrate.py:778 언저리)도
            #    예외를 잡아 강등사유에 적고 지나간다 — 그리고 아래 commit 이 «성공» 한다.
            pass
        try:
            conn.commit()
            return pid, False
        except Exception:                                     # noqa: BLE001
            return pid, True
    finally:
        conn.close()


def _있나(pid: int) -> int:
    """🔴 «새» 커넥션·«새» 트랜잭션에서 되읽는다. 같은 트랜잭션 안에서 재면
    안 써진 것도 써진 것처럼 보인다."""
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        return c.execute("SELECT count(*) FROM tenant.expense_plans WHERE plan_id = %s",
                         (pid,)).fetchone()[0]


# ── ① 잡을 것이 있는지부터 ──────────────────────────────────────────────────

def test_이_입력이_정말_미매핑으로_떨어진다(판):
    """🔴 이게 깨지면 아래 테스트는 **아무것도 안 잡으면서 초록**이 된다.
    미매핑이 안 나오면 `_unmapped_적재` 가 아예 안 불려서 42501 도 안 난다.
    `기록=False` 라 DB 에 안 쓴다 — 분류만 본다."""
    org, _ = 판
    conn = psycopg.connect(앱DSN, connect_timeout=5)
    try:
        r = orchestrate.전제해소(conn.cursor(), _미매핑전제, org_id=org,
                             사업명="예비창업패키지", 비목="재료비", 기록=False)
    finally:
        conn.close()
    assert len(r["미매핑"]) == 1, f"미매핑으로 안 떨어졌다: {r}"


def test_unmapped_premise_는_정책이_하나도_없다(판):
    """이 파일이 서 있는 전제. 정책이 붙으면 여기가 빨개지고, 그때 아래 xfail 도 다시 봐야 한다."""
    with psycopg.connect(관리DSN, connect_timeout=5) as c:
        n = c.execute("SELECT count(*) FROM pg_policies WHERE schemaname='tenant' "
                      "AND tablename='unmapped_premise'").fetchone()[0]
    assert n == 0, f"정책이 {n}개 생겼다 — 42501 이 더 안 날 수 있다. 이 파일을 다시 읽어라"


# ── ② 함정 자체 — 이건 psycopg/PG 의 성질이라 고쳐져도 안 변한다 ────────────

def test_commit_은_abort_된_트랜잭션에서도_예외를_안_던진다(판):
    """🔴 왜 「예외가 안 났다」로 통과시키면 안 되는지의 근거. 이 줄이 초록인 한
    `conn.commit()` 의 «성공» 은 저장의 증거가 아니다."""
    org, 만든 = 판
    pid, 던졌나 = _판정_한건(org, 만든)
    assert not 던졌나, (
        "commit 이 예외를 던졌다 — psycopg 동작이 바뀌었다면 위 xfail 의 근거를 다시 읽어라")
    assert isinstance(pid, int)


# ── ③ 🔴 본체 ───────────────────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason=
                   "미매핑전제 INSERT 가 42501 로 트랜잭션을 abort 시키고, 그 뒤 commit 이 "
                   "조용히 rollback 되어 «앞서 저장한 판정까지» 사라진다. savepoint 수정"
                   "(91fd456)이 아직 우리 트리에 안 왔다(#17). 들어오면 XPASS 로 빨개진다 "
                   "— 그때 이 표시를 떼라")
def test_미매핑전제가_있어도_판정이_실제로_저장된다(판):
    """🔴 이 파일의 본체. **되읽기까지 가야 잡힌다.**

    `_판정_한건` 은 예외를 삼키고 `commit()` 도 «성공» 한다 — 거기까지만 보면 전부 초록이다.
    실제로 남았는지는 «새 트랜잭션» 에서 세어야 안다.
    """
    org, 만든 = 판
    pid, _ = _판정_한건(org, 만든)
    assert _있나(pid) == 1, (
        f"plan_id={pid} 가 commit 뒤에 사라졌다 — 미매핑전제 INSERT(42501)가 트랜잭션을 "
        f"abort 시켰고 commit 이 조용히 rollback 됐다. 판정기는 «저장했다» 고 믿는다")
