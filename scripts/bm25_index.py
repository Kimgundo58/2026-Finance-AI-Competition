# -*- coding: utf-8 -*-
"""BM25 역색인(`corpus.chunk_terms` / `chunk_len` / `term_df`)을 채우고, 색인·쿼리 공용 토큰화를 제공한다.

`retrieve.sparse()` 가 `토큰화()` 를 import 해 쿼리를 토큰화한다. 색인 입력은 `chunks.text` 가 아니라
`[컨텍스트 헤더] + text` 로, 임베딩 입력과 같아야 dense 와 BM25 가 같은 텍스트를 본다.
`증분_색인()` 은 `chunk_len` 에 없는 청크만 색인한다(멱등). import 시점 부수 효과는 없다.

실행: PYTHONIOENCODING=utf-8 python scripts/bm25_index.py --증분 [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter

# 색인에 남기는 품사. 불용어 목록은 두지 않는다 — 흔한 토큰은 IDF 가 낮춘다.
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


# 컨텍스트 헤더: corpus.chunks.embedding 을 만들 때 쓴 헤더와 같은 형식이다.
# 형식을 바꾸면 dense 와 BM25 가 서로 다른 입력을 색인하게 된다.
_RE_장 = re.compile(r"제\s*(\d+)\s*장\s*([^\n<>]{0,20})")


def _장_매핑(articles: list[dict]) -> dict[str, str]:
    """조번호 -> 그 조가 속한 장 제목. 장 표제는 앞 조 본문 끝에 붙어 있으므로 다음 조부터 적용한다."""
    out, state = {}, ""
    for a in articles:
        out[a["조번호"]] = state
        for m in _RE_장.finditer(a.get("본문") or ""):
            제목 = re.sub(r"\s+", " ", m.group(2)).strip(" <>〈〉")
            state = f"제{m.group(1)}장 {제목}".strip()
    return out


def _헤더(layer, 사업, doc_id, 장, 조번호, 조제목) -> str:
    """`[layer | 사업 | 문서명 | 장 | 조번호(조제목)]` 형식의 컨텍스트 헤더."""
    문서 = re.sub(r"^(붙임\d*\.\s*|첨부\s*)", "", doc_id).strip()
    조 = f"{조번호}({조제목})" if 조제목 else str(조번호)
    필드 = [layer, "·".join(사업) if 사업 else "공통", 문서[:60], 장 or "-", 조]
    return "[" + " | ".join(필드) + "]"


def 증분_색인(dry_run: bool = False) -> None:
    """`corpus.chunk_len` 에 없는 청크만 토큰화해 BM25 세 테이블에 추가한다.

    문서 재적재는 `corpus.chunks` 만 갱신하므로 BM25 색인에 누락이 생긴다. staged 청크는 건너뛴다
    (활성화 전에 색인하면 active 청크의 BM25 점수가 바뀐다). 한 트랜잭션으로
    쓰고 `ON CONFLICT DO NOTHING` 이라 재실행해도 안전하다. 끝에 `term_df` 를 REFRESH 한다.
    `dry_run` 이면 대상 건수만 출력하고 DB 에 쓰지 않는다.
    """
    from _lib import db

    SQL_누락 = """
        SELECT c.chunk_id, c.doc_id, c.layer, c.사업명, c.조번호, c.조제목, c.text
          FROM corpus.chunks c
         WHERE NOT EXISTS (SELECT 1 FROM corpus.chunk_len l WHERE l.chunk_id = c.chunk_id)
           AND c.status <> 'staged'      -- 적재 API 의 staged 청크는 활성화 때 색인한다(corpus_ingest)
         ORDER BY c.doc_id, c.chunk_id
    """
    with db.connect() as conn:
        missing = conn.execute(SQL_누락).fetchall()
        print(f"chunk_len 누락 {len(missing):,}건")
        if not missing:
            print("색인할 청크 없음.")
            return

        문서별 = Counter(r[1] for r in missing)
        for doc_id, n in 문서별.most_common():
            print(f"  · {doc_id[:50]:<50} {n}건")

        if dry_run:
            print("\n--dry-run — DB 를 쓰지 않았다.")
            return

        # 장 제목은 문서의 조 전체를 순서대로 봐야 정해지므로 문서당 한 번만 계산한다.
        장_매핑_캐시: dict[str, dict[str, str]] = {}
        texts: list[str] = []
        for chunk_id, doc_id, layer, 사업, 조번호, 조제목, text in missing:
            if doc_id not in 장_매핑_캐시:
                arts = [{"조번호": r[0], "본문": r[1]} for r in conn.execute(
                    "SELECT 조번호, 본문 FROM corpus.doc_articles WHERE doc_id=%s ORDER BY article_id",
                    (doc_id,)).fetchall()]
                장_매핑_캐시[doc_id] = _장_매핑(arts)
            장 = 장_매핑_캐시[doc_id].get(조번호, "")
            h = _헤더(layer, 사업, doc_id, 장, 조번호, 조제목)
            texts.append(f"{h}\n{text}")

        t = time.time()
        docs = 토큰화(texts)
        print(f"\n  토큰화 {time.time() - t:.0f}초", flush=True)

        postings = [(r[0], term, tf) for r, d in zip(missing, docs)
                    for term, tf in Counter(d).items()]
        with conn.cursor() as cur:
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
        print(f"증분 색인 {len(missing):,}건 · 신규 포스팅 {posting:,} · "
              f"누계 chunk_terms {n_t:,} · chunk_len {n_l:,} · 어휘 {n_df:,}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--증분", action="store_true",
                     help="chunk_len 에 없는 청크만 색인한다 (문서 재적재 후 BM25 갱신용)")
    ap.add_argument("--dry-run", action="store_true",
                     help="--증분 전용 — 대상 건수만 보고 DB 를 쓰지 않는다")
    a = ap.parse_args()

    if not a.증분:
        ap.error("전체 재구축은 지원하지 않는다. --증분 으로 누락된 청크만 색인한다")
    증분_색인(dry_run=a.dry_run)


if __name__ == "__main__":
    main()
