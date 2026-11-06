#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 와 대조해 복원한 조문 JSON 을 L1 · L2 문서로 적재한다 (docs/5_룰/5-7c_수집과_현행판_확인.md).

조 구조가 없는 문서(안내 책자)는 일반 업로드로는 품질 low 라 활성화할 수 없다(docs/8_운영/8-7_규정_적재_API.md).
복원본을 입력으로 corpus_ingest 의 같은 저장 · 활성화 함수를 쓴다: 접수 → _준비(복원조문=) → _저장 → 활성화.

입력 JSON: {"조문": [{"조번호", "조제목", "페이지", "본문"}]}. 조순번은 파일 순서로 매긴다.
실행: PYTHONIOENCODING=utf-8 python scripts/corpus_restored_load.py --json ontology/추출/복원/안내책자.json \
        --pdf <원본 PDF> --문서명 "..." --발행기관 창업진흥원 --사업명 "모두의 창업 프로젝트" --시행일 2026-10-15 --적용대상 창업기업
접속: SUDDOE_INGEST_DSN, 없으면 SUDDOE_DSN(로컬 기본값). 운영에는 사용자 승인 뒤에만 돌린다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db  # noqa: E402
import corpus_ingest  # noqa: E402


def 조문_읽기(경로: Path) -> list[dict]:
    d = json.loads(경로.read_text(encoding="utf-8"))
    out = []
    for i, a in enumerate(d["조문"], 1):
        제목 = a.get("조제목")
        out.append({"조번호": a["조번호"], "조제목": None if 제목 == "(소제목 없음)" else 제목, "조순번": i,
                    "본문": a["본문"], "페이지": a.get("페이지")})
    return out


def 적재(json경로: Path, pdf경로: Path, 메타: dict, *, 대체_doc_id: str | None = None) -> dict:
    조문 = 조문_읽기(json경로)
    dsn = os.environ.get("SUDDOE_INGEST_DSN")
    열기 = (lambda: db.psycopg.connect(dsn, connect_timeout=10)) if dsn else (lambda: db.connect(connect_timeout=10))
    with 열기() as conn:
        doc_id = corpus_ingest.접수(conn, 메타, 원본파일명=pdf경로.name, 형식="pdf", 저장경로=pdf경로)
    try:
        with 열기() as conn:
            작업 = corpus_ingest._작업_읽기(conn, doc_id)
        적재데이터 = corpus_ingest._준비(작업, 복원조문=조문)
        with 열기() as conn:
            if not corpus_ingest._저장(conn, 작업, 적재데이터):
                raise RuntimeError("저장하지 못했다(접수 상태가 바뀌었다)")
        with 열기() as conn:
            활성 = corpus_ingest.활성화(conn, doc_id, 대체_doc_id=대체_doc_id, 신규문서=대체_doc_id is None, 중복_허용=True)
    except Exception as e:
        with 열기() as conn:
            conn.execute("DELETE FROM corpus.ingest_jobs WHERE doc_id = %s AND 상태 = 'parsing'", (doc_id,))
        raise SystemExit(f"적재 실패: {type(e).__name__}: {e}") from e
    return {"doc_id": doc_id, "조": len(적재데이터["조문"]), "청크": len(적재데이터["청크"]), "경고": 적재데이터["경고"], **활성}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--문서명", required=True)
    ap.add_argument("--발행기관", required=True)
    ap.add_argument("--사업명", required=True)
    ap.add_argument("--시행일", required=True, type=date.fromisoformat)
    ap.add_argument("--적용대상", default="창업기업")
    ap.add_argument("--layer", default="L2")
    ap.add_argument("--version")
    ap.add_argument("--대체")
    a = ap.parse_args()
    메타 = {"layer": a.layer, "발행기관": a.발행기관, "문서명": a.문서명, "사업명": a.사업명, "시행일": a.시행일,
           "version": a.version, "적용대상": a.적용대상}
    print(json.dumps(적재(Path(a.json), Path(a.pdf), 메타, 대체_doc_id=a.대체), ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
