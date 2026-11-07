-- 필수. expense_plans 에 실제지출일·실제지출액을 더한다(화면의 「실제 지출 등록」, 판정에는 쓰지 않는다).
-- 코드 배포 전에 적용한다(새 코드는 이 컬럼을 읽는다). 이전 코드는 이 컬럼을 읽지 않으므로 api 를 멈추지 않는다.
-- 실행: docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-06_실제지출.sql
BEGIN;
ALTER TABLE tenant.expense_plans
    ADD COLUMN IF NOT EXISTS "실제지출일" DATE,
    ADD COLUMN IF NOT EXISTS "실제지출액" NUMERIC;
COMMIT;
