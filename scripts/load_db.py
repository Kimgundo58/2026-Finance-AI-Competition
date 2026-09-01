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
    "2026년 재도전성공패키지 세부관리기준(11차 개정)",   # 2026-09-01 판본 역전 교정
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
                     "native", path,
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

    # 🔴 `대상`·`평가범위`·`채점모드` 를 반드시 같이 넣는다 (2026-09-01).
    #    이 셋이 빠져 있어서 «대상='주관기관' 16문항은 평가 범위 밖» 이라는
    #    초안 메타의 확정 사항이 DB 에 도달하지 못했고, `eval_store.평가대상()` 이
    #    범위밖 문항까지 채점 분모에 넣고 있었다. 창업팀 전용 서비스인데 주관기관
    #    문항 21% 로 점수를 재던 셈이다 — 검색 hit@5 미스의 큰 덩어리가 이것이었다.
    golds = [(x.get("_세트") or "본세트", str(x["no"]), x.get("사업"), x["질문"],
              x["정답_판정"], json.dumps(x.get("정답_근거"), ensure_ascii=False),
              x.get("근거_원문"), json.dumps(x.get("해야할일"), ensure_ascii=False),
              bool(x.get("verified")), x.get("검수자"),
              x.get("비목"), x.get("대상"), x.get("평가범위"), x.get("채점모드"))
             for x in gold["문항"]]

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        # 🔴 **Stage 2 이후에는 이 스크립트를 돌리면 안 된다** (2026-09-01).
        #    아래 `TRUNCATE corpus.documents CASCADE` 가 chunks 를 FK CASCADE 로 같이 지운다
        #    = 임베딩 2만여 건이 날아가고 GPU 팟을 다시 열어야 한다. 에러가 안 나므로
        #    돌린 사람은 다음 검색이 0건 나올 때까지 모른다.
        #    골든셋만 갱신하려는 것이면 `_work/_골든셋_재적재.py` 를 쓴다.
        n청크 = cur.execute("SELECT count(*) FROM corpus.chunks").fetchone()[0]
        if n청크 and "--청크삭제승인" not in sys.argv:
            sys.exit(
                f"\n🔴 corpus.chunks 에 {n청크:,}건이 있다. 이대로 진행하면 "
                f"TRUNCATE ... CASCADE 로 **전부 지워지고 임베딩을 다시 계산해야 한다.**\n"
                "   · 골든셋만 갱신하려면  : python scripts/_work/_골든셋_재적재.py --commit\n"
                "   · 정말 전체 재적재라면 : 이 명령에 --청크삭제승인 을 붙인다 "
                "(그 뒤 stage2_chunk → 임베딩까지 다시 돌려야 한다)\n")
        cur.execute("TRUNCATE corpus.documents CASCADE;")
        cur.execute("TRUNCATE corpus.refs;")
        cur.execute("TRUNCATE corpus.precedence_rules;")
        cur.execute("TRUNCATE eval.golden_set;")
        cur.executemany("""INSERT INTO corpus.documents
            (doc_id, layer, domain, 기관ID, doc_type, version, 시행일, status,
             parse_quality, extraction, src_path, index_target, retrieval_scope)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", docs)
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
            (세트, no, 사업명, 질문, 정답판정, 정답근거, 근거원문, 해야할일, verified, 검수메모,
             비목, 대상, 평가범위, 채점모드)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", golds)
        conn.commit()

    # ════════════════════════════════════════════════════════════════════════
    # 🔴 후처리 3종 — 이걸 빼면 적재할 때마다 조용히 되돌아간다
    #
    #   위 INSERT 는 JSON 산출물의 값을 그대로 넣는다. 그런데 아래 셋은 JSON 이 아니라
    #   **적재 후 별도 스크립트가 실측으로 정하는 값**이라, TRUNCATE 재적재를 하면
    #   전부 초기값으로 돌아간다. 에러가 안 나므로 알아채기 어렵다:
    #
    #     extraction        전부 native  -> 스캔 판독본이 A등급 인용 가능해진다
    #     retrieval_scope   전부 진입점  -> 민상법·세법이 검색 후보로 돌아온다
    #                                       (실측: RRF hit@5 49.2% -> 44.6% 로 떨어진다)
    #     refs.dst_doc_id   파일경로/약칭 -> refs 폐포가 폐포전용 문서에 도달 못 한다
    #     version/시행일/doc_type  전부 NULL -> 판정 응답의 **버전스탬프**를 못 만든다
    #                       (LLM.md §3-4 [2겹]. 이 아래 INSERT 가 하드코딩 None 을 넣는다)
    #
    #   자동 실행하지 않고 **안내만 한다** — 각 스크립트가 판단(분류 기준)을 담고 있어
    #   무인 실행하면 근거 없이 값이 바뀐다. 대신 현재 상태를 재서 빠졌으면 크게 알린다.
    # ════════════════════════════════════════════════════════════════════════
    with psycopg.connect(DSN) as conn:
        native = conn.execute(
            "SELECT count(*) FROM corpus.documents WHERE extraction='native'").fetchone()[0]
        전체 = conn.execute("SELECT count(*) FROM corpus.documents").fetchone()[0]
        진입점 = conn.execute(
            "SELECT count(*) FROM corpus.documents WHERE retrieval_scope='진입점'").fetchone()[0]
        경로형 = conn.execute("""SELECT count(*) FROM corpus.refs r
            WHERE dst_doc_id IS NOT NULL AND NOT EXISTS
              (SELECT 1 FROM corpus.documents d WHERE d.doc_id = r.dst_doc_id)""").fetchone()[0]
        버전없음 = conn.execute(
            "SELECT count(*) FROM corpus.documents WHERE version IS NULL").fetchone()[0]

    할일 = []
    if native == 전체:
        할일.append(("extraction 이 전부 native", "python scripts/retag_extraction.py --apply"))
    if 진입점 == 전체:
        할일.append(("retrieval_scope 가 전부 진입점", "python scripts/retag_scope.py"))
    if 경로형:
        할일.append((f"refs.dst_doc_id 미해소 {경로형:,}건",
                     "python scripts/normalize_refs.py --apply"))
    if 버전없음 == 전체:
        할일.append(("documents.version 이 전부 NULL (판정 응답의 버전스탬프를 못 만든다)",
                     "python scripts/backfill_doc_meta.py --apply"))
    if 할일:
        print("\n" + "=" * 68)
        print("🔴 후처리가 남았다. 이 상태로 두면 조용히 틀린다:")
        for 무엇, 명령 in 할일:
            print(f"   · {무엇}")
            print(f"       PYTHONIOENCODING=utf-8 {명령}")
        print("=" * 68)
    else:
        print("\n후처리 3종 모두 반영돼 있다 (extraction · retrieval_scope · refs.dst_doc_id)")

    print(f"index_guard 거부 {len(skipped)}건")
    for d, w in skipped[:5]:
        print(f"   {d[:44]} : {w}")
    with psycopg.connect(DSN) as conn:
        for row in conn.execute("SELECT * FROM corpus.v_적재현황").fetchall():
            if row[1]:
                print(f"   {row[0]:30s} {row[1]:>7,}")


if __name__ == "__main__":
    main()
