# -*- coding: utf-8 -*-
"""정본 = DB. 파일을 DB 로 맞춘다 (2026-09-06 오너 확정).

🔴 왜 DB 가 정본인가 — 판정이 «DB 를 읽어서» 난다. 파일은 아무도 안 읽는다.
   파일이 최신이 아니면 사람만 헷갈리고, 파일이 DB 를 덮으면 «판정이 바뀐다».

    python scripts/정본_동기화.py            대조만 (아무것도 안 쓴다)
    python scripts/정본_동기화.py --apply    파일을 DB 로 맞춘다
"""
import argparse, glob, json, sys
sys.path.insert(0, 'scripts/_lib'); import db

골든셋_디렉터리 = "2026_Finance_DATA_FOR_RAG/_골든셋_2026"
어휘집 = "2026_Finance_DATA_FOR_RAG/_비목_어휘집.json"

def 골든셋(apply: bool) -> None:
    with db.connect() as c, c.cursor() as cur:
        cur.execute("select 사업명, no, 정답판정, verified, 해야할일 from eval.golden_set")
        dbm = {(r[0], str(r[1])): r for r in cur.fetchall()}
    바뀜 = 0
    for path in sorted(glob.glob(f"{골든셋_디렉터리}/*.json")):
        d = json.load(open(path, encoding="utf-8"))
        items = d if isinstance(d, list) else (d.get("items") or
                 [v for v in d.values() if isinstance(v, list)][0])
        수정 = 0
        for x in items:
            k = (x.get("사업명"), str(x.get("no")))
            r = dbm.get(k)
            if not r: continue
            _, _, 판정, ver, 할일 = r
            for 칸, 새 in (("정답판정", 판정), ("verified", bool(ver))):
                if x.get(칸) != 새:
                    x[칸] = 새; 수정 += 1
            if 할일 and x.get("해야할일") != 할일:
                x["해야할일"] = 할일; 수정 += 1
        if 수정:
            바뀜 += 수정
            print(f"  {path.split('/')[-1]:36s} {수정}칸 갱신")
            if apply:
                json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"골든셋 — 파일에서 고칠 칸 «{바뀜}개»")

def 어휘(apply: bool) -> None:
    """🔴 파일은 «판정 enum»(창업 10종)이다. DB 의 RND 계통은 여기 «안 들어간다»."""
    d = json.load(open(어휘집, encoding="utf-8"))
    파일enum = set(d.get("guided_json_enum") or [])   # 🔴 실제 키. 짐작하지 말고 파일을 봤다
    with db.connect() as c, c.cursor() as cur:
        cur.execute("select 비목 from corpus.item_vocab where 계통='창업'")
        db창업 = {r[0] for r in cur.fetchall()}
    없 = sorted(db창업 - 파일enum); 남 = sorted(파일enum - db창업)
    print(f"어휘집 — 파일 {len(파일enum)}종 vs DB 창업 {len(db창업)}종 · "
          f"파일에 없음 {len(없)} · 파일에만 {len(남)}")
    if 없 or 남: print(f"   🔴 {없=} {남=}  -> 사람이 봐야 한다(자동 반영 안 한다)")
    else: print("   🟢 일치")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
    골든셋(a.apply); 어휘(a.apply)
    if not a.apply: print("\n🔴 대조만 했다. 파일을 고치려면 --apply")

if __name__ == "__main__": main()
