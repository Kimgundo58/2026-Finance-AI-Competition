# -*- coding: utf-8 -*-
"""어휘 검증용 슬라이스 생성 — 정답 «가린» 문항집 + 채점키를 따로 낸다.

사람(세션)이 정의를 적용해 라벨을 붙이고, 채점은 기계가 한다.
슬라이스는 층화(정답 분포 유지)해서 나눈다 — 안 그러면 슬라이스마다 난이도가 다르다.
"""
import sys, os, json, random
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "_lib"))
import db

레인 = ["A", "B", "C", "D"]
씨앗 = 20260905


def 뽑기(cur):
    cur.execute("""
        select g.gold_id, g.사업명, g.비목, g.질문, coalesce(g.근거원문,'') 근거원문,
               g.정답판정, g.해야할일::text 해야할일
        from eval.golden_set g
        join (select distinct gold_id from eval.run_items where run_id=195) r using (gold_id)
        order by g.gold_id""")
    return [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]


def 층화분할(행들, n):
    통 = {}
    for r in 행들:
        통.setdefault(r["정답판정"], []).append(r)
    바구니 = [[] for _ in range(n)]
    rnd = random.Random(씨앗)
    for 라벨 in sorted(통):
        무리 = 통[라벨][:]
        rnd.shuffle(무리)
        for i, r in enumerate(무리):
            바구니[i % n].append(r)
    return 바구니


def 쓰기(경로, 자료):
    with open(경로, "w", encoding="utf-8") as f:
        json.dump(자료, f, ensure_ascii=False, indent=1)


def main():
    with db.connect() as c, c.cursor() as cur:
        행들 = 뽑기(cur)
    바구니 = 층화분할(행들, len(레인))
    여기 = os.path.dirname(os.path.abspath(__file__))
    for 레, 무리 in zip(레인, 바구니):
        무리 = sorted(무리, key=lambda r: r["gold_id"])
        # 문항집 — 🔴 정답판정·해야할일을 «뺀다»
        쓰기(os.path.join(여기, f"어휘_문항_{레}.json"),
             [{k: r[k] for k in ("gold_id", "사업명", "비목", "질문", "근거원문")} for r in 무리])
        # 채점키 — 별도 파일. 작업 세션은 이걸 열지 않는다
        쓰기(os.path.join(여기, f"어휘_채점키_{레}.json"),
             {str(r["gold_id"]): r["정답판정"] for r in 무리})
        분포 = {}
        for r in 무리:
            분포[r["정답판정"]] = 분포.get(r["정답판정"], 0) + 1
        print(f"레인 {레}  {len(무리):3d}문항  {분포}")
    print(f"\n총 {sum(len(b) for b in 바구니)}문항 · 겹침 0 · 씨앗 {씨앗}")


if __name__ == "__main__":
    main()
