# -*- coding: utf-8 -*-
"""Stage 2 : 청킹 + KURE-v1 임베딩 → chunks / case_chunks 적재.

청킹 정책 (파이프라인 문서 §4.1)
  기본 1조 = 1청크 / 3,000자 초과 → 항 → 호 / 50자 미만 병합 / 오버랩 없음

실행:  python scripts/build_index.py
"""
from __future__ import annotations
import io, os, re, sys, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
MODEL = "nlpai-lab/KURE-v1"
MAX_CHARS = 3000
MIN_CHARS = 50
BATCH = 16

RE_HANG = re.compile(r"(?=[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮])")
RE_HO = re.compile(r"(?=\n\s*\d+\.\s)")
RE_QA = re.compile(r"(?:^|\n)\s*(?:Q\s*\d*[.．:]|질문\s*\d*[.．:]|문\s*\d*[.．:])", re.I)


# ── 청킹 ─────────────────────────────────────────────────────────
def split_by(pattern, text: str) -> list[str]:
    parts = [p.strip() for p in pattern.split(text) if p and p.strip()]
    return parts if len(parts) > 1 else []


def chunk_article(본문: str, 조번호: str) -> list[tuple[str | None, str]]:
    """반환: [(항호, 텍스트)]"""
    본문 = 본문.strip()
    if len(본문) <= MAX_CHARS:
        return [(None, 본문)]

    out = []
    hangs = split_by(RE_HANG, 본문)
    if not hangs:
        hangs = [본문]
    for i, h in enumerate(hangs):
        label = h[0] if h and h[0] in "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮" else (f"항{i+1}" if len(hangs) > 1 else None)
        if len(h) <= MAX_CHARS:
            out.append((label, h))
            continue
        hos = split_by(RE_HO, h)
        if not hos:
            # 호로도 안 쪼개지면 문자 단위 강제 분할
            hos = [h[j:j + MAX_CHARS] for j in range(0, len(h), MAX_CHARS)]
        for j, ho in enumerate(hos):
            out.append((f"{label or ''}호{j+1}".strip(), ho))
    return out or [(None, 본문)]


def merge_tiny(chunks: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    out: list[list] = []
    for label, txt in chunks:
        if out and len(txt) < MIN_CHARS:
            out[-1][1] = out[-1][1] + "\n" + txt
        else:
            out.append([label, txt])
    return [(a, b) for a, b in out]


# ── 사례 Q&A 분할 ────────────────────────────────────────────────
def split_qa(본문: str) -> list[tuple[str, str]]:
    """반환: [(question, answer)]"""
    pieces = RE_QA.split(본문)
    pieces = [p.strip() for p in pieces if p and len(p.strip()) > 30]
    out = []
    for p in pieces:
        m = re.split(r"(?:^|\n)\s*(?:A\s*\d*[.．:]|답변?\s*\d*[.．:])", p, maxsplit=1, flags=re.I)
        if len(m) == 2:
            out.append((m[0].strip()[:1000], m[1].strip()))
        else:
            lines = p.split("\n", 1)
            out.append((lines[0].strip()[:1000], (lines[1] if len(lines) > 1 else p).strip()))
    return out


def main():
    t0 = time.time()
    print("모델 로딩...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL)
    dim = model.get_sentence_embedding_dimension()
    print(f"  {MODEL}  ({dim}차원)")
    assert dim == 1024, f"차원 불일치: {dim}"

    with psycopg.connect(DSN) as conn:
        conn.execute("TRUNCATE chunks, case_chunks;")

        docs = conn.execute("""
            SELECT doc_id, layer, domain, 기관ID, apply_mode, version, status
            FROM documents WHERE index_target = TRUE ORDER BY layer, doc_id
        """).fetchall()

        판정_rows, 사례_rows = [], []
        for doc_id, layer, domain, 기관, apply_mode, version, status in docs:
            arts = conn.execute("""
                SELECT article_id, 조번호, 조제목, 본문, 페이지
                FROM doc_articles WHERE doc_id = %s ORDER BY article_id
            """, (doc_id,)).fetchall()

            사업명 = _사업명(doc_id, layer)

            if layer == "사례":
                for aid, 조번호, 조제목, 본문, 페이지 in arts:
                    for q, a in split_qa(본문):
                        사례_rows.append((doc_id, _출처도메인(doc_id), q, a))
            else:
                for aid, 조번호, 조제목, 본문, 페이지 in arts:
                    for 항호, txt in merge_tiny(chunk_article(본문, 조번호)):
                        판정_rows.append((doc_id, aid, layer, domain, 기관, apply_mode,
                                        version, status, 조번호, 조제목, 항호, 페이지,
                                        사업명, txt))

        print(f"청킹 완료: 판정 {len(판정_rows)} / 사례 {len(사례_rows)}")

        # ── 임베딩 + 적재 ────────────────────────────────────────
        _embed_insert(conn, model, 판정_rows, kind="판정")
        _embed_insert(conn, model, 사례_rows, kind="사례")
        conn.commit()

    print(f"\n완료 — {time.time()-t0:.0f}초")


def _사업명(doc_id: str, layer: str):
    hits = [k for k in ("예비창업패키지", "초기창업패키지", "창업도약패키지") if k in doc_id]
    return hits or None


def _출처도메인(doc_id: str) -> str:
    if any(k in doc_id for k in ("연구재단", "KISTEP", "IITP", "미래창조", "연구비")):
        return "R&D"
    if any(k in doc_id for k in ("기획재정부", "보조금", "국고보조금")):
        return "보조금"
    return "R&D"


def _embed_insert(conn, model, rows, kind: str):
    if not rows:
        print(f"  {kind}: 0건 (건너뜀)")
        return
    texts = [r[-1] for r in rows] if kind == "판정" else [r[2] for r in rows]
    print(f"  {kind} 임베딩 {len(texts)}건...", flush=True)
    t = time.time()
    vecs, STEP = [], 200
    for i in range(0, len(texts), STEP):
        vecs.extend(model.encode(texts[i:i + STEP], normalize_embeddings=True,
                                 batch_size=BATCH, show_progress_bar=False))
        done = min(i + STEP, len(texts))
        el = time.time() - t
        eta = el / done * (len(texts) - done)
        print(f"    {done}/{len(texts)}  경과 {el:.0f}초  남은예상 {eta:.0f}초", flush=True)
    print(f"    임베딩 완료 {time.time()-t:.0f}초", flush=True)

    with conn.cursor() as cur:
        if kind == "판정":
            cur.executemany("""
                INSERT INTO chunks
                  (doc_id, article_id, layer, domain, 기관ID, apply_mode, version, status,
                   조번호, 조제목, 항호, 페이지, 사업명, text, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, [(*r, _v(v)) for r, v in zip(rows, vecs)])
        else:
            cur.executemany("""
                INSERT INTO case_chunks (doc_id, 출처도메인, question, answer, embedding)
                VALUES (%s,%s,%s,%s,%s)
            """, [(*r, _v(v)) for r, v in zip(rows, vecs)])


def _v(vec) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


if __name__ == "__main__":
    main()
