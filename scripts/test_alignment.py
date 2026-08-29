# -*- coding: utf-8 -*-
"""RAG 용도 검증 : '검색'이 아니라 '조문 매칭'이 되는지 시험한다.

두 가지를 잰다.
  A. 기관(L4) 조항 → 창진원(L2/L3) 대응 조항 찾기   ← 관리자 '엄격한 조항 N건' 엔진
  B. 제12차 조항 → 제14차 대응 조항 찾기            ← 개정 diff 엔진

조 번호가 서로 다르므로(제12차 제33조 = 제14차 제36조) 번호 매칭은 불가하고
내용 매칭만 가능하다. 그게 되는지 눈으로 확인하는 것이 목적이다.

실행:  python scripts/test_alignment.py
"""
from __future__ import annotations
import io, os, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_C = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
if _C.exists() and any(_C.rglob("model.safetensors")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# 기관 조항 중 창진원에 대응이 있어야 마땅한 주제들
기관_샘플_주제 = ["여비", "회의비", "인건비", "외주", "장비", "임차"]


def show(conn, model, 질의문, 질의라벨, where, k=3):
    vec = model.encode([질의문], normalize_embeddings=True)[0]
    v = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
    rows = conn.execute(f"""
        SELECT doc_id, 조번호, coalesce(조제목, coalesce(항호,'')) ,
               left(replace(text, chr(10), ' '), 62),
               1 - (embedding <=> %s::vector) AS sim
        FROM chunks WHERE {where}
        ORDER BY embedding <=> %s::vector LIMIT {k}
    """, (v, v)).fetchall()
    print(f"\n  ▶ {질의라벨}")
    for doc, jo, title, txt, sim in rows:
        src = doc.split("_")[0] if doc.startswith("L") else doc[:18]
        print(f"      {sim:.3f}  [{src} {jo} {title[:16]}] {txt}")


def main():
    from sentence_transformers import SentenceTransformer
    print("모델 로딩...", flush=True)
    model = SentenceTransformer("nlpai-lab/KURE-v1")
    model.max_seq_length = 1024

    with psycopg.connect(DSN) as conn:
        print("\n" + "=" * 78)
        print("A. 기관 조항 → 창진원 대응 조항  (관리자 '엄격한 조항' 엔진)")
        print("=" * 78)
        for 주제 in 기관_샘플_주제:
            row = conn.execute("""
                SELECT doc_id, 조번호, coalesce(조제목,''), text
                FROM chunks
                WHERE layer='L4' AND 조제목 ILIKE %s AND length(text) BETWEEN 150 AND 2500
                ORDER BY length(text) DESC LIMIT 1
            """, (f"%{주제}%",)).fetchone()
            if not row:
                continue
            doc, jo, title, text = row
            기관명 = doc.split("_")[1] if "_" in doc else doc
            print(f"\n[기관] {기관명} {jo}({title})")
            print(f"       {text[:110].replace(chr(10),' ')}...")
            show(conn, model, text[:1200], "창진원에서 찾은 대응 조항",
                 "layer IN ('L2','L3') AND parse_quality='high'")

        print("\n\n" + "=" * 78)
        print("B. 제12차 → 제14차 조문 매칭  (개정 diff 엔진)")
        print("=" * 78)
        n12 = conn.execute("""
            SELECT count(*) FROM chunks WHERE doc_id LIKE '%제12차%'
        """).fetchone()[0]
        if n12 == 0:
            print("\n  제12차가 색인돼 있지 않다 (index_target=false).")
            print("  → 개정 diff 를 하려면 구판도 색인해야 한다. 104청크 = 약 2분.")
        else:
            for jo in ("제33조", "제38조", "제42조"):
                row = conn.execute("""
                    SELECT 조번호, coalesce(조제목,''), text FROM chunks
                    WHERE doc_id LIKE '%제12차%' AND 조번호=%s LIMIT 1
                """, (jo,)).fetchone()
                if not row:
                    continue
                print(f"\n[제12차] {row[0]}({row[1]})")
                show(conn, model, row[2][:1200], "제14차에서 찾은 대응 조항",
                     "doc_id LIKE '%제14차%'")

    print("\n" + "=" * 78)
    print("판단 기준: 대응 조항이 1위로 나오고 유사도가 0.7 이상이면 매칭 엔진으로 쓸 만하다.")
    print("          번호는 다른데 주제가 맞아떨어지는지를 보라.")


if __name__ == "__main__":
    main()
