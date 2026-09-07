# -*- coding: utf-8 -*-
"""검색 축 — dense·BM25 를 RRF 로 합치고 참조 조항을 확장해 판정용 후보를 만든다.
pre-filter 는 `layer IN ('L1','L2')` 만 본다. L3 는 `tenant.l3_articles` 에서 별도로 로드한다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/retrieve.py --q "맥북 250만원 사도 되나요"
    PYTHONIOENCODING=utf-8 python scripts/retrieve.py --bench      # 쿼리 임베딩 p50 측정
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

DSN = db.DSN

# ── pre-filter — 검색 대상 청크의 조건 ────────────────────────────────────────
FILTER = """embedding IS NOT NULL AND status='active' AND parse_quality='high'
        AND retrieval_scope='진입점' AND layer IN ('L1','L2')
        AND 적용대상 IN ('창업기업','공통')"""

# 사업명이 지정된 검색에서만 그 사업의 세부관리기준으로 후보를 좁히는 절.
사업절 = " AND (사업명 IS NULL OR %(사업)s = ANY(사업명))"

# 사업 필터를 켜면 다른 사업의 세부관리기준이 섞여 들어오는 걸 막아 검색 정확도가 오르지만,
# 판정 인덱스 경계에 영향을 주는 값이라 기본은 꺼둔다. 켜는 건 이 값 한 줄이다.
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
# 깊이 1 이면 재귀가 필요 없지만, 깊이를 늘려야 할 때 대비해 CTE 형태를 유지한다.
# `dst_조번호 IS NOT NULL` 은 시작 간선과 재귀 간선 둘 다에 건다.
참조확장SQL = """
WITH RECURSIVE 시작(doc_id, 조번호) AS (
    SELECT DISTINCT doc_id, 조번호 FROM corpus.chunks WHERE chunk_id = ANY(%(cids)s)
),
폐포 AS (
    SELECT r.ref_id, r.src_doc_id, r.src_조번호, r.참조문자열, r.관계,
           r.dst_doc_id, r.dst_조번호, r.해소상태, r.보정근거, 1 AS depth
      FROM corpus.refs r JOIN 시작 s
        ON (r.src_doc_id, r.src_조번호) = (s.doc_id, s.조번호)
     WHERE r.해소상태 <> 'dangling' AND r.dst_조번호 IS NOT NULL
    UNION ALL
    SELECT r.ref_id, r.src_doc_id, r.src_조번호, r.참조문자열, r.관계,
           r.dst_doc_id, r.dst_조번호, r.해소상태, r.보정근거, p.depth + 1
      FROM corpus.refs r JOIN 폐포 p
        ON (r.src_doc_id, r.src_조번호) = (p.dst_doc_id, p.dst_조번호)
     WHERE r.해소상태 <> 'dangling' AND r.dst_조번호 IS NOT NULL AND p.depth < %(깊이)s
)
SELECT DISTINCT ON (p.ref_id)
       p.ref_id, p.src_doc_id, p.src_조번호, p.참조문자열, p.관계,
       p.dst_doc_id, p.dst_조번호, p.해소상태, p.보정근거,
       a.article_id, a.조제목, sa.조제목
  FROM 폐포 p
  JOIN corpus.doc_articles a  ON a.doc_id = p.dst_doc_id AND a.조번호 = p.dst_조번호
  LEFT JOIN corpus.doc_articles sa ON sa.doc_id = p.src_doc_id AND sa.조번호 = p.src_조번호
 ORDER BY p.ref_id
"""

# 판정 인덱스 안의 끊긴 참조만 신호다. 밖의 끊긴 참조는 정상이라 세지 않는다.
DANGLING_SQL = """
SELECT DISTINCT r.참조문자열
  FROM corpus.refs r
  JOIN (SELECT DISTINCT doc_id, 조번호 FROM corpus.chunks WHERE chunk_id = ANY(%(cids)s)) s
    ON (r.src_doc_id, r.src_조번호) = (s.doc_id, s.조번호)
 WHERE r.해소상태 = 'dangling'
 ORDER BY 1
"""

W_DENSE, W_SPARSE, RRF_K = 0.9, 0.1, 60
후보K = 50          # dense·BM25 각각에서 가져오는 후보 수
깊이 = 1            # 참조 확장 재귀 깊이

# ── 임베딩 모델 상주 ─────────────────────────────────────────────────────────
_모델 = None
_토큰화 = None
_stdout보관 = None


_모델_잠금 = threading.Lock()


def 모델():
    """KURE-v1 을 프로세스에 한 번만 올린다. CPU. 첫 호출에 ~15초, 이후 0.

    판정 스레드·워밍업·`rule_lookup.warmup()` 이 동시에 부를 수 있어 잠금을 건다 —
    잠금이 없으면 둘 다 `None` 을 보고 각자 모델을 올린다.
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
    """BM25 토큰화는 `stage2_bm25.토큰화` 를 그대로 쓴다 — 색인과 쿼리가 같은 토큰화를 써야 한다.

    `stage2_bm25` 는 import 시점에 `sys.stdout` 을 다시 감싸므로, 이 모듈을 import 하는
    쪽이 그 부작용을 맞지 않도록 되돌린다. 새 래퍼는 참조를 붙잡아 둔다 — 버리면 GC 가
    `__del__` 에서 원래 버퍼를 닫아버린다.
    """
    global _토큰화, _stdout보관
    if _토큰화 is None:
        paths.ensure_on_path()
        원래 = sys.stdout
        from stage2_bm25 import 토큰화 as _t
        if sys.stdout is not 원래:
            _stdout보관 = sys.stdout          # GC 방지. 버리면 원래 버퍼가 닫힌다
            sys.stdout = 원래
        _토큰화 = _t
    return _토큰화(texts)


def 워밍업() -> float:
    """모델 로드 + 첫 인코딩까지 미리 태운다. 첫 판정 요청이 15초를 먹지 않게 한다.
    BM25 쪽 kiwi 도 같이 깨운다 (첫 tokenize 가 사전 로딩으로 ~1초)."""
    t = time.perf_counter()
    모델().encode(["워밍업"], normalize_embeddings=True, convert_to_numpy=True,
                  show_progress_bar=False)
    토큰화(["워밍업"])
    return time.perf_counter() - t


def 임베딩(질문: str) -> str:
    """질문 -> pgvector 리터럴. 정규화된 벡터라 `1 - (a <=> b)` 가 코사인 유사도다."""
    v = 모델().encode([질문], normalize_embeddings=True, convert_to_numpy=True,
                      show_progress_bar=False)[0]
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ── 검색기 3종 ───────────────────────────────────────────────────────────────
def _사업절(사업명: str | None, 사업필터: bool) -> str:
    return 사업절 if (사업필터 and 사업명) else ""


def dense(cur, 벡터: str, *, k: int = 후보K, 사업명: str | None = None,
          사업필터: bool = 사업필터_기본) -> list[tuple[int, float]]:
    """(chunk_id, 코사인유사도) 를 순위대로. 게이트값은 이 첫 원소의 유사도다."""
    cur.execute(DENSE.format(사업=_사업절(사업명, 사업필터)),
                {"v": 벡터, "k": k, "사업": 사업명})
    return [(r[0], float(r[1])) for r in cur.fetchall()]


def sparse(cur, 질문: str, *, k: int = 후보K, 사업명: str | None = None,
           사업필터: bool = 사업필터_기본) -> list[int]:
    cur.execute(BM25.format(사업=_사업절(사업명, 사업필터)),
                {"terms": 토큰화([질문])[0], "k": k, "사업": 사업명})
    return [r[0] for r in cur.fetchall()]


def rrf(순위목록: list[list[int]], k: int = RRF_K,
        가중: tuple[float, ...] = (W_DENSE, W_SPARSE)) -> list[int]:
    """순위의 역수를 가중해 더한다 — 점수 스케일이 달라도 섞인다.
    한쪽에만 나온 청크는 그 항이 0 이다."""
    점수: dict[int, float] = {}
    for 순위, w in zip(순위목록, 가중):
        for i, cid in enumerate(순위, 1):
            점수[cid] = 점수.get(cid, 0.0) + w / (k + i)
    return [c for c, _ in sorted(점수.items(), key=lambda x: -x[1])]


# ── 참조 확장 ─────────────────────────────────────────────────────────────────────
def 폐포수집(cur, 진입점: list[int], *, 깊이값: int = 깊이) -> tuple[list[int], list[dict], list[str]]:
    """진입점 청크가 가리키는 조항을 끌어온다. (참조 확장 article_id, 참조사슬, 끊긴 참조)

    `shifted` 는 보정된 dst 를 쓰되 원래 표기도 함께 넘긴다 — 보정 사실을 화면에 보여주는 재료다.
    """
    if not 진입점:
        return [], [], []
    cur.execute(참조확장SQL, {"cids": 진입점, "깊이": 깊이값})
    폐포, 사슬 = [], []
    본 = set()
    for (_rid, sdoc, s조, 표기, 관계, ddoc, d조, 상태, 보정근거,
         aid, d제목, s제목) in cur.fetchall():
        if aid not in 본:
            본.add(aid)
            폐포.append(aid)
        사슬.append({
            "from": {"doc_id": sdoc, "조번호": s조, "조제목": s제목},
            "표기": 표기,
            "관계": 관계,
            "to": {"doc_id": ddoc, "조번호": d조, "조제목": d제목, "article_id": aid},
            # 보정이 있을 때만 채운다. shifted 가 아니면 None
            "보정": 보정근거 if 상태 == "shifted" else None,
        })
    cur.execute(DANGLING_SQL, {"cids": 진입점})
    return 폐포, 사슬, [r[0] for r in cur.fetchall()]


# ── 검색 인터페이스 ────────────────────────────────────────────────────────────
def 검색(cur, 질문: str, 사업명: str | None, *, top_k: int = 5,
         후보k: int = 후보K, 사업필터: bool = 사업필터_기본) -> dict:
    """질문 하나 -> 판정에 넘길 검색 결과 한 벌.

    0건이어도 `None` 이 아니라 빈 리스트를 돌려준다.
    `게이트값` 은 dense 코사인 최고값이다 — RRF 점수는 스케일이 없어 임계치로 못 쓴다.
    후보가 0건이면 0.0 (판단불가 쪽으로 기운다).
    """
    벡터 = 임베딩(질문)
    d = dense(cur, 벡터, k=후보k, 사업명=사업명, 사업필터=사업필터)
    b = sparse(cur, 질문, k=후보k, 사업명=사업명, 사업필터=사업필터)
    순위 = rrf([[c for c, _ in d], b])
    top = 순위[:top_k]
    폐포, 사슬, dang = 폐포수집(cur, top)
    return {
        "top5": top,
        "폐포": 폐포,
        "참조사슬": 사슬,
        "게이트값": d[0][1] if d else 0.0,
        "dangling": dang,
        "후보수": len({c for c, _ in d} | set(b)),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _p50(cur, 기록: bool = False) -> None:
    """쿼리 임베딩 CPU 지연 측정. 예산 200ms."""
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
    측정 = {"워밍업_초": w}
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
        측정[f"{이름}.p50_ms"], 측정[f"{이름}.p95_ms"] = p50, p95
        측정[f"{이름}.max_ms"] = 지연[-1]
        print(f"{이름:12} n={len(지연):3}  p50 {p50:6.1f}ms  p95 {p95:6.1f}ms  "
              f"max {지연[-1]:6.1f}ms")

    if 기록:
        # 측정 결과를 `eval.runs` 에 남긴다. 병렬 실행 중이면 CPU 경합으로 값이 부풀려질 수
        # 있다는 사실을 `설정` 에 적는다.
        import eval_store
        run_id = eval_store.기록({
            "종류": "retrieval",
            "설정": {"측정": "C5 쿼리 지연", "장치": "cpu", "모델": "KURE-v1",
                     "질문수": len(질문들), "반복": 3, "사업필터": False,
                     "주의": "8세션 병렬 실행 중이면 CPU 경합으로 상향 편향된다. "
                             "🔴 부하 조건이 실행마다 달라 run 끼리 지연을 비교하면 안 된다 "
                             "— 예산(200ms) 안인지만 본다", "세션": "C"},
            "문항수": len(질문들) * 3, "지표": 측정,
            "라벨": "C/retrieve --bench 쿼리 지연",
            "비고": "🔴 이 값은 다른 --bench run 과 비교 불가다. 같은 기계에서 8세션이 "
                    "병렬로 도는 중이라 부하가 실행마다 다르다. 판정 기준은 '예산 200ms "
                    "안인가' 하나뿐이고, run 간 차이를 개선/퇴행으로 읽지 마라."})
        print(f"\n[기록] eval.runs run_id={run_id} (종류=retrieval · C5 지연)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", help="질문 하나를 검색해 본다")
    ap.add_argument("--사업", default=None)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--bench", action="store_true", help="C5 지연 실측")
    ap.add_argument("--기록", action="store_true",
                    help="--bench 결과를 eval.runs 에 남긴다 (D4 eval_store)")
    a = ap.parse_args()

    # 읽기 전용인데도 트랜잭션을 붙들면 다른 세션의 DDL 과 교착이 날 수 있다. autocommit 으로 푼다.
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        if a.bench:
            _p50(cur, 기록=a.기록)
            return
        if not a.q:
            ap.error("--q 또는 --bench 중 하나가 필요하다")
        워밍업()
        t = time.perf_counter()
        r = 검색(cur, a.q, a.사업, top_k=a.top_k)
        경과 = (time.perf_counter() - t) * 1000

        print(f"질문: {a.q}")
        print(f"후보 {r['후보수']} · 게이트값 {r['게이트값']:.3f} · {경과:.0f}ms\n")
        cur.execute("""SELECT chunk_id, doc_id, 조번호, coalesce(조제목,''),
                              left(replace(text, chr(10), ' '), 76)
                         FROM corpus.chunks WHERE chunk_id = ANY(%s)""", (r["top5"],))
        순서 = {c: i for i, c in enumerate(r["top5"])}
        for cid, doc, 조, 제목, txt in sorted(cur.fetchall(), key=lambda x: 순서[x[0]]):
            print(f"  {순서[cid]+1}. {doc[:38]:<38} {조:<9} {제목[:14]}")
            print(f"     {txt}")
        print(f"\n폐포 {len(r['폐포'])}조 · 참조사슬 {len(r['참조사슬'])}건 · "
              f"dangling {len(r['dangling'])}건")
        for c in r["참조사슬"][:6]:
            보정 = f"  (보정: {c['보정']})" if c["보정"] else ""
            print(f"  {c['from']['조번호']} --[{c['표기']}]--> "
                  f"{c['to']['doc_id'][:30]} {c['to']['조번호']}{보정}")
        if r["dangling"]:
            print("  dangling:", json.dumps(r["dangling"][:6], ensure_ascii=False))


if __name__ == "__main__":
    main()
