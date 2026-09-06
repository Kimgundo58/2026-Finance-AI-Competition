# -*- coding: utf-8 -*-
"""0단계 뒤처리 — 모두의창업 별지1·별지2 청크를 되살린다 (중앙 ai-4a).

🔴 내가 낸 회귀다. 기록해 둔다.
`0단계_표복구.py --apply` 는 재청킹 절차를 `_참고3_scoped_reload.py` 에서 그대로 베꼈고,
그 안에 `scope.범위밖_조()` 컷이 들어 있다. 그런데 «운영 인덱스는 그 컷을 태운 적이 없다»
(`scope.py` 주석 그대로: "Stage 2 청킹  미구현"). 실측 — 반영 전 corpus.chunks 에
별지1 3건 · 별지2 2건이 «있었다». 그래서 지우고 다시 넣는 자리에서 5건이 조용히 빠졌다.

그리고 그 컷 자체가 이 문서에서 «틀린다» — 원인은 컷이 위치 기반이기 때문이다.
```
scope.범위밖_조 = 「제3편 로컬트랙」의 «마지막» 매치 다음 조부터 전부 범위 밖
모두의창업 실측  매치가 2곳 —  index 1 = 제2조(용어의 정의)
                              index 51 = 제52조(해석)   <- 편 제목이 아니라 «본문 속 언급»
                 -> 컷이 제52조 뒤로 내려앉아 별지1·별지2·별지5 가 범위 밖이 된다
그런데 그 셋의 조제목이
   별지1 「모두의 창업 프로젝트 일반‧기술트랙 사업비 집행기준」
   별지2 「모두의 창업 프로젝트 일반‧기술트랙 진출자 사업비 집행기준」
   -> 제목이 스스로 «일반·기술트랙» 이라고 말한다. 로컬트랙 자료가 아니다
   (제3편 로컬트랙 조문 자체는 doc_articles 에 아예 없다 — 별지3·4 도 없다)
```
그래서 이 두 조에 대해서만 컷을 «끄고» 다시 청킹한다. `scope.py` 는 «고치지 않는다» —
그건 tag_apply_target·build_refs·build_precedence 가 같이 쓰는 공용 컷이라 중앙 단독으로
바꿀 자리가 아니다. 결함은 보고하고 판단을 받는다.

실행:  PYTHONIOENCODING=utf-8 python scratchpad/0단계_별지복구.py --apply
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "archive" / "indexing"))
sys.path.insert(0, str(ROOT / "scripts" / "archive" / "eval"))

from _lib import db                                  # noqa: E402

DOC = "모두의 창업 프로젝트 세부관리기준(개정본)"
조들 = ["별지1", "별지2"]


def _import_s2():
    """stage2_chunk / pin_golden_chunks 는 import 시점에 sys.stdout 을 갈아끼운다.
    두 겹이 되면 앞 래퍼가 GC 되며 진짜 stdout 을 닫는다 — 붙잡아 둔다."""
    보관: list = []
    원래 = io.TextIOWrapper

    class 붙잡는(원래):                                # noqa: N801
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            보관.append(self)

    진짜 = sys.stdout
    io.TextIOWrapper = 붙잡는
    try:
        import stage2_chunk as s2
        import index_guard
        import pin_golden_chunks as PIN
    finally:
        io.TextIOWrapper = 원래
        sys.stdout = 진짜
    return s2, index_guard, PIN, 보관


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    s2, index_guard, PIN, _keep = _import_s2()

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM corpus.chunks WHERE doc_id=%s AND 조번호=ANY(%s)",
                    (DOC, 조들))
        전 = cur.fetchone()[0]
        cur.execute("SELECT layer, 기관ID, parse_quality, version, status, retrieval_scope, src_path "
                    "FROM corpus.documents WHERE doc_id=%s", (DOC,))
        layer, 기관, pq, ver, status, scope, src = cur.fetchone()
        index_guard.assert_indexable(src or DOC, layer)
        cur.execute("SELECT article_id, 조번호, 조제목, 본문, 페이지, 삭제 "
                    "FROM corpus.doc_articles WHERE doc_id=%s ORDER BY article_id", (DOC,))
        arts = [dict(zip(("article_id", "조번호", "조제목", "본문", "페이지", "삭제"), r))
                for r in cur.fetchall()]
    print(f"현재 별지1·별지2 청크 {전}건 (반영 전 운영값은 5건이었다)")

    태그 = s2.TAT.태그맵(json.loads(s2.APPLY.read_text(encoding="utf-8"))["tags"])
    from transformers import AutoTokenizer            # noqa: PLC0415
    tok = AutoTokenizer.from_pretrained(s2.MODEL)
    장 = s2.장맵(arts)
    사업 = s2.사업_of_doc.get(DOC)

    rows, inputs = [], []
    for art in arts:
        if art["조번호"] not in 조들:
            continue
        # 🔴 범위밖_조 는 «태우지 않는다» — 위 docstring 참조 (이 두 조는 제목이
        #    스스로 일반·기술트랙이라고 말한다). 나머지 게이트는 그대로 태운다.
        if art["삭제"] or not (art["본문"] or "").strip():
            print(f"  {art['조번호']} 삭제/빈본문 — 건너뜀"); continue
        if s2.RE_첨부.match(art["조번호"]) and s2.표인가(art["본문"]):
            print(f"  {art['조번호']} 박스표 — 건너뜀"); continue
        적용 = s2.TAT.적용대상_of(DOC, art["조번호"], 태그)
        if 적용 is None:
            print(f"  {art['조번호']} 적용대상 미결 — 건너뜀"); continue
        h = s2.헤더(layer, 사업, DOC, 장.get(art["조번호"], ""), art["조번호"], art["조제목"])
        for 항호, txt in s2.병합(s2.분할(tok, art["본문"])):
            rows.append((DOC, art["article_id"], layer, 기관, pq, ver, status, "폐포전용",
                         art["조번호"], art["조제목"], 항호, art["페이지"], 사업, 적용, txt))
            inputs.append(f"{h}\n{txt}")

    길이 = s2.토큰수(tok, inputs) if inputs else []
    초과 = [i for i, n in enumerate(길이) if n > s2.GATE_TOK]
    if 초과:
        sys.exit(f"🔴 게이트 실패 — {len(초과)}건이 {s2.GATE_TOK}토큰 초과")
    print(f"게이트 통과 — 새 청크 {len(rows)}건, 최대 {max(길이) if 길이 else 0}토큰")
    if not a.apply:
        print("dry-run — DB 를 쓰지 않았다.")
        return 0

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT gc.gc_id FROM eval.golden_chunks gc
                             JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id
                            WHERE c.doc_id=%s AND c.조번호=ANY(%s)""", (DOC, 조들))
            핀 = [r[0] for r in cur.fetchall()]
            if 핀:
                cur.execute("DELETE FROM eval.golden_chunks WHERE gc_id=ANY(%s)", (핀,))
            cur.execute("DELETE FROM corpus.chunks WHERE doc_id=%s AND 조번호=ANY(%s)",
                        (DOC, 조들))
            지움 = cur.rowcount
            cur.executemany("""INSERT INTO corpus.chunks
                  (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                   retrieval_scope, 조번호, 조제목, 항호, 페이지, 사업명, 적용대상, text)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        conn.commit()
        print(f"chunks DELETE {지움} -> INSERT {len(rows)} (기존 핀 {len(핀)}행 걷음)")

        with conn.cursor() as cur:
            cur.execute("SELECT chunk_id, 조번호, 항호 FROM corpus.chunks "
                        "WHERE doc_id=%s AND 조번호=ANY(%s) ORDER BY chunk_id", (DOC, 조들))
            재조회 = cur.fetchall()
        입력맵 = {(r[8], r[10]): t for r, t in zip(rows, inputs)}
        ids = [c for c, _, _ in 재조회]
        texts = [입력맵[(조, 항)] for _, 조, 항 in 재조회]
        print(f"임베딩 {len(texts)}건 — KURE-v1 CPU ...", flush=True)
        from sentence_transformers import SentenceTransformer    # noqa: PLC0415
        model = SentenceTransformer(s2.MODEL, device="cpu")
        model.max_seq_length = 1024
        t0 = time.time()
        vecs = model.encode(texts, batch_size=16, normalize_embeddings=True,
                            show_progress_bar=False, convert_to_numpy=True)
        print(f"임베딩 완료 {time.time()-t0:.1f}초 shape={vecs.shape}")
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _emb2 (chunk_id BIGINT PRIMARY KEY, v TEXT);")
            with cur.copy("COPY _emb2 (chunk_id, v) FROM STDIN") as cp:
                for cid, v in zip(ids, vecs):
                    cp.write_row((cid, "[" + ",".join(f"{x:.6f}" for x in v) + "]"))
            cur.execute("""UPDATE corpus.chunks c SET embedding=_emb2.v::extensions.vector(1024)
                             FROM _emb2 WHERE _emb2.chunk_id=c.chunk_id""")
            print(f"embedding UPDATE {cur.rowcount}건")
        conn.commit()

        # 별지1·별지2 를 근거로 삼는 골든 핀을 다시 고정한다 (실패로 남아 있는 것 포함)
        with conn.cursor() as cur:
            cur.execute("""SELECT gc_id, gold_id, 근거순번 FROM eval.golden_chunks
                            WHERE doc_id=%s AND 조번호=ANY(%s)""", (DOC, 조들))
            남은 = cur.fetchall()
            if 남은:
                cur.execute("DELETE FROM eval.golden_chunks WHERE gc_id=ANY(%s)",
                            ([r[0] for r in 남은],))
            쌍 = sorted({(g, i) for _, g, i in 남은})
            통계 = {"원문일치": 0, "조번호": 0, "조제목": 0, "실패": 0}
            쓴행 = 0
            for gid, 순번 in 쌍:
                cur.execute("SELECT 정답근거 FROM eval.golden_set WHERE gold_id=%s", (gid,))
                근거 = (cur.fetchone() or [None])[0] or []
                if 순번 >= len(근거):
                    continue
                g = 근거[순번]
                방법, cids, 사유 = PIN.매칭(cur, g.get("doc"), g.get("조번호"), g.get("원문"))
                통계[방법] += 1
                if 방법 == "실패":
                    cur.execute("INSERT INTO eval.golden_chunks "
                                "(gold_id, 근거순번, doc_id, 조번호, 매칭방법, 실패사유) "
                                "VALUES (%s,%s,%s,%s,'실패',%s) ON CONFLICT DO NOTHING",
                                (gid, 순번, g.get("doc"), g.get("조번호"), 사유))
                    쓴행 += cur.rowcount
                    continue
                for cid in cids:
                    cur.execute("INSERT INTO eval.golden_chunks "
                                "(gold_id, 근거순번, chunk_id, article_id, doc_id, 조번호, 매칭방법) "
                                "SELECT %s,%s,c.chunk_id,c.article_id,c.doc_id,%s,%s "
                                "  FROM corpus.chunks c WHERE c.chunk_id=%s ON CONFLICT DO NOTHING",
                                (gid, 순번, g.get("조번호"), 방법, cid))
                    쓴행 += cur.rowcount
        conn.commit()
        print(f"golden_chunks 재고정 {len(쌍)}쌍 -> {쓴행}행  {통계}")

        with conn.cursor() as cur:
            cur.execute("SELECT 조번호, count(*), count(*) FILTER (WHERE embedding IS NULL) "
                        "FROM corpus.chunks WHERE doc_id=%s AND 조번호=ANY(%s) GROUP BY 1 ORDER BY 1",
                        (DOC, 조들))
            for r in cur.fetchall():
                print(f"  사후 {r[0]}  청크 {r[1]}  embedding NULL {r[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
