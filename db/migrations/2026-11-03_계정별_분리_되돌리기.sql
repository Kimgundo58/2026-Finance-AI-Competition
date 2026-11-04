-- 2026-11-03_계정별_분리.sql 을 되돌린다. 평소에는 실행하지 않는다.
--
-- 새 코드가 계정별 스키마에서 동작하지 않을 때 이전 코드로 돌아가기 위한 파일이다.
-- RLS 정책과 프로필 유일 키만 2026-11-03 이전 운영 정의로 되돌린다. account_id 컬럼·값과 current_account() 는
-- 남긴다(이전 코드가 무시한다). 그래서 같은 기관의 계정끼리 데이터를 다시 같이 본다.
--
-- 같은 기관의 두 계정이 같은 사업명으로 협약 정보를 저장했으면 이전 유일 키를 만들 수 없어 아무것도 바꾸지 않고 멈춘다.
-- 어느 행을 남길지는 사람이 정한다.
--
-- 순서(api 정지, 자동 배포 끄기, 이미지 되돌리기)는 db/migrations/README.md 의 이 항목을 따른다.

\set ON_ERROR_STOP on
SET lock_timeout = '10s';

BEGIN;

DO $$
DECLARE _n bigint;
BEGIN
    SELECT count(*) INTO _n FROM (
        SELECT 1 FROM tenant.profile_agreements GROUP BY org_id, "사업명" HAVING count(*) > 1
    ) d;
    IF _n <> 0 THEN
        RAISE EXCEPTION '같은 기관·사업명의 협약 정보가 계정별로 나뉜 조합이 %건 있어 UNIQUE (org_id, 사업명) 을 되돌릴 수 없다', _n;
    END IF;
END
$$;

ALTER TABLE tenant.profile_agreements DROP CONSTRAINT IF EXISTS uq_profile_agreements_key;
ALTER TABLE tenant.profile_agreements DROP CONSTRAINT IF EXISTS "profile_agreements_org_id_사업명_key";
ALTER TABLE tenant.profile_agreements
    ADD CONSTRAINT "profile_agreements_org_id_사업명_key" UNIQUE (org_id, "사업명");

DROP POLICY IF EXISTS account_isolation ON tenant.profile_agreements;
DROP POLICY IF EXISTS org_isolation     ON tenant.profile_agreements;
CREATE POLICY org_isolation ON tenant.profile_agreements USING (org_id = tenant.current_org());

DROP POLICY IF EXISTS account_isolation ON tenant.profile_expenditures;
DROP POLICY IF EXISTS org_isolation     ON tenant.profile_expenditures;
CREATE POLICY org_isolation ON tenant.profile_expenditures USING (
    profile_id IN (SELECT profile_id FROM tenant.profile_agreements WHERE org_id = tenant.current_org()));

DROP POLICY IF EXISTS account_isolation ON tenant.profile_personnel;
DROP POLICY IF EXISTS org_isolation     ON tenant.profile_personnel;
CREATE POLICY org_isolation ON tenant.profile_personnel USING (
    profile_id IN (SELECT profile_id FROM tenant.profile_agreements WHERE org_id = tenant.current_org()));

DROP POLICY IF EXISTS account_isolation ON tenant.expense_plans;
DROP POLICY IF EXISTS org_isolation     ON tenant.expense_plans;
CREATE POLICY org_isolation ON tenant.expense_plans USING (org_id = tenant.current_org());

DROP POLICY IF EXISTS account_isolation ON tenant.plan_tasks;
DROP POLICY IF EXISTS org_isolation     ON tenant.plan_tasks;
CREATE POLICY org_isolation ON tenant.plan_tasks USING (org_id = tenant.current_org());

DROP POLICY IF EXISTS account_isolation ON tenant.decisions;
DROP POLICY IF EXISTS org_isolation     ON tenant.decisions;
CREATE POLICY org_isolation ON tenant.decisions USING (org_id = tenant.current_org());

DROP POLICY IF EXISTS account_isolation ON tenant.judge_cache;
DROP POLICY IF EXISTS org_isolation     ON tenant.judge_cache;
CREATE POLICY org_isolation ON tenant.judge_cache USING (org_id = tenant.current_org());

COMMIT;
