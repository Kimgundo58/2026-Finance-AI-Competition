# -*- coding: utf-8 -*-
"""`_골든셋_확정본.json` → `eval.golden_set` 만 재적재한다. corpus 는 손대지 않는다.

🔴 **`load_db.py` 를 쓰면 안 되는 자리다.** 그쪽은 `TRUNCATE corpus.documents CASCADE`
   로 시작하는데, `corpus.chunks` 가 documents 를 FK CASCADE 로 물고 있어서
   **임베딩 2만여 건이 같이 날아간다.** 골든셋 한 줄 고치자고 GPU 팟을 다시 여는 셈이다.
   에러가 안 나므로 돌린 사람은 다음 검색이 0건 나올 때까지 모른다.
   (그쪽에도 가드를 넣어 뒀다 — `--청크삭제승인` 없이는 멈춘다)

`eval.golden_set` 만 TRUNCATE 한다. `golden_chunks`·`run_items` 는 FK CASCADE 로
같이 지워지므로 **적재 후 `pin_golden_chunks.py --재고정` 을 반드시 돌린다** —
안 돌리면 `eval_store.평가대상()` 이 «골든청크 있는 문항» 0건을 돌려줘서 평가가 빈다.
과거 run 기록도 사라지는데, 문항 집합이 바뀐 이상 이전 run 과는 어차피 분모가 달라
직접 비교하면 안 되는 수치다 (CLAUDE.md 「지표를 읽을 때」).

실행:
    PYTHONIOENCODING=utf-8 python scripts/_work/_골든셋_재적재.py
    PYTHONIOENCODING=utf-8 python scripts/_work/_골든셋_재적재.py --commit
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent.parent
DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
확정본 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_골든셋_스테이징" / "_골든셋_확정본.json"


def main() -> int:
    commit = "--commit" in sys.argv
    문항 = json.loads(확정본.read_text(encoding="utf-8"))["문항"]

    # 🔴 JSON 의 `사업` 에는 «공통(지침 제14차)» 처럼 **사업이 아닌 값**이 섞여 있다.
    #    `golden_set.사업명` 은 `programs.사업명` 을 FK 로 물고 있어 그대로 넣으면
    #    적재가 통째로 죽는다. `eval_store.사업키()` 와 같은 규약으로 접는다 —
    #    사업명은 NULL(= 전 사업 공통), 원표기는 `적용범위` 에 남긴다.
    def 접기(사업):
        if not 사업 or str(사업).startswith("공통"):
            return None, (사업 or None)
        return 사업, None

    rows = []
    for x in 문항:
        사업명, 적용범위 = 접기(x.get("사업"))
        rows.append((x.get("_세트") or "본세트", str(x["no"]), 사업명, x["질문"],
                     x["정답_판정"], json.dumps(x.get("정답_근거"), ensure_ascii=False),
                     x.get("근거_원문"), json.dumps(x.get("해야할일"), ensure_ascii=False),
                     bool(x.get("verified")), x.get("검수자"),
                     x.get("비목"), x.get("대상"), x.get("평가범위"), x.get("채점모드"),
                     적용범위))

    print(f"문항 {len(rows)}")
    print(f"  세트     {dict(Counter(r[0] for r in rows))}")
    print(f"  대상     {dict(Counter(r[11] for r in rows))}")
    print(f"  평가범위 {dict(Counter(r[12] for r in rows))}")
    print(f"  채점모드 {dict(Counter(r[13] for r in rows))}")
    채점 = sum(1 for r in rows if not (r[12] and "범위밖" in r[12]))
    print(f"  → 채점 분모 후보 {채점} (범위밖 {len(rows)-채점} 제외)")

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        # 사업명 FK 검사 — programs 에 없는 사업명이면 INSERT 가 통째로 죽는다
        prog = {r[0] for r in cur.execute('SELECT "사업명" FROM corpus.programs').fetchall()}
        밖 = {r[2] for r in rows if r[2] and r[2] not in prog}
        if 밖:
            print(f"🔴 programs 에 없는 사업명: {밖} — 적재 중단"); return 1
        n청크 = cur.execute("SELECT count(*) FROM corpus.chunks").fetchone()[0]
        print(f"  corpus.chunks {n청크:,} (이 스크립트는 건드리지 않는다)")

        if not commit:
            print("\n(미리보기다. 실제로 쓰려면 --commit)")
            return 0

        cur.execute("TRUNCATE eval.golden_set CASCADE;")
        cur.executemany("""INSERT INTO eval.golden_set
            (세트, no, 사업명, 질문, 정답판정, 정답근거, 근거원문, 해야할일, verified, 검수메모,
             비목, 대상, 평가범위, 채점모드, 적용범위)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        conn.commit()

        n, n청크후 = cur.execute(
            "SELECT (SELECT count(*) FROM eval.golden_set), "
            "(SELECT count(*) FROM corpus.chunks)").fetchone()
        print(f"\ngolden_set {n}행 적재 · corpus.chunks {n청크후:,} (무변경 확인)")
        assert n청크후 == n청크, "🔴 청크가 줄었다 — 즉시 확인할 것"

    print("\n🔴 다음: PYTHONIOENCODING=utf-8 python scripts/pin_golden_chunks.py --재고정")
    return 0


if __name__ == "__main__":
    sys.exit(main())
