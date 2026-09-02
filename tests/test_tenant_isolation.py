# -*- coding: utf-8 -*-
"""회귀 — **기관 격리(TENANT_LEAK) 회귀.** 실 DB 로 태운다.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_tenant_isolation.py -q

🔴 왜 회귀 말고는 방법이 없나
`routes_plans._org조건()` 이 격리의 유일한 관문인데, **호출을 빠뜨린 자리는 아무도
못 잡는다.** 조건을 안 걸어도 테스트는 초록이고 화면에도 데이터가 나온다 — 그냥
남의 기관 것이 섞여 나올 뿐이다. 실패가 조용한 종류다.

`test_contract.py` 가 「표면이 계약대로인가」를 본다면 이 파일은 **「경계가 실제로
막혀 있는가」**를 본다.

🔴 **다만 이 파일이 «증명하지 못하는 것»을 먼저 읽어라 (2026-09-02 정밀검토에서 드러났다).**
`org_id` 는 **인증된 신원이 아니라 쿼리 파라미터, 즉 자기신고**다. 로그인이 없어서
서버는 호출자가 정말 그 기관인지 확인할 방법이 없다. 실측으로 확인했다 —
`GET /api/plans?org_id=<A의 uuid>` 를 아무나 치면 **A 의 계획과 금액이 그대로 200 으로
나오고**, `POST /api/plans/{A계획}/tasks?org_id=<A>` 도 **201 로 들어간다.**

즉 아래 테스트들이 보증하는 것은 **「자기 신원을 정직하게 밝힌 호출자에게 남의 것이
섞여 나오지 않는다」**(= 스코핑 필터가 샌 데 없이 걸린다) 까지다.
**「사칭하는 상대를 막는다」(= 접근 제어)는 이 파일이 증명하지 않는다.** 그건 인증의 몫이다.
지금 사칭을 어렵게 하는 건 org_id 가 122비트 UUID 이고 이를 노출하는 엔드포인트가
하나도 없다는 사실뿐이다 — 🔴 **화면 3(기관 선택)용 기관 목록 API 가 org_id 를 실어
내보내는 순간 그 방어는 사라진다.** 그 API 를 만들 때 이 주석을 다시 읽어라.

🔴 이 파일도 검증만 한다. 구멍을 찾으면 여기서 고치지 말고 해당 계통에서 고친다 —
`server/` 는 여러 계통이 함께 쓴다.

■ 세 주체로 태운다 — 기관 A · 기관 B · 게스트(`org_id` 없음)
■ 픽스처가 만든 것은 **만들 때 id 를 담아** teardown 에서 그 id 로만 지운다.
  (2026-09-01 에 정확히 이걸 빠뜨려 17행을 흘린 적이 있다. 같은 실수 안 한다.)
■ `tenant.decisions` 의 기존 행은 건드리지 않는다 — 이 테스트가 쓰는 판정 행은
  **직접 만들고 직접 지운다.**
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient          # noqa: E402

from server import persist, routes_plans           # noqa: E402
from server._common import DSN                     # noqa: E402
from server.main import app                        # noqa: E402

실DB = True                                         # conftest 가 MOCK=False 로 세운다

client = TestClient(app)


def _접속():
    import psycopg
    return psycopg.connect(DSN, connect_timeout=5)


def _db있음() -> bool:
    try:
        with _접속() as c:
            r = c.execute("SELECT to_regclass('tenant.expense_plans')").fetchone()
            n = c.execute("SELECT count(*) FROM tenant.orgs").fetchone()[0]
            return r[0] is not None and n >= 2
    except Exception:                                          # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _db있음(), reason="tenant DB 미기동 또는 orgs 2개 미만 — 격리 테스트 스킵")


class 세계상태:
    """픽스처가 만든 것과, 만들기 «전» 의 기준선을 함께 들고 있는다."""

    def __init__(self):
        self.A = self.B = None
        self.A계획: list[int] = []
        self.B계획: list[int] = []
        self.게스트계획: list[int] = []
        self.decision_id: int | None = None
        self.A문서 = self.B문서 = None
        self.기준선: dict = {}


def _통계(org_id) -> dict:
    p = {"org_id": org_id} if org_id else {}
    r = client.get("/api/plans", params=p)
    assert r.status_code == 200, r.text
    return r.json()["통계"]


def _목록id(org_id) -> set[int]:
    p = {"org_id": org_id, "크기": 100} if org_id else {"크기": 100}
    r = client.get("/api/plans", params=p)
    assert r.status_code == 200, r.text
    return {x["plan_id"] for x in r.json()["항목"]}


@pytest.fixture
def 세계():
    """A 2건 · B 1건 · 게스트 2건 + 각각의 할일 + 내 판정 1건. 전부 id 로 지운다."""
    s = 세계상태()
    with _접속() as conn:
        # 🔴 «가나다순 앞의 두 기관» 으로 고르지 않는다. 2026-09-02 에 기관 명부
        #    410곳이 적재되자 그 두 자리를 실제 기관이 차지했고, 그 기관엔 L3 문서가
        #    없어서 **L3 격리 테스트 3건이 조용히 skip 으로 바뀌었다.** 통과도 실패도
        #    아닌 채로 사라지는 게 제일 나쁘다 — 아무도 안 본다.
        #    L3 문서를 «가진» 기관을 고른다. 이 테스트가 재려는 게 바로 그거다.
        기관 = conn.execute(
            "SELECT org_id FROM tenant.orgs WHERE org_id IN "
            "  (SELECT org_id FROM tenant.l3_documents) "
            ' ORDER BY "기관명" LIMIT 2').fetchall()
        if len(기관) < 2:                       # 픽스처 미투입 — 넓혀서 잡는다
            기관 = conn.execute(
                'SELECT org_id FROM tenant.orgs ORDER BY "기관명" LIMIT 2').fetchall()
        s.A, s.B = str(기관[0][0]), str(기관[1][0])

        # 🔴 만들기 «전» 을 먼저 잰다. 다른 세션이 같은 DB 를 쓰므로 절대값이 아니라
        #    증분으로 대조해야 흔들리지 않는다.
        s.기준선 = {"A": _통계(s.A), "B": _통계(s.B), "게스트": _통계(None)}

        def 계획(org, 제목, 금액) -> int:
            r = conn.execute(
                'INSERT INTO tenant.expense_plans '
                '(org_id, "제목", "질문원문", "사업명", "확정비목", "금액") '
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING plan_id",
                (org, 제목, f"{제목} 써도 되나요?", "초기창업패키지", "기계장치", 금액),
            ).fetchone()[0]
            return r

        s.A계획 = [계획(s.A, "격리테스트 A-1", 1_000_000),
                  계획(s.A, "격리테스트 A-2", 2_000_000)]
        s.B계획 = [계획(s.B, "격리테스트 B-1", 500_000)]
        # 게스트 «둘» — 서로 보이는지 확인하려면 두 건이 필요하다
        s.게스트계획 = [계획(None, "격리테스트 게스트-1", 300_000),
                     계획(None, "격리테스트 게스트-2", 400_000)]

        for org, plan, 항목 in ((s.A, s.A계획[0], "격리테스트 A 할일"),
                              (s.B, s.B계획[0], "격리테스트 B 할일")):
            conn.execute(
                'INSERT INTO tenant.plan_tasks '
                '(org_id, plan_id, "출처", "구분", "항목", due_date) '
                "VALUES (%s,%s,'user','결제전',%s,'2026-09-30')",
                (org, plan, 항목))

        # 🔴 기존 73행은 안 건드린다. 내 판정 행을 만들어 쓰고 teardown 에서 지운다.
        s.decision_id = conn.execute(
            'INSERT INTO tenant.decisions ("질문원문", "판정", "요약") '
            "VALUES ('격리테스트 판정', '가능', '격리테스트') RETURNING decision_id"
        ).fetchone()[0]

        문서 = conn.execute(
            "SELECT org_id, doc_id FROM tenant.l3_documents").fetchall()
        for org, doc in 문서:
            if str(org) == s.A:
                s.A문서 = str(doc)
            elif str(org) == s.B:
                s.B문서 = str(doc)
        conn.commit()

    yield s

    # ── teardown — 만든 id «만» 지운다 ────────────────────────────
    with _접속() as conn:
        전체 = s.A계획 + s.B계획 + s.게스트계획
        if 전체:
            # plan_tasks 는 plan_id FK 가 ON DELETE CASCADE 라 같이 지워지지만,
            # 여기서 온 것을 여기서 지운다는 게 눈에 보이도록 명시한다.
            conn.execute("DELETE FROM tenant.plan_tasks WHERE plan_id = ANY(%s)", (전체,))
            conn.execute("DELETE FROM tenant.expense_plans WHERE plan_id = ANY(%s)", (전체,))
        if s.decision_id is not None:
            conn.execute("DELETE FROM tenant.plan_tasks WHERE decision_id = %s",
                         (s.decision_id,))
            conn.execute("DELETE FROM tenant.decisions WHERE decision_id = %s",
                         (s.decision_id,))
        conn.commit()


# ════════════════════════════════════════════════════════════════════
# ⓪ 검사기에 이빨이 있는가 — 관문을 무력화하면 실제로 새는가
# ════════════════════════════════════════════════════════════════════

def test_관문을_무력화하면_남의_것이_보인다(세계, monkeypatch):
    """🔴 이게 없으면 아래 테스트들이 «격리된다» 가 아니라 «아무것도 안 본다» 와
    구별되지 않는다. `_org조건` 을 통과시키도록 바꿔 **실제로 새는지** 먼저 본다."""
    보임_정상 = _목록id(세계.A)
    assert 세계.B계획[0] not in 보임_정상

    monkeypatch.setattr(routes_plans, "_org조건",
                        lambda org_id, 별칭="p": ("TRUE", ()))
    보임_무력화 = _목록id(세계.A)
    assert 세계.B계획[0] in 보임_무력화, (
        "관문을 무력화했는데도 남의 계획이 안 보인다 — 이 테스트는 격리를 "
        "검증하고 있는 게 아니라 다른 이유로 초록이다")


# ════════════════════════════════════════════════════════════════════
# ① 목록 — 남의 계획이 안 뜬다
# ════════════════════════════════════════════════════════════════════

def test_목록에_남의_기관_계획이_없다(세계):
    A보임, B보임 = _목록id(세계.A), _목록id(세계.B)
    assert set(세계.A계획) <= A보임, "자기 계획이 자기 목록에 없다"
    assert set(세계.B계획) <= B보임
    assert not (set(세계.B계획) & A보임), "🔴 B 의 계획이 A 의 목록에 샜다"
    assert not (set(세계.A계획) & B보임), "🔴 A 의 계획이 B 의 목록에 샜다"


def test_게스트_목록에_기관_계획이_없다(세계):
    게스트 = _목록id(None)
    assert not (set(세계.A계획 + 세계.B계획) & 게스트), \
        "🔴 org_id 없이 부르면 기관 계획이 보인다"
    assert set(세계.게스트계획) <= 게스트


# ════════════════════════════════════════════════════════════════════
# ② 통계 — 🔴 목록을 걸러도 집계에서 새면 남의 «규모» 가 샌다
# ════════════════════════════════════════════════════════════════════

def _맞대보기(이름: str, api호출, sql호출) -> None:
    """API 집계와 «같은 경계» SQL 집계를 맞댄다 — 동시 작업에 안 흔들리게.

    🔴 이 DB 는 여러 세션이 같이 쓴다. 두 값을 따로 재면 그 «사이» 에 남이 행을
    만들거나 지워서, 격리가 멀쩡한데도 어긋난 것처럼 보인다(실제로 두 번 겪었다).
    그래서 **SQL → API → SQL** 순으로 재고 API 가 두 SQL 값 사이에 있으면 통과한다.
    아무도 안 건드린 조용한 순간엔 두 SQL 이 같으므로 **정확한 등호와 같은 강도**다.
    누수가 있으면 API 가 두 값보다 «크게» 나오므로 여전히 잡힌다.
    """
    앞 = sql호출()
    api = api호출()
    뒤 = sql호출()
    낮, 높 = min(앞, 뒤), max(앞, 뒤)
    assert 낮 <= api <= 높, (
        f"{이름}: API {api} 가 같은 경계 SQL [{낮}, {높}] 밖이다 — "
        f"API 가 크면 남의 것이 섞인 것이다")


def _SQL건수(org_id) -> int:
    with _접속() as c:
        if org_id is None:
            행 = c.execute("SELECT count(*) FROM tenant.expense_plans "
                           "WHERE org_id IS NULL").fetchone()
        else:
            행 = c.execute("SELECT count(*) FROM tenant.expense_plans "
                           "WHERE org_id = %s::uuid", (org_id,)).fetchone()
    return int(행[0])


def test_통계_건수가_자기_것만_센다(세계):
    """🔴 증분으로 재지 않는다 — 금액합계와 같은 이유다.

    처음엔 `after − before == 내가 만든 수` 로 썼는데, 다른 세션이 같은 기관에
    계획을 만들면 흔들린다(실제로 흔들렸다). **같은 경계로 짠 SQL 집계와 맞대보는
    것**이 원래 재려던 성질이고, 누수가 있으면 API 쪽이 커진다."""
    for 이름, org in (("A", 세계.A), ("B", 세계.B), ("게스트", None)):
        _맞대보기(이름 + " 건수", lambda: _통계(org)["전체"], lambda: _SQL건수(org))
    assert set(세계.A계획) <= _목록id(세계.A)
    assert set(세계.B계획) <= _목록id(세계.B)


def _SQL금액합(org_id) -> float:
    """API 와 «같은 경계» 로 DB 에서 직접 집계한다."""
    with _접속() as c:
        if org_id is None:
            행 = c.execute('SELECT coalesce(sum("금액"),0) FROM tenant.expense_plans '
                           "WHERE org_id IS NULL").fetchone()
        else:
            행 = c.execute('SELECT coalesce(sum("금액"),0) FROM tenant.expense_plans '
                           "WHERE org_id = %s::uuid", (org_id,)).fetchone()
    return float(행[0])


def test_통계_금액합계에_남의_금액이_안_섞인다(세계):
    """건수가 맞아도 합계가 새는 회귀가 따로 있다 — 집계 쿼리는 WHERE 를 빠뜨리기 쉽다.

    🔴 증분(after − before)으로 재지 않는다. **게스트 버킷은 다른 세션도 쓰는 공용
    자리**라 그 사이에 행이 늘거나 줄면 증분이 흔들린다(실제로 700,000 을 기대한
    자리에 600,000 이 나왔다 — 격리가 깨진 게 아니라 동시 작업이 있었던 것).
    대신 **API 집계와 같은 경계로 DB 를 직접 집계해 맞대본다** — 이게 원래 재려던
    성질이다. 누수가 있으면 API 쪽이 더 커진다."""
    for 이름, org in (("A", 세계.A), ("B", 세계.B), ("게스트", None)):
        _맞대보기(이름 + " 금액합계",
                lambda: _통계(org)["금액합계"], lambda: _SQL금액합(org))

    # 내가 만든 금액이 내 집계에 실제로 반영돼 있는지 — 「항상 0」 으로 통과하는 걸 막는다
    assert _통계(세계.A)["금액합계"] >= 3_000_000.0


def test_통계는_목록과_같은_경계를_쓴다(세계):
    """목록엔 안 뜨는데 통계엔 세어지는 어긋남을 잡는다.

    🔴 목록과 통계를 **한 번의 응답에서** 꺼내 맞댄다. 따로 두 번 부르면 그 사이에
    다른 세션이 행을 만들어 둘이 어긋날 수 있다 — 격리가 아니라 시점 차이로 빨개진다."""
    for org, 내계획 in ((세계.A, 세계.A계획), (세계.B, 세계.B계획)):
        p = {"org_id": org, "크기": 100}
        j = client.get("/api/plans", params=p).json()
        보임 = {x["plan_id"] for x in j["항목"]}
        assert set(내계획) <= 보임
        assert j["통계"]["전체"] == j["건수"] == len(보임), (
            f"같은 응답 안에서 통계 {j['통계']['전체']} · 건수 {j['건수']} · "
            f"항목 {len(보임)} 이 어긋난다 — 목록과 집계가 다른 경계를 쓰고 있다")


# ════════════════════════════════════════════════════════════════════
# ③ 상세 — 남의 id 로 부르면 404. 🔴 403 이 아니다
# ════════════════════════════════════════════════════════════════════

def test_남의_계획_상세는_404_이고_403_이_아니다(세계):
    r = client.get(f"/api/plans/{세계.A계획[0]}", params={"org_id": 세계.B})
    assert r.status_code != 403, \
        "403 은 «그 id 는 존재한다» 를 알려준다 — 존재 자체가 새면 안 된다"
    assert r.status_code == 404, r.text
    assert set(r.json()) == {"오류", "상태"}


def test_게스트도_남의_계획_상세를_못_본다(세계):
    assert client.get(f"/api/plans/{세계.A계획[0]}").status_code == 404


def test_자기_계획_상세는_보인다(세계):
    """위 404 가 «격리» 때문인지 «원래 안 보임» 때문인지 가른다."""
    r = client.get(f"/api/plans/{세계.A계획[0]}", params={"org_id": 세계.A})
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] == 세계.A계획[0]


# ════════════════════════════════════════════════════════════════════
# ④ 할일 목록
# ════════════════════════════════════════════════════════════════════

def _할일항목(org_id) -> set[str]:
    p = {"org_id": org_id} if org_id else {}
    r = client.get("/api/tasks", params=p)
    assert r.status_code == 200, r.text
    return {t["항목"] for t in r.json()["항목"]}


def test_할일_목록이_기관을_안_넘는다(세계):
    A, B, 게 = _할일항목(세계.A), _할일항목(세계.B), _할일항목(None)
    assert "격리테스트 A 할일" in A
    assert "격리테스트 B 할일" in B
    assert "격리테스트 B 할일" not in A, "🔴 B 의 할일이 A 에게 보인다"
    assert "격리테스트 A 할일" not in B, "🔴 A 의 할일이 B 에게 보인다"
    assert not ({"격리테스트 A 할일", "격리테스트 B 할일"} & 게), \
        "🔴 게스트에게 기관 할일이 보인다"


# ════════════════════════════════════════════════════════════════════
# ⑤ 판정 영속화 — 남의 계획에 판정이 붙으면 안 된다
# ════════════════════════════════════════════════════════════════════

def _계획행(plan_id: int):
    with _접속() as c:
        return c.execute(
            "SELECT latest_decision_id, 상태 FROM tenant.expense_plans WHERE plan_id = %s",
            (plan_id,)).fetchone()


def _판정행(decision_id: int):
    with _접속() as c:
        return c.execute("SELECT plan_id FROM tenant.decisions WHERE decision_id = %s",
                         (decision_id,)).fetchone()


def test_남의_계획에는_판정이_저장되지_않는다(세계):
    """🔴 반환값만 보면 안 된다 — DB 를 직접 조회해서 «안 바뀌었는지» 확인한다.

    `persist._실_저장` 은 `expense_plans` UPDATE 의 rowcount 로 소유를 판별한다.
    여기가 뚫리면 남의 계획에 판정이 붙는다."""
    앞_계획 = _계획행(세계.A계획[0])
    앞_판정 = _판정행(세계.decision_id)

    결과 = persist.판정_저장(plan_id=세계.A계획[0], body=None, out={},
                          org_id=세계.B, decision_id=세계.decision_id)

    assert 결과["저장"] is False, f"🔴 남의 계획에 판정이 저장됐다: {결과}"
    assert _계획행(세계.A계획[0]) == 앞_계획, "🔴 남의 계획 행이 바뀌었다"
    assert _판정행(세계.decision_id) == 앞_판정, "🔴 판정 행의 plan_id 가 바뀌었다"
    assert 앞_판정[0] is None


def test_자기_계획에는_판정이_저장된다(세계):
    """위 테스트가 «항상 False» 라서 통과하는 게 아님을 보인다."""
    결과 = persist.판정_저장(plan_id=세계.A계획[0], body=None, out={},
                          org_id=세계.A, decision_id=세계.decision_id)
    assert 결과["저장"] is True, f"자기 계획인데 저장이 안 됐다: {결과}"
    assert _계획행(세계.A계획[0])[0] == 세계.decision_id
    assert _판정행(세계.decision_id)[0] == 세계.A계획[0]


def test_게스트가_기관_계획에_판정을_못_붙인다(세계):
    앞 = _계획행(세계.A계획[0])
    결과 = persist.판정_저장(plan_id=세계.A계획[0], body=None, out={},
                          org_id=None, decision_id=세계.decision_id)
    assert 결과["저장"] is False, f"🔴 게스트가 기관 계획에 판정을 붙였다: {결과}"
    assert _계획행(세계.A계획[0]) == 앞


# ════════════════════════════════════════════════════════════════════
# ⑥ L3 문서
# ════════════════════════════════════════════════════════════════════

# 🔴 여기서 내 예측이 틀렸다. 기록해 둔다.
#
#   예측: `_실_상태` 의 `(%s IS NULL OR org_id = %s)` 는 org_id 를 안 보내면
#         필터가 꺼지니 게스트가 남의 문서를 읽는다 — 격리 구멍이다.
#   실측: 게스트도 404 다. 대신 **주인도 404 다.**
#
#   원인은 격리가 아니라 SQL 이다. `%s IS NULL` 에만 쓰인 파라미터는 Postgres 가
#   타입을 못 정한다 — psycopg 가 세 파라미터를 따로 보내서 $2 가 `$2 IS NULL`
#   한 곳에만 나오기 때문이다:
#       IndeterminateDatatype: could not determine data type of parameter $2
#   `_질의` 가 예외를 조용히 삼켜 빈 리스트를 주고, 그걸 코드가 404 로 바꾼다.
#
#   → 즉 **실 경로에서 `GET /api/l3/{doc_id}` 는 누구에게나 항상 404 다.**
#     격리 구멍이 아니라 그 엔드포인트가 통째로 안 도는 것이고, 격리는
#     «막혀서» 가 아니라 «아무것도 안 나와서» 지켜지는 것처럼 보인다.
#     고칠 자리는 `routes_l3.py` 다.
#
# ✅ 2026-09-02 (BE) 고쳤다 — 위 진단에 **한 곳을 더 좁혔다.**
#   위 기록은 "쿼리가 매번 죽는다"고 적었는데, 실제로 태워 보니 갈린다:
#     org_id 를 `uuid.UUID` 객체로 넘기면 **성공**하고, **str** 로 넘기면 죽는다.
#   HTTP 쿼리 파라미터는 항상 str 이라 실서버에서는 100% 죽은 게 맞다 — 결론은
#   같지만, 원인을 «파라미터 타입 문맥» 으로 좁혀야 고칠 자리가 보인다.
#
#   고친 방식: `_org조건()` 파이썬 분기(`routes_plans` 의 관용구)로 바꿨다.
#   캐스트(`%s::text IS NULL`)도 실측으로 되살아나지만 그 길은 **위 «예측» 이
#   말한 구멍을 진짜로 연다** — 게스트가 남의 기관 L3 를 읽는다(태워서 확인).
#   분기 쪽은 `l3_documents.org_id` 가 NOT NULL 이라 게스트가 0행 → 404 다.
#   그래서 아래 세 테스트가 이제 «아무것도 안 나와서» 가 아니라 «막혀서» 통과한다.

def _L3_조회가능() -> bool:
    """주인이 자기 문서를 실제로 읽을 수 있나. 못 읽으면 격리 검증이 무의미하다."""
    with _접속() as c:
        행 = c.execute("SELECT doc_id, org_id FROM tenant.l3_documents LIMIT 1").fetchall()
    if not 행:
        return False
    doc, org = str(행[0][0]), str(행[0][1])
    return client.get(f"/api/l3/{doc}", params={"org_id": org}).status_code == 200


def test_L3_조회경로가_살아있다(세계):
    if not 세계.A문서:
        pytest.skip("A 의 L3 문서가 없다")
    r = client.get(f"/api/l3/{세계.A문서}", params={"org_id": 세계.A})
    assert r.status_code == 200, r.text


def test_남의_L3_문서는_404(세계):
    """🔴 조회 경로가 죽어 있으면 이 테스트는 «격리된다» 가 아니라 «아무것도 안 나온다»
    를 보는 것이라, 통과해도 아무 뜻이 없다. 그래서 살아있을 때만 센다."""
    if not (세계.A문서 and 세계.B문서):
        pytest.skip("l3_documents 에 A·B 각각의 문서가 없다")
    if not _L3_조회가능():
        pytest.skip("L3 조회 경로가 통째로 404 라 격리를 검증할 수 없다 (위 xfail 참조)")
    r = client.get(f"/api/l3/{세계.A문서}", params={"org_id": 세계.B})
    assert r.status_code == 404, f"🔴 B 가 A 의 L3 문서를 읽었다: {r.text[:200]}"


def test_게스트가_기관_L3_문서를_못_읽는다(세계):
    """계획은 `org_id=None` 을 «게스트 버킷» 으로 좁히는데 L3 는 «필터 해제» 로 넓다 —
    같은 `None` 이 한쪽에선 좁히고 한쪽에선 연다. 지금은 SQL 고장에 가려 안 보이지만,
    **고장을 고치면 이 구멍이 드러난다.** 그때 이 테스트가 잡는다."""
    if not 세계.A문서:
        pytest.skip("A 의 L3 문서가 없다")
    if not _L3_조회가능():
        pytest.skip("L3 조회 경로가 통째로 404 라 격리를 검증할 수 없다 (위 xfail 참조)")
    assert client.get(f"/api/l3/{세계.A문서}").status_code == 404


# ════════════════════════════════════════════════════════════════════
# ⑦ 🔴 쓰기 경로 — 여기가 제일 넓게 뚫려 있다
#     `POST/PATCH .../tasks` 와 `tasks:sync` 는 **org_id 를 아예 안 받는다.**
#     호출자 신원이 없으니 아무나 남의 계획에 할일을 꽂고 남의 할일을 바꾼다.
#     아래 셋은 «그래야 한다» 를 적어둔 것이라 지금은 xfail 이다.
#
# ✅ 2026-09-02 (BE) 셋 다 닫았다 — xfail 을 걷었다.
#   세 엔드포인트가 `org_id` 쿼리 파라미터를 받고, 계획/할일을 `_org조건()` 으로
#   좁힌 뒤에야 쓴다. 소유가 안 맞으면 404 다.
#   🔴 같이 고친 것: `persist.py` 의 판정→저장 경로가 `동기화()` 를 **위치인자 2개**로
#      부르고 있었다. org_id 를 안 넘기면 게스트 조건으로 떨어져 기관 계획의 저장이
#      통째로 깨진다 — 그래서 그 호출부에 org_id 를 같이 실었다.
#   ⚠️ 이건 «격리가 된다» 의 증거이지 «판정이 맞다» 의 증거가 아니다.
# ════════════════════════════════════════════════════════════════════

def test_남의_계획에_할일을_못_넣는다(세계):
    r = client.post(f"/api/plans/{세계.A계획[0]}/tasks",
                    params={"org_id": 세계.B},
                    json={"항목": "격리테스트 침입", "구분": "결제전", "유형": "기타"})
    assert r.status_code == 404, f"🔴 B 가 A 의 계획에 할일을 꽂았다: {r.status_code}"


def test_남의_할일을_못_고친다(세계):
    with _접속() as c:
        행 = c.execute("SELECT task_id FROM tenant.plan_tasks WHERE plan_id = %s",
                       (세계.A계획[0],)).fetchone()
    if not 행:
        pytest.skip("A 계획에 할일이 없다")
    r = client.patch(f"/api/plans/{세계.A계획[0]}/tasks/{행[0]}",
                     params={"org_id": 세계.B}, json={"상태": "완료"})
    assert r.status_code == 404, f"🔴 B 가 A 의 할일을 고쳤다: {r.status_code}"


def test_남의_계획을_동기화하지_못한다(세계):
    r = client.post(f"/api/plans/{세계.A계획[0]}/tasks:sync",
                    params={"org_id": 세계.B}, json={"해야할일": []})
    assert r.status_code == 404, f"🔴 B 가 A 의 계획을 동기화했다: {r.status_code}"


# ════════════════════════════════════════════════════════════════════
# ⑧ 게스트 버킷 — 🔴 «현재 상태» 를 못 박는 것이지 «보장» 이 아니다
# ════════════════════════════════════════════════════════════════════

def test_게스트끼리는_지금_서로_보인다_이것은_의도가_아니라_현재_상태다(세계):
    """🔴 **이 테스트는 보장이 아니다. 결함을 못 박아 둔 것이다.**

    `org_id IS NULL` 이 단일 버킷이라 게스트 A 가 만든 계획을 게스트 B 가 본다.
    구조적이고, 알려져 있고, 사용자에게 올라가 있다.

    **로그인이 닫히면 이 테스트가 빨개진다 — 그게 목적이다.** 빨개지면 지우지 말고
    «게스트끼리 안 보인다» 로 뒤집어라. 그때 이 파일이 «바뀌었다» 를 알리는 신호가 된다.
    지금 이걸 안 적어두면 다음 사람이 이 동작을 보장된 것으로 읽는다."""
    게스트 = _목록id(None)
    assert set(세계.게스트계획) <= 게스트, (
        "게스트끼리 안 보이게 바뀌었다 — 결함이 고쳐진 것이다. "
        "이 테스트를 «서로 안 보인다» 로 뒤집어라")
