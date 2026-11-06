-- 필수. 올린 문서가 세부기준이 맞는지 확인을 기다리는 상태(파싱품질 'review')와 판정이 쓴 기준 문서 기록을 더한다.
-- 코드 배포 전에 적용한다. 이전 코드는 새 값·컬럼을 읽지 않으므로 api 를 멈추지 않는다.
-- 'review' 문서는 l3_load 가 읽지 않아 판정에 들어가지 않는다.
-- 실행: docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-06_L3_문서확인.sql
BEGIN;
ALTER TABLE tenant.l3_documents DROP CONSTRAINT IF EXISTS "l3_documents_파싱품질_check";
ALTER TABLE tenant.l3_documents ADD CONSTRAINT "l3_documents_파싱품질_check"
    CHECK ("파싱품질" IN ('pending', 'pass', 'warn', 'fail', 'review'));
ALTER TABLE tenant.l3_documents
    ADD COLUMN IF NOT EXISTS "검토결과" JSONB,
    ADD COLUMN IF NOT EXISTS sha256 TEXT;
CREATE INDEX IF NOT EXISTS ix_l3_documents_sha ON tenant.l3_documents (org_id, sha256);
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "기준문서" JSONB;
COMMIT;
