# -*- coding: utf-8 -*-
"""판정 파이프라인 3단계(검색 + 참조 확장): dense·BM25 순위를 RRF 로 합치고, 상위 청크가 참조하는 조문을 함께 가져온다.

`orchestrate.py` 가 `검색()` 을 호출하고, `eval_retrieval.py` 가 같은 함수들로 검색 품질을 잰다.
`검색()` 반환 키: top5, 참조확장(참조 확장 article_id), 참조관계, 최고유사도(dense 최고 유사도), 미해결참조, 후보수.
읽기 전용: corpus.chunks, chunk_terms, chunk_len, term_df, refs, doc_articles, documents.
검색 대상은 FILTER 로 제한한다(L1·L2, active, 창업기업·공통 대상). L3 는 검색하지 않는다(`l3_load` 가 기관별로 조회한다).

실행: PYTHONIOENCODING=utf-8 python scripts/retrieve.py --q "맥북 250만원 사도 되나요" [--사업명 ...] | --bench
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db, paths                                           # noqa: E402
paths.ensure_on_path()

# ── 검색 대상 범위(사전 필터) ────────────────────────────────────────────────
FILTER = """embedding IS NOT NULL AND status='active' AND parse_quality='high'
        AND retrieval_scope='검색대상' AND layer IN ('L1','L2')
        AND 적용대상 IN ('창업기업','공통')"""

# 사업명이 지정된 검색에서만 그 사업의 세부관리기준으로 후보를 좁히는 절.
사업절 = " AND (사업명 IS NULL OR %(사업)s = ANY(사업명))"

# 사업 필터는 다른 사업의 세부관리기준이 후보에 섞이는 것을 막는다. 검색 범위를 바꾸는 값이라
# 모듈 기본값은 끄고, 판정 파이프라인(orchestrate)이 SUDDOE_PROGRAM_FILTER 에 따라 호출 시 켠다.
사업필터_기본 = False

DENSE = f"""SELECT chunk_id, 1 - (embedding <=> %(v)s::extensions.vector(1024)) AS sim
              FROM corpus.chunks WHERE {FILTER}{{사업}}
             ORDER BY embedding <=> %(v)s::extensions.vector(1024) LIMIT %(k)s"""

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
WHERE ct.chunk_id IN (SELECT chunk_id FROM corpus.chunks WHERE """ + FILTER + """{사업})
GROUP BY ct.chunk_id ORDER BY score DESC LIMIT %(k)s
"""

# ── 참조 확장 ──────────────────────────────────────────────────────────────────
# 깊이 1 이면 재귀가 필요 없지만, 깊이를 늘릴 수 있게 재귀 CTE 형태를 유지한다.
# `dst_조번호 IS NOT NULL` 은 시작 간선과 재귀 간선 둘 다에 건다.
# 도착 문서가 active 가 아니면(개정으로 대체된 문서 등) 효력 없는 조문이 인용되지 않도록 제외한다.
참조확장SQL = """
WITH RECURSIVE 시작(doc_id, 조번호) AS (
    SELECT DISTINCT doc_id, 조번호 FROM corpus.chunks WHERE chunk_id = ANY(%(cids)s)
),
참조확장 AS (
    SELECT r.ref_id, r.src_doc_id, r.src_조번호, r.참조문자열, r.관계,
           r.dst_doc_id, r.dst_조번호, r.해결상태, r.해결근거, 1 AS depth
      FROM corpus.refs r JOIN 시작 s
        ON (r.src_doc_id, r.src_조번호) = (s.doc_id, s.조번호)
     WHERE r.해결상태 <> 'dangling' AND r.dst_조번호 IS NOT NULL
    UNION ALL
    SELECT r.ref_id, r.src_doc_id, r.src_조번호, r.참조문자열, r.관계,
           r.dst_doc_id, r.dst_조번호, r.해결상태, r.해결근거, p.depth + 1
      FROM corpus.refs r JOIN 참조확장 p
        ON (r.src_doc_id, r.src_조번호) = (p.dst_doc_id, p.dst_조번호)
     WHERE r.해결상태 <> 'dangling' AND r.dst_조번호 IS NOT NULL AND p.depth < %(깊이)s
)
SELECT DISTINCT ON (p.ref_id)
       p.ref_id, p.src_doc_id, p.src_조번호, p.참조문자열, p.관계,
       p.dst_doc_id, p.dst_조번호, p.해결상태, p.해결근거,
       a.article_id, a.조제목, sa.조제목
  FROM 참조확장 p
  JOIN corpus.documents dd ON dd.doc_id = p.dst_doc_id AND dd.status = 'active'
  JOIN corpus.doc_articles a  ON a.doc_id = p.dst_doc_id AND a.조번호 = p.dst_조번호
  LEFT JOIN corpus.doc_articles sa ON sa.doc_id = p.src_doc_id AND sa.조번호 = p.src_조번호
 ORDER BY p.ref_id
"""

# 검색 대상 범위 안(top-k 시작 청크)에서 출발한 미해결 참조만 센다. 범위 밖의 미해결 참조는 판정과 무관하다.
DANGLING_SQL = """
SELECT DISTINCT r.참조문자열
  FROM corpus.refs r
  JOIN (SELECT DISTINCT doc_id, 조번호 FROM corpus.chunks WHERE chunk_id = ANY(%(cids)s)) s
    ON (r.src_doc_id, r.src_조번호) = (s.doc_id, s.조번호)
 WHERE r.해결상태 = 'dangling'
 ORDER BY 1
"""

W_DENSE, W_SPARSE, RRF_K = 0.9, 0.1, 60
후보K = 50          # dense·BM25 각각에서 가져오는 후보 수
깊이 = 1            # 참조 확장 재귀 깊이

# ── 임베딩 모델 (프로세스당 1회 로드) ────────────────────────────────────────
_모델 = None
_토큰화 = None
_모델_잠금 = threading.Lock()


def 임베딩_모델():
    """KURE-v1 을 프로세스에 한 번만 로드한다(CPU, 첫 호출 약 15초).

    판정 스레드·워밍업·`rule_lookup.워밍업()` 이 동시에 부를 수 있어 락을 건다 —
    락이 없으면 둘 다 `None` 을 보고 각자 모델을 로드한다.
    """
    global _모델
    if _모델 is None:
        with _모델_잠금:
            if _모델 is None:
                from sentence_transformers import SentenceTransformer
                m = SentenceTransformer("nlpai-lab/KURE-v1", device="cpu")
                m.max_seq_length = 1024
                _모델 = m
    return _모델


def 토큰화(texts: list[str]) -> list[list[str]]:
    """BM25 토큰화는 `bm25_index.토큰화` 를 그대로 쓴다 — 색인과 쿼리가 같은 토큰화를 써야 한다."""
    global _토큰화
    if _토큰화 is None:
        paths.ensure_on_path()
        from bm25_index import 토큰화 as _t
        _토큰화 = _t
    return _토큰화(texts)


def 워밍업() -> float:
    """모델 로드와 첫 인코딩을 미리 실행해, 첫 판정 요청이 모델 로딩 지연을 겪지 않게 한다.
    BM25 용 kiwi 사전도 함께 로드한다(첫 tokenize 약 1초). 걸린 시간(초)을 반환한다."""
    t = time.perf_counter()
    임베딩_모델().encode(["워밍업"], normalize_embeddings=True, convert_to_numpy=True,
                      show_progress_bar=False)
    토큰화(["워밍업"])
    return time.perf_counter() - t


def 임베딩(질문: str) -> str:
    """질문 -> pgvector 리터럴. 정규화된 벡터라 `1 - (a <=> b)` 가 코사인 유사도다."""
    v = 임베딩_모델().encode([질문], normalize_embeddings=True, convert_to_numpy=True,
                          show_progress_bar=False)[0]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ── 검색기: dense · BM25 · RRF 결합 ──────────────────────────────────────────
def _사업절(사업명: str | None, 사업필터: bool) -> str:
    return 사업절 if (사업필터 and 사업명) else ""


def dense(cur, 벡터: str, *, k: int = 후보K, 사업명: str | None = None,
          사업필터: bool = 사업필터_기본) -> list[tuple[int, float]]:
    """(chunk_id, 코사인 유사도) 목록을 유사도 순으로 반환한다. 최고유사도는 첫 원소의 유사도다."""
    cur.execute(DENSE.format(사업=_사업절(사업명, 사업필터)),
                {"v": 벡터, "k": k, "사업": 사업명})
    return [(r[0], float(r[1])) for r in cur.fetchall()]


def sparse(cur, 질문: str, *, k: int = 후보K, 사업명: str | None = None,
           사업필터: bool = 사업필터_기본) -> list[int]:
    """BM25 점수 순 chunk_id 목록."""
    cur.execute(BM25.format(사업=_사업절(사업명, 사업필터)),
                {"terms": 토큰화([질문])[0], "k": k, "사업": 사업명})
    return [r[0] for r in cur.fetchall()]


def rrf(순위목록: list[list[int]], k: int = RRF_K,
        가중: tuple[float, ...] = (W_DENSE, W_SPARSE)) -> list[int]:
    """Reciprocal Rank Fusion: 순위의 역수를 가중해 더한다 — 점수 스케일이 달라도 합칠 수 있다.
    한쪽에만 나온 청크는 다른 쪽 항이 0 이다."""
    점수: dict[int, float] = {}
    for 순위, w in zip(순위목록, 가중):
        for i, cid in enumerate(순위, 1):
            점수[cid] = 점수.get(cid, 0.0) + w / (k + i)
    return [c for c, _ in sorted(점수.items(), key=lambda x: -x[1])]


# ── 참조 확장 ─────────────────────────────────────────────────────────────────────
def 참조확장_수집(cur, 시작청크: list[int], *, 깊이값: int = 깊이) -> tuple[list[int], list[dict], list[str]]:
    """시작 청크의 조문이 참조하는 조문을 가져온다.

    반환: (참조 확장 article_id 목록, 참조관계, 미해결 참조 표기 목록). 시작 청크가 없으면 빈 값 셋.
    해결상태가 `remapped` 인 참조는 다시 매핑된 도착 조문을 쓰고 해결 근거를 `해결근거` 에 함께 넣는다 —
    화면에 도착 조문을 바꾼 사실을 보여주기 위해서다.
    """
    if not 시작청크:
        return [], [], []
    cur.execute(참조확장SQL, {"cids": 시작청크, "깊이": 깊이값})
    확장조문, 참조관계 = [], []
    처리한_조문 = set()
    for (_rid, sdoc, s조, 참조문자열, 관계, ddoc, d조, 상태, 해결근거,
         aid, d제목, s제목) in cur.fetchall():
        if aid not in 처리한_조문:
            처리한_조문.add(aid)
            확장조문.append(aid)
        참조관계.append({
            "from": {"doc_id": sdoc, "조번호": s조, "조제목": s제목},
            "참조문자열": 참조문자열,
            "관계": 관계,
            "to": {"doc_id": ddoc, "조번호": d조, "조제목": d제목, "article_id": aid},
            # 도착 조문을 다시 매핑했을 때만 채운다. remapped 가 아니면 None
            "해결근거": 해결근거 if 상태 == "remapped" else None,
        })
    cur.execute(DANGLING_SQL, {"cids": 시작청크})
    return 확장조문, 참조관계, [r[0] for r in cur.fetchall()]


# ── 검색 인터페이스 ────────────────────────────────────────────────────────────
def 검색(cur, 질문: str, 사업명: str | None, *, top_k: int = 5,
         후보k: int = 후보K, 사업필터: bool = 사업필터_기본, 거르기=None) -> dict:
    """질문 하나에 대한 검색 결과(dict)를 반환한다. 키는 모듈 docstring 참조.

    0건이어도 `None` 이 아니라 빈 리스트를 반환한다.
    `최고유사도` 는 dense 코사인 최고값이다 — RRF 점수는 스케일이 없어 임계치로 쓸 수 없다.
    후보가 0건이면 0.0 이라 확인 필요(게이트 B) 쪽으로 판정된다.
    `거르기`(청크 ID 목록 → 남길 청크 ID 목록, 순서 유지)를 주면 RRF 순위를 거른 뒤 top_k 를 자르고 참조 확장도 그 top_k 로 한다
    (새 경로의 다른 자금 거르기, ontology_context.검색청크_거르기). FILTER · 후보 범위는 그대로다.
    """
    벡터 = 임베딩(질문)
    d = dense(cur, 벡터, k=후보k, 사업명=사업명, 사업필터=사업필터)
    b = sparse(cur, 질문, k=후보k, 사업명=사업명, 사업필터=사업필터)
    순위 = rrf([[c for c, _ in d], b])
    if 거르기 is not None:
        순위 = 거르기(순위)
    top = 순위[:top_k]
    확장조문, 참조관계, 미해결 = 참조확장_수집(cur, top)
    return {
        "top5": top,
        "참조확장": 확장조문,
        "참조관계": 참조관계,
        "최고유사도": d[0][1] if d else 0.0,
        "미해결참조": 미해결,
        "후보수": len({c for c, _ in d} | set(b)),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _지연측정(cur) -> None:
    """검색 지연을 측정한다(CPU, 목표 200ms). 부하 조건이 실행마다 달라 목표 안인지만 본다."""
    질문들 = [
        "디자이너 쓸 맥북 250만원 사도 되나요?",
        "창업활동비 이번 달 60만원 써도 되나요?",
        "외주용역 2500만원 계약했는데 괜찮나요?",
        "홍보용 기프티콘 뿌려도 되나요?",
        "해외 전시회 출장 가는데 비행기표 되나요?",
        "직원 4대보험 회사부담분을 사업비로 내도 되나요?",
        "특허 출원 비용을 사업비로 집행할 수 있나요?",
        "사무실 임차료를 사업비에서 지출해도 되나요?",
        "시제품 제작용 3D 프린터 필라멘트 구입은 가능한가요?",
        "팀원 워크숍 숙박비를 사업비로 결제해도 되나요?",
    ]
    w = 워밍업()
    print(f"워밍업 {w:.1f}초 (모델 로드 + 첫 인코딩 + kiwi)\n")
    for 이름, fn in (("쿼리 임베딩", lambda q: 임베딩(q)),
                     ("BM25 토큰화", lambda q: 토큰화([q])),
                     ("검색 전체", lambda q: 검색(cur, q, None))):
        지연 = []
        for _ in range(3):
            for q in 질문들:
                t = time.perf_counter()
                fn(q)
                지연.append((time.perf_counter() - t) * 1000)
        지연.sort()
        p50, p95 = statistics.median(지연), 지연[int(len(지연) * 0.95) - 1]
        print(f"{이름:12} n={len(지연):3}  p50 {p50:6.1f}ms  p95 {p95:6.1f}ms  "
              f"max {지연[-1]:6.1f}ms")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", help="질문 하나를 검색해 본다")
    ap.add_argument("--사업명", default=None)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--bench", action="store_true", help="검색 지연 측정 (목표 200ms)")
    a = ap.parse_args()

    # 읽기 전용이라도 트랜잭션을 열어 두면 다른 세션의 DDL 과 락 대기가 생길 수 있어 autocommit 으로 연다.
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        if a.bench:
            _지연측정(cur)
            return
        if not a.q:
            ap.error("--q 또는 --bench 중 하나가 필요하다")
        워밍업()
        t = time.perf_counter()
        r = 검색(cur, a.q, a.사업명, top_k=a.top_k)
        경과 = (time.perf_counter() - t) * 1000

        print(f"질문: {a.q}")
        print(f"후보 {r['후보수']} · 최고유사도 {r['최고유사도']:.3f} · {경과:.0f}ms\n")
        cur.execute("""SELECT chunk_id, doc_id, 조번호, coalesce(조제목,''),
                              left(replace(text, chr(10), ' '), 76)
                         FROM corpus.chunks WHERE chunk_id = ANY(%s)""", (r["top5"],))
        순서 = {c: i for i, c in enumerate(r["top5"])}
        for cid, doc, 조, 제목, txt in sorted(cur.fetchall(), key=lambda x: 순서[x[0]]):
            print(f"  {순서[cid]+1}. {doc[:38]:<38} {조:<9} {제목[:14]}")
            print(f"     {txt}")
        print(f"\n참조확장 {len(r['참조확장'])}조 · 참조관계 {len(r['참조관계'])}건 · "
              f"미해결참조 {len(r['미해결참조'])}건")
        for c in r["참조관계"][:6]:
            해결 = f"  (해결근거: {c['해결근거']})" if c["해결근거"] else ""
            print(f"  {c['from']['조번호']} --[{c['참조문자열']}]--> "
                  f"{c['to']['doc_id'][:30]} {c['to']['조번호']}{해결}")
        if r["미해결참조"]:
            print("  미해결참조:", json.dumps(r["미해결참조"][:6], ensure_ascii=False))


if __name__ == "__main__":
    main()
