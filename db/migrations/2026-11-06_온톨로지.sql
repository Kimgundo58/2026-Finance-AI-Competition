-- 필수. 룰 · 온톨로지 재설계 v2.1 의 런타임 테이블을 만든다(docs/5_룰/5-8_저장과_조회.md, 작업 P3-1).
--   1. corpus.concepts · concept_labels · concept_relations · norms · provenance 와 인덱스 3개
--   2. tenant.l3_articles.concept_ids (NULL 미확인 · '{}' 확인했고 해당 세목 없음)
--   3. tenant.decisions."경로" ('옛' | '온톨로지', 전환 계획 docs/5_룰/5-11_전환_계획.md)
--   4. decisions_검증코드_check 에 ONTOLOGY_BAN · LIMIT_EXCEEDED · VAT_BASIS_UNKNOWN 을 더한다
--   5. 권한: suddoe_app 읽기, suddoe_ingest 쓰기. 적재 때 없는 개념 ID 를 지우도록 suddoe_ingest 에 l3_articles 의
--      article_id · concept_ids 만 연다. suddoe_app 은 tenant 전체 쓰기 권한(10b_app_grants.sql)과 org_isolation 으로
--      concept_ids 를 쓸 수 있어 더 주지 않는다.
-- 코드 배포 전에 적용한다. 새 코드의 데이터 버전 해시(server/response_cache.py)가 새 테이블을 읽어, 테이블이 없으면 해시가
-- "unknown" 이 되어 데이터가 바뀌어도 캐시가 적중한다. 옛 코드는 영향이 없어 api 를 멈추지 않는다. 멱등이다.
-- 2026-11-01_corpus_ingest.sql 이 만든 롤 suddoe_ingest 가 있어야 한다.
-- 스키마 변경은 db/init/19_ontology.sql · db/init/04_validation_eval.sql 과 같다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-06_온톨로지.sql


-- 1. 온톨로지 테이블 ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS corpus.concepts (
    concept_id TEXT PRIMARY KEY,
    종류       TEXT NOT NULL,
    pref_label TEXT NOT NULL,
    정의       TEXT,
    적용사업   TEXT REFERENCES corpus.programs ("사업명"),
    검수단계   TEXT NOT NULL,
    원천해시   TEXT NOT NULL,
    CHECK (검수단계 IN ('AI초안','AI대조','AI검증','사람확정','재검토'))
);

CREATE TABLE IF NOT EXISTS corpus.concept_labels (
    concept_id TEXT NOT NULL REFERENCES corpus.concepts ON DELETE CASCADE,
    alt_label  TEXT NOT NULL,
    정규화     TEXT NOT NULL,            -- rule_lookup._norm 결과. 노드_결정() 이 정확 일치로만 찾는다
    적용사업   TEXT REFERENCES corpus.programs ("사업명"),
    UNIQUE NULLS NOT DISTINCT (정규화, 적용사업)
);

-- src_id · dst_id 는 개념 ID 이거나 `조:` 조문 ID 라 FK 를 두지 않는다. 적재 검사가 확인한다.
CREATE TABLE IF NOT EXISTS corpus.concept_relations (
    src_id   TEXT NOT NULL,
    관계     TEXT NOT NULL,
    dst_id   TEXT NOT NULL,
    적용사업 TEXT REFERENCES corpus.programs ("사업명"),
    연결종류 TEXT,                       -- 원문명시 · 해석. 측정의 놓친 유형 분류가 읽는다
    문맥     JSONB,                      -- 동치의 문맥조문 · 적용조문
    검수단계 TEXT NOT NULL,
    UNIQUE NULLS NOT DISTINCT (src_id, 관계, dst_id, 적용사업)
);

CREATE TABLE IF NOT EXISTS corpus.norms (
    norm_id  TEXT PRIMARY KEY,
    종류     TEXT NOT NULL,
    계층     TEXT NOT NULL,
    적용사업 TEXT REFERENCES corpus.programs ("사업명"),
    대상     TEXT[] NOT NULL,
    수범자   TEXT[],
    승인주체 TEXT[],
    조건     JSONB,
    수치     JSONB,
    결과     JSONB,
    예외     TEXT[],
    해석미결 BOOLEAN NOT NULL DEFAULT false,
    검수단계 TEXT NOT NULL,
    원천해시 TEXT NOT NULL,
    verified BOOLEAN GENERATED ALWAYS AS (검수단계 IN ('AI검증','사람확정')) STORED,
    CHECK (검수단계 IN ('AI초안','AI대조','AI검증','사람확정','재검토')),
    CHECK (종류 IN ('금지','허용','한도','임계값','의무','요건','우선순위'))
);

CREATE TABLE IF NOT EXISTS corpus.provenance (
    owner_id   TEXT NOT NULL,            -- 개념 ID · 규범 ID · 관계는 scripts/ontology_load.py 의 관계_소유자ID()
    owner_kind TEXT NOT NULL,            -- 개념 · 관계 · 규범
    칸         TEXT NOT NULL,            -- 규범은 수치 · 조건 · 대상, 개념 · 관계는 근거
    doc_id     TEXT NOT NULL REFERENCES corpus.documents,
    조번호     TEXT NOT NULL,
    항호       TEXT,
    발췌       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_concept_labels_norm ON corpus.concept_labels ("정규화");
CREATE INDEX IF NOT EXISTS ix_concept_relations_src ON corpus.concept_relations (src_id);
CREATE INDEX IF NOT EXISTS ix_norms_target ON corpus.norms USING GIN ("대상");


-- 2. L3 조문 ↔ 개념 연결 ────────────────────────────────────────────────────
ALTER TABLE tenant.l3_articles ADD COLUMN IF NOT EXISTS concept_ids TEXT[];


-- 3. 판정 기록의 경로 이름 ──────────────────────────────────────────────────
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "경로" TEXT;
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_경로_check";
ALTER TABLE tenant.decisions ADD CONSTRAINT "decisions_경로_check"
    CHECK ("경로" IN ('옛', '온톨로지'));
-- 온톨로지결과: 결과 화면 값(분류 · 근거경로 · 코드판정 · 조건 · 용어풀이). 계획 다시 열기(GET /api/plans/{id})가 읽는다. 옛 경로는 NULL.
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "온톨로지결과" JSONB;


-- 4. 검증코드. 기준 목록은 scripts/llm_validate.py 의 검증코드_전체 이고, 새 코드 3개는 P4-3 에서 그 목록에 더한다 ─
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_검증코드_check";
ALTER TABLE tenant.decisions
  ADD CONSTRAINT "decisions_검증코드_check"
  CHECK ("검증코드" <@ ARRAY[
    'INVALID_JUDGMENT','CITE_NOT_IN_MAP','CITE_DB_MISSING','CITE_PARAGRAPH_MISMATCH',
    'PREMISE_NO_BASIS','PREMISE_BASIS_NOT_IN_MAP','PREMISE_INVALID_OUTCOME','PREMISE_UNMAPPED',
    'PREMISE_DB_MISSING',
    'NO_CITATION','VLM_DOWNGRADE','B_GRADE_DOWNGRADE','UNVERIFIED_RULE',
    'TASK_CODE_INVALID','TASK_DB_MISSING',
    'L3_ONLY_DOWNGRADE','TENANT_LEAK','DANGLING_WARN',
    'DOMAIN_WARN','PRECEDENCE_OVERRIDE',
    'TASK_STATE_UNSOURCED','TASK_STATE_MISMATCH',
    'TASK_NUMBER_UNSOURCED','TASK_BASIS_NOT_IN_MAP',
    'ANSWER_UNCONFIRMED',
    'ONTOLOGY_BAN','LIMIT_EXCEEDED','VAT_BASIS_UNKNOWN'
  ]::text[]);


-- 5. 권한 ───────────────────────────────────────────────────────────────────
GRANT SELECT ON corpus.concepts, corpus.concept_labels, corpus.concept_relations, corpus.norms, corpus.provenance
    TO suddoe_app;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON corpus.concepts, corpus.concept_labels, corpus.concept_relations, corpus.norms, corpus.provenance
    TO suddoe_ingest;

-- 본문은 열지 않는다. l3_articles 는 RLS 라 정책 없이는 0행으로 보인다(judge_cache 의 ingest_cache_purge 와 같은 방식).
GRANT SELECT (article_id, concept_ids), UPDATE (concept_ids) ON tenant.l3_articles TO suddoe_ingest;
DROP POLICY IF EXISTS ingest_concept_ids_read ON tenant.l3_articles;
CREATE POLICY ingest_concept_ids_read ON tenant.l3_articles FOR SELECT TO suddoe_ingest USING (true);
DROP POLICY IF EXISTS ingest_concept_ids_cleanup ON tenant.l3_articles;
CREATE POLICY ingest_concept_ids_cleanup ON tenant.l3_articles FOR UPDATE TO suddoe_ingest
    USING (true) WITH CHECK (true);
