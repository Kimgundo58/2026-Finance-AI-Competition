#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 와 대조해 복원한 조문 JSON 을 L1 · L2 문서로 적재한다 (docs/5_룰/5-7c_수집과_현행판_확인.md).

조 구조가 없는 문서(안내 책자)는 일반 업로드로는 품질 low 라 활성화할 수 없다(docs/8_운영/8-7_규정_적재_API.md).
복원본을 입력으로 corpus_ingest 의 같은 저장 · 활성화 함수를 쓴다: 접수 → _준비(복원조문=) → _저장 → 활성화.

입력 JSON: {"조문": [{"조번호", "조제목", "페이지", "본문", "적용대상"(선택)}]}. 조순번은 파일 순서로 매긴다.
적용대상을 null 로 준 조는 조문만 저장하고 청크를 만들지 않는다. --범위 참조확장전용 은 신규 문서를 검색에서 뺀다.
--대체 로 옛 판을 내리면 eval.golden_chunks 의 옛 판 청크를 같은 조의 새 판 청크로 옮긴다(표가 없는 DB 는 건너뛴다).
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
        항목 = {"조번호": a["조번호"], "조제목": None if 제목 == "(소제목 없음)" else 제목, "조순번": i,
                "본문": a["본문"], "페이지": a.get("페이지")}
        if "적용대상" in a:
            항목["적용대상"] = a["적용대상"]
        out.append(항목)
    return out


def _정답셋_청크_옮기기(conn, 옛: str, 새: str) -> int:
    """활성화가 옛 판 청크를 지우기 전에 정답셋 근거를 같은 조의 새 판 청크로 옮긴다(8-7 「골든셋 재매칭」).

    옮기는 행의 내용은 읽지 않는다. 새 판에 같은 조가 없으면 옮기지 못해 활성화가 409 로 멈춘다.
    """
    if not conn.execute("SELECT to_regclass('eval.golden_chunks')").fetchone()[0]:
        return 0
    return conn.execute(
        """UPDATE eval.golden_chunks g
              SET chunk_id = n.chunk_id, article_id = n.article_id, doc_id = n.doc_id, "매칭방법" = '조번호'
             FROM corpus.chunks o,
                  LATERAL (SELECT c.chunk_id, c.article_id, c.doc_id FROM corpus.chunks c
                            WHERE c.doc_id = %(새)s AND c."조번호" = o."조번호"
                            ORDER BY (c."항호" IS NOT DISTINCT FROM o."항호") DESC, c.chunk_id LIMIT 1) n
            WHERE g.chunk_id = o.chunk_id AND o.doc_id = %(옛)s""", {"옛": 옛, "새": 새}).rowcount


def 적재(json경로: Path, pdf경로: Path, 메타: dict, *, 대체_doc_id: str | None = None, 범위: str | None = None) -> dict:
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
            옮긴_근거 = _정답셋_청크_옮기기(conn, 대체_doc_id, doc_id) if 대체_doc_id else 0
            활성 = corpus_ingest.활성화(conn, doc_id, 대체_doc_id=대체_doc_id, 신규문서=대체_doc_id is None, 중복_허용=True)
            if 범위:
                conn.execute("UPDATE corpus.documents SET retrieval_scope = %s WHERE doc_id = %s", (범위, doc_id))
                conn.execute("UPDATE corpus.chunks SET retrieval_scope = %s WHERE doc_id = %s", (범위, doc_id))
                활성["retrieval_scope"] = 범위
    except Exception as e:
        with 열기() as conn:
            conn.execute("DELETE FROM corpus.ingest_jobs WHERE doc_id = %s AND 상태 = 'parsing'", (doc_id,))
        raise SystemExit(f"적재 실패: {type(e).__name__}: {e}") from e
    return {"doc_id": doc_id, "조": len(적재데이터["조문"]), "청크": len(적재데이터["청크"]), "경고": 적재데이터["경고"],
            "옮긴_정답셋_근거": 옮긴_근거, **활성}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--문서명", required=True)
    ap.add_argument("--발행기관", required=True)
    ap.add_argument("--사업명", help="L2 는 필수(ingest_jobs_사업명_check). L1 은 뺀다")
    ap.add_argument("--시행일", required=True, type=date.fromisoformat)
    ap.add_argument("--적용대상", default="창업기업")
    ap.add_argument("--layer", default="L2")
    ap.add_argument("--version")
    ap.add_argument("--대체")
    ap.add_argument("--범위", choices=["검색대상", "참조확장전용"])
    a = ap.parse_args()
    메타 = {"layer": a.layer, "발행기관": a.발행기관, "문서명": a.문서명, "사업명": a.사업명, "시행일": a.시행일,
           "version": a.version, "적용대상": a.적용대상}
    print(json.dumps(적재(Path(a.json), Path(a.pdf), 메타, 대체_doc_id=a.대체, 범위=a.범위), ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
