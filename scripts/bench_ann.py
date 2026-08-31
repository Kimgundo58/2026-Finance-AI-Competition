# -*- coding: utf-8 -*-
"""A. dense 검색 벤치마크 — HNSW 를 지을지 말지를 실측으로 정한다.

미결 대장 #10 "1만 청크 초과 시 ANN 재검토 — 실측" 의 실행부.
2026-08-31 현재 `corpus.chunks` 20,055행이라 임계를 넘었다.

🔴 **인덱스를 만드는 것이 목적이 아니다.** 전량 스캔이 이미 예산 안이면 안 만드는 게 낫다 —
   HNSW 는 근사(ANN)라 재현율 손실이 붙기 때문이다. 그래서 세 가지를 같이 잰다:

     1. 전량 스캔 지연        <- 기준선. 예산은 Agent.md (3)-c 의 top-50 < 200ms
     2. HNSW 지연             <- ef_search 별로
     3. 재현율 recall@50      <- 전량 스캔 결과를 정답으로 둔 겹침률

   1이 예산 안이면 2·3 을 볼 필요 없이 "인덱스 불필요" 가 결론이다.

🔴 **질의 벡터는 반드시 실제 질문을 임베딩해서 쓴다** (`eval.golden_set.질문`).
   초판은 코퍼스 청크의 임베딩을 질의로 재사용했는데 **결과가 완전히 뒤집혔다** (2026-08-31):

     청크를 질의로 (틀린 방법)   ef=100  recall 99.5%
     실제 질문을 질의로 (맞음)   ef=100  recall 90.9%, 최악 64.0%

   질의가 코퍼스 안의 벡터와 동일하면 HNSW 그래프가 곧장 수렴해서 재현율이 부풀려진다.
   실제 질문은 법조문과 임베딩 공간의 다른 영역에 있고, 그 희소한 지점에서 그리디
   그래프 탐색이 지역 최소에 빠진다. "보수적으로 나온다" 던 초판 주석은 반대로 생각한 것이다.

## 결론 (2026-08-31) — HNSW 를 쓰지 않는다

인덱스가 실제로 켜지는 구간에서는 재현율이 못 쓸 수준이고, 재현율이 쓸 만한 구간에서는
플래너가 인덱스를 버린다. 중간이 없다:

    ef<=100  Index Scan  50ms   recall 82~91% (최악 30%)
    ef>=200  Seq Scan   138ms   recall 100%     <- 인덱스를 안 쓴다
    전량스캔             134ms   recall 100%

recall 64% 는 이 도메인에서 위험하다 — 놓친 조문에 효력이 이기는 상위 조항이 있으면
판정이 조용히 틀린다. 2만 건 규모에서는 전량 스캔이 정답이다.
**코퍼스가 크게 늘면 이 스크립트로 다시 잰다.**

실행:
    PYTHONIOENCODING=utf-8 python scripts/bench_ann.py
    PYTHONIOENCODING=utf-8 python scripts/bench_ann.py --build      # HNSW 생성까지
    PYTHONIOENCODING=utf-8 python scripts/bench_ann.py --drop       # 인덱스 제거
    PYTHONIOENCODING=utf-8 python scripts/bench_ann.py --chunk-queries  # 옛 방법(비권장)
"""
from __future__ import annotations

import argparse
import io
import os
import statistics
import sys
import time

import psycopg

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
IDX = "ix_chunks_embedding_hnsw"

# Agent.md (3)-b 의 판정 필터. 벤치마크가 실제 질의와 다른 집합을 재면 의미가 없다.
FILTER = """status = 'active' AND parse_quality = 'high'
        AND retrieval_scope = '진입점'
        AND layer IN ('L1','L2') AND 적용대상 IN ('창업기업','공통')"""

TOPK = 50
NQ = 30          # 질의 수. 중앙값을 보려면 이 정도면 된다


def 질의벡터(cur, n: int, 청크질의: bool = False) -> list[tuple[int, str]]:
    """질의 벡터를 만든다.

    기본은 `eval.golden_set.질문` 을 KURE-v1 로 임베딩한다 — 실제 질의 분포다.
    CPU 로 77건에 약 90초. GPU 를 띄우는 것보다 빠르다 (팟 왕복이 15분).
    """
    if 청크질의:
        # 옛 방법. 재현율이 부풀려지므로 지연 비교에만 쓴다 (모듈 주석 참조)
        cur.execute(f"""SELECT chunk_id FROM corpus.chunks
                         WHERE embedding IS NOT NULL AND {FILTER} ORDER BY chunk_id""")
        ids = [r[0] for r in cur.fetchall()]
        out = []
        for i in ids[::max(1, len(ids) // n)][:n]:
            cur.execute("SELECT embedding::text FROM corpus.chunks WHERE chunk_id=%s", (i,))
            out.append((i, cur.fetchone()[0]))
        return out

    from sentence_transformers import SentenceTransformer
    cur.execute("SELECT gold_id, 질문 FROM eval.golden_set ORDER BY gold_id")
    rows = cur.fetchall()
    print(f"골든셋 질문 {len(rows)}건 임베딩 (KURE-v1, CPU) ...", flush=True)
    t = time.time()
    m = SentenceTransformer("nlpai-lab/KURE-v1", device="cpu")
    m.max_seq_length = 1024
    V = m.encode([r[1] for r in rows], batch_size=8, normalize_embeddings=True,
                 convert_to_numpy=True, show_progress_bar=False)
    print(f"  {time.time() - t:.0f}초\n")
    return [(r[0], "[" + ",".join(f"{x:.6f}" for x in v) + "]") for r, v in zip(rows, V)]


def 검색(cur, vec: str) -> tuple[list[int], float]:
    sql = f"""SELECT chunk_id FROM corpus.chunks
               WHERE embedding IS NOT NULL AND {FILTER}
               ORDER BY embedding <=> %s::extensions.vector(1024)
               LIMIT {TOPK}"""
    t = time.perf_counter()
    cur.execute(sql, (vec,))
    rows = [r[0] for r in cur.fetchall()]
    return rows, (time.perf_counter() - t) * 1000


def 재다(cur, qs, 라벨: str) -> tuple[dict[int, list[int]], list[float]]:
    결과, 지연 = {}, []
    for qid, vec in qs:
        검색(cur, vec)                      # 워밍업 1회 (캐시 효과 제거)
        ids, ms = 검색(cur, vec)
        결과[qid] = ids
        지연.append(ms)
    지연.sort()
    print(f"  {라벨:22} p50 {statistics.median(지연):7.1f}ms   "
          f"p95 {지연[int(len(지연) * 0.95) - 1]:7.1f}ms   "
          f"max {지연[-1]:7.1f}ms")
    return 결과, 지연


def 재현율(정답: dict, 근사: dict) -> tuple[float, float]:
    """평균과 **최악**을 같이 낸다. 평균만 보면 recall 64% 짜리 질문이 묻힌다."""
    비율 = [len(set(정답[q]) & set(근사[q])) / len(정답[q]) for q in 정답 if 정답[q]]
    return sum(비율) / len(비율) * 100, min(비율) * 100


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="HNSW 를 짓고 인덱스 후 성능까지 잰다")
    ap.add_argument("--drop", action="store_true", help="인덱스 제거하고 끝")
    ap.add_argument("--m", type=int, default=16)
    ap.add_argument("--ef-construction", type=int, default=64)
    ap.add_argument("--chunk-queries", action="store_true",
                    help="옛 방법: 코퍼스 청크를 질의로 쓴다. 재현율이 부풀려진다 — 비권장")
    a = ap.parse_args()

    with psycopg.connect(DSN, autocommit=True) as conn:
        cur = conn.cursor()

        if a.drop:
            cur.execute(f"DROP INDEX IF EXISTS corpus.{IDX}")
            print(f"인덱스 제거: {IDX}")
            return

        cur.execute(f"SELECT count(*) FROM corpus.chunks WHERE embedding IS NOT NULL AND {FILTER}")
        후보 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM corpus.chunks WHERE embedding IS NOT NULL")
        전체 = cur.fetchone()[0]
        print(f"코퍼스 {전체:,}행 중 판정 필터 통과 {후보:,}행 · top-{TOPK} · 질의 {NQ}건")
        print(f"예산: Agent.md (3)-c  top-50 < 200ms\n")

        qs = 질의벡터(cur, NQ, a.chunk_queries)

        cur.execute(f"SELECT indexname FROM pg_indexes "
                    f"WHERE schemaname='corpus' AND indexname='{IDX}'")
        있음 = cur.fetchone() is not None
        if 있음:
            print(f"기존 HNSW 인덱스가 있다 — 전량 스캔 기준선을 재려면 --drop 후 다시 실행.\n")

        print("[1] 전량 스캔 (인덱스 없음)" if not 있음 else "[1] 현재 상태")
        cur.execute("SET LOCAL enable_indexscan = off") if 있음 else None
        정답, 기준지연 = 재다(cur, qs, "brute force")

        기준p95 = sorted(기준지연)[int(len(기준지연) * 0.95) - 1]
        print()
        if 기준p95 < 200:
            print(f"판정: 전량 스캔 p95 {기준p95:.1f}ms < 200ms — **인덱스 없이 예산 안**")
            print("      HNSW 는 근사라 재현율 손실이 붙는다. 필요 없으면 안 만드는 게 낫다.")
        else:
            print(f"판정: 전량 스캔 p95 {기준p95:.1f}ms >= 200ms — 인덱스가 필요하다")

        if not a.build:
            print("\n(--build 를 주면 HNSW 를 짓고 지연·재현율을 비교한다)")
            return

        print(f"\n[2] HNSW 생성 (m={a.m}, ef_construction={a.ef_construction}) ...")
        t = time.perf_counter()
        cur.execute(f"""CREATE INDEX IF NOT EXISTS {IDX} ON corpus.chunks
                        USING hnsw (embedding extensions.vector_cosine_ops)
                        WITH (m = {a.m}, ef_construction = {a.ef_construction})""")
        빌드 = time.perf_counter() - t
        cur.execute(f"SELECT pg_size_pretty(pg_relation_size('corpus.{IDX}'))")
        크기 = cur.fetchone()[0]
        print(f"  빌드 {빌드:.0f}초 · 인덱스 크기 {크기}")

        print("\n[3] HNSW 지연 + 재현율 (전량 스캔을 정답으로)")
        for ef in (40, 64, 100, 200):
            cur.execute(f"SET hnsw.ef_search = {ef}")
            근사, _ = 재다(cur, qs, f"ef_search={ef}")
            평균, 최악 = 재현율(정답, 근사)
            print(f"  {'':22} recall@{TOPK} 평균 {평균:.1f}%  최악 {최악:.1f}%")


if __name__ == "__main__":
    main()
