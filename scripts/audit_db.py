# -*- coding: utf-8 -*-
"""DB 무결성 전수 감사 — 한 번에 다 본다.

## 왜 만들었나

결손이 조각조각 나왔다. `documents.version` 전부 NULL 을 발견하기까지
검색 품질 측정 → 룰 검수 → 스키마 지도 → LLM 스키마 작업까지 네 단계를 거쳤다.
**그때그때 눈에 띈 것만 보면 매번 새로 나온다.** 전수로 한 번에 보고, 이 스크립트를
다시 돌려 확인한다.

## 무엇을 보나

  1. 행 수      — 빈 표는 "의도된 빈 표" 목록과 대조
  2. NULL       — 전부 NULL / 절반 이상 NULL 컬럼. **의도된 것은 화이트리스트로 뺀다**
  3. 참조 무결성 — FK 로 안 걸리는 논리적 고아 (doc_id 문자열 참조 등)
  4. 계약       — 판정 파이프라인이 전제하는 불변식

🔴 **화이트리스트가 이 스크립트의 본체다.** "NULL 이 많다" 는 정보가 아니다 —
   "NULL 이면 안 되는 곳이 NULL 이다" 가 정보다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/audit_db.py
    PYTHONIOENCODING=utf-8 python scripts/audit_db.py --verbose   # 정상 항목도 출력
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# ── 의도된 빈 표 — 서비스가 안 돌았거나 후속 단계 대기 ──────────────────
빈표_정상 = {
    "tenant.orgs": "서비스 미가동", "tenant.accounts": "서비스 미가동",
    "tenant.l3_documents": "사용자 업로드 대기", "tenant.l3_articles": "사용자 업로드 대기",
    "tenant.f_profile": "서비스 미가동", "tenant.f_exec": "서비스 미가동",
    "tenant.f_personnel": "서비스 미가동", "tenant.decisions": "판정기 미구현",
    "tenant.expense_plans": "프론트 미연결", "tenant.plan_tasks": "프론트 미연결",
    "tenant.unmapped_premise": "판정기 미구현",
    "corpus.case_chunks": "Stage 2 사례 인덱싱 대기",
    "corpus.check_items": "룰 확정 후 생성",
    "corpus.item_alias": "시드 미투입 (미적용 대장 #06)",
    "corpus.xref_mismatch": "build_refs 가 기록하지 않는다 (미적용 대장 #11)",
}

# ── 의도된 NULL — 왜 비어도 되는지 근거를 반드시 적는다 ─────────────────
#    ⚠️ 키는 **소문자**로 적는다. Postgres 가 따옴표 없는 식별자를 소문자화하므로
#       information_schema 에서 `기관ID` 가 `기관id` 로 나온다. 대문자로 적으면 조회가 빗나가
#       "사유 없는 전부 NULL" 로 잘못 잡힌다 (2026-08-31 실제로 겪음).
NULL_정상 = {
    ("corpus.documents", "기관id"): "L3 전용 컬럼. L3 문서가 아직 없다",
    ("corpus.chunks", "기관id"): "L3 전용. 판정 인덱스는 L1·L2 뿐",
    ("corpus.rules", "기관id"): "L3 오버레이 전용. 기관 규정이 아직 없다",
    # 🔴 페이지는 "의도된 NULL" 이 아니라 **파서가 못 뽑은 것**이다.
    #    `_stage0_articles.json` 조 레코드에 '페이지': None 으로 들어 있다.
    #    인용은 doc_id + 조번호로 특정되고 쪽번호는 표시용 부가정보라 판정에 필수가 아니다.
    #    고치려면 Stage 0 재파싱이 필요하다 — 후순위. 미결 대장에 올려뒀다.
    ("corpus.doc_articles", "페이지"): "🔴 파서 미추출 (의도된 NULL 아님). 표시용이라 판정 영향 없음 — 재파싱 필요",
    ("corpus.chunks", "페이지"): "🔴 doc_articles.페이지 가 비어 상속받지 못함. 위와 같은 건",
    ("corpus.chunks", "항호"): "조 단위 청킹. 900토큰 초과로 분할된 조만 값이 있다",
    ("corpus.rules", "한도_유형"): "한도 없는 비목이 다수",
    ("corpus.rules", "한도_값"): "한도 없는 비목이 다수",
    ("corpus.rules", "한도_단위"): "한도 없는 비목이 다수",
    ("corpus.refs", "보정근거"): "shifted 47건에만 붙는다",
    ("corpus.refs", "dst_조번호"): "「소득세법」처럼 조 없이 인용한 것. 폐포에서 제외 (RAG.md §4-3)",
    ("corpus.refs", "depth_hint"): "위임 계통 단계. 설계상 선택 필드",
    ("corpus.precedence_rules", "우선규범"): "L2>L3 6건은 상위규범 표기가 불필요 (미적용 #12)",
    ("corpus.documents", "시행일"): "연도만 있는 문서(세부관리기준 등)는 날짜를 지어내지 않는다",
    ("corpus.documents", "version"): "공고·사례집 등 판본 표기가 없는 문서",
    ("corpus.documents", "doc_type"): "분류 불가 문서 (전자협약 매뉴얼 등)",
    ("corpus.evidence_sources", "해당비목_정본"): "TIPS·R&D 계통은 창업패키지 10종에 매핑하지 않는다",
    # check_items 39행은 전부 L1 통합관리지침 근거라 전 사업에 걸린다.
    #    ⚠️ 사업별로 갈리는 항목(모두의창업 국외여비 전면 불가 등)은 아직 없다 — 결손이지 정상이 아니다.
    #       사업별 룰이 6사업으로 늘었으니 그때 채운다. 미결로 등록해 둔다.
    ("corpus.check_items", "사업명"): "🔴 전 사업 공통분만 있다. 사업별 항목 미작성 (결손)",
    ("corpus.check_items", "비목"): "전 비목 공통 항목 6건은 비목이 없다",
    ("corpus.check_items", "기본_오프셋일"): "집행일 기준 오프셋이 정해진 항목만 값이 있다",
    ("corpus.check_items", "검수자"): "verified=false 인 행",
    ("corpus.check_items", "검수일"): "verified=false 인 행",
}

# ── 계약: 판정 파이프라인이 전제하는 불변식 ─────────────────────────────
계약 = [
    ("임베딩 누락", "SELECT count(*) FROM corpus.chunks WHERE embedding IS NULL", 0),
    ("BM25 색인 누락", """SELECT count(*) FROM corpus.chunks c WHERE NOT EXISTS
        (SELECT 1 FROM corpus.chunk_len l WHERE l.chunk_id=c.chunk_id)""", 0),
    ("chunks -> documents 고아", """SELECT count(*) FROM corpus.chunks c WHERE NOT EXISTS
        (SELECT 1 FROM corpus.documents d WHERE d.doc_id=c.doc_id)""", 0),
    ("doc_articles -> documents 고아", """SELECT count(*) FROM corpus.doc_articles a WHERE NOT EXISTS
        (SELECT 1 FROM corpus.documents d WHERE d.doc_id=a.doc_id)""", 0),
    ("refs.dst 미해소 (경로/약칭)", """SELECT count(*) FROM corpus.refs r
        WHERE dst_doc_id IS NOT NULL AND NOT EXISTS
        (SELECT 1 FROM corpus.documents d WHERE d.doc_id=r.dst_doc_id)""", 42),
    ("rules 근거 조가 실재 안 함", """SELECT count(*) FROM corpus.rules, jsonb_array_elements(근거) e
        WHERE NOT EXISTS (SELECT 1 FROM corpus.doc_articles a
                           WHERE a.doc_id=e->>'doc_id' AND a.조번호=e->>'조번호')""", 0),
    ("rules.비목이 어휘집 밖", """SELECT count(DISTINCT 비목) FROM corpus.rules WHERE 비목 NOT IN
        ('재료비','인건비','기계장치','외주용역비','지급수수료','광고선전비','여비',
         '교육훈련비','창업활동비','특허권등무형자산취득비')""", 0),
    ("골든셋 정답근거 문서가 실재 안 함", """SELECT count(*) FROM eval.golden_set,
        jsonb_array_elements(정답근거) e WHERE 정답근거 IS NOT NULL AND NOT EXISTS
        (SELECT 1 FROM corpus.documents d WHERE d.doc_id = e->>'doc')""", 0),
    ("chunks/documents scope 드리프트", """SELECT count(*) FROM corpus.chunks c
        JOIN corpus.documents d ON d.doc_id=c.doc_id
        WHERE c.retrieval_scope <> d.retrieval_scope
          AND c.조번호 ~ '^제[0-9]'""", 0),   # 부속은 일부러 폐포전용이라 제외
    ("public 스키마 노출", "SELECT count(*) FROM pg_tables WHERE schemaname='public'", 0),
    ("골든셋 미검수", "SELECT count(*) FROM eval.golden_set WHERE NOT verified", 0),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="정상 항목도 출력")
    a = ap.parse_args()
    문제 = []

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()

        # ── 1·2. 행 수 + NULL ──────────────────────────────────────────
        cur.execute("""SELECT table_schema, table_name FROM information_schema.tables
                        WHERE table_schema IN ('corpus','tenant','eval')
                          AND table_type='BASE TABLE' ORDER BY 1,2""")
        print("── 표 · NULL ───────────────────────────────────────────────────────")
        for sch, tbl in cur.fetchall():
            풀 = f"{sch}.{tbl}"
            n = cur.execute(f'SELECT count(*) FROM {sch}."{tbl}"').fetchone()[0]
            if n == 0:
                사유 = 빈표_정상.get(풀)
                if 사유:
                    if a.verbose: print(f"  {풀:32} 0행  (정상: {사유})")
                else:
                    문제.append(f"{풀} 이 비어 있는데 사유가 등록돼 있지 않다")
                    print(f"🔴 {풀:32} 0행  ← 의도된 것인지 확인 필요")
                continue
            cur.execute("""SELECT column_name FROM information_schema.columns
                            WHERE table_schema=%s AND table_name=%s
                            ORDER BY ordinal_position""", (sch, tbl))
            나쁨 = []
            for (col,) in cur.fetchall():
                널 = cur.execute(f'SELECT count(*) FROM {sch}."{tbl}" WHERE "{col}" IS NULL').fetchone()[0]
                if 널 == 0:
                    continue
                사유 = NULL_정상.get((풀, col))
                비율 = 널 * 100 // n
                if 널 == n and not 사유:
                    나쁨.append(f"🔴{col}=전부NULL")
                    문제.append(f"{풀}.{col} 이 전부 NULL 인데 사유가 없다")
                elif 비율 > 50 and not 사유:
                    나쁨.append(f"⚠️{col}={비율}%")
            상태 = " · ".join(나쁨) if 나쁨 else "이상 없음"
            if 나쁨 or a.verbose:
                print(f"  {풀:32} {n:>9,}  {상태}")

        # ── 3·4. 계약 ─────────────────────────────────────────────────
        print("\n── 계약 (판정 파이프라인의 불변식) ─────────────────────────────")
        for 이름, sql, 기대 in 계약:
            실제 = cur.execute(sql).fetchone()[0]
            if 실제 == 기대:
                if a.verbose: print(f"  ✅ {이름:38} {실제}")
            else:
                print(f"  🔴 {이름:38} {실제}  (기대 {기대})")
                문제.append(f"{이름}: {실제} (기대 {기대})")

    print("\n" + "=" * 68)
    if 문제:
        print(f"🔴 문제 {len(문제)}건")
        for x in 문제: print(f"   · {x}")
        sys.exit(1)
    print("✅ 감사 통과 — 미등록 결손 없음")


if __name__ == "__main__":
    main()
