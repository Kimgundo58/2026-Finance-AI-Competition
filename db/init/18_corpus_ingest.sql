-- 규정 적재 API(server/routes_ingest.py, scripts/corpus_ingest.py)용 스키마·권한. 새 로컬 DB 전용.
--
-- 운영 DB 는 db/migrations/2026-11-01_corpus_ingest.sql 로 적용한다. 내용은 같고 비밀번호만 다르다.
-- 여기서는 로컬 전용 비밀번호 'devpw' 를 쓴다(10a_rls_guc.sql 의 suddoe_app 과 같은 관례).
-- 로컬 접속: SUDDOE_INGEST_DSN=postgresql://suddoe_ingest:devpw@localhost:5432/suddoe
-- init 스크립트는 빈 볼륨에서만 돈다. 이미 띄운 로컬 DB 에는 마이그레이션 파일을 -v ingest_password=devpw 로 실행한다.
-- 앞선 파일이 만든 corpus.programs(04), tenant.judge_cache(13) 가 있어야 한다.

-- 1. 상태 값. staged 는 적재 직후 상태이고 검색 필터(status='active')에 걸리지 않는다.
ALTER TABLE corpus.documents DROP CONSTRAINT IF EXISTS documents_status_check;
ALTER TABLE corpus.documents ADD CONSTRAINT documents_status_check
    CHECK (status IN ('active','superseded','reference','staged'));

-- 2. 업로드 기록. documents 행은 파싱이 성공한 뒤에 생기므로 doc_id 는 documents 를 참조하지 않는다.
CREATE TABLE IF NOT EXISTS corpus.ingest_jobs (
    doc_id        TEXT PRIMARY KEY,                 -- 문서명_YYYYMMDD(시행일)
    layer         TEXT NOT NULL CHECK (layer IN ('L1','L2')),
    발행기관      TEXT NOT NULL,
    문서명        TEXT NOT NULL,
    문서키        TEXT NOT NULL,                    -- 이전 판 자동 매칭 키 (괄호·공백·구두점 제거)
    사업명        TEXT REFERENCES corpus.programs ("사업명"),
    시행일        DATE NOT NULL,
    version       TEXT NOT NULL,
    적용대상      TEXT NOT NULL DEFAULT '공통' CHECK (적용대상 IN ('주관기관','창업기업','공통')),
    원본파일명    TEXT NOT NULL,
    파일형식      TEXT NOT NULL CHECK (파일형식 IN ('pdf','hwp','hwpx','docx','xml')),
    저장경로      TEXT,                             -- 파싱이 끝나면 NULL (원본은 지운다)
    상태          TEXT NOT NULL DEFAULT 'parsing' CHECK (상태 IN ('parsing','done','failed')),
    실패사유      TEXT,
    경고          JSONB NOT NULL DEFAULT '[]'::jsonb,
    분해전략      TEXT,
    조_건수       INT,
    대체한_doc_id TEXT,                             -- 활성화 때 superseded 로 바꾼 이전 판
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ingest_jobs_사업명_check CHECK ((layer = 'L2') = (사업명 IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_ingest_jobs_key ON corpus.ingest_jobs (layer, 문서키);
COMMENT ON TABLE corpus.ingest_jobs IS
  '규정 적재 API 업로드 기록. 상태 parsing/failed 는 여기에만 있고, done 이면 documents.status 가 기준이다.';

-- 3. 적재 롤. 비특권이고 corpus 쓰기는 파이프라인이 쓰는 테이블로만 한정한다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_ingest') THEN
        CREATE ROLE suddoe_ingest;
    END IF;
END
$$;
ALTER ROLE suddoe_ingest WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD 'devpw';
ALTER ROLE suddoe_ingest SET search_path = corpus, tenant, extensions, public;

GRANT USAGE ON SCHEMA corpus, extensions, tenant TO suddoe_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA corpus TO suddoe_ingest;
GRANT INSERT, UPDATE, DELETE ON
    corpus.documents, corpus.doc_articles, corpus.chunks, corpus.chunk_terms, corpus.chunk_len,
    corpus.refs, corpus.ingest_jobs
    TO suddoe_ingest;
GRANT USAGE ON SEQUENCE
    corpus.doc_articles_article_id_seq, corpus.chunks_chunk_id_seq, corpus.refs_ref_id_seq
    TO suddoe_ingest;
GRANT MAINTAIN ON corpus.term_df TO suddoe_ingest;     -- REFRESH MATERIALIZED VIEW CONCURRENTLY (PG17 이상)

-- REFRESH MATERIALIZED VIEW CONCURRENTLY 는 고유 인덱스가 있어야 한다. 활성화 중에도 검색이 term_df 를 읽을 수 있다.
CREATE UNIQUE INDEX IF NOT EXISTS ux_term_df_term ON corpus.term_df (term);

-- 활성화 때 판정 캐시를 비운다. judge_cache 는 RLS(org_isolation) 라 정책 없이는 0행만 지워진다.
GRANT DELETE ON tenant.judge_cache TO suddoe_ingest;
DROP POLICY IF EXISTS ingest_cache_purge ON tenant.judge_cache;
CREATE POLICY ingest_cache_purge ON tenant.judge_cache FOR DELETE TO suddoe_ingest USING (true);
