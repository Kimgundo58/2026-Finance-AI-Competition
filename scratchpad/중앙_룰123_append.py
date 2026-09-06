# -*- coding: utf-8 -*-
"""G1·G2·G3 113건 + 룰E «배열» 10건 = 123건을 corpus.rules 배열 칸에 «append» 한다.

🔴 --dry 가 기본이다. --apply 를 줘야 쓴다.
🔴 배열은 append «만». 통째 대체 금지 (확정 원칙).
🔴 제외 3건: 481(확정·참조 깊이1 미추적) · 456(미확정) · 455(기각)
🔴 룰E 의 «스칼라» 20건은 «넣지 않는다» — 기존 값을 덮어 사실이 사라진다(룰E_보류_0906.md)
🔴 중복 append 금지 — 이미 그 문자열이 배열에 있으면 건너뛴다
"""
import argparse, json, re, sys
from collections import defaultdict
sys.path.insert(0, 'scripts/_lib'); import db

제외 = {481, 456, 455}
칸맵 = {"금지예시": "금지예시", "허용예시": "허용예시"}      # 사전승인_조건은 스칼라 -> 이 스크립트 대상 아님

def 납작(s): return re.sub(r"\s+", "", s or "")

def 모으기():
    변경 = defaultdict(lambda: defaultdict(list))   # rule_id -> 칸 -> [항목]
    스칼라보류, 제외된 = [], []
    for f in ("룰제안_G1", "룰제안_G2", "룰제안_G3"):
        for x in json.load(open(f"scratchpad/{f}.json", encoding="utf-8")):
            rid, 칸 = x["rule_id"], x.get("칸")
            if rid in 제외: 제외된.append((rid, 칸, x.get("항목"))); continue
            if 칸 in 칸맵: 변경[rid][칸맵[칸]].append(x["항목"])
            else: 스칼라보류.append((rid, 칸, x.get("항목")))     # 사전승인_조건
    for x in json.load(open("scratchpad/룰제안_룰E.json", encoding="utf-8")):
        rid, 칸 = x["rule_id"], x.get("칸")
        if not isinstance(칸, dict): 스칼라보류.append((rid, 칸, None)); continue
        for k, vs in 칸.items():
            if not k.endswith("_추가"): 스칼라보류.append((rid, k, None)); continue
            if rid in 제외: 제외된.append((rid, k, vs)); continue
            변경[rid][k[:-3]] += list(vs)
    return 변경, 스칼라보류, 제외된

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
    변경, 스칼라보류, 제외된 = 모으기()
    print(f"대상 rule {len(변경)}종 · append 항목 {sum(len(v) for d in 변경.values() for v in d.values())}건")
    print(f"🔴 스칼라(사전승인_조건 등) «보류» {len(스칼라보류)}건 — 이 스크립트가 «안» 건드린다")
    print(f"🔴 제외(481·456·455) {len(제외된)}건\n")
    넣음 = 중복 = 없는룰 = 0
    with db.connect() as c, c.cursor() as cur:
        for rid in sorted(변경):
            cur.execute("select 사업명, 비목, 금지예시, 허용예시 from corpus.rules where rule_id=%s", (rid,))
            r = cur.fetchone()
            if r is None: print(f"🔴 rule {rid} 없다 — 건너뜀"); 없는룰 += 1; continue
            사업, 비목, 금지, 허용 = r
            현재 = {"금지예시": list(금지 or []), "허용예시": list(허용 or [])}
            for 칸, 항목들 in 변경[rid].items():
                기존납작 = {납작(x) for x in 현재[칸]}
                신규 = []
                for it in 항목들:
                    if 납작(it) in 기존납작: 중복 += 1; continue
                    기존납작.add(납작(it)); 신규.append(it)
                if not 신규: continue
                print(f"  rule {rid:4d} {str(사업)[:10]:12s} {str(비목)[:10]:12s} {칸} {len(현재[칸])} -> {len(현재[칸])+len(신규)}")
                for it in 신규: print(f"        + {it[:78]}")
                넣음 += len(신규)
                if a.apply:
                    cur.execute(f"update corpus.rules set {칸} = {칸} || %s::text[] where rule_id=%s",
                                (신규, rid))
                    assert cur.rowcount == 1
        if a.apply: c.commit(); print("\nCOMMIT 반환됨")
    print(f"\n{'적용' if a.apply else 'DRY'} — append {넣음}건 · 이미있어 건너뜀 {중복}건 · 없는 rule {없는룰}건")
    if not a.apply: print("🔴 아무것도 안 썼다. 쓰려면 --apply")

if __name__ == "__main__": main()
