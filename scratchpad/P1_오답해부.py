# -*- coding: utf-8 -*-
"""P1 — run 191 오답 해부 (읽기 전용).

🔴 **튜닝 52 안에서만 연다.** 미사용 41(본세트 34 + 공식 7)은 열지 않는다 —
   읽는 순간 held-out 이 아니다. 부분집합은 `scratchpad/P4_부분집합_0903.json` 이 기준.

DB 는 한 행도 쓰지 않는다. run 191 의 `eval.run_items.원출력` 과 `eval.golden_set` 만 읽는다.

    PYTHONIOENCODING=utf-8 python scratchpad/P1_오답해부.py            # 표
    PYTHONIOENCODING=utf-8 python scratchpad/P1_오답해부.py --덤프 429  # 한 문항 전문
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from _lib import db  # noqa: E402

import psycopg  # noqa: E402

RUN = 191
부분집합 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "P4_부분집합_0903.json")

# 재위임 표현 — 「우리 코퍼스 밖으로 위임했다」는 신호.
# 🔴 '~에 따른다'(지침 제N조) 는 넣지 않는다. 그건 코퍼스 «안»으로 가는 참조라
#    참조 확장이 이미 따라간다. 여기서 찾는 것은 **따라갈 곳이 없는** 위임이다.
_재위임 = re.compile(
    r"(전문기관의?\s*장이[^.]{0,20}(별도로\s*)?(정|인정)"
    r"|주관기관의?\s*장이[^.]{0,20}(별도로\s*)?(정|인정)"
    r"|따로\s*정한다|별도로\s*정할\s*수\s*있다|집행\s*가이드를\s*따른다)")


def 로드(cur, ids: list[int]) -> list[dict]:
    cur.execute(
        """SELECT i.gold_id, i.정답, i.예측, i.적중, i.원출력,
                  g.세트, g.사업명, g.적용범위, g.비목, g.질문
             FROM eval.run_items i JOIN eval.golden_set g USING (gold_id)
            WHERE i.run_id = %s AND i.gold_id = ANY(%s)
            ORDER BY i.gold_id""", (RUN, ids))
    cols = ("gold_id", "정답", "예측", "적중", "원출력",
            "세트", "사업명", "적용범위", "비목", "질문")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def 인용축(it: dict) -> dict:
    """인용 한 벌에서 규칙 재료를 뽑는다. 원출력에 이미 있는 것만 쓴다."""
    인용 = it["원출력"].get("인용목록") or []
    docs = [c.get("doc_id") or "" for c in 인용]
    원문 = " ".join((c.get("원문") or "") for c in 인용)
    # L1 통합관리지침·법령 vs 사업별 세부관리기준(L2). doc_id 접두사가 기준이다.
    l1 = [d for d in docs if d.startswith("L1_")]
    l2 = [d for d in docs if not d.startswith("L1_")]
    return {
        "인용수": len(인용),
        "L1수": len(l1), "L2수": len(l2),
        "L1만": bool(인용) and not l2,
        "재위임": bool(_재위임.search(원문)),
        "docs": sorted(set(docs)),
    }


def 표(items: list[dict]) -> None:
    print(f"튜닝 {len(items)}문항 · run {RUN}\n")
    맞 = [x for x in items if x["적중"]]
    틀 = [x for x in items if not x["적중"]]
    print(f"  적중 {len(맞)} · 오답 {len(틀)}  (일치율 {len(맞)/len(items)*100:.1f}%)")
    print("\n혼동 (정답->예측)")
    for k, v in Counter(f"{x['정답']}->{x['예측']}" for x in items).most_common():
        print(f"    {k:22} {v}{'   🔴오답' if k.split('->')[0] != k.split('->')[1] else ''}")

    for it in items:
        it["축"] = 인용축(it)

    print("\n인용축 × 적중")
    for 이름, pred in (("L1만 인용", lambda a: a["L1만"]),
                       ("L2(세부기준) 인용 있음", lambda a: a["L2수"] > 0),
                       ("재위임 표현 있음", lambda a: a["재위임"]),
                       ("인용 0건", lambda a: a["인용수"] == 0)):
        부분 = [x for x in items if pred(x["축"])]
        맞n = sum(x["적중"] for x in 부분)
        print(f"    {이름:24} n={len(부분):3}  적중 {맞n:3}  오답 {len(부분)-맞n:3}"
              f"  예측분포 {dict(Counter(x['예측'] for x in 부분))}")


def 규칙(items: list[dict], 이름: str, 조건, 새판정: str) -> None:
    """조건에 걸린 문항의 판정을 `새판정` 으로 바꿨을 때의 이득/손실.

    🔴 이득만 세지 않는다. 오늘 이미 한 번 뒤집혔다 —
       강등코드 닫기는 이득 5 만 보면 채택이지만 손실 20 을 세면 순증 −15 다.
    """
    대상 = [x for x in items if 조건(x)]
    이득 = [x for x in 대상 if not x["적중"] and x["정답"] == 새판정]
    손실 = [x for x in 대상 if x["적중"] and x["정답"] != 새판정]
    무해 = len(대상) - len(이득) - len(손실)
    print(f"\n[{이름}] → {새판정}")
    print(f"    대상 {len(대상):3}  이득 {len(이득):3}  손실 {len(손실):3}  무해 {무해:3}"
          f"   **순증 {len(이득)-len(손실):+d}**")
    if 이득:
        print(f"    이득 {[x['gold_id'] for x in 이득]}")
    if 손실:
        print(f"    손실 {[(x['gold_id'], x['정답']) for x in 손실]}")


def 덤프(items: list[dict], gid: int) -> None:
    it = next((x for x in items if x["gold_id"] == gid), None)
    if it is None:
        sys.exit(f"gold_id={gid} 는 튜닝 52 안에 없다 (미사용 41 이면 열지 않는다)")
    o = it["원출력"]
    print(f"gold_id={gid} 정답={it['정답']} 예측={it['예측']} 세트={it['세트']} "
          f"사업={it['사업명'] or it['적용범위']} 비목={it['비목']}")
    print(f"Q: {it['질문']}")
    print(f"강등코드: {o.get('강등코드')}  신뢰등급={o.get('신뢰등급')}  경로={o.get('경로')}")
    print(f"요약: {o.get('요약')}")
    for c in o.get("인용목록") or []:
        print(f"  인용 {c.get('s번호')} {c.get('doc_id')} {c.get('조번호')} "
              f"({c.get('원문범위')}) | {(c.get('원문') or '')[:200]}")
    for p in o.get("전제목록") or []:
        print(f"  전제 사실={p.get('사실')!r} 근거={p.get('근거조항')} "
              f"미충족시={p.get('미충족시')} 매핑={p.get('매핑')}")
    for s in o.get("강등사유") or []:
        print(f"  사유 {s}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--덤프", type=int)
    a = ap.parse_args()

    ids = json.load(open(부분집합, encoding="utf-8"))["튜닝52"]
    with psycopg.connect(db.DSN) as conn:
        items = 로드(conn.cursor(), ids)
    if len(items) != len(ids):
        print(f"⚠️ 부분집합 {len(ids)} 중 run {RUN} 에 {len(items)}건만 있다")

    if a.덤프:
        for it in items:
            it["축"] = 인용축(it)
        return 덤프(items, a.덤프)

    표(items)
    규칙(items, "L1만 인용 + 사업지정",
        lambda x: x["축"]["L1만"] and x["사업명"], "판단불가")
    규칙(items, "재위임 표현이 인용에 있음",
        lambda x: x["축"]["재위임"], "판단불가")
    규칙(items, "L1만 인용 + 사업지정 + 예측이 판단불가가 아님",
        lambda x: x["축"]["L1만"] and x["사업명"] and x["예측"] != "판단불가", "판단불가")


if __name__ == "__main__":
    main()
