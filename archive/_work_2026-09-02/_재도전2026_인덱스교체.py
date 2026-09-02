# -*- coding: utf-8 -*-
"""재도전성공패키지 검색 인덱스 판본 교체 — 2025년판(구) → 2026년판 11차 개정(현행).

왜 이 스크립트가 따로 있는가
────────────────────────────
`_G_재도전2026_적재.py` 가 `doc_articles` 와 `documents.status` 까지는 돌려놓고
**"chunks 는 건드리지 않는다 — 검색 인덱스 재구축은 별건이다"** 로 끝냈다.
그 별건이 이것이다. 그 사이 서비스는 `status='active'` 인 2026년판을 검색에서
못 보고, `superseded` 인 2025년판 35청크로 판정하고 있었다.

`stage2_chunk.py` 를 그냥 돌리면 20,525청크를 TRUNCATE 하고 전부 다시 임베딩한다
(GPU 팟 필요). 바뀐 문서는 하나뿐이므로 **그 문서만 갈아 끼운다.**
청킹 규칙이 갈라지면 안 되므로 `stage2_chunk` 의 함수를 그대로 import 해서 쓴다 —
분할·병합·헤더·게이트가 전부 같은 코드다.

임베딩은 CPU 로 한다. 33조 → 청크 수십 건이라 팟을 열 이유가 없다.
🔴 모델·정규화가 본 파이프라인과 같아야 한다: KURE-v1 · normalize_embeddings=True.
   다르면 벡터 공간이 어긋나 이 문서만 검색에서 조용히 밀린다.

선행 조건 (이미 끝나 있어야 한다)
─────────────────────────────────
  · `_stage0_articles.json` 에 2026년판 33조 + `장` 필드
  · `_apply_target.json` 에 2026년판 태깅 (사업비 비목 6조가 '창업기업')
  · `tag_apply_target.py` · `load_db.py` 의 `현행` 상수가 2026년판

실행:  PYTHONIOENCODING=utf-8 python scripts/_work/_재도전2026_인덱스교체.py
       PYTHONIOENCODING=utf-8 python scripts/_work/_재도전2026_인덱스교체.py --commit
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 🔴 `stage2_chunk` · `stage2_bm25` · `tag_apply_target` 이 **각자 모듈 최상단에서**
#    `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 를 한다. 둘 이상을 import
#    하면 두 번째 래퍼가 첫 번째를 sys.stdout 자리에서 밀어내고, 참조를 잃은 첫 래퍼가
#    GC 될 때 **바닥 버퍼(fd 1)를 닫는다** — 이후 모든 print 가
#    `ValueError: I/O operation on closed file` 로 죽는다.
#    래퍼를 살려 두면 바닥이 안 닫힌다. 실행할 때 `PYTHONIOENCODING=utf-8` 이 붙으므로
#    (훅이 강제한다) 우리는 원래 stdout 으로 돌아가기만 하면 된다.
_원래_stdout = sys.stdout
_래퍼_보관: list = []

import psycopg                                                    # noqa: E402
import index_guard                                                # noqa: E402
import stage2_chunk as SC                                         # noqa: E402
_래퍼_보관.append(sys.stdout)
import stage2_bm25 as BM                                          # noqa: E402
_래퍼_보관.append(sys.stdout)
import tag_apply_target as TAT                                    # noqa: E402
_래퍼_보관.append(sys.stdout)
import stage2_embed as SE                                         # noqa: E402  (MAX_SEQ)
sys.stdout = _원래_stdout

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
APPLY = ROOT / "2026_Finance_DATA_FOR_RAG" / "_apply_target.json"

신 = "2026년 재도전성공패키지 세부관리기준(11차 개정)"
구 = "재도전성공패키지 세부관리기준(2025년)"


def main() -> int:
    commit = "--commit" in sys.argv
    t0 = time.time()

    캐시 = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
    if 캐시.exists():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    from transformers import AutoTokenizer
    print(f"토크나이저 {SC.MODEL} ...", flush=True)
    tok = AutoTokenizer.from_pretrained(SC.MODEL)
    태그 = TAT.태그맵(json.loads(APPLY.read_text(encoding="utf-8"))["tags"])

    with psycopg.connect(DSN) as conn:
        # ── 0. 선행 조건 검사 ────────────────────────────────────────────
        row = conn.execute("""SELECT layer, 기관ID, parse_quality, version, status,
                                     retrieval_scope, src_path
                                FROM corpus.documents WHERE doc_id=%s""", (신,)).fetchone()
        if not row:
            print(f"🔴 {신!r} 이 documents 에 없다."); return 1
        layer, 기관, pq, ver, status, scope, src = row
        if status != "active":
            print(f"🔴 2026년판 status={status!r} — active 여야 한다. "
                  "_G_재도전2026_적재.py --commit 을 먼저 돌린다."); return 1
        index_guard.assert_indexable(src or 신, layer)

        n태그 = sum(1 for (d, _) in 태그 if d == 신)
        if n태그 == 0:
            print("🔴 _apply_target.json 에 2026년판 태깅이 없다. "
                  "tag_apply_target.py 를 먼저 돌린다."); return 1

        # ── 1. 청크 조립 — stage2_chunk 와 같은 코드로 ───────────────────
        arts = [dict(zip(("article_id", "조번호", "조제목", "본문", "페이지", "삭제"), r))
                for r in conn.execute("""
                    SELECT article_id, 조번호, 조제목, 본문, 페이지, 삭제
                      FROM corpus.doc_articles WHERE doc_id=%s ORDER BY article_id
                """, (신,)).fetchall()]
        장 = SC.장맵(arts)
        # 🔴 `chunks.사업명` 은 TEXT[] 다. 정본 매핑은 `stage2_chunk.사업_of_doc` 하나뿐이라
        #    여기서 문자열을 새로 쓰지 않고 그걸 조회한다 — 표기가 갈리면 사업 필터가 샌다.
        사업 = SC.사업_of_doc.get(신)
        if not 사업:
            print(f"🔴 stage2_chunk.사업_of_doc 에 {신!r} 이 없다."); return 1

        rows, 임베딩입력, 컷 = [], [], Counter()
        for a in arts:
            조번호 = a["조번호"]
            if a["삭제"]:
                컷["삭제"] += 1; continue
            if SC.RE_첨부.match(조번호 or "") and SC.표인가(a["본문"]):
                컷["첨부표"] += 1; continue
            부속 = bool(SC.RE_첨부.match(조번호 or ""))
            if not (a["본문"] or "").strip():
                컷["빈본문"] += 1; continue
            적용 = TAT.적용대상_of(신, 조번호, 태그)
            if 적용 is None:
                컷["미결"] += 1; continue      # 2단 LLM 대기 — 인덱스에 올리지 않는다
            조각 = SC.병합(SC.분할(tok, a["본문"]))
            h = SC.헤더(layer, 사업, 신, 장.get(조번호, ""), 조번호, a["조제목"])
            for 항호, txt in 조각:
                rows.append((신, a["article_id"], layer, 기관, "high", ver, status,
                             "폐포전용" if 부속 else scope,
                             조번호, a["조제목"], 항호, a["페이지"], 사업, 적용, txt))
                임베딩입력.append(f"{h}\n{txt}")

        # 🔴 게이트 — 본 파이프라인과 같은 1,024토큰 상한
        길이 = SC.토큰수(tok, 임베딩입력)
        초과 = [i for i, n in enumerate(길이) if n > SC.GATE_TOK]
        print(f"\n청크 {len(rows)}건 · 컷 {dict(컷)} · "
              f"토큰 최대 {max(길이)} 평균 {sum(길이)/len(길이):.0f} 초과 {len(초과)}")
        if 초과:
            for i in 초과[:5]:
                print(f"  {길이[i]}토큰 {rows[i][8]} {rows[i][10]}")
            print("🔴 게이트 실패 — 임베딩하면 꼬리가 잘린다."); return 1

        적용분포 = Counter(r[13] for r in rows)
        print(f"적용대상 {dict(적용분포)}")
        구청크 = conn.execute("SELECT count(*) FROM corpus.chunks WHERE doc_id=%s",
                            (구,)).fetchone()[0]
        print(f"교체: {구!r} {구청크}청크 → {신!r} {len(rows)}청크")

        if not commit:
            print(f"\n(미리보기다. 실제로 쓰려면 --commit)  {time.time()-t0:.0f}초")
            return 0

        # ── 2. 임베딩 (CPU) ─────────────────────────────────────────────
        print("\n임베딩 (CPU) ...", flush=True)
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(SC.MODEL, device="cpu")
        m.max_seq_length = SE.MAX_SEQ      # 본 파이프라인과 같은 1,024
        vecs = m.encode(임베딩입력, batch_size=8, normalize_embeddings=True,
                        show_progress_bar=False, convert_to_numpy=True)
        print(f"  {vecs.shape} · {time.time()-t0:.0f}초")

        # ── 3. 교체 — 한 트랜잭션 ────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("DELETE FROM corpus.chunks WHERE doc_id = ANY(%s)", ([구, 신],))
            cur.execute("""UPDATE corpus.documents
                              SET index_target=TRUE, parse_quality='high'
                            WHERE doc_id=%s""", (신,))
            # 🔴 extraction='vlm' 은 그대로 둔다. parse_quality 는 «추출을 믿을 만한가»
            #    (검색 진입 자격)이고 extraction 은 «어떻게 뽑았나»(인용 등급)다.
            #    VLM_DOWNGRADE 가 계속 발화해 A등급 인용을 막는다 — 판정은 되고
            #    등급만 B 다. 이게 CLAUDE.md 의 «A등급 인용 금지» 그대로다.
            cur.execute("UPDATE corpus.documents SET index_target=FALSE WHERE doc_id=%s",
                        (구,))
            for r, v in zip(rows, vecs):
                cur.execute("""
                    INSERT INTO corpus.chunks
                      (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                       retrieval_scope, 조번호, 조제목, 항호, 페이지, 사업명, 적용대상,
                       text, embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (*r, "[" + ",".join(f"{x:.6f}" for x in v) + "]"))
        conn.commit()

        # ── 4. BM25 재적재 — 전체다 (chunk_id 가 바뀌었다) ───────────────
        print("\nBM25 재적재 ...", flush=True)
        전체 = [{"chunk_id": c, "text": t} for c, t in conn.execute(
            "SELECT chunk_id, text FROM corpus.chunks ORDER BY chunk_id").fetchall()]
        BM.적재(전체)

        # ── 5. 확인 ─────────────────────────────────────────────────────
        for q in ("SELECT count(*) FROM corpus.chunks",
                  f"SELECT count(*) FROM corpus.chunks WHERE doc_id='{신}'",
                  f"SELECT count(*) FROM corpus.chunks WHERE doc_id='{구}'",
                  "SELECT count(*) FROM corpus.chunks WHERE embedding IS NULL"):
            print(f"  {conn.execute(q).fetchone()[0]:>7,}  {q.split('WHERE')[-1][:60]}")

    print(f"\n완료 — {time.time()-t0:.0f}초")
    return 0


if __name__ == "__main__":
    sys.exit(main())
