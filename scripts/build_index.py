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

# 모델이 이미 캐시에 있으면 오프라인으로 고정한다.
# (기본 동작은 매 로드마다 HF Hub 에 업데이트를 확인하러 나간다 →
#  네트워크가 끊기면 수십 초 지연되거나 40분짜리 작업이 실패할 수 있다)
_CACHE = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
if _CACHE.exists() and any(_CACHE.rglob("model.safetensors")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

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


def split_비목표(본문: str) -> list[tuple[str, str]]:
    """[붙임N] 비목 해설표를 **비목 단위**로 쪼갠다.

    이 표는 ①②③ 구조가 없어서 일반 항/호 분할을 쓰면 비목 경계를 무시하고
    엉뚱한 곳에서 잘린다. 실측: 4,521자가 2조각으로 잘려 '창업활동비 월 50만원'이
    인건비 덩어리 끝에 묻혔고, 창업활동비 질문에 검색이 실패했다.

    구조: <비목명 줄들> / '정의' / ... / '증빙' '서류' / ... / '유의' '사항' / ...
    → '정의'만 있는 줄을 기준으로 앞의 비목명 블록까지 거슬러 올라가 경계를 잡는다.
    """
    lines = 본문.split("\n")
    라벨무시 = {"정의", "증빙", "서류", "유의", "사항", "비목", "내용", "구분", "기 타", "기타"}
    경계 = []
    for i, ln in enumerate(lines):
        if ln.strip() != "정의":
            continue
        # '정의' 위쪽으로 거슬러 올라가며 비목명을 모은다.
        # 비목명과 '정의' 사이, 그리고 비목명 줄들 사이에 빈 줄이 낀다
        # ( ['', '재료비', '', '정의'] · ['창업', '', '활동비', '', '정의'] )
        # → 빈 줄은 건너뛰고, 라벨/불릿을 만나면 멈춘다.
        j, name, start, steps = i - 1, [], i, 0
        while j >= 0 and len(name) < 3 and steps < 8:
            s = lines[j].strip()
            steps += 1
            if not s:
                j -= 1
                continue
            if s in 라벨무시 or s.startswith("•"):
                break
            name.insert(0, s)
            start = j
            j -= 1
        if name:
            경계.append((start, "".join(name)))

    if len(경계) < 3:
        return []

    out = []
    for k, (start, name) in enumerate(경계):
        end = 경계[k + 1][0] if k + 1 < len(경계) else len(lines)
        seg = "\n".join(lines[start:end]).strip()
        if len(seg) >= 40:
            out.append((name[:40], seg))
    return out


def chunk_article(본문: str, 조번호: str) -> list[tuple[str | None, str]]:
    """반환: [(항호, 텍스트)]"""
    본문 = 본문.strip()

    # 붙임/별표의 비목 해설표는 비목 단위로 (길이와 무관하게)
    if (조번호 or "").startswith(("붙임", "별표")):
        표 = split_비목표(본문)
        if 표:
            return [(name, seg) for name, seg in 표]

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
    # bge-m3 계열은 max_seq_length 가 8192 로 잡혀 있어 긴 청크에서 매우 느리다.
    # 조 단위 청크(최대 3,000자)는 1024 토큰이면 충분하다.
    print(f"  {MODEL}  ({dim}차원, max_seq_length {model.max_seq_length} → 1024)")
    model.max_seq_length = 1024
    assert dim == 1024, f"차원 불일치: {dim}"

    with psycopg.connect(DSN) as conn:
        conn.execute("TRUNCATE chunks, case_chunks;")

        docs = conn.execute("""
            SELECT doc_id, layer, 기관ID, parse_quality, version, status
            FROM documents WHERE index_target = TRUE ORDER BY layer, doc_id
        """).fetchall()

        판정_rows, 사례_rows = [], []
        for doc_id, layer, 기관, parse_quality, version, status in docs:
            arts = conn.execute("""
                SELECT article_id, 조번호, 조제목, 본문, 페이지
                FROM doc_articles WHERE doc_id = %s ORDER BY article_id
            """, (doc_id,)).fetchall()

            if layer == "사례":
                for aid, 조번호, 조제목, 본문, 페이지 in arts:
                    for q, a in split_qa(본문):
                        사례_rows.append((doc_id, _출처도메인(doc_id), q, a))
            else:
                for aid, 조번호, 조제목, 본문, 페이지 in arts:
                    사업명 = _사업명(doc_id, layer, 조번호)
                    for 항호, txt in merge_tiny(chunk_article(본문, 조번호)):
                        판정_rows.append((doc_id, aid, layer, 기관, parse_quality,
                                        version, status, 조번호, 조제목, 항호, 페이지,
                                        사업명, txt))

        print(f"청킹 완료: 판정 {len(판정_rows)} / 사례 {len(사례_rows)}")

        # ── 임베딩 + 적재 ────────────────────────────────────────
        _embed_insert(conn, model, 판정_rows, kind="판정")
        _embed_insert(conn, model, 사례_rows, kind="사례")
        conn.commit()

    print(f"\n완료 — {time.time()-t0:.0f}초")


# ── 역참조(cited_by) 인덱스 ──────────────────────────────────────
# scripts/build_citations.py 산출물. L1 법령 조문은 doc_id 에 사업명이 없으므로
# "어느 문서가 이 조를 인용했는가"로 사업명을 붙인다. 없으면 NULL(전 사업 공통).
_CITE_MAP: dict[tuple[str, str], list[str]] | None = None


def _load_cites() -> dict[tuple[str, str], list[str]]:
    """(doc_id, 조번호) → 사업명 리스트."""
    global _CITE_MAP
    if _CITE_MAP is not None:
        return _CITE_MAP
    _CITE_MAP = {}
    cj = ROOT / "법령 PDF" / "_law_citations.json"
    rj = ROOT / "법령 PDF" / "_law_report.json"
    if not (cj.exists() and rj.exists()):
        return _CITE_MAP

    import json as _json
    cites = _json.loads(cj.read_text(encoding="utf-8"))["citations"]
    report = _json.loads(rj.read_text(encoding="utf-8"))

    # ref_id → 그 규범의 현행본 doc_id(파일 stem)
    ref2doc = {}
    for r in report:
        for f in r.get("files", []):
            if f.get("kind") == "현행":
                ref2doc[r["ref_id"]] = Path(f["file"]).stem
                break

    for ref, per_jo in cites.items():
        doc_id = ref2doc.get(ref)
        if not doc_id:
            continue                      # 미수집 규범(R05 등)은 건너뛴다
        for jo, recs in per_jo.items():
            if jo == "_미특정":
                continue
            조번호 = (jo if jo.startswith("별표")
                    else f"제{jo.split('-')[0]}조의{jo.split('-')[1]}" if "-" in jo
                    else f"제{jo}조")
            biz = sorted({b for r in recs for b in r.get("사업명", [])})
            if biz:
                _CITE_MAP[(doc_id, 조번호)] = biz
    return _CITE_MAP


def _사업명(doc_id: str, layer: str, 조번호: str | None = None):
    hits = [k for k in ("예비창업패키지", "초기창업패키지", "창업도약패키지") if k in doc_id]
    if hits:
        return hits
    if layer == "L1" and 조번호:
        return _load_cites().get((doc_id, 조번호))
    return None


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
                  (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                   조번호, 조제목, 항호, 페이지, 사업명, text, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
