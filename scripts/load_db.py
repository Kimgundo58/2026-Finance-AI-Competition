# -*- coding: utf-8 -*-
"""(b) 적재 — Stage 0 산출물을 corpus/tenant/eval 스키마에 넣는다.

  corpus.documents        문서 대장 (layer · status · index_target · retrieval_scope)
  corpus.doc_articles     조 단위 원문 (삭제 플래그 포함)
  corpus.refs             참조 그래프 (src_layer 포함)
  corpus.precedence_rules 우선순위 조항
  eval.golden_set         평가 정답지  ← corpus 가 아니다. Supabase 덤프 대상 밖

`corpus.chunks` 는 Stage 2 몫이라 여기서 안 채운다.
`corpus.rules` 는 `seed_rules.py` 가 채운다.

실행:  python scripts/load_db.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import psycopg  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import index_guard  # noqa: E402
from scope import 범위밖_조  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "2026_Finance_DATA_FOR_RAG"
DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# 현행 판본. `documents.status` 를 Stage 0 이 아직 안 채운다 — tag_apply_target 과 같은 목록이다.
# status 가 Stage 0 산출물에 들어오면 이 상수는 지운다.
현행 = {
    "예비창업패키지 세부관리기준(2025년)",
    "초기창업패키지 세부관리기준(2025년)",
    "재도전성공패키지 세부관리기준(2025년)",
    "창업도약패키지 세부관리기준(2025년)",
    "창업중심대학 세부관리기준2025년 개정",
    "초격차 스타트업 프로젝트 세부관리기준(제10차)",
    "모두의 창업 프로젝트 세부관리기준(개정본)",
    "붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문",
    "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223",
}


def law_index_flags() -> dict[str, bool]:
    """`_law_sources.json` 의 index 플래그. 파일 stem -> bool."""
    p = ROOT / "법령 PDF" / "_law_sources.json"
    if not p.exists():
        return {}
    src = json.loads(p.read_text(encoding="utf-8"))
    return {m["file"].rsplit(".", 1)[0]: bool(m.get("index"))
            for m in src.values() if m.get("file")}


def main() -> None:
    s0 = json.loads((DATA / "_stage0_articles.json").read_text(encoding="utf-8"))
    refs = json.loads((DATA / "_refs.json").read_text(encoding="utf-8"))
    prec = json.loads((DATA / "_precedence_rules.json").read_text(encoding="utf-8"))
    gold = json.loads((DATA / "_골든셋_스테이징" / "_골든셋_확정본.json").read_text(encoding="utf-8"))
    flags = law_index_flags()

    docs, arts, skipped = [], [], []
    for doc_id, d in s0.items():
        layer = d.get("layer")
        path = d.get("path", "")
        # 🔴 인덱싱 경계는 코드가 강제한다 (RAG.md §1). 새 경로는 반드시 이 게이트를 탄다.
        why = index_guard.reject_reason(path, layer)
        if why:
            skipped.append((doc_id, why))
            continue

        # status: 현행 목록 + index=true 법령만 active. 나머지 구판은 superseded.
        if doc_id in 현행:
            status = "active"
        elif layer == "L1" and flags.get(doc_id) is True:
            status = "active"
        elif layer == "사례":
            status = "active"
        elif layer == "L1" and flags.get(doc_id) is False:
            status = "reference"      # 타부처 보조금 규정 등 — 코퍼스에는 두되 판정 밖
        else:
            status = "superseded"     # 구판 세부관리기준

        quality = d.get("quality") or "high"
        index_target = status == "active" and layer in ("L1", "L2")
        docs.append((doc_id, layer, "창업지원사업", None, None, None, None, status,
                     "high" if quality == "high" else "low",
                     "native", path, ["judgment_index"] if index_target else [],
                     index_target,
                     # 좁히기는 골든셋 Recall@5 A/B 후에 한다. 지금은 전부 진입점.
                     "진입점"))

        밖 = 범위밖_조(doc_id, d.get("articles") or [])
        for a in d.get("articles") or []:
            if a["조번호"] in 밖:
                continue              # 모두의창업 제3편 로컬트랙 — 범위 밖
            arts.append((doc_id, a["조번호"], a.get("조제목"), a.get("조번호_int"),
                         a.get("본문") or "", a.get("페이지"), bool(a.get("삭제"))))

    doc_ids = {d[0] for d in docs}
    edges = [(e["src_doc_id"], e["src_조번호"], e.get("src_layer"), e["참조문자열"],
              e["관계"], e.get("dst_doc_id"), e.get("dst_조번호"),
              e["해소상태"], e.get("보정근거"))
             for e in refs["edges"] if e["src_doc_id"] in doc_ids]

    precs = [(r["사업명"], r["우선계층"], r["열위계층"], r["범위"], r.get("우선규범"),
              json.dumps(r["근거"], ensure_ascii=False), r["원문"], r.get("해석"),
              bool(r.get("verified")), r.get("검수자"), r.get("검수일"))
             for r in prec["rules"]]

    golds = [(x.get("_세트") or "본세트", str(x["no"]), x.get("사업"), x["질문"],
              x["정답_판정"], json.dumps(x.get("정답_근거"), ensure_ascii=False),
              x.get("근거_원문"), json.dumps(x.get("해야할일"), ensure_ascii=False),
              bool(x.get("verified")), x.get("검수자"))
             for x in gold["문항"]]

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE corpus.documents CASCADE;")
        cur.execute("TRUNCATE corpus.refs;")
        cur.execute("TRUNCATE corpus.precedence_rules;")
        cur.execute("TRUNCATE eval.golden_set;")
        cur.executemany("""INSERT INTO corpus.documents
            (doc_id, layer, domain, 기관ID, doc_type, version, 시행일, status,
             parse_quality, extraction, src_path, roles, index_target, retrieval_scope)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", docs)
        cur.executemany("""INSERT INTO corpus.doc_articles
            (doc_id, 조번호, 조제목, 조번호_int, 본문, 페이지, 삭제)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", arts)
        cur.executemany("""INSERT INTO corpus.refs
            (src_doc_id, src_조번호, src_layer, 참조문자열, 관계,
             dst_doc_id, dst_조번호, 해소상태, 보정근거)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", edges)
        cur.executemany("""INSERT INTO corpus.precedence_rules
            (사업명, 우선계층, 열위계층, 범위, 우선규범, 근거, 원문, 해석,
             verified, 검수자, 검수일)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", precs)
        cur.executemany("""INSERT INTO eval.golden_set
            (세트, no, 사업명, 질문, 정답판정, 정답근거, 근거원문, 해야할일, verified, 검수메모)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", golds)
        conn.commit()

    print(f"index_guard 거부 {len(skipped)}건")
    for d, w in skipped[:5]:
        print(f"   {d[:44]} : {w}")
    with psycopg.connect(DSN) as conn:
        for row in conn.execute("SELECT * FROM corpus.v_적재현황").fetchall():
            if row[1]:
                print(f"   {row[0]:30s} {row[1]:>7,}")


if __name__ == "__main__":
    main()
