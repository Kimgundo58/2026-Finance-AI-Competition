# -*- coding: utf-8 -*-
"""스칼라 항목 반영 — 배열화(fe58488) 뒤에 돈다. 🔴 --dry 가 기본.

판정별 처리
  append / 빈칸이라 안전  ->  사전승인_조건은 «배열 append», 한도_* 는 «빈칸일 때만» set
  덮어쓰기(정보이동)      ->  한도_단위를 줄인다. 🔴 «짝이 되는 사전승인_조건 append 가
                              같은 rule 에 있어야만» 줄인다. 없으면 «건너뛴다»(정보 손실 방지)
  중복 / 위험             ->  «안 넣는다»
"""
import argparse, json, re, sys
sys.path.insert(0, 'scripts/_lib'); import db

def 납작(s): return re.sub(r"\s+", "", s or "")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
    d = json.load(open("scratchpad/스칼라57_반영안.json", encoding="utf-8"))
    items = d if isinstance(d, list) else (d.get("items") or [v for v in d.values() if isinstance(v, list)][0])

    조건append = {}          # rule_id -> [값]
    한도set, 한도줄임, 스킵 = [], [], []
    for x in items:
        판, 칸, rid = x["판정"], x["칸"], x["rule_id"]
        if 판 in ("중복", "위험"):
            스킵.append((rid, 칸, 판, x["사유"][:60])); continue
        if 칸 == "사전승인_조건":
            조건append.setdefault(rid, []).append(x["넣을값"])
        elif 판.startswith("덮어쓰기"):
            한도줄임.append(x)
        else:
            한도set.append(x)

    print(f"사전승인_조건 append  {sum(len(v) for v in 조건append.values())}건 ({len(조건append)} rule)")
    print(f"한도_* 빈칸 set       {len(한도set)}건")
    print(f"한도_단위 줄임        {len(한도줄임)}건  🔴 짝 검사 대상")
    print(f"안 넣음(중복·위험)    {len(스킵)}건")
    for s in 스킵:
        if s[2] == "위험": print(f"   🔴 위험 rule {s[0]} — {s[3]}")

    # 🔴 짝 검사 — 한도_단위를 줄이는 rule 에 사전승인_조건 append 가 «있나»
    print("\n=== 짝 검사 (정보 손실 방지) ===")
    안전, 위험줄임 = [], []
    for x in 한도줄임:
        rid = x["rid"] if "rid" in x else x["rule_id"]
        괄호 = re.search(r"\((.+)\)", x["현재값"] or "")
        핵심 = 납작(괄호.group(1)) if 괄호 else ""
        받는곳 = 납작(" ".join(조건append.get(rid, [])))
        # 괄호 내용의 «앞 12자» 가 짝 append 안에 있으면 옮겨진 것으로 본다
        옮겨짐 = bool(핵심) and 핵심[:12] in 받는곳
        (안전 if 옮겨짐 else 위험줄임).append((rid, x))
        print(f"  rule {rid:4d} 괄호내용 {'🟢 짝에 있다' if 옮겨짐 else '🔴 짝이 «없다» — 건너뛴다'}"
              f"  | {str(x['현재값'])[:52]}")
    print(f"\n  -> 줄여도 안전 {len(안전)}건 · 🔴 건너뜀 {len(위험줄임)}건")

    if not a.apply:
        print("\n🔴 DRY — 아무것도 안 썼다. 쓰려면 --apply"); return

    넣음 = 0
    with db.connect() as c, c.cursor() as cur:
        for rid, vals in sorted(조건append.items()):
            cur.execute("select 사전승인_조건 from corpus.rules where rule_id=%s", (rid,))
            현재 = list(cur.fetchone()[0] or [])
            기존 = {납작(v) for v in 현재}
            신규 = []
            for v in vals:                      # 🔴 제안끼리도 중복일 수 있다
                if 납작(v) in 기존:
                    continue
                기존.add(납작(v)); 신규.append(v)
            if not 신규: continue
            cur.execute("update corpus.rules set 사전승인_조건 = coalesce(사전승인_조건,'{}') || %s::text[] "
                        "where rule_id=%s", (신규, rid))
            assert cur.rowcount == 1
            넣음 += len(신규)
            print(f"  + rule {rid} 사전승인_조건 {len(현재)} -> {len(현재)+len(신규)}")
        for x in 한도set:
            cur.execute(f'update corpus.rules set "{x["칸"]}"=%s where rule_id=%s and "{x["칸"]}" is null',
                        (x["넣을값"], x["rule_id"]))
            if cur.rowcount: 넣음 += 1; print(f"  + rule {x['rule_id']} {x['칸']} = {str(x['넣을값'])[:40]}")
        for rid, x in 안전:
            cur.execute("update corpus.rules set 한도_단위=%s where rule_id=%s", (x["넣을값"], rid))
            assert cur.rowcount == 1
            넣음 += 1; print(f"  ~ rule {rid} 한도_단위 -> {x['넣을값']}")
        c.commit(); print("\nCOMMIT 반환됨")
    print(f"적용 — 총 {넣음}건")

if __name__ == "__main__": main()
