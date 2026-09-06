# -*- coding: utf-8 -*-
"""배치 ③ — W 레인 「진짜 누락」을 corpus.rules 배열 칸에 «append» 한다 (오너 승인).

🔴 --dry 가 기본. --apply 를 줘야 쓴다.
🔴 배열 칸(금지예시·허용예시)만 넣는다. 스칼라(사전승인_조건)는 «배열화 뒤» 로 미룬다.
🔴 «칸이 애매한 것» 은 자동 반영하지 «않는다» — 보류로 빼서 사람이 본다.
   (칸 라벨이 서술형이라 "금지예시(원칙 불가)+사전승인_조건(예외 요건)" 같은 것이 있고,
    문안이 문자열 하나면 어느 칸에 무엇이 가는지 «코드가 못 가른다».)
"""
import argparse, json, re, sys
from collections import defaultdict
sys.path.insert(0, 'scripts/_lib'); import db

def 납작(s): return re.sub(r"\s+", "", s or "")

def 항목들(x):
    """(칸, 항목) 목록으로 편다. 못 가르면 [] 를 돌려 «보류» 로 만든다."""
    문안, 칸 = x.get("넣을문안"), (x.get("칸") or "")
    if isinstance(문안, list):
        out = []
        for it in 문안:
            k = (it.get("칸") or "").strip()
            if k not in ("금지예시", "허용예시"):
                return []                       # 스칼라 섞임 -> 통째 보류
            out.append((k, it["항목"]))
        return out
    if not isinstance(문안, str) or not 문안.strip():
        return []
    if 칸 in ("금지예시", "허용예시"):
        return [(칸, 문안.strip())]
    return []                                    # 서술형·복합 칸 + 문자열 하나 -> 보류

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
    후보, 보류, 스킵 = defaultdict(lambda: defaultdict(list)), [], []
    for f in ("W_단서누락_44", "W_단서누락_14"):
        d = json.load(open(f"scratchpad/{f}.json", encoding="utf-8"))
        items = d.get("items") if isinstance(d, dict) else [x for x in d if "rule_id" in x]
        for x in items:
            판정 = str(x.get("판정") or "")
            if not 판정.startswith("가") or "이미" in 판정:
                스킵.append((x.get("rule_id"), 판정)); continue
            쌍 = 항목들(x)
            if not 쌍:
                보류.append((x.get("rule_id"), x.get("비목"), x.get("칸"),
                            str(x.get("넣을문안"))[:70])); continue
            for k, v in 쌍:
                후보[x["rule_id"]][k].append(v)
    print(f"대상 rule {len(후보)}종 · append 항목 {sum(len(v) for d in 후보.values() for v in d.values())}건")
    print(f"🔴 보류(칸을 코드가 못 가름) {len(보류)}건 · 대상 아님(다/이미반영) {len(스킵)}건")
    for b in 보류: print(f"    보류 rule {b[0]} [{b[2]}] {b[3]}")

    넣음 = 중복 = 0
    with db.connect() as c, c.cursor() as cur:
        for rid in sorted(후보):
            cur.execute("select 사업명, 비목, 허용, 금지예시, 허용예시 from corpus.rules where rule_id=%s", (rid,))
            r = cur.fetchone()
            if r is None: print(f"🔴 rule {rid} 없다"); continue
            사업, 비목, 허용, 금지, 허용ex = r
            현재 = {"금지예시": list(금지 or []), "허용예시": list(허용ex or [])}
            for 칸, vs in 후보[rid].items():
                기존 = {납작(x) for x in 현재[칸]}
                신규 = []
                for v in vs:
                    if 납작(v) in 기존: 중복 += 1; continue
                    기존.add(납작(v)); 신규.append(v)
                if not 신규: continue
                print(f"  rule {rid:4d} {str(사업)[:10]:12s}{str(비목)[:12]:14s} 허용={허용} "
                      f"{칸} {len(현재[칸])} -> {len(현재[칸])+len(신규)}")
                for v in 신규: print(f"        + {v[:76]}")
                넣음 += len(신규)
                if a.apply:
                    cur.execute(f"update corpus.rules set {칸} = {칸} || %s::text[] where rule_id=%s", (신규, rid))
                    assert cur.rowcount == 1
        if a.apply: c.commit(); print("\nCOMMIT 반환됨")
    print(f"\n{'적용' if a.apply else 'DRY'} — append {넣음}건 · 이미있어 건너뜀 {중복}건")
    if not a.apply: print("🔴 아무것도 안 썼다. 쓰려면 --apply")

if __name__ == "__main__": main()
