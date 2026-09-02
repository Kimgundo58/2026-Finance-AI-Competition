# -*- coding: utf-8 -*-
"""C 세션 선언 변형 ①  — "진입점 문서의 부속물 청크도 진입점으로 본다" 의 순효과.

🔴 **실험이다. `retrieve.py` 도 DB 도 건드리지 않는다.** SQL 문자열만 이 파일 안에서
   다시 조립해 재고, 결과를 보고한다. 채택 여부는 A 의 판단이다 (계약 §9 BLOCKED).

## 왜 재나

`eval_retrieval.py --진단` 이 hit@5 미스 33건을 갈라 보니 **23건이 필터밖**이었고,
그중 6건이 `retrieval_scope='폐포전용'` 때문이었다. 그런데 그 6건의 정답은
`예비창업패키지 세부관리기준 붙임2`·`초격차 참고2` 처럼 **진입점 문서 안의 부속물**이다.

실측하면 부속물 청크 452개가 **예외 없이 전부** `폐포전용` 이고, 진입점 문서 안에만
137개가 있다. 문서 단위로 진입점을 판정해 놓고 청크 단위에서 부속물을 일괄 강등한
결과로 보인다.

그런데 `예비창업패키지 세부관리기준 제22조②` 는 이렇게 쓴다:

    "지침에서 정하지 않은 사업비 집행 세부사항은 다음 각 호 및 본 기준
     '[붙임 2] 창업기업등 사업비 비목 해설표' 에서 정하는 바에 따른다"

즉 붙임2 는 참조 **도착지**가 아니라 본문이 위임한 **규범 본체**다. `RAG.md` §4-2 의
진입점 기준("이 돈 써도 되나요의 답이 그 문서 안에 문장으로 적혀 있는가")에 정면으로 맞는다.
그리고 §4-3 실측대로 폐포로도 도달하지 않는다(내 폐포 커버리지 52.9% -> 52.9%).

## 무엇을 조심하나

후보 풀이 1,252 -> 1,389 (+10.9%) 로 늘어난다. **늘어난 후보가 지금 맞히던 문항을
밀어낼 수 있다.** 그래서 회수만 세지 않고 **뒤집힘을 양방향으로** 센다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/_work/_C_실험_부속물scope.py
"""
from __future__ import annotations

import os
import sys

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import retrieve                      # noqa: E402
import eval_retrieval as ev          # noqa: E402

# 현행과 확장. 확장은 `retrieval_scope='진입점'` 을 문서 단위로 완화한 것 하나뿐이다.
확장필터 = """embedding IS NOT NULL AND status='active' AND parse_quality='high'
        AND layer IN ('L1','L2') AND 적용대상 IN ('창업기업','공통')
        AND (retrieval_scope='진입점'
             OR doc_id IN (SELECT doc_id FROM corpus.documents
                            WHERE retrieval_scope='진입점'))"""


def sql(F: str) -> tuple[str, str]:
    """retrieve.py 의 DENSE·BM25 를 필터만 바꿔 다시 조립한다 (원본은 안 건드린다)."""
    dense = (f"""SELECT chunk_id, 1 - (embedding <=> %(v)s::extensions.vector(1024)) AS sim
                   FROM corpus.chunks WHERE {F}{{사업}}
                  ORDER BY embedding <=> %(v)s::extensions.vector(1024) LIMIT %(k)s""")
    bm25 = (retrieve.BM25.split("WHERE ct.chunk_id IN")[0]
            + f"WHERE ct.chunk_id IN (SELECT chunk_id FROM corpus.chunks WHERE {F}{{사업}})"
            + "\nGROUP BY ct.chunk_id ORDER BY score DESC LIMIT %(k)s\n")
    return dense, bm25


def 검색묶음(cur, F, vec, q, 사업, k):
    d_sql, b_sql = sql(F)
    절 = retrieve.사업절 if 사업 else ""
    cur.execute(d_sql.format(사업=절), {"v": vec, "k": k, "사업": 사업})
    dn = [r[0] for r in cur.fetchall()]
    cur.execute(b_sql.format(사업=절),
                {"terms": retrieve.토큰화([q])[0], "k": k, "사업": 사업})
    bm = [r[0] for r in cur.fetchall()]
    return retrieve.rrf([dn, bm])


def main() -> None:
    K = 50
    with psycopg.connect(retrieve.DSN, autocommit=True) as conn:
        cur = conn.cursor()
        for 이름, F in (("현행", retrieve.FILTER), ("확장", 확장필터)):
            cur.execute(f"SELECT count(*) FROM corpus.chunks WHERE {F}")
            print(f"{이름} 후보 풀 {cur.fetchone()[0]:,}")

        cur.execute("""SELECT gold_id, 세트, 질문, 사업명 FROM eval.golden_set
                        WHERE 정답근거 IS NOT NULL ORDER BY gold_id""")
        문항 = []
        for gid, 세트, q, 사업 in cur.fetchall():
            정답 = ev.정답청크_고정(cur, gid)
            if 정답:
                문항.append((gid, 세트, q, 정답,
                             None if not 사업 or 사업.startswith("공통") else 사업))
        print(f"평가 {len(문항)}문항 (golden_chunks 고정 · 사업필터 on)\n")

        retrieve.워밍업()
        결과 = {"현행": [], "확장": []}
        뒤집 = []
        for gid, 세트, q, 정답, 사업 in 문항:
            vec = retrieve.임베딩(q)
            친 = {}
            for 이름, F in (("현행", retrieve.FILTER), ("확장", 확장필터)):
                순위 = 검색묶음(cur, F, vec, q, 사업, K)[:K]
                결과[이름].append((순위, 정답))
                친[이름] = any(c in 정답 for c in 순위[:5])
            if 친["현행"] != 친["확장"]:
                뒤집.append((gid, 세트, "회수" if 친["확장"] else "🔴손실"))

        KS = [1, 5, 10, 20, K]
        print(f"{'':8} " + " ".join(f"{'hit@'+str(k):>8}" for k in KS) + f"{'MRR':>8}")
        for 이름 in ("현행", "확장"):
            g = ev.지표(결과[이름], KS)
            print(f"{이름:8} " + " ".join(f"{g['hit@'+str(k)]:7.1f}%" for k in KS)
                  + f" {g['MRR']:7.3f}")
        회수 = [x for x in 뒤집 if x[2] == "회수"]
        손실 = [x for x in 뒤집 if x[2] != "회수"]
        print(f"\nhit@5 회수 {len(회수)}건 · 손실 {len(손실)}건  ->  순증 {len(회수)-len(손실)}문항")
        for x in 뒤집:
            print("   ", x)


if __name__ == "__main__":
    main()
