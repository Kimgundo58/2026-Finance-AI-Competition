-- 필수. plan_tasks.집행예정일_연동 을 더하고, 예상 지출일이 있는 계획에 집행 줄을 채운다.
-- 지금은 서버가 집행 줄을 만들지 않아 계획의 지출일이 일정 탭에 보이지 않는다. 기존 행은 바꾸지 않고 행만 더한다.
-- 코드 배포 전에 적용한다(새 코드는 이 컬럼을 읽는다). 이전 코드는 이 컬럼을 읽지 않으므로 api 를 멈추지 않는다.
-- 실행: docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-06_집행줄.sql
BEGIN;
ALTER TABLE tenant.plan_tasks
    ADD COLUMN IF NOT EXISTS 집행예정일_연동 BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_집행줄 ON tenant.plan_tasks (plan_id) WHERE 집행예정일_연동;
COMMENT ON COLUMN tenant.plan_tasks.집행예정일_연동 IS
    '이 행의 due_date 는 계획의 집행예정일과 같다. 계획 수정으로만 바뀌고, 재점검이 지우지 않는다(출처 user).';
INSERT INTO tenant.plan_tasks (org_id, account_id, plan_id, 출처, 구분, 항목, due_date, 상태, 날짜_사용자수정, 집행예정일_연동)
SELECT p.org_id, p.account_id, p.plan_id, 'user', '집행', p.제목 || ' 집행', p."집행예정일", '집행예정', true, true
FROM tenant.expense_plans p
WHERE p."집행예정일" IS NOT NULL
ON CONFLICT (plan_id) WHERE 집행예정일_연동 DO NOTHING;
COMMIT;
