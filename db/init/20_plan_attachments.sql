-- 지출계획 첨부파일. 파일 자체는 서버 디스크(compose 볼륨 plan_attachments)에 attachment_id 이름으로 둔다.

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
