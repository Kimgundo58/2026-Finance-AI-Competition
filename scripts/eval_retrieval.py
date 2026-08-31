# -*- coding: utf-8 -*-
"""검색 품질 평가 — dense / BM25 / RRF 가 정답 조문을 실제로 찾아오는가.

🔴 **A·B 가 잰 것과 다르다.**
   A = 전량 스캔 대비 재현율   -> "임베딩 모델이 가깝다고 본 것"을 얼마나 재현하나
   B = 우리 SQL == bm25s       -> 구현 동등성
   둘 다 기계가 제대로 도는지를 봤을 뿐, **맞는 조문을 찾는지는 안 봤다.**
   여기가 그걸 잰다.

정답은 `eval.golden_set.정답근거` (jsonb: doc / 조번호 / 원문).
`doc` 이 `corpus.chunks.doc_id` 와 그대로 일치하고, `원문` 은 조문에서 그대로 따온
문장이라 **원문 부분일치로 정답 청크를 역추적**한다. 조번호는 항호까지 붙어 있어
("제20조(1)") 청크 단위와 어긋날 수 있으므로 보조 수단으로만 쓴다.

지표
    hit@k   정답 청크가 상위 k 안에 하나라도 있는가 (판정에는 이게 1순위 —
            근거를 못 가져오면 LLM 이 무엇을 해도 틀린다)
    MRR     정답이 처음 나온 순위의 역수 평균
    RRF     dense 와 BM25 를 합쳤을 때 (Agent.md (3)-e)

실행:
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --k 20
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import psycopg

# 🔴 sys.stdout 을 여기서 감싸지 않는다. stage2_bm25 를 import 하면 그쪽이 다시 감싸고,
#    이쪽 래퍼가 GC 되면서 밑의 버퍼를 닫아버린다 ("I/O operation on closed file").
#    출력 인코딩은 PYTHONIOENCODING=utf-8 로 준다 (훅이 강제한다).

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# Agent.md (3)-b 판정 필터. 평가가 실제 질의와 다른 집합을 보면 의미가 없다.
FILTER = """embedding IS NOT NULL AND status='active' AND parse_quality='high'
        AND retrieval_scope='진입점' AND layer IN ('L1','L2')
        AND 적용대상 IN ('창업기업','공통')"""

DENSE = f"""SELECT chunk_id FROM corpus.chunks WHERE {FILTER}
            ORDER BY embedding <=> %s::extensions.vector(1024) LIMIT %s"""

BM25 = """
WITH q(term) AS (SELECT unnest(%(terms)s::text[])),
     s AS (SELECT count(*)::numeric n, avg(dl)::numeric avgdl FROM corpus.chunk_len)
SELECT ct.chunk_id,
       sum( ln(1 + (s.n - df.df + 0.5) / (df.df + 0.5))
            * (ct.tf * 2.2) / (ct.tf + 1.2 * (0.25 + 0.75 * cl.dl / s.avgdl)) ) AS score
FROM q JOIN corpus.chunk_terms ct ON ct.term = q.term
       JOIN corpus.term_df     df ON df.term = q.term
       JOIN corpus.chunk_len   cl ON cl.chunk_id = ct.chunk_id
CROSS JOIN s
WHERE ct.chunk_id IN (SELECT chunk_id FROM corpus.chunks WHERE """ + FILTER + """)
GROUP BY ct.chunk_id ORDER BY score DESC LIMIT %(k)s
"""


def 정규화(s: str) -> str:
    """공백·괄호·문장부호 차이를 흡수한다. 원문 인용이 완전히 같지 않을 수 있다."""
    return re.sub(r"[\s　·,.\"'()（）「」『』]", "", s or "")


def 정답청크(cur, 근거: list[dict]) -> set[int]:
    """정답근거 -> chunk_id 집합. 원문 부분일치가 1차, 조번호가 2차."""
    found: set[int] = set()
    for g in 근거 or []:
        doc, 조, 원문 = g.get("doc"), g.get("조번호") or "", g.get("원문") or ""
        핵심 = 정규화(원문)[:40]          # 앞 40자면 조문을 특정하기 충분하다
        if doc and 핵심:
            cur.execute("""SELECT chunk_id FROM corpus.chunks
                            WHERE doc_id = %s
                              AND regexp_replace(text, '[\\s·,."''()（）「」『』]', '', 'g')
                                  LIKE '%%' || %s || '%%'""", (doc, 핵심))
            found |= {r[0] for r in cur.fetchall()}
        if not found and doc and 조:
            조번호 = re.match(r"(제\d+조(?:의\d+)?)", 조)
            if 조번호:
                cur.execute("SELECT chunk_id FROM corpus.chunks WHERE doc_id=%s AND 조번호=%s",
                            (doc, 조번호.group(1)))
                found |= {r[0] for r in cur.fetchall()}
    return found


# RRF 가중 — 2026-08-31 골든셋 70문항 스윕으로 정했다 (`RAG.md` §4-4).
#   🔴 초판 사양 0.6/0.4 는 **최악에 가까웠다**: hit@5 41.4% (0.9/0.1 은 48.6%).
#      BM25 를 많이 섞으면 정답을 밀어낸다. 소량만 보완재로 쓰는 게 맞다.
#   🔴 이 평가 코드 자체도 가중치를 빼먹고 1:1 로 섞고 있었다 — 사양과 어긋나 있었다.
#   ⚠️ 70문항에서 1문항 = 1.4%p 다. 0.9/0.1 과 1.0/0.0(dense 단독)의 hit@5 차이는
#      **1문항**이라 과적합 위험이 있다. 골든셋이 커지면 다시 스윕할 것.
W_DENSE, W_SPARSE, RRF_K = 0.9, 0.1, 60


def rrf(순위목록: list[list[int]], k: int = RRF_K,
        가중: tuple[float, ...] = (W_DENSE, W_SPARSE)) -> list[int]:
    """Agent.md (3)-e. 순위의 역수를 가중해 더한다 — 점수 스케일이 달라도 섞인다."""
    점수: dict[int, float] = {}
    for 순위, w in zip(순위목록, 가중):
        for i, cid in enumerate(순위, 1):
            점수[cid] = 점수.get(cid, 0.0) + w / (k + i)
    return [c for c, _ in sorted(점수.items(), key=lambda x: -x[1])]


def 지표(결과: list[tuple[list[int], set[int]]], ks: list[int]) -> dict:
    out = {f"hit@{k}": 0 for k in ks}
    rr = []
    for 순위, 정답 in 결과:
        첫 = next((i for i, c in enumerate(순위, 1) if c in 정답), None)
        rr.append(1.0 / 첫 if 첫 else 0.0)
        for k in ks:
            if 첫 and 첫 <= k:
                out[f"hit@{k}"] += 1
    n = len(결과)
    return {**{k: v / n * 100 for k, v in out.items()}, "MRR": sum(rr) / n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50, help="검색 깊이")
    a = ap.parse_args()
    KS = [1, 5, 10, 20, a.k]

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from stage2_bm25 import 토큰화
    from sentence_transformers import SentenceTransformer

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        cur.execute("""SELECT gold_id, 세트, 질문, 정답근거 FROM eval.golden_set
                        WHERE 정답근거 IS NOT NULL ORDER BY gold_id""")
        rows = cur.fetchall()

        # 정답 청크 역추적. 못 찾은 문항은 평가에서 빼고 그 사실을 보고한다 —
        # 조용히 빼면 지표가 부풀려진다.
        데이터, 미해결 = [], []
        for gid, 세트, q, 근거 in rows:
            정답 = 정답청크(cur, 근거)
            (데이터 if 정답 else 미해결).append((gid, 세트, q, 정답))
        print(f"골든셋 {len(rows)}문항 중 정답 청크를 찾은 것 {len(데이터)}건, "
              f"못 찾은 것 {len(미해결)}건")
        if 미해결:
            print("  못 찾은 문항 (평가 제외):")
            for gid, 세트, q, _ in 미해결[:8]:
                print(f"    gold_id={gid} [{세트}] {q[:52]}")
            if len(미해결) > 8:
                print(f"    ... 외 {len(미해결)-8}건")
        if not 데이터:
            sys.exit("정답 청크를 하나도 못 찾았다. 매칭 규칙을 고칠 것.")

        print(f"\n질문 {len(데이터)}건 임베딩 (KURE-v1, CPU) ...", flush=True)
        t = time.time()
        m = SentenceTransformer("nlpai-lab/KURE-v1", device="cpu")
        m.max_seq_length = 1024
        V = m.encode([d[2] for d in 데이터], batch_size=8, normalize_embeddings=True,
                     convert_to_numpy=True, show_progress_bar=False)
        print(f"  {time.time()-t:.0f}초\n")

        d_res, b_res, r_res = [], [], []
        for (gid, 세트, q, 정답), v in zip(데이터, V):
            vec = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
            cur.execute(DENSE, (vec, a.k))
            dense = [r[0] for r in cur.fetchall()]
            terms = 토큰화([q])[0]
            cur.execute(BM25, {"terms": terms, "k": a.k})
            bm = [r[0] for r in cur.fetchall()]
            d_res.append((dense, 정답)); b_res.append((bm, 정답))
            r_res.append((rrf([dense, bm])[:a.k], 정답))

        print(f"{'검색기':10} " + " ".join(f"{'hit@'+str(k):>8}" for k in KS) + f"{'MRR':>8}")
        print("-" * (10 + 9 * len(KS) + 8))
        for 이름, res in (("dense", d_res), ("BM25", b_res), ("RRF", r_res)):
            g = 지표(res, KS)
            print(f"{이름:10} " + " ".join(f"{g['hit@'+str(k)]:7.1f}%" for k in KS)
                  + f" {g['MRR']:7.3f}")

        print("\n세트별 RRF hit@%d" % a.k)
        for 세트 in sorted({d[1] for d in 데이터}):
            부분 = [r for r, d in zip(r_res, 데이터) if d[1] == 세트]
            print(f"  {세트:8} {len(부분):3}건  {지표(부분, [a.k])['hit@'+str(a.k)]:5.1f}%")


if __name__ == "__main__":
    main()
