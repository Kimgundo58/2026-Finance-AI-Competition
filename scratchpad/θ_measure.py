# -*- coding: utf-8 -*-
"""θ 측정 — 골든셋 정답='불가' 182건이 금지적중() 게이트로 몇 건 잡히는지 실측.

방법 (재현 가능하도록 명시):
  품목 = 그 문항의 질문 원문 전체 그대로 (LLM 정규화 없이 재현 가능한 유일한 값)
  용도 = None
  사업명 = golden_set.사업명 그대로 (NULL 이면 그대로 None 으로 넘긴다 — base_룰() 이
          사업명 None 을 "공통 문항, 창업계통 L1만" 으로 다루도록 이미 설계돼 있다)
  비목 = golden_set.비목 그대로 (NULL 이면 None — base_룰() 이 비목 필터를 생략하고
        해당 사업의 전 비목 금지예시를 다 본다. 이건 "관대한" 모드다 — 표로 명시한다)

호출: scripts.rule_lookup.금지적중(cur, 품목, 용도, 사업명, 비목)
갈래:
  (가) 게이트적중   금지적중() != None
  (나) 미적중       금지적중() == None 이지만 base_룰(사업명,비목) 이 1행 이상
  (다) 룰없음       base_룰(사업명,비목) 이 0행
"""
import sys, json
sys.path.insert(0, 'scripts')
sys.path.insert(0, 'scripts/_lib')
import rule_lookup as rl
import db

rows = []
with db.connect() as c, c.cursor() as cur:
    cur.execute("""select gold_id, 사업명, 비목, 질문, verified from eval.golden_set
                   where 정답판정='불가' order by gold_id""")
    golden = cur.fetchall()

    for gold_id, biz, item, q, verified in golden:
        hit = rl.금지적중(cur, q, None, biz, item)
        base = rl.base_룰(cur, biz, item)
        if hit is not None:
            갈래 = "가_게이트적중"
        elif base:
            갈래 = "나_미적중_룰있음"
        else:
            갈래 = "다_룰없음"
        rows.append({
            "gold_id": gold_id, "사업명": biz, "비목": item, "질문": q,
            "verified": verified, "갈래": 갈래,
            "적중예시": hit["예시_원문"] if hit else None,
            "적중rule_id": hit["rule_id"] if hit else None,
            "base_룰행수": len(base),
        })

from collections import Counter
전체 = Counter(r["갈래"] for r in rows)
print("전체", dict(전체), "총", len(rows))

by_biz = {}
for r in rows:
    biz = r["사업명"] or "(공통/NULL)"
    by_biz.setdefault(biz, Counter())[r["갈래"]] += 1
print("\n사업별:")
for biz, c2 in sorted(by_biz.items()):
    print(" ", biz, dict(c2))

by_item = {}
for r in rows:
    it = r["비목"] or "(비목NULL)"
    by_item.setdefault(it, Counter())[r["갈래"]] += 1
print("\n비목별:")
for it, c2 in sorted(by_item.items()):
    print(" ", it, dict(c2))

with open(r'C:\Users\dogun\AppData\Local\Temp\claude\theta\result.json', 'w', encoding='utf-8') as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)
