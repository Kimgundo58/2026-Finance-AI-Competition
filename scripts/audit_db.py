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
    # D 세션 2026-08-31 신설 3표.
    "tenant.incidents": "기관ID 누수 사고 기록. 사고가 없으면 비는 게 정상이다 (D1-d)",
    "eval.runs": "평가 실행 기록. eval_e2e.py / sweep_retrieval.py 를 돌리면 채워진다 (D1-e)",
    "eval.run_items": "eval.runs 종속. 위와 같은 건",
}

# ── 의도된 NULL — 왜 비어도 되는지 근거를 반드시 적는다 ─────────────────
#    ⚠️ 키는 **소문자**로 적는다. Postgres 가 따옴표 없는 식별자를 소문자화하므로
#       information_schema 에서 `기관ID` 가 `기관id` 로 나온다. 대문자로 적으면 조회가 빗나가
#       "사유 없는 전부 NULL" 로 잘못 잡힌다 (2026-08-31 실제로 겪음).
NULL_정상 = {
    ("corpus.documents", "기관id"): "L3 전용 컬럼. L3 문서가 아직 없다",
    ("corpus.chunks", "기관id"): "L3 전용. 판정 인덱스는 L1·L2 뿐",
    ("corpus.rules", "기관id"): "L3 오버레이 전용. 기관 규정이 아직 없다",
    # B 세션 2026-08-31. 상품명→비목 매핑은 8사업이 같은 통합관리지침 비목 체계를 써서
    # 사업별로 갈리지 않는다. 갈리는 것은 "그 비목에서 되냐" 이고 그건 corpus.rules 의 몫.
    # 사업 전용 별칭이 생기면 그때 NOT NULL 로 들어온다 (조회는 이미 두 경우를 다 본다).
    ("corpus.item_alias", "사업명"): "전 사업 공통 별칭. 사업별로 갈리는 매핑이 아직 없다 (B)",
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
    # ── D 세션 2026-08-31 ─────────────────────────────────────────────
    # 🔴 추측으로 채우지 않았다. 사업별 최대 지원금액은 공고문마다 다르고
    #    규정 코퍼스(세부관리기준·통합관리지침)에서 확정 근거를 찾지 못했다.
    #    채울 때는 반드시 상한_근거 JSONB 에 doc_id·조번호를 같이 박는다.
    ("corpus.programs", "정부지원상한"): "🔴 코퍼스에 확정 근거 없음. 추측 금지 — 근거 확보 시 상한_근거와 함께 채운다",
    ("corpus.programs", "상한_단위"): "정부지원상한과 한 쌍. 위와 같은 건",
    ("corpus.programs", "우선규범"): "L1>L2 인 2사업(초격차·모두의창업)에만 값이 있다. 나머지 6은 통합관리지침 기본",
    ("corpus.programs", "트랙범위"): "사업 안에서 범위를 잘라야 하는 것은 모두의창업(제3편 로컬트랙 제외) 하나뿐",
    ("corpus.programs", "비고"): "특기사항이 있는 사업에만 적는다",
    ("corpus.recheck_queue", "처리자"): "사람이 검토한 뒤에 채워진다. 대기 상태에서 비는 게 정상 (H1)",
    ("corpus.recheck_queue", "처리일"): "위와 같은 건",
    # 🔴 실패사유는 "비어 있어야 정상" 이다 — 값이 있으면 그 근거를 역추적하지 못했다는 뜻.
    ("eval.golden_chunks", "실패사유"): "역추적 성공 행은 비어 있다. 값이 있는 4행이 코퍼스 결손(D3)",
    ("eval.golden_chunks", "article_id"): "chunks 에 article_id 가 없는 청크가 있다",
    # 🔴 적용범위는 공통 문항에만 있다. 사업 지정 문항(50건)은 NULL 이 정상 (D2).
    ("eval.golden_set", "적용범위"): "공통 27문항에만 값이 있다. 사업 지정 50문항은 NULL 이 정상 (D2)",
    # 🔴 "의도된 NULL" 이 아니라 **골든셋 작성 시 안 채운 필드**다. 오늘 채우지 않는다 —
    #    계약 §10 이 골든셋 문항 수정을 금지한다(정답지를 건드리면 어제와 비교 불가).
    #    비목은 정답근거 조문에서 유도할 수 있지만 유도값을 정답지에 적으면 그건 더는 정답지가 아니다.
    #    영향: eval_e2e 의 비목별 분해를 못 낸다. 사업별·세트별 분해는 된다.
    ("eval.golden_set", "비목"): "🔴 골든셋 미기입 (의도된 NULL 아님). 정답지 수정 금지라 오늘 안 채운다 — 비목별 분해 불가",
    ("eval.golden_set", "입력필드"): "🔴 골든셋 미기입. F1~F5 입력 전제가 필요한 문항 표시용 — 위와 같은 건",

    # ── tenant.decisions — D 세션 2026-09-01 등록 ─────────────────────────
    # 🔴 등록 시점 실측: decisions **1행**, 그 1행이 판정='판단불가'
    #    (경로 `1정규화+3검색+4조립+6검증`). 표본 1이라 "전부 NULL" 이 결손인지
    #    정상인지 통계로 가릴 수 없다. 그래서 **구조적으로 옳은 것**과
    #    **표본 부족이라 판정 유보**를 나눠 적는다. 뭉뚱그려 통과시키면 안 된다.
    #
    # (가) 구조적으로 NULL 이 맞는 것 — 데이터가 쌓여도 계속 NULL 일 수 있다
    ("tenant.decisions", "실패단계"): "NULL = 정상 완주. 판단불가로 떨어진 지점에만 값이 붙는다 (D1-c 설계)",
    ("tenant.decisions", "plan_id"): "프론트 미연결. 지출계획에서 들어온 판정에만 붙는다",
    ("tenant.decisions", "org_id"): "골든셋 평가는 기관 미지정으로 돈다. L3 경로를 탈 때만 붙는다",
    ("tenant.decisions", "기관id"): "org_id 와 한 쌍. 위와 같은 건",
    #
    # 🔴 게스트 버킷. 로그인이 아직 없어서 `server/routes_plans.py::_org조건()` 이
    #    "org_id 가 안 오면 `org_id IS NULL` 행만" 으로 격리한다. 즉 이 NULL 은 결손이
    #    아니라 **격리에 쓰이는 값**이다 (ai-14 · 레인 A, 2026-09-01).
    #    ⚠️ 예외는 이 표 하나에만 건다. `tenant.decisions`·`tenant.l3_documents` 의
    #       org_id NULL 은 계속 물어야 한다 — 거기는 격리 키로 쓰이지 않는다.
    #    🔴 **지울 날:** 로그인(`tenant.accounts` 배선)이 닫히면 게스트 버킷이 사라진다.
    #       그때 이 줄을 지우고 NOT NULL 로 조인다. 그 전까지는 `POST /api/guest` 가
    #       발급한 `guest_<uuid4>` 가 붙은 행이 정상이고, NULL 은 **구 클라이언트**다.
    ("tenant.expense_plans", "org_id"): "게스트 버킷. _org조건() 이 이 값으로 격리한다 — "
                                        "로그인 배선이 닫히면 이 예외를 지운다",
    #
    # (나) 🔴 표본 1이라 판정 유보 — **실전 E2E 후 반드시 다시 볼 것**
    #     비목이 실전에서도 전부 NULL 이면 그건 B 의 비목확정()이 배선되지 않았다는 뜻이고
    #     신뢰등급이 전부 NULL 이면 A 의 등급 부여가 안 걸린다는 뜻이다. 둘 다 버그 신호다.
    ("tenant.decisions", "비목"): "🔴 표본 1(판단불가 1건). 판단불가는 비목 확정 전에 끝날 수 있다 — 실전 E2E 후 재확인",
    ("tenant.decisions", "신뢰등급"): "🔴 표본 1(판단불가 1건). 판단불가에는 A/B 등급을 안 매긴다 — 실전 E2E 후 재확인",
    ("tenant.decisions", "버전스탬프"): "🔴 표본 1(판단불가 1건). 조립까지 간 판정에만 붙는다 — 실전 E2E 후 재확인",
    ("corpus.documents", "version"): "공고·사례집 등 판본 표기가 없는 문서",
    ("corpus.documents", "doc_type"): "분류 불가 문서 (전자협약 매뉴얼 등)",
    ("corpus.evidence_sources", "해당비목_정본"): "TIPS·R&D 계통은 창업패키지 10종에 매핑하지 않는다",
    # check_items 39행은 전부 L1 통합관리지침 근거라 전 사업에 걸린다.
    #    ⚠️ 사업별로 갈리는 항목(모두의창업 국외여비 전면 불가 등)은 아직 없다 — 결손이지 정상이 아니다.
    #       사업별 룰이 6사업으로 늘었으니 그때 채운다. 미결로 등록해 둔다.
    ("corpus.check_items", "사업명"): "L1 통합관리지침 근거 항목 38건은 8사업 공통이라 NULL. "
                                     "근거가 L2 인 11건만 사업명이 있다 (2026-08-31 E4)",
    # 🔴 픽스처 탓이 아니라 L3 경로의 **영구 속성**이다. HWPX 는 XML 이고 쪽번호는 렌더
    #    시점에 생기는 레이아웃 산물이라 원문에 없다 — PDF 좌표에서 쪽을 뽑는 L1·L2 와
    #    근본이 다르다. 인용은 org_id+article_id+조번호로 특정되므로 판정에 영향이 없다.
    ("tenant.l3_articles", "페이지"): "HWPX 는 XML 이라 쪽 개념이 없다 (렌더 산물). "
                                     "L3 경로의 영구 NULL — 인용은 article_id+조번호로 특정된다",
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
    # 2026-09-01: 기대치 42 -> 0. `build_refs.py` 약칭 해소 뒤 dst 가 붙은 참조는
    # 전부 실재 문서를 가리킨다. 42 를 남겨두면 **개선이 🔴 문제로 보고된다.**
    # (미해소 자체는 dst_doc_id IS NULL 로 남지 그 수는 이 계약이 세는 값이 아니다)
    ("refs.dst 미해소 (경로/약칭)", """SELECT count(*) FROM corpus.refs r
        WHERE dst_doc_id IS NOT NULL AND NOT EXISTS
        (SELECT 1 FROM corpus.documents d WHERE d.doc_id=r.dst_doc_id)""", 0),
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
