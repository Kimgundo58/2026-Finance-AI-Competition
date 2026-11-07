# -*- coding: utf-8 -*-
"""CI 잡 `ontology-db` 의 빈 DB 에 schema.json 문서키 문서의 조문을 넣는다(docs/5_룰/5-9_검증과_측정.md, P5-1a).

적재 검사는 "발췌 ⊂ DB 본문 · 문서키 활성 판" 을 보는데 레포에는 코퍼스 DB 가 없다. 그래서 그 문서들의 documents · doc_articles 행만
`ontology/테스트/CI_원문.json` 으로 둔다. 판이 바뀌거나 문서키가 늘면 로컬 DB 에서 다시 내보낸다.

    python scripts/ontology_ci_seed.py --내보내기   # SUDDOE_DSN(기본 로컬) 에서 읽어 파일을 쓴다
    python scripts/ontology_ci_seed.py              # SUDDOE_INGEST_DSN 에 넣는다. 파일에 없는 문서키가 있으면 실패한다
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db, paths  # noqa: E402

ROOT = paths.ensure_on_path()
파일 = ROOT / "ontology" / "테스트" / "CI_원문.json"
_문서칸 = ("doc_id", "layer", "domain", "doc_type", "version", "시행일", "status", "parse_quality", "extraction", "src_path",
          "index_target", "retrieval_scope")
_조문칸 = ("조번호", "조제목", "조순번", "본문", "페이지", "삭제")


def _문서키들() -> dict[str, str]:
    return json.loads((ROOT / "ontology" / "schema.json").read_text(encoding="utf-8"))["문서키"]


def 내보내기() -> int:
    문서키 = _문서키들()
    문서들 = []
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        for 키, doc_id in sorted(문서키.items()):
            행 = cur.execute(f"SELECT {', '.join(_문서칸)} FROM corpus.documents WHERE doc_id = %s", (doc_id,)).fetchone()
            if 행 is None or 행[_문서칸.index("status")] != "active":
                print(f"실패: {키} 의 {doc_id} 가 active 가 아니다")
                return 1
            문서 = {칸: (값.isoformat() if 칸 == "시행일" and 값 else 값) for 칸, 값 in zip(_문서칸, 행)}
            문서["조문"] = [dict(zip(_조문칸, r)) for r in cur.execute(
                f"SELECT {', '.join(_조문칸)} FROM corpus.doc_articles WHERE doc_id = %s ORDER BY article_id", (doc_id,)).fetchall()]
            문서들.append(문서)
    파일.write_text(json.dumps({"설명": "CI 잡 ontology-db 의 입력. scripts/ontology_ci_seed.py --내보내기 로 만든다. 고쳐 쓰지 않는다.",
                               "문서": 문서들}, ensure_ascii=False, indent=0) + "\n", encoding="utf-8", newline="\n")
    print(f"내보냄: 문서 {len(문서들)} · 조문 {sum(len(d['조문']) for d in 문서들)} · {파일.stat().st_size} 바이트")
    return 0


def 넣기(dsn: str) -> int:
    묶음 = json.loads(파일.read_text(encoding="utf-8"))["문서"]
    없는것 = sorted(set(_문서키들().values()) - {d["doc_id"] for d in 묶음})
    if 없는것:
        print(f"실패: {파일.name} 에 없는 문서키 문서 {없는것}. 로컬에서 --내보내기 로 다시 만든다")
        return 1
    조문수 = 0
    with db.psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        for d in 묶음:
            cur.execute(f"INSERT INTO corpus.documents ({', '.join(_문서칸)}) VALUES ({', '.join(['%s'] * len(_문서칸))})",
                        [d[칸] for 칸 in _문서칸])
            cur.executemany(
                f"INSERT INTO corpus.doc_articles (doc_id, {', '.join(_조문칸)}) VALUES (%s, {', '.join(['%s'] * len(_조문칸))})",
                [[d["doc_id"], *(a[칸] for 칸 in _조문칸)] for a in d["조문"]])
            조문수 += len(d["조문"])
    print(f"넣음: 문서 {len(묶음)} · 조문 {조문수}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CI 잡의 조문 입력")
    p.add_argument("--내보내기", action="store_true", help="로컬 DB 에서 읽어 파일을 쓴다")
    a = p.parse_args(argv)
    return 내보내기() if a.내보내기 else 넣기(os.environ["SUDDOE_INGEST_DSN"])


if __name__ == "__main__":
    sys.exit(main())
