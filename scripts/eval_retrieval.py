# -*- coding: utf-8 -*-
"""검색 품질 평가 : Recall@1/@3/@8 + MRR.

골든셋(30문항) 이전 단계의 **축소판**이다. 판정이 아니라 **검색만** 잰다.
"근거 조항이 상위 k개 안에 들어오는가" — 이게 파이프라인 전체의 상한선이다.
여기서 못 찾으면 ④ LLM 이 아무리 좋아도 답을 낼 수 없다.

실행:  python scripts/eval_retrieval.py
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
K = 8

# 정답 조항은 여러 개일 수 있다 (L2 비목 정의 / L3 붙임2 해설표 / L3 본문 예외)
# 하나라도 상위 k 안에 있으면 hit.
#   ("L2", "제39조")  → doc_id 가 L2_ 로 시작하고 조번호가 제39조
#   ("붙임2", None)   → 조번호가 붙임2
평가셋 = [
    ("디자이너 쓸 맥북 250만원 사도 되나요?",        [("L2", "제39조"), ("붙임2", None)]),
    ("창업활동비 이번 달 60만원 써도 되나요?",        [("붙임2", None)]),
    ("외주용역 2500만원 계약했는데 사전 절차 있나요?", [("L2", "제38조"), ("2025_", "제22조")]),
    ("홍보용 기프티콘 배포해도 되나요?",              [("L2", "제45조"), ("붙임2", None)]),
    ("해외 전시회 출장 여비는 어떻게 처리하나요?",     [("L2", "제43조"), ("붙임2", None), ("2025_", "제22조")]),
    ("재료비 증빙 서류로 뭐가 필요한가요?",           [("L2", "제37조"), ("붙임2", None)]),
    ("특허 출원비를 사업비로 쓸 수 있나요?",          [("L2", "제40조"), ("붙임2", None)]),
    ("4대보험 미가입 직원 인건비 지급 가능한가요?",    [("L2", "제41조"), ("붙임2", None)]),
    ("멘토링비 하루에 얼마까지 줄 수 있나요?",        [("L2", "제42조"), ("붙임2", None)]),
    ("교육 수강료를 사업비로 결제해도 되나요?",       [("L2", "제44조"), ("붙임2", None)]),
    ("중고 장비를 구매해도 되나요?",                 [("L2", "제39조"), ("붙임2", None)]),
    ("외주용역 선급금은 얼마까지 지급 가능한가요?",    [("L2", "제38조"), ("붙임2", None)]),
    ("사무실 임차료도 지원되나요?",                  [("L2", "제42조"), ("붙임2", None)]),
    ("회의비는 1인당 얼마까지 쓸 수 있나요?",         [("L2", "제33조"), ("2025_예비", "제20조"), ("2025_초기", "제19조")]),
    ("사업비로 산 장비를 따로 등록해야 하나요?",       [("2025_", "제22조"), ("L2", "제39조")]),
]


def hit(row, 정답들) -> bool:
    doc_id, 조번호 = row[0], row[1]
    for doc_pat, 조 in 정답들:
        if 조 is None:
            if doc_pat in (조번호 or ""):
                return True
        elif doc_id.startswith(doc_pat) and 조번호 == 조:
            return True
    return False


def main():
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("nlpai-lab/KURE-v1")
    model.max_seq_length = 1024

    r1 = r3 = r8 = 0
    mrr = 0.0
    실패 = []

    with psycopg.connect(DSN) as conn:
        n = conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
        print(f"검색 대상 청크 {n} · 평가 문항 {len(평가셋)} · top-{K}\n")

        for q, 정답들 in 평가셋:
            vec = model.encode([q], normalize_embeddings=True)[0]
            v = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            rows = conn.execute(f"""
                SELECT doc_id, 조번호, coalesce(조제목,''), layer,
                       left(replace(text, chr(10), ' '), 70)
                FROM chunks
                WHERE status='active' AND parse_quality='high' AND layer IN ('L1','L2','L3')
                ORDER BY embedding <=> %s::vector LIMIT {K}
            """, (v,)).fetchall()

            순위 = next((i for i, r in enumerate(rows, 1) if hit(r, 정답들)), None)
            if 순위:
                mrr += 1 / 순위
                r1 += 순위 <= 1
                r3 += 순위 <= 3
                r8 += 1
                mark = "✓" if 순위 <= 3 else "△"
                print(f"{mark} [{순위}위] {q}")
            else:
                기대 = " 또는 ".join(f"{d}{'/'+j if j else ''}" for d, j in 정답들)
                print(f"✗ [실패] {q}")
                print(f"         기대: {기대}")
                for r in rows[:3]:
                    print(f"         나온것: {r[3]} {r[1]:<8} {r[2][:18]:<20} {r[4][:46]}")
                실패.append(q)

    t = len(평가셋)
    print("\n" + "=" * 62)
    print(f"Recall@1 {r1/t:.2f}  ({r1}/{t})")
    print(f"Recall@3 {r3/t:.2f}  ({r3}/{t})")
    print(f"Recall@8 {r8/t:.2f}  ({r8}/{t})   ← 파이프라인 상한선")
    print(f"MRR      {mrr/t:.3f}")
    print("=" * 62)
    print("기준: Recall@8 ≥ 0.85 이면 쓸 만함. 이보다 낮으면 ④ LLM 이 근거를 못 받는다.")
    print("     지금은 벡터 검색만이다. BM25 + RRF 를 붙이면 올라간다.")
    if 실패:
        print(f"\n실패 {len(실패)}건 — 위 '나온것'을 보고 원인을 판단할 것")


if __name__ == "__main__":
    main()
