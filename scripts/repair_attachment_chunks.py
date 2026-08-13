# -*- coding: utf-8 -*-
"""붙임/별표 청크만 비목 단위로 다시 쪼개고 재임베딩한다.

전체 재임베딩(45분) 대신 해당 청크만 교체한다(수십 초).
build_index.py 의 split_비목표 를 그대로 쓰므로 로직은 한 곳에만 있다.

실행:  python scripts/repair_attachment_chunks.py
"""
from __future__ import annotations
import io, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
# stdout 래핑은 build_index import 뒤에 한다.
# (build_index 가 module-level 에서 sys.stdout 을 다시 감싸므로 먼저 감싸면 닫힌다)

_C = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
if _C.exists() and any(_C.rglob("model.safetensors")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import psycopg
from build_index import chunk_article, merge_tiny, _v, _사업명
# build_index 가 이미 sys.stdout 을 utf-8 로 감쌌다. 여기서 또 감싸면
# 중간 래퍼가 GC 되면서 버퍼가 닫힌다("I/O operation on closed file").

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")


def main():
    with psycopg.connect(DSN) as conn:
        arts = conn.execute("""
            SELECT a.article_id, a.doc_id, a.조번호, a.조제목, a.본문, a.페이지,
                   d.layer, d.domain, d.기관ID, d.apply_mode, d.version, d.status
            FROM doc_articles a JOIN documents d ON d.doc_id = a.doc_id
            WHERE d.index_target AND d.layer <> '사례'
              AND a.조번호 ~ '^(붙임|별표|별지|서식)'
            ORDER BY a.article_id
        """).fetchall()
        print(f"대상 붙임/별표 조: {len(arts)}개")

        before = conn.execute("""
            SELECT count(*) FROM chunks WHERE 조번호 ~ '^(붙임|별표|별지|서식)'
        """).fetchone()[0]

        rows = []
        for (aid, doc_id, 조번호, 조제목, 본문, 페이지,
             layer, domain, 기관, apply_mode, version, status) in arts:
            사업명 = _사업명(doc_id, layer)
            for 항호, txt in merge_tiny(chunk_article(본문, 조번호)):
                rows.append((doc_id, aid, layer, domain, 기관, apply_mode, version,
                             status, 조번호, 조제목, 항호, 페이지, 사업명, txt))

        print(f"재청킹: {before} → {len(rows)}개")

        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("nlpai-lab/KURE-v1")
        model.max_seq_length = 1024
        vecs = model.encode([r[-1] for r in rows], normalize_embeddings=True,
                            batch_size=16, show_progress_bar=False)

        conn.execute("DELETE FROM chunks WHERE 조번호 ~ '^(붙임|별표|별지|서식)'")
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO chunks
                  (doc_id, article_id, layer, domain, 기관ID, apply_mode, version, status,
                   조번호, 조제목, 항호, 페이지, 사업명, text, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, [(*r, _v(v)) for r, v in zip(rows, vecs)])
        conn.commit()

        n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        print(f"완료 — chunks 총 {n}개")

        for 항호, txt in conn.execute("""
            SELECT 항호, left(replace(text, chr(10), ' '), 55) FROM chunks
            WHERE 조번호='붙임2' AND doc_id LIKE '2025_예비%' ORDER BY chunk_id
        """).fetchall():
            print(f"   {str(항호)[:22]:<24} {txt}")


if __name__ == "__main__":
    main()
