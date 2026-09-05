# -*- coding: utf-8 -*-
"""Q5 항목6 — 경상국립대(창업중심대학) 데모 데이터를 심는다.

`GET /api/profile` 전부 0·null · `GET /api/tasks` 0건인 건 API 고장이 아니라
데이터가 없어서다(QA_레인_프롬프트_0904.md ## Q5 §5). org 는 Q1 이 이미 세웠다
(경상국립대학교 창업중심대학사업단, cfeba091-251a-5ae4-8cc9-88c6e6679440).

🔴 **`SUDDOE_MOCK=0` 로 돈다 — `SUDDOE_MOCK=1` 로 처음 짰다가 빈손으로 끝난 자리다.**
   `POST /api/plans`·`GET /api/profile` 는 `if MOCK: ...` 로 **DB 를 아예 안 보고
   인메모리 목 데이터만 돌려준다**(routes_plans.py:175 · main.py:816). MOCK=1 로
   돌리면 plan_id 가 나오고 200 도 뜨지만 그건 파이썬 리스트에 쌓인 것이지
   `tenant.expense_plans` 행이 아니다 — 되읽기(새 연결)에서 0건으로 드러났다
   (2026-09-04 1차 시도 실측). 그래서 MOCK=0 으로 **진짜 DB 경로**를 타고, GPU 가
   없으니 `orchestrate.판정` 만 콜백으로 갈아끼워 4-way 를 순서대로 낸다
   (tests/test_계약_키집합.py 오케물리기 와 같은 기법).

🔴 **`f_profile` 만 예외다 — 쓰기 API 가 아직 없다.** `server/main.py::_실_프로필_저장()`
가 `"저장": False, "이유": "f_profile 쓰기 경로 미배선 (tenant 소유는 E 세션)"` 을
그대로 돌려준다. 그래서 여기만 직접 INSERT 한다 — 코드를 새로 배선하지 않는다
(그건 E 세션 소관), 데이터만 심는다.

실행 (로컬):
    PYTHONIOENCODING=utf-8 python scripts/archive/seed/seed_demo_expense.py
운영에는 안 친다 — 중앙 승인 후 중앙이 돌린다.
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import os
import sys
import types
from pathlib import Path

os.environ["SUDDOE_MOCK"] = "0"          # 🔴 진짜 DB 경로. GPU 는 아래서 콜백으로 대체한다
os.environ.setdefault("SUDDOE_DEMO_SECRET", "local-verify-secret")

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import llm_schema  # noqa: E402
from server import main as 서버  # noqa: E402
from server import auth  # noqa: E402
from server._common import DSN  # noqa: E402

ORG = "cfeba091-251a-5ae4-8cc9-88c6e6679440"   # 경상국립대학교 창업중심대학사업단
사업명 = "창업중심대학"

# ── 오케를 스키마 그대로 갈아끼운다 (GPU 없이 4-way 를 순서대로) ──────────
_다음_판정 = {"값": "가능"}

_요약 = {"가능": "1인 1대 한도 내에서 구매 가능합니다.",
        "조건부": "사전승인을 받으면 구매할 수 있습니다.",
        "불가": "개인 용도 지출로 인정되지 않습니다.",
        "판단불가": "판단에 필요한 정보가 더 필요합니다."}


def _가짜_판정(질문, **kw):
    판정 = _다음_판정["값"]
    인용 = llm_schema.인용(s번호="S14", doc_id="D1", 조번호="제39조",
                          조제목="기계장치", 원문="…", extraction="text")
    전제 = llm_schema.전제(사실="협약상 참여인력이다", 근거조항="S14",
                          매핑=["F4.역할"], 미충족시="불가")
    r = llm_schema.최종응답(
        판정=판정, 요약=_요약[판정],
        해야할일=([{"항목": "비교견적 2곳 이상 확보", "설명": "50만원 이상 구매는 비교견적을 남깁니다."},
                 {"항목": "자산 등록", "설명": "취득일로부터 30일 이내에 자산으로 등록합니다."}]
                if 판정 in ("가능", "조건부") else []),
        인용목록=[인용] if 판정 != "판단불가" else [],
        전제목록=[전제], 신뢰등급="B" if 판정 == "조건부" else "A",
        버전스탬프="데모 시드", 참조사슬=[])
    반환 = r.to_dict()

    # 🔴 실 오케(`scripts/orchestrate.py::decisions_적재`)는 판정 시점에 `tenant.decisions`
    #    행을 직접 INSERT 하고 `decision_id` 를 반환값에 심는다(orchestrate.py:863) —
    #    `persist._실_저장()` 은 그 행을 **UPDATE 로 잇기만** 하지 INSERT 는 안 한다
    #    (persist.py 머리말 「decisions 에 INSERT 하지 않는다」). 그래서 가짜 오케도
    #    이 INSERT 를 대신 해줘야 한다 — 안 하면 `decision_id=None` → 저장이
    #    "decision_id 없음" 으로 거부된다(1차 시도 실측).
    #    🔴 2026-09-04 (ai-c4) — GUC 를 세운다. 원래 「로컬 postgres(superuser)로 도니
    #       RLS 안 걸린다」는 전제였는데, 운영 seed 는 비특권 롤 suddoe_app 으로 돈다 —
    #       GUC 없이 이 INSERT 를 하면 RLS(org_id=current_org())가 막아 0행 → decision_id
    #       None → 저장 거부다(정확히 CLAUDE.md 「로컬 superuser 라 안 보이던 게 운영에서
    #       터진다」 자리). org_id 는 kw 로 이미 아니까 이 연결에도 직접 세운다.
    정규화결과 = kw.get("정규화결과") or {}
    with psycopg.connect(DSN) as conn:
        conn.execute("SELECT set_config('app.org_id', %s, true)", (kw.get("org_id"),))
        row = conn.execute(
            """INSERT INTO tenant.decisions
                   (org_id, "사업명", 질문원문, 정규화, 비목, 금액, 판정, 신뢰등급,
                    인용, 해야할일, 전제, 코퍼스버전)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING decision_id""",
            (kw.get("org_id"), kw.get("사업명"), 질문,
             psycopg.types.json.Json(정규화결과), kw.get("_비목고정"),
             정규화결과.get("금액"), 판정, r.신뢰등급,
             psycopg.types.json.Json(반환.get("인용목록", [])),
             psycopg.types.json.Json(반환.get("해야할일", [])),
             psycopg.types.json.Json(반환.get("전제목록", [])),
             "데모시드-불변코퍼스아님"),
        ).fetchone()
        conn.commit()
    반환["decision_id"] = row[0]
    return 반환


_가짜_orch = types.ModuleType("orchestrate")
_가짜_orch.판정 = _가짜_판정
sys.modules["orchestrate"] = _가짜_orch
서버._워치독.게이트 = lambda: None

c = TestClient(서버.app)
토큰, _ = auth.데모토큰_발급(ORG)
H = {"Authorization": f"Bearer {토큰}"}


def _확인(설명, 조건):
    print(("  OK  " if 조건 else "  FAIL ") + 설명)
    return 조건


# ── ① f_profile — 유일하게 쓰기 API 가 없어 직접 INSERT 한다 ─────────────
print("=== ① f_profile ===")
with psycopg.connect(DSN) as conn:
    conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
    기존 = conn.execute(
        'SELECT profile_id FROM tenant.f_profile WHERE org_id=%s AND "사업명"=%s',
        (ORG, 사업명)).fetchone()
    if 기존:
        print("이미 있음 — 건너뜀:", 기존[0])
    else:
        row = conn.execute(
            """INSERT INTO tenant.f_profile
                   (org_id, "사업명", "협약시작일", "협약종료일",
                    "정부지원_현금", "자기부담_현금")
               VALUES (%s,%s,'2024-03-01','2025-02-28', 60000000, 15000000)
               RETURNING profile_id""",
            (ORG, 사업명)).fetchone()
        conn.commit()
        print("생성:", row[0])

# ── ② 지출계획 — 4-way 를 하나씩 (실경로: POST /api/plans → POST /api/judge) ──
시나리오 = [
    dict(목="가능", 품목="노트북", 금액=1_800_000, 용도="현장 코딩용 노트북 구매",
         확정비목="기계장치"),
    dict(목="조건부", 품목="전시부스 참가비", 금액=3_000_000, 용도="창업경진대회 부스 참가",
         확정비목="여비"),
    dict(목="불가", 품목="대표자 개인 차량 보험료", 금액=800_000,
         용도="대표자 개인 차량 보험 갱신", 확정비목="지급수수료"),
    dict(목="판단불가", 품목="시제품 재료", 금액=2_400_000,
         용도="시제품 제작용 원자재 구매", 확정비목="재료비"),
]

with psycopg.connect(DSN) as conn:
    conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
    _기존건수 = conn.execute(
        "SELECT count(*) FROM tenant.expense_plans WHERE org_id=%s", (ORG,)).fetchone()[0]

만든_plan_id: list[int] = []
if _기존건수:
    # 🔴 이 스크립트는 재실행에 안전하지 않다 — `POST /api/plans` 는 멱등이 아니라
    #    다시 돌리면 plan 이 또 생긴다. 이미 심어져 있으면 건너뛴다(지우고 다시
    #    심고 싶으면 DELETE 후 돌릴 것 — 자동으로는 안 지운다, 데모 데이터 지우는
    #    쪽이 더 위험하다).
    print(f"\n=== ② 건너뜀 — 이미 {_기존건수}건 있음 (재실행은 중복을 만든다) ===")
else:
    print("\n=== ② 지출계획 (4-way 배지 하나씩) ===")
    for s in 시나리오:
        plan = c.post("/api/plans", headers=H, json={
            "사업명": 사업명, "품목": s["품목"], "금액": s["금액"], "용도": s["용도"],
            "확정비목": s["확정비목"], "org_id": ORG,
        })
        if not _확인(f"POST /api/plans({s['품목']}) 201", plan.status_code == 201):
            print("   응답:", plan.text[:300])
            continue
        plan_id = plan.json()["plan_id"]

        _다음_판정["값"] = s["목"]
        jr = c.post("/api/judge", headers=H, json={
            "정규화": {"_원문": s["용도"], "품목": s["품목"], "금액": s["금액"], "용도": s["용도"]},
            "확정비목": s["확정비목"], "사업명": 사업명, "plan_id": plan_id, "org_id": ORG,
        })
        if not _확인(f"  POST /api/judge({s['목']}) 200", jr.status_code == 200):
            print("   응답:", jr.text[:300])
            continue
        # 🔴 judge() 의 SSE 200 은 「응답을 만들었다」의 증거지 「저장됐다」의 증거가 아니다
        #    (`_판정_저장_시도` 는 실패해도 스트림을 안 죽인다). GET 으로 **되읽어** 확인한다.
        상세 = c.get(f"/api/plans/{plan_id}", headers=H).json()
        _확인(f"  plan {plan_id}: 판정={상세.get('판정')} (기대 {s['목']}) · 상태={상세.get('상태')}",
             상세.get("판정") == s["목"] and 상세.get("상태") == "judged")
        만든_plan_id.append(plan_id)

# ── ③ 할일 — 별도 호출이 필요 없다. `persist.판정_저장()` 이 `routes_tasks.동기화()`
#    를 이미 안에서 부른다(`out.get("해야할일")` 를 그대로 넘긴다, server/persist.py:79
#    실측) — ②에서 judge 를 태운 시점에 `plan_tasks` 도 같이 채워진다(가능·조건부만
#    해야할일을 채웠다 — 위 `_가짜_판정` 참조).
print("\n=== ③ 할일 — API 로 확인만 ===")
목록 = c.get("/api/tasks", headers=H)
_확인(f"GET /api/tasks 가 0건이 아니다 (건수={목록.json().get('건수')})",
     목록.json().get("건수", 0) > 0)

# ── ④ 되읽기 — 새 트랜잭션·새 연결로 실제로 저장됐는지 확인한다 ──────────
print("\n=== ④ 되읽기 (새 연결) ===")
with psycopg.connect(DSN) as conn:
    conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
    n1 = conn.execute(
        "SELECT count(*) FROM tenant.expense_plans WHERE org_id=%s", (ORG,)).fetchone()
    n2 = conn.execute(
        "SELECT count(*) FROM tenant.decisions WHERE org_id=%s", (ORG,)).fetchone()
    n3 = conn.execute(
        "SELECT count(*) FROM tenant.plan_tasks WHERE org_id=%s", (ORG,)).fetchone()
    n4 = conn.execute(
        "SELECT count(*) FROM tenant.f_profile WHERE org_id=%s", (ORG,)).fetchone()
    print(f"expense_plans={n1[0]} · decisions={n2[0]} · plan_tasks={n3[0]} · f_profile={n4[0]}")

pr = c.get("/api/profile", headers=H).json()
_확인(f"GET /api/profile 이 0·null 이 아니다 (f1={pr.get('f1')})",
     bool(pr.get("f1", {}).get("정부지원_현금")))

print("\n완료.")
