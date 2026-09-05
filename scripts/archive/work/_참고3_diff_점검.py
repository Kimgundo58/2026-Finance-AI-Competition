# -*- coding: utf-8 -*-
"""4문서 재파싱 -> DB 현재값과 비교해 실제로 달라진 (doc_id, 조번호)만 뽑는다."""
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

import sys
from pathlib import Path
sys.path.insert(0, "scripts")
import stage0_run as s0
from _lib import db

파일들 = [
    "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초격차 스타트업 프로젝트/초격차 스타트업 프로젝트 세부관리기준(제10차).pdf",
    "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/민관공동창업자발굴육성(TIPS)/2026/붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문.pdf",
    "2026_Finance_DATA_FOR_RAG/창진원/창업도약패키지/창업도약패키지 세부관리기준(2025년).pdf",
    "2026_Finance_DATA_FOR_RAG/창진원/창업중심대학/창업중심대학 세부관리기준2025년 개정.pdf",
]

with db.connect(autocommit=True) as conn, conn.cursor() as cur:
    for rel in 파일들:
        p = Path(rel)
        arts, strategy = s0.분해(p)
        doc_id = p.stem
        cur.execute("SELECT 조번호, 본문 FROM corpus.doc_articles WHERE doc_id=%s", (doc_id,))
        현재 = {r[0]: r[1] for r in cur.fetchall()}
        print(f"\n=== {doc_id}  (strategy={strategy}, DB조수={len(현재)}, 신규조수={len(arts)}) ===")
        if not 현재:
            print("  DB 에 이 doc_id 로 적재된 조가 없다 — 다른 doc_id 로 들어있을 수 있다.")
            continue
        바뀜 = 0
        for a in arts:
            조 = a.get("조번호")
            새본문 = a.get("본문") or ""
            if 조 in 현재 and 현재[조] != 새본문:
                바뀜 += 1
                print(f"  DIFF  {조}  DB길이={len(현재[조])} 신규길이={len(새본문)}")
        if 바뀜 == 0:
            print("  차이 없음")
