# -*- coding: utf-8 -*-
"""중앙(ai-c5) 적재 — 검증 통과분만 `eval.golden_set` 에 넣는다.

🔴 기본은 dry-run 이다. `--apply` 를 줘야 실제로 쓴다.
🔴 기존 113행을 **지우지 않는다.** append 만 한다 — 되돌리기가 쉬운 쪽을 고른다.
   새로 넣는 행은 `검수메모` 에 적재표식을 박아 나중에 통째로 골라낼 수 있게 한다.
🔴 «저장은 0건인데 COMMIT 이 성공을 돌려주는» 사고가 이 프로젝트에 있었다(비특권 롤 + abort 된 tx).
   그래서 ① 행마다 SAVEPOINT ② 커밋 후 **새 연결**로 다시 세어 확인한다.

    python 적재_ai-c5.py            # dry-run: 무엇이 들어갈지만 출력
    python 적재_ai-c5.py --apply    # 실제 적재
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import _lib.db as db  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "검증", Path(__file__).resolve().parent / "중앙_검증_ai-c5.py")
검증 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(검증)

골든셋_DIR = Path(__file__).resolve().parents[1]
적재표식 = "골든셋2026 재구축 · 중앙 ai-c5 검증 · 2026-09-04"

컬럼 = ["세트", "no", "사업명", "질문", "정답판정", "정답근거", "근거원문", "해야할일",
        "채점대상", "verified", "검수메모", "비목", "입력필드", "적용범위", "대상",
        "평가범위", "채점모드"]
JSONB = {"정답근거", "해야할일", "입력필드"}


def 읽기() -> list[tuple[str, dict]]:
    묶음 = []
    for f in sorted(p for p in 골든셋_DIR.glob("*.json") if not p.name.startswith("_")):
        데이터 = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(데이터, dict):
            데이터 = 데이터.get("문항") or 데이터.get("items") or []
        묶음 += [(f.stem, q) for q in 데이터]
    return 묶음


def main(적용: bool) -> int:
    묶음 = 읽기()
    if not 묶음:
        print("적재할 문항이 없다.")
        return 1

    통과, 탈락 = [], []
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        sc = 검증.스키마(cur)
        cur.execute("select 사업명, 질문 from eval.golden_set")
        기존 = {(a, (b or "").strip()) for a, b in cur.fetchall()}
        for 파일, q in 묶음:
            나쁨 = 검증.검사(cur, q, sc)
            if (q.get("사업명"), (q.get("질문") or "").strip()) in 기존:
                나쁨.append("기존 golden_set 에 같은 (사업명,질문) 이 이미 있다")
            (탈락 if 나쁨 else 통과).append((파일, q, 나쁨))

    print(f"── 문항 {len(묶음)} · 적재대상 {len(통과)} · 탈락 {len(탈락)}")
    for 파일, q, 나쁨 in 탈락:
        print(f"  🔴 {파일} [{q.get('no')}] {나쁨[:2]}")
    if not 적용:
        print("\n(dry-run — 아무것도 안 썼다. 실제 적재는 --apply)")
        return 0
    if 탈락:
        print("\n🔴 탈락이 남아 있으면 적재하지 않는다. 먼저 0 으로 만들어라.")
        return 1

    이전 = len(기존)
    자리 = ", ".join(f'"{c}"' for c in 컬럼)
    값 = ", ".join("%s" for _ in 컬럼)
    쓴수 = 0
    with db.connect() as conn:                      # autocommit 아님 — 트랜잭션 하나로 묶는다
        cur = conn.cursor()
        for 파일, q, _ in 통과:
            # 🔴 표식만 넣고 원본을 덮으면 저작자가 쓴 판정 근거가 DB 에서 사라진다.
            #    2026-09-04 에 실제로 그렇게 잃었다가 파일에서 되살렸다. 앞에 붙인다.
            메모 = f"{적재표식} · {파일}"
            원본 = (q.get("검수메모") or "").strip()
            if 원본:
                메모 += " | " + 원본
            q = {**q, "검수메모": 메모}
            행 = [json.dumps(q.get(c), ensure_ascii=False) if c in JSONB else q.get(c)
                  for c in 컬럼]
            cur.execute("SAVEPOINT s1")             # 한 행이 죽어도 트랜잭션 전체를 안 죽인다
            try:
                cur.execute(f"insert into eval.golden_set ({자리}) values ({값})", 행)
                cur.execute("RELEASE SAVEPOINT s1")
                쓴수 += 1
            except Exception as e:                  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT s1")
                print(f"  🔴 적재실패 {파일} [{q.get('no')}]: {e}")
        conn.commit()

    # 🔴 COMMIT 이 성공을 돌려줘도 믿지 않는다 — 새 연결로 다시 센다
    with db.connect(autocommit=True) as conn2:
        cur2 = conn2.cursor()
        cur2.execute("select count(*) from eval.golden_set")
        지금 = cur2.fetchone()[0]
        cur2.execute("select count(*) from eval.golden_set where 검수메모 like %s",
                     [f"{적재표식}%"])
        표식 = cur2.fetchone()[0]
    print(f"\n── insert 성공 {쓴수} · 적재 전 {이전} → 지금 {지금} (증분 {지금 - 이전}) · 표식행 {표식}")
    if 지금 - 이전 != 쓴수:
        print("🔴 증분이 insert 수와 다르다 — 조용한 롤백을 의심해라. 롤 권한부터 확인.")
        return 1
    print("   되돌리려면: delete from eval.golden_set where 검수메모 like "
          f"'{적재표식}%';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
