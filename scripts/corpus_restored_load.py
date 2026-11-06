#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 와 대조해 복원한 조문 JSON 을 L1 · L2 문서로 적재한다 (docs/5_룰/5-7c_수집과_현행판_확인.md).

조 구조가 없는 문서(안내 책자)는 일반 업로드로는 품질 low 라 활성화할 수 없다(docs/8_운영/8-7_규정_적재_API.md).
복원본을 입력으로 corpus_ingest 의 같은 저장 · 활성화 함수를 쓴다: 접수 → _준비(복원조문=) → _저장 → 활성화.

입력 JSON: {"메타": {"대조": "어떻게 대조했나"}, "조문": [{"조번호", "조제목", "페이지", "본문", "적용대상"(선택)}]}. 조순번은 파일 순서로 매긴다.
메타.대조 가 없으면 적재를 거부한다. 품질 high 의 근거라 ingest_jobs.경고 에 그대로 남긴다.
적용대상을 null 로 준 조는 조문만 저장하고 청크를 만들지 않는다. 범위 참조확장전용 은 신규 문서를 검색에서 뺀다.
대체 로 옛 판을 내리면 eval.golden_chunks 의 옛 판 청크를 같은 조의 새 판 청크로 옮기고, 활성화가 active 문서의 참조를 새 판으로 옮긴다.
실행: PYTHONIOENCODING=utf-8 python scripts/corpus_restored_load.py --목록 ontology/수집/적재목록.json [--저장소 <경로>] [--문서 <doc 이름 일부>] [--확인만]
      (한 건만: --json <조문.json> --원본 <원본 파일> --문서명 ... --발행기관 ... --시행일 ... [--사업명 ...])
접속: SUDDOE_INGEST_DSN, 없으면 SUDDOE_DSN(로컬 기본값). 운영에는 사용자 승인 뒤에만 돌린다. 절차: ontology/수집/P0-6_P0-7_기록.md
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


_파일형식 = {".pdf": "pdf", ".hwp": "hwp", ".hwpx": "hwpx", ".docx": "docx", ".xml": "xml", ".html": "html"}


def 조문_읽기(경로: Path) -> tuple[list[dict], str]:
    """(조문, 메타.대조). 어떻게 대조했는지 적히지 않은 입력은 품질을 high 로 둘 수 없어 거부한다."""
    d = json.loads(경로.read_text(encoding="utf-8"))
    대조 = (d.get("메타") or {}).get("대조")
    if not 대조:
        raise SystemExit(f"입력 메타에 대조 칸이 없다: {경로}")
    out = []
    for i, a in enumerate(d["조문"], 1):
        제목 = a.get("조제목")
        항목 = {"조번호": a["조번호"], "조제목": None if 제목 == "(소제목 없음)" else 제목, "조순번": i,
                "본문": a["본문"], "페이지": a.get("페이지")}
        if "적용대상" in a:
            항목["적용대상"] = a["적용대상"]
        out.append(항목)
    return out, 대조


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


def 적재(json경로: Path, 원본경로: Path, 메타: dict, *, 대체_doc_id: str | None = None, 범위: str | None = None) -> dict:
    조문, 대조 = 조문_읽기(json경로)
    if 원본경로.suffix.lower() not in _파일형식:
        raise SystemExit(f"원본 파일 형식을 모른다: {원본경로.name}")
    dsn = os.environ.get("SUDDOE_INGEST_DSN")
    열기 = (lambda: db.psycopg.connect(dsn, connect_timeout=10)) if dsn else (lambda: db.connect(connect_timeout=10))
    with 열기() as conn:
        doc_id = corpus_ingest.접수(conn, 메타, 원본파일명=원본경로.name, 형식=_파일형식[원본경로.suffix.lower()], 저장경로=원본경로)
    try:
        with 열기() as conn:
            작업 = corpus_ingest._작업_읽기(conn, doc_id)
        적재데이터 = corpus_ingest._준비(작업, 복원조문=조문)
        적재데이터["경고"].append(f"복원 입력 대조: {대조}")
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
        # 활성화 전이면 staged 문서까지 함께 지워 같은 명령으로 다시 돌릴 수 있게 한다. active 가 됐으면 삭제가 거부하므로 그대로 둔다.
        with 열기() as conn:
            try:
                corpus_ingest.삭제(conn, doc_id)
            except corpus_ingest.요청거부:
                pass
        raise SystemExit(f"적재 실패: {type(e).__name__}: {e}") from e
    return {"doc_id": doc_id, "조": len(적재데이터["조문"]), "청크": len(적재데이터["청크"]),
            "옮긴_정답셋_근거": 옮긴_근거, **활성, "경고": 적재데이터["경고"] + 활성["경고"]}


def _목록_적재(a) -> int:
    """적재 목록의 문서를 차례로 적재한다. 이미 active 인 doc_id 는 건너뛰어 끊긴 곳부터 다시 돌릴 수 있다."""
    저장소 = Path(a.저장소)
    목록 = json.loads(Path(a.목록).read_text(encoding="utf-8"))["문서"]
    if a.문서:
        목록 = [x for x in 목록 if a.문서 in x["문서명"]]
    for x in 목록:
        원본, 입력 = 저장소 / x["원본"], 저장소 / x["json"]
        조문, 대조 = 조문_읽기(입력)
        if not 원본.exists():
            raise SystemExit(f"원본 파일이 없다: {원본}")
        시행일 = date.fromisoformat(x["시행일"])
        메타 = {"layer": x["layer"], "발행기관": x["발행기관"], "문서명": x["문서명"], "사업명": x.get("사업명"), "시행일": 시행일,
               "version": x.get("version"), "적용대상": x["적용대상"]}
        if a.확인만:
            print(f"확인: {x['문서명']} 조 {len(조문)} · 대체 {x.get('대체')} · 범위 {x.get('범위')} · 대조 {대조[:40]}")
            continue
        doc_id = x.get("doc_id")
        with (db.psycopg.connect(os.environ["SUDDOE_INGEST_DSN"], connect_timeout=10) if os.environ.get("SUDDOE_INGEST_DSN")
              else db.connect(connect_timeout=10)) as conn:
            있음 = conn.execute("SELECT status FROM corpus.documents WHERE doc_id = %s", (doc_id,)).fetchone()
        if 있음 and 있음[0] == "active":
            print(f"건너뜀(이미 active): {doc_id}")
            continue
        print(json.dumps(적재(입력, 원본, 메타, 대체_doc_id=x.get("대체"), 범위=x.get("범위")), ensure_ascii=False, default=str), flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--목록", help="적재 목록 JSON (ontology/수집/적재목록.json). 주면 나머지 문서 인자는 쓰지 않는다")
    ap.add_argument("--저장소", default=str(Path(__file__).resolve().parent.parent), help="목록의 상대 경로 기준")
    ap.add_argument("--문서", help="목록에서 문서명에 이 글자가 든 문서만")
    ap.add_argument("--확인만", action="store_true", help="DB 없이 목록의 파일 · 입력 메타만 확인한다")
    ap.add_argument("--json")
    ap.add_argument("--원본")
    ap.add_argument("--문서명")
    ap.add_argument("--발행기관")
    ap.add_argument("--사업명", help="L2 는 필수(ingest_jobs_사업명_check). L1 은 뺀다")
    ap.add_argument("--시행일", type=date.fromisoformat)
    ap.add_argument("--적용대상", default="창업기업")
    ap.add_argument("--layer", default="L2")
    ap.add_argument("--version")
    ap.add_argument("--대체")
    ap.add_argument("--범위", choices=["검색대상", "참조확장전용"])
    a = ap.parse_args()
    if a.목록:
        return _목록_적재(a)
    if not (a.json and a.원본 and a.문서명 and a.발행기관 and a.시행일):
        ap.error("--목록 이 없으면 --json --원본 --문서명 --발행기관 --시행일 이 필요하다")
    메타 = {"layer": a.layer, "발행기관": a.발행기관, "문서명": a.문서명, "사업명": a.사업명, "시행일": a.시행일,
           "version": a.version, "적용대상": a.적용대상}
    print(json.dumps(적재(Path(a.json), Path(a.원본), 메타, 대체_doc_id=a.대체, 범위=a.범위), ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
