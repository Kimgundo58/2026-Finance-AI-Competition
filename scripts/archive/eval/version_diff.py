# -*- coding: utf-8 -*-
"""개정 diff : 통합관리지침 구판 → 신판 조문 매칭.

왜 벡터가 필요한가
  제12차 제33~42조 = 제14차 제36~45조. 조 번호가 밀려서 번호로는 못 잇는다.
  83 x 83 = 6,889 쌍을 LLM 에 물어볼 수는 없다. 내용 매칭만이 유일한 방법이다.

하는 일
  1) 구판(제12차)이 색인 안 돼 있으면 증분 색인한다 (기존 chunks 는 건드리지 않는다)
  2) 신판 각 조 → 구판에서 가장 가까운 조를 찾는다
  3) 번호 이동 / 신설 / 내용 변경을 분류해 출력한다

실행:  python scripts/archive/eval/version_diff.py
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

import io, os, sys, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
sys.path.insert(0, str(ROOT / "scripts"))

_C = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
if _C.exists() and any(_C.rglob("model.safetensors")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
구판 = "L2_창업사업화지원사업통합관리지침_제12차"
신판 = "L2_중소기업창업지원사업통합관리지침_제14차_20251223"

동일 = 0.97   # 이 이상이면 사실상 동일 조문
유사 = 0.80   # 이 이상이면 같은 조가 개정된 것
신설 = 0.80   # 이 미만이면 대응 조항 없음 = 신설


def ensure_indexed(conn, model, doc_id: str) -> int:
    n = conn.execute("SELECT count(*) FROM chunks WHERE doc_id=%s", (doc_id,)).fetchone()[0]
    if n:
        print(f"  {doc_id[:44]} 이미 색인됨 ({n}청크)")
        return n

    from build_index import chunk_article, merge_tiny, _v, _사업명
    meta = conn.execute("""
        SELECT layer, 기관ID, parse_quality, version, status
        FROM documents WHERE doc_id=%s
    """, (doc_id,)).fetchone()
    if not meta:
        print(f"  ✗ documents 에 {doc_id} 없음")
        return 0
    layer, 기관, parse_quality, version, status = meta

    arts = conn.execute("""
        SELECT article_id, 조번호, 조제목, 본문, 페이지 FROM doc_articles
        WHERE doc_id=%s ORDER BY article_id
    """, (doc_id,)).fetchall()

    rows = []
    for aid, 조번호, 조제목, 본문, 페이지 in arts:
        for 항호, txt in merge_tiny(chunk_article(본문, 조번호)):
            rows.append((doc_id, aid, layer, 기관, parse_quality, version,
                         status, 조번호, 조제목, 항호, 페이지, _사업명(doc_id, layer), txt))

    print(f"  {doc_id[:44]} 색인 중… {len(rows)}청크", flush=True)
    t = time.time()
    vecs = model.encode([r[-1] for r in rows], normalize_embeddings=True,
                        batch_size=16, show_progress_bar=False)
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO chunks
              (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
               조번호, 조제목, 항호, 페이지, 사업명, text, embedding)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [(*r, _v(v)) for r, v in zip(rows, vecs)])
    conn.commit()
    print(f"    완료 {time.time()-t:.0f}초")
    return len(rows)


def main():
    from sentence_transformers import SentenceTransformer
    print("모델 로딩...", flush=True)
    model = SentenceTransformer("nlpai-lab/KURE-v1")
    model.max_seq_length = 1024

    with psycopg.connect(DSN) as conn:
        print("\n[1] 구판 색인")
        conn.execute("UPDATE documents SET index_target=TRUE WHERE doc_id=%s", (구판,))
        conn.commit()
        if not ensure_indexed(conn, model, 구판):
            return

        print("\n[2] 신판 각 조 → 구판 대응 조 찾기")
        신 = conn.execute("""
            SELECT 조번호, coalesce(조제목,''), text FROM chunks
            WHERE doc_id=%s AND 조번호 ~ '^제[0-9]+조' ORDER BY article_id, chunk_id
        """, (신판,)).fetchall()

        이동, 개정, 신설목록, 동일수 = [], [], [], 0
        for 조번호, 조제목, text in 신:
            v = model.encode([text[:1500]], normalize_embeddings=True)[0]
            vs = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
            hit = conn.execute("""
                SELECT 조번호, coalesce(조제목,''), 1 - (embedding <=> %s::vector)
                FROM chunks WHERE doc_id=%s AND 조번호 ~ '^제[0-9]+조'
                ORDER BY embedding <=> %s::vector LIMIT 1
            """, (vs, 구판, vs)).fetchone()
            if not hit:
                continue
            구조번호, 구제목, sim = hit
            if sim < 신설:
                신설목록.append((조번호, 조제목, sim))
            elif 구조번호 != 조번호:
                이동.append((구조번호, 구제목, 조번호, 조제목, sim))
                if sim < 동일:
                    개정.append((구조번호, 조번호, 조제목, sim))
            else:
                동일수 += 1
                if sim < 동일:
                    개정.append((구조번호, 조번호, 조제목, sim))

        print(f"\n  신판 조 {len(신)}개 대조 완료")
        print(f"   · 번호 유지        {동일수}")
        print(f"   · 번호 이동        {len(이동)}")
        print(f"   · 대응 없음(신설)  {len(신설목록)}")

        print(f"\n[3] 번호가 바뀐 조 — 번호로는 절대 못 찾는 것들 (상위 18)")
        for 구, 구t, 신_, 신t, sim in sorted(이동, key=lambda x: -x[4])[:18]:
            mark = "동일" if sim >= 동일 else "개정"
            print(f"   {sim:.3f} [{mark}] 제12차 {구:<8}({구t[:14]:<14}) → 제14차 {신_:<8}({신t[:14]})")

        print(f"\n[4] 신설된 조 — 구판에 대응이 없음 (상위 10)")
        for 조, t, sim in sorted(신설목록, key=lambda x: x[2])[:10]:
            print(f"   {sim:.3f} 제14차 {조:<8} {t[:34]}")

    print("\n" + "=" * 74)
    print("이게 관리자 화면 '제15차 개정 · 변경 N개 조항 자동 반영' 의 엔진이다.")
    print("번호가 밀린 조를 내용으로 이어붙이는 일은 벡터 말고 방법이 없다.")


if __name__ == "__main__":
    main()
