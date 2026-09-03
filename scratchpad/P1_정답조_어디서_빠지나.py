# -*- coding: utf-8 -*-
"""P1 — 「정답 조가 왜 s맵에 안 들어왔나」를 (a)/(b)/(c)로 가른다 (읽기 전용).

🔴 **튜닝 52 안에서만.** 미사용 41 은 열지 않는다.
🔴 `retrieve.py` 는 P3 소유다 — **읽기만 하고 함수를 부르기만 한다. 안 고친다.**
DB 는 한 행도 쓰지 않는다.

갈래 (ai-e8 지시):
    (c) 필터컷   정답 청크가 `적용대상 IN ('창업기업','공통')` 밖 → 검색이 볼 수조차 없다
    (b) 후보밖   필터는 통과하는데 dense50 ∪ sparse50 에 없다 → 색인·임베딩 문제
    (a) 순위밀림 후보 50 안엔 있는데 RRF top-5 에서 밀렸다 → 순위 문제
    (+) 폐포     top-5 는 아닌데 참조 확장(폐포)으로 들어왔다 → 사실상 구제됨

    PYTHONIOENCODING=utf-8 python scratchpad/P1_정답조_어디서_빠지나.py
    PYTHONIOENCODING=utf-8 python scratchpad/P1_정답조_어디서_빠지나.py --7건
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import psycopg  # noqa: E402
from _lib import db  # noqa: E402
import retrieve as R  # noqa: E402

RUN = 191
부분집합 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "P4_부분집합_0903.json")
# ai-e8 이 준 「인용 오조준」 12건 중 정답 조가 s맵에 없던 7건 (튜닝 52 안).
일곱 = [382, 384, 390, 398, 430, 431, 439]


_RE_조 = __import__("re").compile(r"(제\d+조(?:의\d+)?)")


def 정답청크(cur, 근거: list[dict]) -> list[tuple[int, str, str]]:
    """정답근거 (doc, 조번호) → 그 조의 청크들. `(chunk_id, 적용대상, 조번호)`.

    🔴 **정답셋의 `조번호` 는 라벨이지 키가 아니다.** 실측 4건이 그랬다 —
       `'제22조(사업비 집행) 제1호'` · `'제36조(여비)'` · `'[붙임2] 인건비 유의사항'`.
       그냥 `=` 로 맞추면 **코퍼스엔 있는데 「근거없음」으로 세어진다**(내가 그렇게 셌다).
       `assemble_context.격리_근거:445` 가 쓰는 `제N조` 추출과 **같은 정규화**를 쓰고,
       거기서도 안 걸리면 코퍼스의 실제 조번호가 라벨 안에 있는지로 한 번 더 찾는다.
    """
    out = []
    for b in 근거 or []:
        doc, 라벨 = b.get("doc"), b.get("조번호") or ""
        m = _RE_조.match(라벨)
        키 = m.group(1) if m else 라벨
        cur.execute("SELECT chunk_id, 적용대상, 조번호 FROM corpus.chunks "
                    " WHERE doc_id = %s AND 조번호 = %s ORDER BY chunk_id", (doc, 키))
        r = cur.fetchall()
        if not r and not m:
            # 「[붙임2] 인건비 유의사항」 ← 코퍼스 조번호는 '붙임2'. 공백·괄호를 지우고 포함으로 찾는다.
            납작 = 라벨.replace(" ", "")
            cur.execute("SELECT chunk_id, 적용대상, 조번호 FROM corpus.chunks "
                        " WHERE doc_id = %s AND replace(조번호,' ','') <> '' "
                        "   AND position(replace(조번호,' ','') in %s) > 0 ORDER BY chunk_id",
                        (doc, 납작))
            r = cur.fetchall()
        out.extend(r)
    return out


def 갈래(cur, 질문: str, 사업명: str | None, 정답: list[tuple[int, str, str]]) -> tuple[str, dict]:
    """한 문항의 갈래와 근거 수치. 정답 청크가 **하나라도** 닿으면 그 쪽으로 센다."""
    if not 정답:
        return "근거없음", {}
    # (c) 판정 인덱스의 경계 — `retrieve.py:43` FILTER 와 같은 조건이다.
    통과 = [c for c, 적, _ in 정답 if 적 in ("창업기업", "공통") or 적 is None]
    if not 통과:
        return "c_필터컷", {"적용대상": sorted({적 for _, 적, _ in 정답})}

    벡터 = R.임베딩(질문)
    d = R.dense(cur, 벡터, k=R.후보K, 사업명=사업명)
    b = R.sparse(cur, 질문, k=R.후보K, 사업명=사업명)
    d순 = [c for c, _ in d]
    후보 = set(d순) | set(b)
    순위 = R.rrf([d순, list(b)])
    top5 = 순위[:5]
    폐포, _사슬, _dang = R.폐포수집(cur, top5)

    맞 = [c for c in 통과 if c in 후보]
    if not 맞:
        return "b_후보밖", {"후보수": len(후보)}
    자리 = min(순위.index(c) for c in 맞 if c in 순위)
    if any(c in top5 for c in 맞):
        return "top5", {"순위": 자리 + 1}
    if any(c in 폐포 for c in 맞):
        return "폐포구제", {"순위": 자리 + 1}
    return "a_순위밀림", {"순위": 자리 + 1,
                        "dense": min([d순.index(c) + 1 for c in 맞 if c in d순] or [0]),
                        "bm25": min([list(b).index(c) + 1 for c in 맞 if c in b] or [0])}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--7건", dest="일곱만", action="store_true",
                    help="ai-e8 이 준 7건만 (기본은 튜닝 52 전체)")
    a = ap.parse_args()

    ids = json.load(open(부분집합, encoding="utf-8"))["튜닝52"]
    대상 = [x for x in 일곱 if x in ids] if a.일곱만 else ids

    R.워밍업()
    with psycopg.connect(db.DSN) as conn:
        cur = conn.cursor()
        cur.execute("""SELECT i.gold_id, i.적중, g.질문, g.사업명, g.정답근거
                         FROM eval.run_items i JOIN eval.golden_set g USING (gold_id)
                        WHERE i.run_id = %s AND i.gold_id = ANY(%s) ORDER BY i.gold_id""",
                    (RUN, 대상))
        행 = cur.fetchall()

        cnt: Counter = Counter()
        cnt적중: Counter = Counter()
        for gid, 적중, 질문, 사업명, 근거 in 행:
            정답 = 정답청크(cur, 근거)
            k, 상세 = 갈래(cur, 질문, 사업명, 정답)
            cnt[k] += 1
            if 적중:
                cnt적중[k] += 1
            if a.일곱만:
                print(f"  {gid}  {k:10} {상세}  정답청크 {[c for c, _, _ in 정답]}")

    print(f"\n갈래 (run {RUN} · {len(행)}문항)")
    print(f"  {'갈래':<12} {'건수':>4} {'그중 적중':>8}")
    for k in ("top5", "폐포구제", "a_순위밀림", "b_후보밖", "c_필터컷", "근거없음"):
        if cnt[k]:
            print(f"  {k:<12} {cnt[k]:>4} {cnt적중[k]:>8}")
    닿음 = cnt["top5"] + cnt["폐포구제"]
    후보안 = 닿음 + cnt["a_순위밀림"]
    분모 = sum(cnt.values()) - cnt["근거없음"]
    if 분모:
        print(f"\n  정답 조가 **후보 50 안에** 있는 비율 {후보안}/{분모} = {후보안/분모*100:.0f}%"
              f"   ← 검색 개선의 천장")
        print(f"  지금 실제로 닿는 비율          {닿음}/{분모} = {닿음/분모*100:.0f}%")


if __name__ == "__main__":
    main()
