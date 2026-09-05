# -*- coding: utf-8 -*-
"""Stage 2-e : BM25 역색인 -> `corpus.chunk_terms` / `chunk_len` / `term_df`.

형태소 분석은 앱이 색인 시점에 하고 **결과 토큰만 적재**한다 (`RAG.md` §2-4).
재인덱싱이 트랜잭션 하나가 되고 워커가 상태를 안 가진다.

🔴 **색인 입력은 `chunks.text` 가 아니라 `[컨텍스트 헤더] + text` 다** (§3-5).
   임베딩과 같은 입력을 써야 두 검색기가 같은 것을 본다. 입력은 stage2_chunk.py 가
   내보낸 `_stage2_chunks.jsonl` 이고 chunk_id 로 정렬돼 있다.

🔴 **토큰화 정책은 색인과 쿼리에 똑같이 적용한다.** 이게 동등성 검증의 재현 조건이라
   `토큰화()` 를 앱도 그대로 import 해서 쓴다. 여기서만 고치면 조용히 어긋난다.

🔴 **`--보충` 은 `stage2_chunk` 를 import 하지 않는다.** `장맵()`/`헤더()` 를 그대로
   가져다 쓰고 싶지만, `stage2_chunk` 는 `tag_apply_target` 을 물고 오고 그건
   module-level 에서 `sys.stdout` 을 다시 감싼다 — 이 파일이 이미 위에서 한 번
   감싸 놓은 뒤라 두 번째 감싸기가 첫 래퍼의 버퍼를 닫아 `ValueError: I/O
   operation on closed file` 로 죽는다(실측, `stage2_chunk.py` 상단 주석과 같은
   원인). 그래서 `장맵()`/`헤더()` 는 아래에 그대로 복제해 뒀다 — 원본을 고치면
   여기도 같이 고친다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/stage2_bm25.py
    PYTHONIOENCODING=utf-8 python scripts/stage2_bm25.py --verify   # bm25s 겹침률
    PYTHONIOENCODING=utf-8 python scripts/stage2_bm25.py --보충 --dry-run  # 누락분 개수만
    PYTHONIOENCODING=utf-8 python scripts/stage2_bm25.py --보충            # 누락분만 채운다
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "scripts" / "_work" / "_stage2_chunks.jsonl"

# 채택 품사 (`RAG.md` §2-4). 불용어 목록은 두지 않는다 — IDF 가 감쇠시킨다.
채택품사 = frozenset({
    "NNG", "NNP", "NNB",      # 체언
    "VV", "VA",               # 용언 어간
    "XR",                     # 어근
    "SL", "SN", "SH",         # 외국어 · 숫자 · 한자
})

_kiwi = None


def kiwi():
    global _kiwi
    if _kiwi is None:
        from kiwipiepy import Kiwi
        _kiwi = Kiwi()
    return _kiwi


def 토큰화(texts: list[str]) -> list[list[str]]:
    """색인·쿼리 공용. 영문은 소문자화한다."""
    out = []
    for 토큰들 in kiwi().tokenize(texts):
        out.append([t.form.lower() for t in 토큰들 if t.tag in 채택품사])
    return out


# ── `stage2_chunk.py` 복제분 (import 하면 죽는다 — 위 docstring 참조) ──────────
import re as _re

_RE_장 = _re.compile(r"제\s*(\d+)\s*장\s*([^\n<>]{0,20})")


def _장맵(articles: list[dict]) -> dict[str, str]:
    """조번호 -> 그 조가 속한 장. `stage2_chunk.장맵()` 과 동일 로직."""
    out, state = {}, ""
    for a in articles:
        out[a["조번호"]] = state
        for m in _RE_장.finditer(a.get("본문") or ""):
            제목 = _re.sub(r"\s+", " ", m.group(2)).strip(" <>〈〉")
            state = f"제{m.group(1)}장 {제목}".strip()
    return out


def _헤더(layer, 사업, doc_id, 장, 조번호, 조제목) -> str:
    """`stage2_chunk.헤더()` 와 동일 로직 — 임베딩과 같은 입력이어야 한다 (§3-5)."""
    문서 = _re.sub(r"^(붙임\d*\.\s*|첨부\s*)", "", doc_id).strip()
    조 = f"{조번호}({조제목})" if 조제목 else str(조번호)
    칸 = [layer, "·".join(사업) if 사업 else "공통", 문서[:60], 장 or "-", 조]
    return "[" + " | ".join(칸) + "]"


def 보충(dry_run: bool = False) -> None:
    """전체 재구축(TRUNCATE) 대신 `corpus.chunk_len` 에 없는 chunk_id 만 채운다.

    스코프 재적재(`_참고3_scoped_reload.py` 류)가 `corpus.chunks` 만 만지고
    BM25 세 테이블을 안 건드려 생기는 구멍을 메우는 경로다. `토큰화()` 는 색인
    본체와 같은 함수를 그대로 쓴다 — 여기서 새로 정의하면 색인/질의 규칙이 갈린다.
    """
    from _lib import db

    # audit_db.py 의 "BM25 색인 누락" 정의와 같은 anti-join (chunk_len 을 닻으로 쓴다)
    SQL_누락 = """
        SELECT c.chunk_id, c.doc_id, c.layer, c.사업명, c.조번호, c.조제목, c.text
          FROM corpus.chunks c
         WHERE NOT EXISTS (SELECT 1 FROM corpus.chunk_len l WHERE l.chunk_id = c.chunk_id)
         ORDER BY c.doc_id, c.chunk_id
    """
    with db.connect() as conn:
        missing = conn.execute(SQL_누락).fetchall()
        print(f"chunk_len 누락 {len(missing):,}건")
        if not missing:
            print("보충할 것 없음.")
            return

        문서별 = Counter(r[1] for r in missing)
        for doc_id, n in 문서별.most_common():
            print(f"  · {doc_id[:50]:<50} {n}건")

        if dry_run:
            print("\n--dry-run — DB 를 쓰지 않았다.")
            return

        # doc_id 별 장맵은 doc_articles 전체가 있어야 계산된다 — 문서당 한 번만.
        장맵_캐시: dict[str, dict[str, str]] = {}
        texts: list[str] = []
        for chunk_id, doc_id, layer, 사업, 조번호, 조제목, text in missing:
            if doc_id not in 장맵_캐시:
                arts = [{"조번호": r[0], "본문": r[1]} for r in conn.execute(
                    "SELECT 조번호, 본문 FROM corpus.doc_articles WHERE doc_id=%s ORDER BY article_id",
                    (doc_id,)).fetchall()]
                장맵_캐시[doc_id] = _장맵(arts)
            장 = 장맵_캐시[doc_id].get(조번호, "")
            h = _헤더(layer, 사업, doc_id, 장, 조번호, 조제목)
            texts.append(f"{h}\n{text}")

        t = time.time()
        docs = 토큰화(texts)
        print(f"\n  토큰화 {time.time() - t:.0f}초", flush=True)

        postings = [(r[0], term, tf) for r, d in zip(missing, docs)
                    for term, tf in Counter(d).items()]
        with conn.cursor() as cur:
            # 한 트랜잭션. ON CONFLICT DO NOTHING — 재실행해도 안전(멱등).
            cur.executemany(
                "INSERT INTO corpus.chunk_len (chunk_id, dl) VALUES (%s,%s) "
                "ON CONFLICT (chunk_id) DO NOTHING",
                [(r[0], len(d)) for r, d in zip(missing, docs)])
            cur.executemany(
                "INSERT INTO corpus.chunk_terms (chunk_id, term, tf) VALUES (%s,%s,%s) "
                "ON CONFLICT (chunk_id, term) DO NOTHING",
                postings)
            cur.execute("REFRESH MATERIALIZED VIEW corpus.term_df;")
        conn.commit()
        posting = len(postings)

        n_t, n_l, n_df = conn.execute("""
            SELECT (SELECT count(*) FROM corpus.chunk_terms),
                   (SELECT count(*) FROM corpus.chunk_len),
                   (SELECT count(*) FROM corpus.term_df)
        """).fetchone()
        print(f"보충 {len(missing):,}건 · 신규 포스팅 {posting:,} · "
              f"누계 chunk_terms {n_t:,} · chunk_len {n_l:,} · 어휘 {n_df:,}")


def 적재(rows: list[dict]) -> None:
    from _lib import db
    t = time.time()
    docs = 토큰화([r["text"] for r in rows])
    print(f"  토큰화 {time.time() - t:.0f}초", flush=True)

    총토큰 = sum(len(d) for d in docs)
    posting = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            # 한 트랜잭션. 중간 상태가 검색에 노출되지 않는다.
            cur.execute("TRUNCATE corpus.chunk_terms, corpus.chunk_len;")
            with cur.copy("COPY corpus.chunk_len (chunk_id, dl) FROM STDIN") as cp:
                for r, d in zip(rows, docs):
                    cp.write_row((r["chunk_id"], len(d)))
            with cur.copy("COPY corpus.chunk_terms (chunk_id, term, tf) FROM STDIN") as cp:
                for r, d in zip(rows, docs):
                    for term, tf in Counter(d).items():
                        cp.write_row((r["chunk_id"], term, tf))
                        posting += 1
            cur.execute("REFRESH MATERIALIZED VIEW corpus.term_df;")
        conn.commit()

        n_t, n_l, n_df, avgdl = conn.execute("""
            SELECT (SELECT count(*) FROM corpus.chunk_terms),
                   (SELECT count(*) FROM corpus.chunk_len),
                   (SELECT count(*) FROM corpus.term_df),
                   (SELECT avg(dl) FROM corpus.chunk_len)
        """).fetchone()
    print(f"  chunk_terms {n_t:,} · chunk_len {n_l:,} · 어휘 {n_df:,} · "
          f"평균길이 {avgdl:.1f} · 총토큰 {총토큰:,}")
    assert posting == n_t, f"포스팅 불일치 {posting} vs {n_t}"


# ── 동등성 검증 ──────────────────────────────────────────────────────────────
질의들 = [
    "노트북 구입 가능한가요",
    "창업기업 인건비 대표자 급여",
    "해외 출장 항공료 지급 기준",
    "사업비 정산 회계감사",
    "부가가치세 환급 사업비 처리",
    "지식재산권 출원 비용",
    "사무실 임차료 사업비",
    "재료비 사전승인 필요한가",
    "협약 변경 승인 절차",
    "제재 환수 부정사용",
]


def verify() -> None:
    """SQL BM25 와 `bm25s` 의 top-20 겹침률. 정답셋 평가보다 **먼저** 한다."""
    try:
        import bm25s
    except ImportError:
        sys.exit("bm25s 가 없다.  pip install bm25s")
    import numpy as np
    from _lib import db

    rows = [json.loads(l) for l in JSONL.open(encoding="utf-8")]
    ids = np.array([r["chunk_id"] for r in rows])
    print(f"bm25s 색인 {len(rows):,}건 (같은 토큰화)...", flush=True)
    corpus = 토큰화([r["text"] for r in rows])
    # k1·b 를 SQL 쪽과 맞춘다. method='lucene' 이 §2-4 의 식과 같은 계열이다.
    idx = bm25s.BM25(k1=1.2, b=0.75, method="lucene")
    idx.index(corpus, show_progress=False)

    SQL = """
    WITH q(term) AS (SELECT unnest(%(terms)s::text[])),
         s AS (SELECT count(*)::numeric n, avg(dl)::numeric avgdl FROM corpus.chunk_len)
    SELECT ct.chunk_id,
           sum( ln(1 + (s.n - df.df + 0.5) / (df.df + 0.5))
                * (ct.tf * 2.2) / (ct.tf + 1.2 * (0.25 + 0.75 * cl.dl / s.avgdl)) ) AS score
    FROM q
    JOIN corpus.chunk_terms ct ON ct.term = q.term
    JOIN corpus.term_df     df ON df.term = q.term
    JOIN corpus.chunk_len   cl ON cl.chunk_id = ct.chunk_id
    CROSS JOIN s
    GROUP BY ct.chunk_id
    ORDER BY score DESC LIMIT 20;
    """
    겹침 = []
    with db.connect() as conn:
        for q in 질의들:
            terms = 토큰화([q])[0]
            sql_top = [r[0] for r in conn.execute(SQL, {"terms": terms}).fetchall()]
            res, _ = idx.retrieve([terms], k=20, show_progress=False)
            s_top = [int(ids[i]) for i in res[0]]
            r = len(set(sql_top) & set(s_top)) / 20
            겹침.append(r)
            print(f"  {r*100:5.0f}%  {q}")
    평균 = sum(겹침) / len(겹침)
    print(f"\n평균 겹침률 {평균*100:.1f}%")
    if 평균 < 0.8:
        print("⚠️  80% 미만이다. 토큰화나 스코어 식이 어긋나 있다 — 골든셋 평가 전에 잡는다.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="bm25s 와 top-20 겹침률만")
    ap.add_argument("--보충", action="store_true",
                     help="전체 재구축 대신 chunk_len 에 없는 chunk_id 만 채운다 "
                          "(스코프 재적재 후 BM25 갱신용)")
    ap.add_argument("--dry-run", action="store_true",
                     help="--보충 전용 — 대상 건수만 보고 DB 를 쓰지 않는다")
    a = ap.parse_args()

    if a.보충:
        보충(dry_run=a.dry_run)
        return

    if not JSONL.exists():
        sys.exit(f"{JSONL} 가 없다. 먼저 stage2_chunk.py 를 돌린다.")
    if a.verify:
        verify()
        return

    t0 = time.time()
    rows = [json.loads(l) for l in JSONL.open(encoding="utf-8")]
    print(f"입력 {len(rows):,}건 ({JSONL.name})", flush=True)
    적재(rows)
    print(f"\n완료 — {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
