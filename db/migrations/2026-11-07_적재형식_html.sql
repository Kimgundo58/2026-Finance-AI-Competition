-- 필수. P0-6 · P0-7 문서를 적재하기 전에 실행한다. 절차: ontology/수집/P0-6_P0-7_기록.md
-- 법령 본문 HTML 을 원본으로 적재한다(scripts/corpus_restored_load.py). ingest_jobs.파일형식 에 html 을 더한다.
-- 멱등이다. 행은 바꾸지 않는다(이미 pdf 로 적힌 로컬 행은 그대로 둔다). api 를 멈추지 않는다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-07_적재형식_html.sql

ALTER TABLE corpus.ingest_jobs DROP CONSTRAINT IF EXISTS "ingest_jobs_파일형식_check";
ALTER TABLE corpus.ingest_jobs ADD CONSTRAINT "ingest_jobs_파일형식_check"
    CHECK ("파일형식" IN ('pdf','hwp','hwpx','docx','xml','html'));
