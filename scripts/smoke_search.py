# -*- coding: utf-8 -*-
"""인덱스 스모크 테스트 : 실제 질문으로 벡터 검색이 맞는 조항을 물어오는지 확인.

파이프라인이 살아있는지 보는 최소 검증이다. 정식 평가는 정답셋(§8)으로 한다.

실행:  python scripts/smoke_search.py
"""
from __future__ import annotations
import io, os, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

질문들 = [
    ("디자이너 쓸 맥북 250만원 사도 되나요?", "기계장치 / PC 1인 1대"),
    ("창업활동비 이번 달 60만원 써도 되나요?", "창업활동비 월 50만원 한도"),
    ("외주용역 2500만원 계약했는데 괜찮나요?", "2천만원 초과 사전심의"),
    ("홍보용 기프티콘 뿌려도 되나요?", "광고선전비 기프티콘 불가"),
    ("해외 전시회 출장 가는데 비행기표 되나요?", "여비 / 국외출장 사전보고"),
]


def main():
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("nlpai-lab/KURE-v1")

    with psycopg.connect(DSN) as conn:
        n = conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
        print(f"검색 대상 청크: {n}\n")

        for q, 기대 in 질문들:
            vec = model.encode([q], normalize_embeddings=True)[0]
            v = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            rows = conn.execute("""
                SELECT layer, doc_id, 조번호, coalesce(조제목,''),
                       1 - (embedding <=> %s::vector) AS sim,
                       left(replace(text, chr(10), ' '), 95)
                FROM chunks
                WHERE status='active' AND parse_quality='high'
                  AND layer IN ('L1','L2','L3')
                ORDER BY embedding <=> %s::vector
                LIMIT 3
            """, (v, v)).fetchall()

            print(f"Q: {q}")
            print(f"   기대: {기대}")
            for layer, doc, 조, 제목, sim, txt in rows:
                print(f"   [{sim:.3f}] {layer} {doc[:34]:<34} {조:<8} {제목[:16]}")
                print(f"           {txt}")
            print()


if __name__ == "__main__":
    main()
