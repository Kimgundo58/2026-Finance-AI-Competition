-- 기업 데이터를 기관(org) 단위에서 계정(account) 단위로 나눈다. 필수.
--
-- 배경: org 는 주관기관(예: 경상국립대학교 창업중심대학사업단)이고 한 주관기관에 창업기업 계정이 여럿 있다.
--   지금까지 tenant 표는 org 로만 격리되어 같은 주관기관 계정끼리 협약 정보·지출계획·판정 이력을 같이 보고
--   덮어썼다. L3(주관기관 규정, l3_documents·l3_articles)는 그대로 org 단위로 같이 쓴다.
--
-- 1. tenant.current_account(): GUC app.account_id 를 읽는다(current_org 와 같은 형식, 없으면 NULL).
-- 2. account_id 컬럼: profile_agreements · expense_plans · plan_tasks · decisions · judge_cache.
--    profile_personnel · profile_expenditures 는 profile_id 로 profile_agreements 를 따라간다.
--    incidents 는 기관 간 유출 감사 기록이라 RLS 없이 org 단위로 둔다(바꾸지 않는다).
-- 3. 기존 행 배정: 계정이 있는 기관의 account_id NULL 행을 그 기관의 계정 하나에 붙인다.
--    계정이 하나면 그 계정, 여럿이면 created_at 이 가장 이른 계정(같으면 email 오름차순).
--    계정이 없는 기관(데모 세션 기관 등)의 행은 NULL 로 둔다. 기관별 배정 결과를 NOTICE 로 남긴다.
--    judge_cache 의 기존 행은 키에 계정이 없어 새 코드에서 적중하지 않으므로 지운다.
-- 4. profile_agreements 의 UNIQUE(org_id, 사업명) 를 UNIQUE NULLS NOT DISTINCT (org_id, account_id, 사업명) 로 바꾼다.
-- 5. 위 표의 RLS 정책 org_isolation 을 account_isolation 으로 바꾼다:
--      org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account()
--    데모 세션은 계정이 없어 account_id 가 NULL 이고, NULL 끼리 같은 것으로 본다.
-- 멱등이다. 다시 실행해도 같은 상태가 된다(3번은 account_id 가 NULL 인 행만 다시 배정한다).
--
-- 배포 순서: 이 정책이 적용되면 app.account_id 를 설정하지 않는 이전 코드는 계정이 있는 기관의 행을 하나도
--   읽지 못하고, 새로 쓰는 행은 account_id NULL 로 남는다. 그래서 api 를 멈춘 상태에서 적용하고 새 이미지로
--   다시 띄운다(절차는 db/migrations/README.md 의 이 파일 항목).
--
-- 실행(운영, 레포 루트 기준 경로는 db/migrations/README.md):
--   ./backup.sh
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
--     < ../../db/migrations/2026-11-03_계정별_분리.sql
--
-- 되돌리기: 2026-11-03_계정별_분리_되돌리기.sql (순서는 db/migrations/README.md). 3번 배정 결과는 NOTICE 로 남는다.

\set ON_ERROR_STOP on
SET lock_timeout = '10s';

BEGIN;

-- 1. current_account()
CREATE OR REPLACE FUNCTION tenant.current_account() RETURNS uuid
LANGUAGE plpgsql STABLE AS $fn$
DECLARE v text;
BEGIN
    BEGIN
        v := current_setting('request.jwt.claims', true)::json ->> 'account_id';
    EXCEPTION WHEN others THEN
        v := NULL;
    END;
    IF v IS NULL OR v = '' THEN
        v := nullif(current_setting('app.account_id', true), '');
    END IF;
    RETURN v::uuid;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$fn$;

-- 2. account_id 컬럼. 계정을 지우면 그 계정의 데이터도 지운다. decisions 는 감사 기록이라 연결만 끊는다.
ALTER TABLE tenant.profile_agreements ADD COLUMN IF NOT EXISTS account_id UUID
    REFERENCES tenant.accounts(account_id) ON DELETE CASCADE;
ALTER TABLE tenant.expense_plans ADD COLUMN IF NOT EXISTS account_id UUID
    REFERENCES tenant.accounts(account_id) ON DELETE CASCADE;
ALTER TABLE tenant.plan_tasks ADD COLUMN IF NOT EXISTS account_id UUID
    REFERENCES tenant.accounts(account_id) ON DELETE CASCADE;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS account_id UUID
    REFERENCES tenant.accounts(account_id) ON DELETE SET NULL;
ALTER TABLE tenant.judge_cache ADD COLUMN IF NOT EXISTS account_id UUID
    REFERENCES tenant.accounts(account_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS ix_plans_account_time
    ON tenant.expense_plans (org_id, account_id, updated_at DESC);

-- 3. 기존 행 배정. 계정을 붙이는 것은 사용자의 수정이 아니므로 updated_at(계획 목록 정렬 기준)을 그대로 둔다.
ALTER TABLE tenant.expense_plans DISABLE TRIGGER trg_touch;
ALTER TABLE tenant.plan_tasks    DISABLE TRIGGER trg_touch;

DO $$
DECLARE
    _r record;
    _계획 int; _할일 int; _판정 int; _프로필 int; _캐시 int;
BEGIN
    FOR _r IN
        SELECT DISTINCT ON (a.org_id) a.org_id, a.account_id, a.email,
               (SELECT count(*) FROM tenant.accounts b WHERE b.org_id = a.org_id) AS 계정수
          FROM tenant.accounts a
         ORDER BY a.org_id, a.created_at, a.email
    LOOP
        UPDATE tenant.expense_plans SET account_id = _r.account_id
         WHERE org_id = _r.org_id AND account_id IS NULL;
        GET DIAGNOSTICS _계획 = ROW_COUNT;
        UPDATE tenant.plan_tasks SET account_id = _r.account_id
         WHERE org_id = _r.org_id AND account_id IS NULL;
        GET DIAGNOSTICS _할일 = ROW_COUNT;
        UPDATE tenant.decisions SET account_id = _r.account_id
         WHERE org_id = _r.org_id AND account_id IS NULL;
        GET DIAGNOSTICS _판정 = ROW_COUNT;
        UPDATE tenant.profile_agreements SET account_id = _r.account_id
         WHERE org_id = _r.org_id AND account_id IS NULL;
        GET DIAGNOSTICS _프로필 = ROW_COUNT;
        DELETE FROM tenant.judge_cache WHERE org_id = _r.org_id AND account_id IS NULL;
        GET DIAGNOSTICS _캐시 = ROW_COUNT;
        RAISE NOTICE '계정 배정: org=% → % (%, 기관 계정 %개) · 계획 % · 할일 % · 판정 % · 프로필 % · 캐시 삭제 %',
            _r.org_id, _r.email, _r.account_id, _r.계정수, _계획, _할일, _판정, _프로필, _캐시;
    END LOOP;
END
$$;

ALTER TABLE tenant.expense_plans ENABLE TRIGGER trg_touch;
ALTER TABLE tenant.plan_tasks    ENABLE TRIGGER trg_touch;

-- 4. 프로필 유일 키. 계정 없는 기관(데모)은 account_id 가 NULL 이라 NULLS NOT DISTINCT 로 한 행에 모은다.
ALTER TABLE tenant.profile_agreements DROP CONSTRAINT IF EXISTS "profile_agreements_org_id_사업명_key";
ALTER TABLE tenant.profile_agreements DROP CONSTRAINT IF EXISTS uq_profile_agreements_key;
ALTER TABLE tenant.profile_agreements
    ADD CONSTRAINT uq_profile_agreements_key UNIQUE NULLS NOT DISTINCT (org_id, account_id, "사업명");

-- 5. RLS 정책
DROP POLICY IF EXISTS org_isolation     ON tenant.profile_agreements;
DROP POLICY IF EXISTS account_isolation ON tenant.profile_agreements;
CREATE POLICY account_isolation ON tenant.profile_agreements USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());

DROP POLICY IF EXISTS org_isolation     ON tenant.profile_expenditures;
DROP POLICY IF EXISTS account_isolation ON tenant.profile_expenditures;
CREATE POLICY account_isolation ON tenant.profile_expenditures USING (
    profile_id IN (SELECT profile_id FROM tenant.profile_agreements
                    WHERE org_id = tenant.current_org()
                      AND account_id IS NOT DISTINCT FROM tenant.current_account()));

DROP POLICY IF EXISTS org_isolation     ON tenant.profile_personnel;
DROP POLICY IF EXISTS account_isolation ON tenant.profile_personnel;
CREATE POLICY account_isolation ON tenant.profile_personnel USING (
    profile_id IN (SELECT profile_id FROM tenant.profile_agreements
                    WHERE org_id = tenant.current_org()
                      AND account_id IS NOT DISTINCT FROM tenant.current_account()));

DROP POLICY IF EXISTS org_isolation     ON tenant.expense_plans;
DROP POLICY IF EXISTS account_isolation ON tenant.expense_plans;
CREATE POLICY account_isolation ON tenant.expense_plans USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());

DROP POLICY IF EXISTS org_isolation     ON tenant.plan_tasks;
DROP POLICY IF EXISTS account_isolation ON tenant.plan_tasks;
CREATE POLICY account_isolation ON tenant.plan_tasks USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());

DROP POLICY IF EXISTS org_isolation     ON tenant.decisions;
DROP POLICY IF EXISTS account_isolation ON tenant.decisions;
CREATE POLICY account_isolation ON tenant.decisions USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());

DROP POLICY IF EXISTS org_isolation     ON tenant.judge_cache;
DROP POLICY IF EXISTS account_isolation ON tenant.judge_cache;
CREATE POLICY account_isolation ON tenant.judge_cache USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());

-- 확인: 계정이 있는 기관에 account_id 가 NULL 인 행이 남으면 그 행은 어느 계정에서도 보이지 않는다.
DO $$
DECLARE _n bigint;
BEGIN
    SELECT (SELECT count(*) FROM tenant.expense_plans t WHERE t.account_id IS NULL
               AND EXISTS (SELECT 1 FROM tenant.accounts a WHERE a.org_id = t.org_id))
         + (SELECT count(*) FROM tenant.plan_tasks t WHERE t.account_id IS NULL
               AND EXISTS (SELECT 1 FROM tenant.accounts a WHERE a.org_id = t.org_id))
         + (SELECT count(*) FROM tenant.decisions t WHERE t.account_id IS NULL
               AND EXISTS (SELECT 1 FROM tenant.accounts a WHERE a.org_id = t.org_id))
         + (SELECT count(*) FROM tenant.profile_agreements t WHERE t.account_id IS NULL
               AND EXISTS (SELECT 1 FROM tenant.accounts a WHERE a.org_id = t.org_id))
      INTO _n;
    IF _n <> 0 THEN
        RAISE EXCEPTION '계정이 있는 기관에 account_id 가 NULL 인 행이 %건 남았다', _n;
    END IF;
END
$$;

COMMIT;
