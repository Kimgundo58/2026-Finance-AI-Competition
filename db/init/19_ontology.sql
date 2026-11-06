-- 룰 · 온톨로지 재설계 v2.1 의 런타임 테이블(docs/5_룰/5-8_저장과_조회.md). 새 로컬 DB 전용.
--
-- 운영 DB 는 db/migrations/2026-11-06_온톨로지.sql 로 적용한다. 내용은 같다.
-- 검증코드 CHECK 와 tenant.decisions."경로" 는 04_validation_eval.sql 에 있다.
-- 앞선 파일이 만든 corpus.programs(04), 롤 suddoe_app(10a) · suddoe_ingest(18) 가 있어야 한다.

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

-- NULL 미확인 · '{}' 확인했고 해당 세목 없음
ALTER TABLE tenant.l3_articles ADD COLUMN IF NOT EXISTS concept_ids TEXT[];

GRANT SELECT ON corpus.concepts, corpus.concept_labels, corpus.concept_relations, corpus.norms, corpus.provenance
    TO suddoe_app;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON corpus.concepts, corpus.concept_labels, corpus.concept_relations, corpus.norms, corpus.provenance
    TO suddoe_ingest;

-- 적재 때 없는 개념 ID 를 지운다. 본문은 열지 않는다. l3_articles 는 RLS 라 정책 없이는 0행으로 보인다.
GRANT SELECT (article_id, concept_ids), UPDATE (concept_ids) ON tenant.l3_articles TO suddoe_ingest;
DROP POLICY IF EXISTS ingest_concept_ids_read ON tenant.l3_articles;
CREATE POLICY ingest_concept_ids_read ON tenant.l3_articles FOR SELECT TO suddoe_ingest USING (true);
DROP POLICY IF EXISTS ingest_concept_ids_cleanup ON tenant.l3_articles;
CREATE POLICY ingest_concept_ids_cleanup ON tenant.l3_articles FOR UPDATE TO suddoe_ingest
    USING (true) WITH CHECK (true);
