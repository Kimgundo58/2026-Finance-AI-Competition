# -*- coding: utf-8 -*-
"""2026-09-05 사고(레인 C, ai-35 배정) 마무리 재청킹.

`scripts/archive/work/_참고3_scoped_reload.py` 의 2~5단계(재청킹·임베딩·BM25)를
그대로 가져오되, **diff 로 대상을 찾지 않고 명시적 조 목록을 받는다** — 그
스크립트의 1단계(`doc_articles` UPDATE, DB 현재값과 신규 파싱값 대조)가 이미
한 번 실행돼 커밋됐다(2026-09-05, `table_splice.py` 손실복구 패치 직후).
그래서 다시 돌리면 "바뀐 것 없음" 으로 끝나 재청킹이 아예 안 돈다 — 이 스크립트가
그 이어붙이기다. `archive/` 는 읽기 전용 정책이라 원본을 고칠 수 없어 새로 판다.

실행 전에 `eval.golden_chunks` 에서 이 8개 조를 참조하는 행을 지워 둬야 한다
(FK ON DELETE SET NULL 이 golden_chunks_실패_check 제약을 깬다 — 2026-09-05
1차 시도 실측). 이 스크립트는 그 삭제가 이미 됐다고 가정하고 검증만 한다.

실행: PYTHONIOENCODING=utf-8 python scripts/_work/table_loss_rechunk.py --apply
      (기본은 dry-run)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
# stage2_chunk.py 는 scripts/archive/indexing/ 에 있다(2026-09-05 이관) — 그
# 하위 폴더들도 sys.path 에 걸어야 sibling import(`import stage0_run` 등)가 산다.
_archive = ROOT / "scripts" / "archive"
if _archive.is_dir():
    for _d in _archive.iterdir():
        if _d.is_dir():
            sys.path.insert(0, str(_d))

import stage2_chunk as s2                                        # noqa: E402
from scope import 범위밖_조                                       # noqa: E402
from _lib import db                                               # noqa: E402

대상 = [
    ("창업중심대학 세부관리기준2025년 개정", "참고3"),
    ("창업중심대학 세부관리기준2025년 개정", "참고5"),
    ("초격차 스타트업 프로젝트 세부관리기준(제10차)", "참고3"),
    ("초격차 스타트업 프로젝트 세부관리기준(제10차)", "참고6"),
    ("붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문", "별첨1"),
    ("붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문", "붙임3"),
    ("붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문", "붙임5"),
    ("창업도약패키지 세부관리기준(2025년)", "별지서식"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    바뀐것: dict[str, set[str]] = {}
    for doc_id, 조 in 대상:
        바뀐것.setdefault(doc_id, set()).add(조)

    with db.connect() as conn:
        # 0. golden_chunks 잔존 참조 확인 — 있으면 DELETE 가 다시 막힌다.
        with conn.cursor() as cur:
            잔존 = 0
            for doc_id, 조 in 대상:
                cur.execute("SELECT count(*) FROM eval.golden_chunks WHERE doc_id=%s AND 조번호=%s",
                            (doc_id, 조))
                잔존 += cur.fetchone()[0]
            if 잔존:
                sys.exit(f"🔴 eval.golden_chunks 에 이 8개 조를 참조하는 행이 {잔존}건 남아 있다 — "
                          "먼저 지울 것 (golden_chunks_실패_check 제약에 다시 걸린다).")

        태그 = s2.TAT.태그맵(json.loads(s2.APPLY.read_text(encoding="utf-8"))["tags"])
        캐시 = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
        import os
        if 캐시.exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from transformers import AutoTokenizer
        print(f"토크나이저 로딩 {s2.MODEL} ...", flush=True)
        tok = AutoTokenizer.from_pretrained(s2.MODEL)

        new_rows: list[tuple] = []
        embed_inputs: list[str] = []

        for doc_id, 조번호들 in 바뀐것.items():
            drow = conn.execute("""
                SELECT layer, 기관ID, parse_quality, version, status, retrieval_scope, src_path
                  FROM corpus.documents WHERE doc_id=%s""", (doc_id,)).fetchone()
            if drow is None:
                sys.exit(f"🔴 corpus.documents 에 {doc_id} 가 없다.")
            layer, 기관, pq, ver, status, scope, src = drow

            arts = [dict(zip(("article_id", "조번호", "조제목", "본문", "페이지", "삭제"), r))
                    for r in conn.execute("""
                        SELECT article_id, 조번호, 조제목, 본문, 페이지, 삭제
                          FROM corpus.doc_articles WHERE doc_id=%s ORDER BY article_id
                    """, (doc_id,)).fetchall()]
            범위밖 = 범위밖_조(doc_id, arts)
            장 = s2.장맵(arts)
            사업 = s2.사업_of_doc.get(doc_id)

            for art in arts:
                조번호 = art["조번호"]
                if 조번호 not in 조번호들:
                    continue

                if art["삭제"]:
                    print(f"  [{doc_id}/{조번호}] 삭제조 — 청크 없음"); continue
                if s2.RE_첨부.match(조번호 or "") and s2.표인가(art["본문"]):
                    print(f"  [{doc_id}/{조번호}] 박스표 판정 — 청크 없음(룰 재료로만)"); continue
                if 조번호 in 범위밖:
                    print(f"  [{doc_id}/{조번호}] 범위밖 — 청크 없음"); continue
                if not (art["본문"] or "").strip():
                    print(f"  [{doc_id}/{조번호}] 본문 없음 — 청크 없음"); continue

                적용 = s2.TAT.적용대상_of(doc_id, 조번호, 태그)
                if 적용 is None:
                    print(f"  [{doc_id}/{조번호}] 적용대상 미결 — 청크 없음"); continue

                부속 = bool(s2.RE_첨부.match(조번호 or ""))
                조각 = s2.병합(s2.분할(tok, art["본문"]))
                h = s2.헤더(layer, 사업, doc_id, 장.get(조번호, ""), 조번호, art["조제목"])
                for 항호, txt in 조각:
                    new_rows.append((doc_id, art["article_id"], layer, 기관, pq, ver, status,
                                      "폐포전용" if 부속 else scope,
                                      조번호, art["조제목"], 항호, art["페이지"],
                                      사업, 적용, txt))
                    embed_inputs.append(f"{h}\n{txt}")

        길이 = s2.토큰수(tok, embed_inputs) if embed_inputs else []
        초과 = [i for i, n in enumerate(길이) if n > s2.GATE_TOK]
        if 초과:
            for i in 초과[:5]:
                print(f"    {길이[i]}토큰  {new_rows[i][0][:40]} {new_rows[i][8]} {new_rows[i][10]}")
            sys.exit("🔴 게이트 실패 — 초과 청크가 있다.")
        print(f"게이트 통과 — 새 청크 {len(new_rows)}건, 최대 {max(길이) if 길이 else 0}토큰")

        if not a.apply:
            print("\n--dry-run — DB 를 쓰지 않았다. --apply 로 실행할 것.")
            return

        with conn.cursor() as cur:
            for doc_id, 조번호들 in 바뀐것.items():
                for 조번호 in 조번호들:
                    cur.execute("DELETE FROM corpus.chunks WHERE doc_id=%s AND 조번호=%s",
                                (doc_id, 조번호))
                    print(f"  DELETE corpus.chunks {doc_id[:30]:<30} {조번호}  ({cur.rowcount}건 제거)")
            if new_rows:
                cur.executemany("""
                    INSERT INTO corpus.chunks
                      (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                       retrieval_scope, 조번호, 조제목, 항호, 페이지, 사업명, 적용대상, text)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, new_rows)
        conn.commit()
        print(f"corpus.chunks INSERT 완료 — {len(new_rows)}건")

        if not new_rows:
            print("새 청크가 없다 — 임베딩 단계 건너뜀.")
            return

        조건, 파라미터 = [], []
        for doc_id, 조번호들 in 바뀐것.items():
            for 조번호 in 조번호들:
                조건.append("(doc_id=%s AND 조번호=%s)")
                파라미터 += [doc_id, 조번호]
        재조회 = conn.execute(f"""
            SELECT chunk_id, doc_id, 조번호, 항호 FROM corpus.chunks
             WHERE {' OR '.join(조건)} ORDER BY chunk_id
        """, 파라미터).fetchall()

        입력맵 = {}
        for row, txt in zip(new_rows, embed_inputs):
            입력맵[(row[0], row[8], row[10])] = txt
        ordered_ids, ordered_texts = [], []
        for cid, doc_id, 조번호, 항호 in 재조회:
            key = (doc_id, 조번호, 항호)
            if key not in 입력맵:
                sys.exit(f"🔴 임베딩 입력 매칭 실패: {key}")
            ordered_ids.append(cid)
            ordered_texts.append(입력맵[key])

        print(f"임베딩 대상 {len(ordered_texts)}건 — KURE-v1 CPU 로딩 중...", flush=True)
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(s2.MODEL, device="cpu")
        model.max_seq_length = 1024
        import time
        t0 = time.time()
        vecs = model.encode(ordered_texts, batch_size=16, normalize_embeddings=True,
                             show_progress_bar=False, convert_to_numpy=True)
        print(f"임베딩 완료 {time.time()-t0:.1f}초  shape={vecs.shape}")

        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _emb (chunk_id BIGINT PRIMARY KEY, v TEXT);")
            with cur.copy("COPY _emb (chunk_id, v) FROM STDIN") as cp:
                for cid, v in zip(ordered_ids, vecs):
                    cp.write_row((cid, "[" + ",".join(f"{x:.6f}" for x in v) + "]"))
            cur.execute("""
                UPDATE corpus.chunks c
                   SET embedding = _emb.v::extensions.vector(1024)
                  FROM _emb WHERE _emb.chunk_id = c.chunk_id
            """)
            갱신 = cur.rowcount
        conn.commit()
        print(f"corpus.chunks.embedding UPDATE {갱신}건")

        빈칸 = conn.execute("SELECT count(*) FROM corpus.chunks WHERE embedding IS NULL").fetchone()[0]
        print(f"전체 embedding NULL 잔존: {빈칸}건 (재적재 전과 같아야 정상)")

    print("\nBM25 갱신 — stage2_bm25.py --보충 …")
    import os
    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stage2_bm25.py"), "--보충"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0:
        sys.exit(f"🔴 BM25 갱신 실패(exit {r.returncode})")


if __name__ == "__main__":
    main()
