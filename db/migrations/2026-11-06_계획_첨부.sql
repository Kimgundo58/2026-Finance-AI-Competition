-- 필수. 지출계획 첨부파일 표를 더한다. deploy/ncp 의 볼륨(plan_attachments)과 같이 적용한다.
-- 코드 배포 전에 적용한다(새 코드의 계획 상세가 이 표를 읽는다). 이전 코드는 읽지 않으므로 api 를 멈추지 않는다.
-- 실행: docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-06_계획_첨부.sql
BEGIN;
CREATE TABLE IF NOT EXISTS tenant.plan_attachments (
    attachment_id    UUID PRIMARY KEY,
    org_id           UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    -- 계획의 소유 계정(RLS 기준). 데모 세션은 NULL 이다
    account_id       UUID REFERENCES tenant.accounts(account_id) ON DELETE CASCADE,
    -- 파일을 올린 계정
    올린_account_id  UUID REFERENCES tenant.accounts(account_id) ON DELETE SET NULL,
    plan_id          BIGINT NOT NULL REFERENCES tenant.expense_plans(plan_id) ON DELETE CASCADE,
    -- 증빙 항목에 올린 파일이면 그 항목. 재점검으로 항목이 사라지면 일반 첨부로 남는다
    task_id          BIGINT REFERENCES tenant.plan_tasks(task_id) ON DELETE SET NULL,
    원본파일명       TEXT NOT NULL,
    확장자           TEXT NOT NULL,
    크기             INTEGER NOT NULL CHECK (크기 > 0),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_plan_attachments_plan ON tenant.plan_attachments (plan_id, created_at);

ALTER TABLE tenant.plan_attachments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_isolation ON tenant.plan_attachments;
CREATE POLICY account_isolation ON tenant.plan_attachments USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant.plan_attachments TO suddoe_app;
COMMIT;
