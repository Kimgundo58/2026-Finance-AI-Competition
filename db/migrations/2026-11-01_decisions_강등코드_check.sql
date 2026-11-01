-- tenant.decisions 강등코드 CHECK 를 llm_validate.강등코드_전체(24개)와 맞춘다.
-- 실행 시점: 수동 마이그레이션(필수). 운영 DB 에 테이블 소유자(postgres)로 한 번 실행한다.
--   cd /opt/suddoe/deploy/ncp
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-01_decisions_강등코드_check.sql
-- 이유: 기존 CHECK 에 PREMISE_DB_MISSING, TASK_DB_MISSING 이 없어 이 코드가 붙은 판정은
--   decisions INSERT 가 CHECK 위반으로 실패하고 감사 기록이 남지 않는다.
-- NOT VALID 로 추가한 뒤 VALIDATE 한다. VALIDATE 는 기존 행을 읽기만 하고 쓰기를 막지 않는다.
-- 멱등: 다시 실행해도 같은 상태가 된다. db/init/04_agent.sql 과 같은 목록이다.

BEGIN;
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_강등코드_check";
ALTER TABLE tenant.decisions ADD CONSTRAINT "decisions_강등코드_check" CHECK ("강등코드" <@ ARRAY[
    'INVALID_JUDGMENT','CITE_NOT_IN_MAP','CITE_DB_MISSING','CITE_HANG_MISMATCH',
    'PREMISE_NO_BASIS','PREMISE_BASIS_NOT_IN_MAP','PREMISE_ENUM','PREMISE_UNMAPPED',
    'PREMISE_DB_MISSING',
    'NO_CITATION','VLM_DOWNGRADE','B_GRADE_DOWNGRADE','UNVERIFIED_RULE',
    'TASK_CODE_INVALID','TASK_DB_MISSING',
    'L3_ONLY_DOWNGRADE','TENANT_LEAK','DANGLING_WARN',
    'DOMAIN_WARN','PRECEDENCE_FLIP',
    'TASK_STATE_UNSOURCED','TASK_STATE_MISMATCH',
    'TASK_NUMBER_UNSOURCED','TASK_BASIS_NOT_IN_MAP'
]::text[]) NOT VALID;
COMMIT;

ALTER TABLE tenant.decisions VALIDATE CONSTRAINT "decisions_강등코드_check";
