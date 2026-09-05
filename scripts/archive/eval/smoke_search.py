# -*- coding: utf-8 -*-
"""인덱스 스모크 테스트 : 실제 질문으로 벡터 검색이 맞는 조항을 물어오는지 확인.

파이프라인이 살아있는지 보는 최소 검증이다. 정식 평가는 정답셋(§8)으로 한다.

실행:  python scripts/archive/eval/smoke_search.py
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
