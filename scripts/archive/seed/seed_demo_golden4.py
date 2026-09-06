# -*- coding: utf-8 -*-
"""데모 계정 홈화면 데이터를 «골든셋 4문항 + 진짜 판정» 으로 심는다.

기존 `seed_demo_expense.py` 는 GPU 가 없던 시절이라 `orchestrate.판정` 을 스텁으로
갈아끼웠다 — plan 23271~23274 의 판정(dec 4172~4175)은 «하드코딩된 가짜»다
(요약 "1인 1대 한도 내에서 구매 가능합니다." 등). 이 스크립트는 스텁을 «안 쓴다».
Qwen API 경로(SUDDOE_LLM=qwen)로 실제 판정을 받아 넣는다.

문항은 오너가 지정한 골든셋 4건이고, 판정 4종을 하나씩 덮는다:
    497 가능      예비창업패키지 · 인건비
    552 조건부    창업도약패키지 · 특허권등무형자산취득비
    559 불가      창업중심대학  · 외주용역비
    569 판단불가  창업중심대학  · 인건비

🔴 **정답판정은 「기대」일 뿐 강제하지 않는다.** 실제 판정이 다르게 나오면 다르게
   저장하고 그대로 보고한다 — 데모를 예쁘게 만들려고 답을 맞춰 넣으면 그건 다시
   목데이터다. 이 스크립트가 없애려는 바로 그것이다.

실행:
    PYTHONIOENCODING=utf-8 SUDDOE_LLM=qwen SUDDOE_QWEN_MODEL=qwen3.7-plus \
    SUDDOE_DSN=<CloudSQL 프록시 DSN> DASHSCOPE_API_KEY=... \
    python scripts/archive/seed/seed_demo_golden4.py
"""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import psycopg                                              # noqa: E402
from fastapi.testclient import TestClient                   # noqa: E402
from llm_qwen import 스위치_적용                              # noqa: E402
print("LLM 경로:", 스위치_적용(), flush=True)                  # 🔴 「환경변수를 줬다」≠「그 코드가 불린다」

from server import main as 서버                              # noqa: E402
from server import auth                                     # noqa: E402
from server._common import DSN                              # noqa: E402

ORG = "cfeba091-251a-5ae4-8cc9-88c6e6679440"   # 경상국립대학교 창업중심대학사업단

문항 = [
    dict(gold=497, 기대="가능",     사업명="예비창업패키지",  확정비목="인건비",
         제목="만 67세 직원 인건비", 금액=2_800_000,
         질문="만 67세인 분을 채용했는데 고용보험 가입이 안 됩니다. "
              "4대보험이 다 안 되니 인건비 지급은 못 하는 건가요?"),
    dict(gold=552, 기대="조건부",   사업명="창업도약패키지",  확정비목="특허권등무형자산취득비",
         제목="협약 전 출원 특허 등록비", 금액=1_200_000,
         질문="협약 시작 전에 이미 출원해 둔 특허가 있습니다. 이번에 등록 결정이 나서 "
              "등록비를 내야 하는데 사업비로 되나요?"),
    dict(gold=559, 기대="불가",     사업명="창업중심대학",   확정비목="외주용역비",
         제목="시제품 제작 용역 선급금", 금액=30_000_000,
         질문="3,000만원짜리 시제품 제작 용역을 맡기는데 업체가 착수 시점에 선급금 "
              "2,000만원을 요구합니다. 선급금보증보험증권은 받아둘 예정입니다. "
              "이대로 집행해도 되나요?"),
    dict(gold=569, 기대="판단불가", 사업명="창업중심대학",   확정비목="인건비",
         제목="타 부처 과제 참여율 중복", 금액=3_600_000,
         질문="직원 한 명이 다른 부처 R&D 과제에 참여율 40%로 등재돼 있습니다. 이 직원을 "
              "창업중심대학 사업에 참여율 60%로 잡고 인건비를 지급해도 되나요?"),
]

c = TestClient(서버.app)
H = {"Authorization": f"Bearer {auth.데모토큰_발급(ORG)[0]}"}

print(f"\nDSN host: {DSN.split('@')[-1] if '@' in DSN else DSN}\n")
결과 = []
for f in 문항:
    plan = c.post("/api/plans", headers=H, json={
        "사업명": f["사업명"], "품목": f["제목"], "금액": f["금액"],
        "용도": f["질문"], "확정비목": f["확정비목"], "org_id": ORG})
    if plan.status_code != 201:
        print(f"🔴 gold{f['gold']} POST /api/plans 실패 {plan.status_code}: {plan.text[:200]}")
        continue
    pid = plan.json()["plan_id"]
    jr = c.post("/api/judge", headers=H, json={
        "정규화": {"_원문": f["질문"], "품목": f["제목"], "금액": f["금액"], "용도": f["질문"]},
        "확정비목": f["확정비목"], "사업명": f["사업명"], "plan_id": pid, "org_id": ORG})
    if jr.status_code != 200:
        print(f"🔴 gold{f['gold']} POST /api/judge 실패 {jr.status_code}: {jr.text[:200]}")
        continue
    # 🔴 SSE 200 은 「만들었다」지 「저장됐다」가 아니다 — GET 으로 되읽는다
    상세 = c.get(f"/api/plans/{pid}", headers=H).json()
    실제, 상태 = 상세.get("판정"), 상세.get("상태")
    맞음 = "일치" if 실제 == f["기대"] else f"🔴 다름(기대 {f['기대']})"
    print(f"gold{f['gold']} plan{pid} · 판정={실제} {맞음} · 상태={상태}")
    print(f"   요약: {(상세.get('요약') or '')[:110]}")
    결과.append((f["gold"], pid, 실제, f["기대"]))

print("\n=== 되읽기 (새 연결) ===")
with psycopg.connect(DSN) as conn:
    conn.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
    for gold, pid, _, _ in 결과:
        r = conn.execute('SELECT p."제목", d."판정", left(d."요약",80) FROM tenant.expense_plans p '
                         'JOIN tenant.decisions d ON d.decision_id=p.latest_decision_id '
                         'WHERE p.plan_id=%s', (pid,)).fetchone()
        print(f"  gold{gold} plan{pid}: {r}" if r else f"  🔴 gold{gold} plan{pid}: 판정 연결 없음")
print(f"\n심은 계획: {[p for _,p,_,_ in 결과]}")
