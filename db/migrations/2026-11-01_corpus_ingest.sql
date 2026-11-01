-- 규정 적재 API(server/routes_ingest.py, scripts/corpus_ingest.py)용 스키마·권한 마이그레이션. 운영 DB 용.
--
-- 하는 일
--   1. corpus.documents.status CHECK 에 'staged' 를 넣는다. 적재 직후 상태이고 검색 필터(status='active')에
--      걸리지 않는다. 다른 상태 값은 늘리지 않는다(parsing·failed 는 corpus.ingest_jobs 에만 있다).
--   2. corpus.ingest_jobs 를 만든다. 업로드 메타데이터와 파싱 상태(parsing/done/failed), 경고, 실패 사유.
--   3. 롤 suddoe_ingest 를 만든다. 비특권 LOGIN 롤이고 비밀번호는 psql 변수로만 받는다.
--      corpus 전체 SELECT, 파이프라인이 쓰는 테이블만 INSERT/UPDATE/DELETE, 시퀀스 USAGE,
--      corpus.term_df REFRESH(PG17 MAINTAIN), tenant.judge_cache 전체 삭제(정책 ingest_cache_purge).
--   suddoe_app 권한은 바꾸지 않는다. 앱은 corpus 에 계속 SELECT 만 한다.
--
-- 언제: 규정 적재 API 가 들어간 이미지를 배포하기 전에 한 번. 다시 실행해도 결과가 같다(비밀번호는 새 값으로 바뀐다).
--
-- 실행 (NCP VM 의 deploy/ncp 디렉터리, postgres 롤). 비밀번호는 DSN URL 에 그대로 들어가므로 hex 로 만든다.
--   PW=$(openssl rand -hex 24)
--   docker compose exec -T -e PGCLIENTENCODING=UTF8 db psql -U postgres -d suddoe -X -v ON_ERROR_STOP=1 \
--       -v ingest_password="$PW" -f - < ../../db/migrations/2026-11-01_corpus_ingest.sql
--   그다음 deploy/ncp/.env 에 SUDDOE_INGEST_DB_PASSWORD=$PW 를 넣고, api 서비스 environment 에
--   SUDDOE_INGEST_DSN: postgresql://suddoe_ingest:${SUDDOE_INGEST_DB_PASSWORD}@db:5432/suddoe 를 추가한 뒤
--   docker compose up -d api 로 다시 띄운다. 자세한 절차는 docs/8_운영/8-7_규정_적재_API.md.
--
-- 확인 (postgres 로):
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'documents_status_check';
--   SELECT rolname, rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'suddoe_ingest';
--   SELECT has_table_privilege('suddoe_ingest', 'corpus.chunks', 'INSERT'),
--          has_table_privilege('suddoe_ingest', 'corpus.rules', 'INSERT'),        -- false 여야 한다
--          has_table_privilege('suddoe_ingest', 'corpus.term_df', 'MAINTAIN'),
--          has_schema_privilege('suddoe_ingest', 'extensions', 'USAGE');
--
-- 되돌리기 (postgres 로, staged 문서와 적재 기록을 지운다. active·superseded 로 바뀐 문서는 그대로 둔다):
--   BEGIN;
--   DELETE FROM corpus.refs WHERE src_doc_id IN (SELECT doc_id FROM corpus.documents WHERE status = 'staged');
--   DELETE FROM corpus.documents WHERE status = 'staged';
--   DROP POLICY IF EXISTS ingest_cache_purge ON tenant.judge_cache;
--   DROP OWNED BY suddoe_ingest;
--   DROP ROLE IF EXISTS suddoe_ingest;
--   DROP TABLE IF EXISTS corpus.ingest_jobs;
--   ALTER TABLE corpus.documents DROP CONSTRAINT IF EXISTS documents_status_check;
--   ALTER TABLE corpus.documents ADD CONSTRAINT documents_status_check
--       CHECK (status IN ('active','superseded','reference'));
--   COMMIT;

\if :{?ingest_password}
\else
    \warn 'ingest_password 변수가 없습니다. psql -v ingest_password=... 로 실행하세요. 아무것도 바꾸지 않았습니다.'
    \quit
\endif

BEGIN;
SET LOCAL lock_timeout = '10s';     -- 판정 트래픽이 documents 를 읽는 중이면 기다리다 포기한다

-- 1. 상태 값
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

-- 3. 적재 롤
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_ingest') THEN
        CREATE ROLE suddoe_ingest;
    END IF;
END
$$;
ALTER ROLE suddoe_ingest WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'ingest_password';
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
GRANT MAINTAIN ON corpus.term_df TO suddoe_ingest;     -- REFRESH MATERIALIZED VIEW (PG17 이상)

-- 활성화 때 판정 캐시를 비운다. judge_cache 는 RLS(org_isolation) 라 정책 없이는 0행만 지워진다.
-- DELETE 전용 정책이고 SELECT 권한은 주지 않는다. 앱이 WHERE 없이 지우므로 다른 기관 행을 읽을 일이 없다.
GRANT DELETE ON tenant.judge_cache TO suddoe_ingest;
DROP POLICY IF EXISTS ingest_cache_purge ON tenant.judge_cache;
CREATE POLICY ingest_cache_purge ON tenant.judge_cache FOR DELETE TO suddoe_ingest USING (true);

COMMIT;
