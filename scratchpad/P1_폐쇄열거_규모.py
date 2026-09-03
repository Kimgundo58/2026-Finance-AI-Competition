# -*- coding: utf-8 -*-
"""P1 — 「폐쇄 열거 조문」이 몇 건짜리 문제인가 (읽기 전용 · 처방 없음).

🔴 **ai-e8 지시: 처방은 내지 마라. 규모만 재라.**
🔴 **튜닝 52 안에서만 문항을 연다.** 미사용 41 은 열지 않는다.
DB 는 한 행도 쓰지 않는다.

## 왜 이게 축인가 (gold 430 이 보여 준 기전)
질문은 「교육 수강료」인데 정답 조(모두의창업 제33조①)는 **사용가능 비목 폐쇄 열거**다.
정답이 「그 목록에 **없다**」이므로 **정답 조에 질문 어휘가 원리적으로 없다.**
어휘 겹침으로 찾는 BM25 로는 「X 가 없는 목록」을 못 찾는다 — 실측 공통 토큰 1개 · BM25 434위.

## 「폐쇄 열거 조문」의 조작적 정의 (이 파일이 쓰는 것)
  ① 본문에 「각 호」 또는 「각호」가 있다  (「다음 각 호와 같다」 꼴)
  ② 호 표지가 3개 이상 있다              (줄머리 `1.` `2.` … 또는 `1)` `2)`)
  ③ 그 호 중 2개 이상이 **비목 표기로 시작**한다
     — 비목 어휘의 기준 문서는 `_비목_어휘집.json` 의 `guided_json_enum` 이다 (CLAUDE.md)
  🔴 이건 **어림자**다. ①②③ 를 바꾸면 수가 바뀐다. 그래서 아래에서 조건별 수도 같이 낸다.

    PYTHONIOENCODING=utf-8 python scratchpad/P1_폐쇄열거_규모.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import psycopg  # noqa: E402
from _lib import db  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
부분집합 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "P4_부분집합_0903.json")
RUN = 191


def 비목표기() -> list[str]:
    """기준 문서는 어휘집이다. 여기 하드코딩하지 않는다 (`llm_schema.py` 주석과 같은 이유)."""
    p = os.path.join(ROOT, "2026_Finance_DATA_FOR_RAG", "_비목_어휘집.json")
    e = list(json.load(open(p, encoding="utf-8"))["guided_json_enum"])
    # 어휘집은 붙여쓰기(`창업활동비`)인데 조문은 띄어쓴다(`창업 활동비`). 공백 허용으로 푼다.
    return e


def _느슨(말: str) -> str:
    return r"\s*".join(re.escape(ch) for ch in 말 if not ch.isspace())


_RE_각호 = re.compile(r"각\s*호")
_RE_호머리 = re.compile(r"^[ \t]*(\d{1,2})[.)]\s*(.{0,40})", re.M)


def 판정(본문: str, RE비목) -> tuple[bool, int, int]:
    """(폐쇄열거인가, 호 개수, 비목으로 시작하는 호 개수)."""
    if not 본문:
        return False, 0, 0
    호 = _RE_호머리.findall(본문)
    비목호 = sum(1 for _n, 머리 in 호 if RE비목.match(머리.strip()))
    있 = bool(_RE_각호.search(본문)) and len(호) >= 3 and 비목호 >= 2
    return 있, len(호), 비목호


def main() -> None:
    표기 = 비목표기()
    RE비목 = re.compile("(?:" + "|".join(_느슨(t) for t in 표기) + "|" + _느슨("특허권") + ")")
    튜닝 = json.load(open(부분집합, encoding="utf-8"))["튜닝52"]

    with psycopg.connect(db.DSN) as conn:
        cur = conn.cursor()

        # ── 1. 코퍼스에 몇 개인가 ────────────────────────────────────────
        cur.execute("""SELECT a.doc_id, a.조번호, a.조제목, a.본문, d.layer
                         FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id=a.doc_id
                        WHERE d.layer IN ('L1','L2')""")
        조문 = cur.fetchall()
        열거 = []
        느슨한수 = 0
        for doc, 조, 제목, 본문, layer in 조문:
            있, n호, n비목 = 판정(본문, RE비목)
            if _RE_각호.search(본문 or "") and n호 >= 3:
                느슨한수 += 1
            if 있:
                열거.append((doc, 조, 제목, layer, n호, n비목))
        print(f"■ 코퍼스 (L1·L2 조문 {len(조문):,}건)")
        print(f"    ①「각 호」+ ② 호 3개 이상            {느슨한수:,}건")
        print(f"    + ③ 비목으로 시작하는 호 2개 이상   **{len(열거):,}건**  ← 폐쇄 열거로 센 것")
        층 = Counter(x[3] for x in 열거)
        print(f"    layer 별 {dict(층)}")
        문서별 = Counter(x[0] for x in 열거)
        print(f"    이런 조를 가진 문서 {len(문서별)}개")
        for d, n in 문서별.most_common(8):
            print(f"      {n:>2}건  {d[:52]}")

        열거키 = {(x[0], x[1]) for x in 열거}

        # ── 2. 정답셋에서 몇 건인가 (튜닝 52 만) ─────────────────────────
        cur.execute("""SELECT i.gold_id, i.적중, i.정답, i.예측, g.비목, g.정답근거
                         FROM eval.run_items i JOIN eval.golden_set g USING (gold_id)
                        WHERE i.run_id=%s AND i.gold_id = ANY(%s) ORDER BY i.gold_id""",
                    (RUN, 튜닝))
        걸린 = []
        for gid, 적, 정, 예, 비목, 근 in cur.fetchall():
            히트 = [(b.get("doc"), b.get("조번호")) for b in (근 or [])
                   if (b.get("doc"), re.match(r"(제\d+조(?:의\d+)?)", b.get("조번호") or "").group(1)
                       if re.match(r"(제\d+조(?:의\d+)?)", b.get("조번호") or "") else b.get("조번호"))
                   in 열거키]
            if 히트:
                걸린.append((gid, 적, 정, 예, 비목, 히트))
        print(f"\n■ 튜닝 52 — 정답근거가 폐쇄 열거 조인 문항 **{len(걸린)}건**")
        print(f"    {'gid':>4} {'적중':>5} {'정답':>5} {'예측':>5}  비목 / 근거조")
        for gid, 적, 정, 예, 비목, 히트 in 걸린:
            print(f"    {gid:>4} {str(적):>5} {정:>5} {예:>5}  {비목} / "
                  f"{'·'.join(f'{d[:14]} {j}' for d, j in 히트)}")
        if 걸린:
            맞 = sum(1 for x in 걸린 if x[1])
            print(f"    적중 {맞}/{len(걸린)}")

        # ── 3. 같은 기전인가 — 질문 어휘가 정답 조에 있는가 ──────────────
        print(f"\n■ 기전 대조 — 「질문 비목이 그 열거 «안»에 있나」 · 「BM25 가 찾나」")
        import retrieve as R
        R.워밍업()
        print(f"    {'gid':>4} {'비목':<10} {'열거안':>6} {'공통토큰':>7} {'BM25순위':>9} {'정답조':>7}")
        for gid, 적, 정, 예, 비목, 히트 in 걸린:
            cur.execute("SELECT 질문, 사업명 FROM eval.golden_set WHERE gold_id=%s", (gid,))
            q, 사업 = cur.fetchone()
            doc, 조 = 히트[0]
            조키 = re.match(r"(제\d+조(?:의\d+)?)", 조 or "")
            cur.execute("SELECT chunk_id, text FROM corpus.chunks WHERE doc_id=%s AND 조번호=%s",
                        (doc, 조키.group(1) if 조키 else 조))
            r = cur.fetchone()
            if not r:
                print(f"    {gid:>4} {str(비목):<10}  (청크 없음)")
                continue
            cid, 본문 = r
            안 = bool(비목 and RE비목.match(str(비목))
                     and re.search(_느슨(str(비목)), 본문 or ""))
            공통 = len(set(R.토큰화([q])[0]) &
                     {t[0] for t in cur.execute(
                         "SELECT term FROM corpus.chunk_terms WHERE chunk_id=%s", (cid,)).fetchall()})
            전체 = R.sparse(cur, q, k=100000, 사업명=사업)
            위 = 전체.index(cid) + 1 if cid in 전체 else None
            print(f"    {gid:>4} {str(비목):<10} {('있음' if 안 else '**없음**'):>6} "
                  f"{공통:>7} {str(위):>9} {'닿음' if 적 else '틀림':>7}")

    print("\n🔴 이 표는 «몇 건짜리 문제인가» 다. 처방은 없다 (ai-e8 지시).")
    print("🔴 정의를 바꾸면 수가 바뀐다 — ①②만이면 위 느슨한 수, ③까지면 좁은 수다.")


if __name__ == "__main__":
    main()
