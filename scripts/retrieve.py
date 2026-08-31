# -*- coding: utf-8 -*-
"""검색 축 — Agent.md (3)-b~e 를 한 모듈로. `RAG.md` §4 가 정본이다.

`eval_retrieval.py` 안에 흩어져 있던 SQL·RRF·임베딩을 여기로 뽑았다. 평가와 실전이
**같은 코드를 부르게** 하는 것이 목적이다 — 둘이 갈라지면 잰 숫자가 실전을 설명하지 못한다.

## 이 모듈이 지키는 것

  · pre-filter 가 검색보다 먼저다 (`FILTER`). `적용대상 IN (...)` 은 NULL 을 통과시키지
    않으므로 태깅이 비면 조용히 사라진다 — 회귀 방어로 남겨 둔 조건이다 (§4-2)
  · RRF 가중 0.9/0.1 · K=60. 🔴 초판 0.6/0.4 는 hit@5 가 7.2%p 낮았다 (§4-4)
  · 폐포는 **깊이 1 · `dst_조번호 IS NOT NULL`** 만. 조 없는 인용을 펴면 근로기준법
    하나가 6,026청크를 끌고 온다 (2026-08-31 실측 · §4-3)
  · 게이트값은 **dense 코사인 최고값**. RRF 점수는 스케일이 없어 임계치로 못 쓴다

## 🔴 판정 인덱스 경계

`FILTER` 는 `layer IN ('L1','L2')` 다. L3 는 `tenant.l3_articles` 에서 검색 없이 통째
로드하며(E의 `l3_load`), 여기서 절대 섞지 않는다 — 테이블이 달라 누수가 구조적으로 불가능하다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/retrieve.py --q "맥북 250만원 사도 되나요"
    PYTHONIOENCODING=utf-8 python scripts/retrieve.py --bench      # 쿼리 임베딩 p50 실측
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# ── (3)-b pre-filter — 평가와 실전이 같은 집합을 봐야 한다 ────────────────────
FILTER = """embedding IS NOT NULL AND status='active' AND parse_quality='high'
        AND retrieval_scope='진입점' AND layer IN ('L1','L2')
        AND 적용대상 IN ('창업기업','공통')"""

# 사업 필터는 별도 절로 뗐다 — C7 실측 전까지 기본은 끔. 아래 `사업필터_기본` 주석 참조.
사업절 = " AND (사업명 IS NULL OR %(사업)s = ANY(사업명))"

# 🔴 C7 실측 결론 (2026-08-31 · `eval_retrieval.py --c7`) — **필터는 무력하지 않다.**
#
#    "사업명 97% NULL 이라 필터가 안 걸린다" 는 전제가 틀렸다. 전체 20,525청크 기준으로는
#    97.6% 가 NULL 이 맞지만, 검색 후보(진입점 1,252)로 좁히면 **204개(16.3%)에 사업명이
#    있고 그게 전부 8개 사업의 세부관리기준**이다. 그 204개가 서로의 top-5 를 잡아먹는다.
#
#    사업 지정 44문항 짝지어 비교 (같은 임베딩 · 필터만 다름):
#        hit@1  9.1 -> 15.9   hit@5  27.3 -> 36.4   hit@10 34.1 -> 40.9
#        hit@20 43.2 -> 52.3  hit@50 50.0 -> 56.8   MRR 0.158 -> 0.251
#        15개 지표 전부 상승 · 하락 0 · hit@5 뒤집힘 4건 전부 False->True
#    전체 70문항 RRF hit@5 로는 52.9% -> 58.6%.
#
#    🔴 이건 튜닝이 아니라 **인덱스 경계 위반**이다. gold_id=10(예비창업 외주용역)의
#       필터 끈 top-5 는 2~5위가 재도전·창업도약·모두의창업 **세부관리기준**이었다.
#       CLAUDE.md 절대규칙 — "남의 규정이 인용되는 순간 그 자체가 오답".
#       필터를 켜면 그 자리에 예비창업 제22조와 L1 계약 조항이 들어온다.
#
#    그런데도 기본을 False 로 둔다 — 오늘 밤 8세션이 52.9% 를 공통 기준선으로 쓰고
#    (D7 하이퍼파라미터 스윕 포함) 있어서, 여기서 조용히 바꾸면 오늘 잰 E2E 낙폭이
#    무엇 때문인지 못 가린다. **A 에게 넘긴 판단이다** — 켜는 건 이 값 한 줄이다.
#    C8(사업명 백필)은 조건("필터 무력")이 성립하지 않아 실행하지 않았다.
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

# ── 폐포 (§4-3) ──────────────────────────────────────────────────────────────
# 깊이 1 이면 재귀가 필요 없지만 CTE 형태를 유지한다 — 코퍼스가 바뀌어 깊이를 다시 재야
# 할 때 `깊이` 하나만 올리면 되고, RAG.md §4-3 의 SQL 과 눈으로 대조된다.
#   🔴 `dst_조번호 IS NOT NULL` 은 시작 간선에도, 재귀 간선에도 둘 다 건다.
폐포SQL = """
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

# 판정 인덱스 **안의** dangling 만 신호다 (§4-3). 밖의 dangling 은 정상이라 세지 않는다.
DANGLING_SQL = """
SELECT DISTINCT r.참조문자열
  FROM corpus.refs r
  JOIN (SELECT DISTINCT doc_id, 조번호 FROM corpus.chunks WHERE chunk_id = ANY(%(cids)s)) s
    ON (r.src_doc_id, r.src_조번호) = (s.doc_id, s.조번호)
 WHERE r.해소상태 = 'dangling'
 ORDER BY 1
"""

W_DENSE, W_SPARSE, RRF_K = 0.9, 0.1, 60
후보K = 50          # dense·BM25 각각의 깊이. §4-2 "top-50 + top-50 -> RRF -> top-5"
깊이 = 1            # §4-3 실측: 1·2·3 의 hit@5+폐포가 전부 같다

# ── 임베딩 상주 (C5) ─────────────────────────────────────────────────────────
_모델 = None
_토큰화 = None
_stdout보관 = None


def 모델():
    """KURE-v1 을 프로세스에 한 번만 올린다. CPU. 첫 호출에 ~15초, 이후 0."""
    global _모델
    if _모델 is None:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("nlpai-lab/KURE-v1", device="cpu")
        m.max_seq_length = 1024
        _모델 = m
    return _모델


def 토큰화(texts: list[str]) -> list[list[str]]:
    """BM25 토큰화는 `stage2_bm25.토큰화` 를 그대로 쓴다 — 색인과 쿼리가 갈라지면
    동등성 검증이 재현되지 않는다 (`RAG.md` §2-4).

    🔴 `stage2_bm25` 는 import 시점에 `sys.stdout` 을 무조건 다시 감싼다. 이 모듈을
       import 하는 쪽(A 의 orchestrate)이 그 부작용을 맞으면 안 되므로 되돌린다.
       다만 새 래퍼를 그냥 버리면 GC 가 __del__ 에서 밑의 버퍼를 닫아버린다
       ("I/O operation on closed file") — 그래서 참조를 붙잡아 둔다.
    """
    global _토큰화, _stdout보관
    if _토큰화 is None:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    """순위의 역수를 가중해 더한다 — 점수 스케일이 달라도 섞인다 (Agent.md (3)-e).
    한쪽에만 나온 청크는 그 항이 0 이다."""
    점수: dict[int, float] = {}
    for 순위, w in zip(순위목록, 가중):
        for i, cid in enumerate(순위, 1):
            점수[cid] = 점수.get(cid, 0.0) + w / (k + i)
    return [c for c, _ in sorted(점수.items(), key=lambda x: -x[1])]


# ── 폐포 ─────────────────────────────────────────────────────────────────────
def 폐포수집(cur, 진입점: list[int], *, 깊이값: int = 깊이) -> tuple[list[int], list[dict], list[str]]:
    """진입점 청크가 가리키는 조항을 끌어온다. (폐포 article_id, 참조사슬, dangling)

    `shifted` 는 보정된 dst 를 쓰되 **원래 표기도 함께** 넘긴다 — 화면 7 이
    "귀 기관 규정은 제33조라 하지만 현행 기준 제39조입니다" 를 그리는 재료다.
    """
    if not 진입점:
        return [], [], []
    cur.execute(폐포SQL, {"cids": 진입점, "깊이": 깊이값})
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
            # 보정이 있을 때만 채운다. shifted 가 아니면 None (동결 인터페이스 §4)
            "보정": 보정근거 if 상태 == "shifted" else None,
        })
    cur.execute(DANGLING_SQL, {"cids": 진입점})
    return 폐포, 사슬, [r[0] for r in cur.fetchall()]


# ── 동결 인터페이스 (`0831_최종구현.md` §4) ──────────────────────────────────
def 검색(cur, 질문: str, 사업명: str | None, *, top_k: int = 5,
         후보k: int = 후보K, 사업필터: bool = 사업필터_기본) -> dict:
    """질문 하나 -> 판정에 넘길 검색 결과 한 벌.

    🔴 0건이어도 `None` 을 돌려주지 않는다. 빈 리스트다 — A 가 `for` 로 받는다.
    🔴 `게이트값` 은 dense 코사인 최고값이다. RRF 점수는 스케일이 없어 임계치로 못 쓴다.
       후보가 0건이면 0.0 (판단불가 쪽으로 기운다 — 기본값은 언제나 판단불가다).
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
    """C5 — 쿼리 임베딩 CPU 지연 실측. 예산 200ms."""
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
        # C5 를 `eval.runs` 에 남긴다 — 내일 "이 지연이 어느 조건이었나" 를 되짚을 수 있게.
        # 🔴 8세션 병렬 중이면 CPU 경합으로 값이 부풀려진다. 그 사실을 `설정` 에 적는다.
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

    # 🔴 읽기 전용인데도 트랜잭션을 붙들면 다른 세션의 DDL 과 교착이 난다
    #    (2026-08-31 8세션 병렬 중 DeadlockDetected 실측). autocommit 으로 푼다.
    with psycopg.connect(DSN, autocommit=True) as conn:
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
