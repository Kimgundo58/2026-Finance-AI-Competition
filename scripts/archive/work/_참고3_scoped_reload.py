# -*- coding: utf-8 -*-
"""참고3 재파싱 수정분을 corpus.doc_articles + corpus.chunks 에 **범위를 좁혀** 반영한다.

전체 재적재(`load_db.py`/`stage2_chunk.py`)는 corpus.chunks 20,525건을 통째로
TRUNCATE 하고 임베딩을 처음부터 다시 계산한다 (CPU 4시간). 이번 변경은 4문서·
8개 조(참고/붙임/별첨/별지서식)만 바뀌었으므로, 그 조들만 UPDATE·재청킹·재임베딩한다.

절차:
  1. 4개 소스 파일을 다시 분해해 바뀐 (doc_id, 조번호) 만 추린다 (diff).
  2. doc_articles 그 행만 UPDATE.
  3. stage2_chunk.py 의 청킹 함수를 그대로 불러와 그 조들만 재청킹 —
     기존 chunks 행은 DELETE 후 새로 INSERT (embedding NULL).
  4. KURE-v1 로 그 새 청크만 로컬 CPU 로 임베딩(개수가 적어 초 단위) 후 UPDATE.

실행:  PYTHONIOENCODING=utf-8 python scripts/archive/work/_참고3_scoped_reload.py --apply
       (기본은 dry-run — 무엇이 바뀔지만 출력)
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
sys.path.insert(0, str(ROOT / "scripts"))

import stage0_run as s0                                          # noqa: E402
import stage2_chunk as s2                                        # noqa: E402
from scope import 범위밖_조                                       # noqa: E402
from _lib import db                                               # noqa: E402

파일들 = [
    "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초격차 스타트업 프로젝트/초격차 스타트업 프로젝트 세부관리기준(제10차).pdf",
    "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/민관공동창업자발굴육성(TIPS)/2026/붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문.pdf",
    "2026_Finance_DATA_FOR_RAG/창진원/창업도약패키지/창업도약패키지 세부관리기준(2025년).pdf",
    "2026_Finance_DATA_FOR_RAG/창진원/창업중심대학/창업중심대학 세부관리기준2025년 개정.pdf",
]


def 바뀐것_찾기(conn) -> dict[str, dict]:
    """doc_id -> {조번호: (새본문, 새조제목)} — DB 현재값과 실제로 다른 것만."""
    out: dict[str, dict] = {}
    for rel in 파일들:
        p = ROOT / rel
        arts, strategy = s0.분해(p)
        doc_id = p.stem
        cur = conn.execute("SELECT 조번호, 본문 FROM corpus.doc_articles WHERE doc_id=%s", (doc_id,))
        현재 = {r[0]: r[1] for r in cur.fetchall()}
        바뀜 = {}
        for a in arts:
            조 = a.get("조번호")
            새본문 = a.get("본문") or ""
            if 조 in 현재 and 현재[조] != 새본문:
                바뀜[조] = (새본문, a.get("조제목"))
        if 바뀜:
            out[doc_id] = 바뀜
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 DB 를 쓴다. 기본은 dry-run")
    a = ap.parse_args()

    with db.connect() as conn:
        바뀐것 = 바뀐것_찾기(conn)
        총 = sum(len(v) for v in 바뀐것.values())
        print(f"바뀐 조: {총}건 ({len(바뀐것)}개 문서)")
        for doc_id, 조들 in 바뀐것.items():
            for 조 in 조들:
                print(f"  · {doc_id[:40]:<40} {조}")

        if not 바뀐것:
            print("변경 없음 — 할 일 없다.")
            return

        if not a.apply:
            print("\n--dry-run — DB 를 쓰지 않았다. --apply 로 실행할 것.")
            return

        # ── 태그맵 로딩 (stage2_chunk.main() 과 동일 경로) ──────────────────
        태그 = s2.TAT.태그맵(json.loads(s2.APPLY.read_text(encoding="utf-8"))["tags"])

        캐시 = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
        import os
        if 캐시.exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from transformers import AutoTokenizer
        print(f"토크나이저 로딩 {s2.MODEL} ...", flush=True)
        tok = AutoTokenizer.from_pretrained(s2.MODEL)

        # ── 1. doc_articles UPDATE ──────────────────────────────────────────
        with conn.cursor() as cur:
            for doc_id, 조들 in 바뀐것.items():
                for 조, (새본문, 새제목) in 조들.items():
                    cur.execute("""UPDATE corpus.doc_articles SET 본문=%s
                                   WHERE doc_id=%s AND 조번호=%s""",
                                (새본문, doc_id, 조))
                    if cur.rowcount != 1:
                        sys.exit(f"🔴 UPDATE 행수 이상 {doc_id}/{조}: {cur.rowcount}")
        conn.commit()
        print(f"doc_articles UPDATE 완료 ({총}건)")

        # ── 2. 재청킹 — stage2_chunk 의 문서 단위 로직을 그대로, 문서 범위만 좁힌다 ──
        new_rows: list[tuple] = []
        embed_inputs: list[str] = []
        row_meta: list[tuple[str, str]] = []   # (doc_id, 조번호) — DELETE 범위 표시용

        for doc_id in 바뀐것:
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
                if 조번호 not in 바뀐것[doc_id]:
                    continue     # 이번 판에서 바뀐 조만 재청킹 — 나머지는 그대로 둔다

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
                row_meta.append((doc_id, 조번호))

        # ── 게이트 — stage2_chunk.py 와 동일 (1,024토큰 초과 0건) ───────────
        길이 = s2.토큰수(tok, embed_inputs) if embed_inputs else []
        초과 = [i for i, n in enumerate(길이) if n > s2.GATE_TOK]
        if 초과:
            for i in 초과[:5]:
                print(f"    {길이[i]}토큰  {new_rows[i][0][:40]} {new_rows[i][8]} {new_rows[i][10]}")
            sys.exit("🔴 게이트 실패 — 초과 청크가 있다. 임베딩하면 꼬리가 잘린다.")
        print(f"게이트 통과 — 새 청크 {len(new_rows)}건, 최대 {max(길이) if 길이 else 0}토큰")

        # ── 3. DELETE 옛 청크 + INSERT 새 청크 (조 단위 스코프) ─────────────
        with conn.cursor() as cur:
            for doc_id, 조들 in 바뀐것.items():
                for 조 in 조들:
                    cur.execute("""DELETE FROM corpus.chunks
                                   WHERE doc_id=%s AND 조번호=%s""", (doc_id, 조))
                    print(f"  DELETE corpus.chunks {doc_id[:30]:<30} {조}  ({cur.rowcount}건 제거)")
            if new_rows:
                cur.executemany("""
                    INSERT INTO corpus.chunks
                      (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                       retrieval_scope, 조번호, 조제목, 항호, 페이지, 사업명, 적용대상, text)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, new_rows)
        conn.commit()
        print(f"corpus.chunks INSERT 완료 — {len(new_rows)}건")

        # ── 4. 로컬 CPU 임베딩 (개수 적음 — 초 단위) ────────────────────────
        if not new_rows:
            print("새 청크가 없다 — 임베딩 단계 건너뜀.")
            return

        # INSERT 순서와 chunk_id 조회 순서가 doc_id·조번호 오름차순이라 embed_inputs
        # 원래 순서(문서 루프 순서)와 어긋날 수 있다 — chunk_id 로 다시 매칭한다.
        # 조합 IN 은 OR 로 풀어 쓴다 — 드라이버별 튜플-IN 어댑팅 차이를 안 탄다.
        조건, 파라미터 = [], []
        for doc_id, 조들 in 바뀐것.items():
            for 조 in 조들:
                조건.append("(doc_id=%s AND 조번호=%s)")
                파라미터 += [doc_id, 조]
        재조회 = conn.execute(f"""
            SELECT chunk_id, doc_id, 조번호, 항호 FROM corpus.chunks
             WHERE {' OR '.join(조건)} ORDER BY chunk_id
        """, 파라미터).fetchall()

        # embed_inputs 는 new_rows 와 같은 순서(문서 루프 순). new_rows 의
        # (doc_id, 조번호, 항호) 로 텍스트를 찾아 재조회 순서에 맞춰 정렬한다.
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

    # ── 5. BM25 갱신 — 새 청크가 chunk_terms/chunk_len 색인 누락으로 안 남게 ────
    # 이 스크립트가 만지는 건 doc_articles·chunks·embedding 뿐이라, 여기서 끝내면
    # 새 청크는 BM25 검색에 안 걸린다 (2026-09-05 실측 — 39삭제/53신규 후 53건 누락).
    # stage2_bm25.py 를 **별도 프로세스**로 부른다: 여기서 import 하면 위에서 이미
    # stage2_chunk -> tag_apply_target 이 감싼 sys.stdout 을 stage2_bm25 모듈 상단이
    # 다시 감싸면서 앞 래퍼의 버퍼를 닫아 `ValueError: I/O operation on closed file`
    # 로 죽는다(실측, stage2_bm25.py 상단 주석과 같은 원인) — 같은 프로세스 안에서
    # 두 스크립트를 같이 import 하지 않는 게 유일하게 안전한 방법이다.
    print("\nBM25 갱신 — stage2_bm25.py --보충 …")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stage2_bm25.py"), "--보충"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0:
        sys.exit("🔴 BM25 갱신 실패(exit "
                  f"{r.returncode}) — chunk_terms/chunk_len 에 새 청크가 안 채워졌을 수 있다. "
                  "python scripts/stage2_bm25.py --보충 --dry-run 으로 확인할 것.")


if __name__ == "__main__":
    main()
