-- 프론트 요구서 반영 ALTER. tenant.* 만 건드린다 — corpus.* 변경은 04_agent.sql 로 분리돼 있다.


-- plan_tasks.유형 — 캘린더 일정 유형 배지. 판정 4-way, plan_tasks.상태 와 다른 축이다.
ALTER TABLE tenant.plan_tasks
    ADD COLUMN IF NOT EXISTS "유형" TEXT NOT NULL DEFAULT '기타';

DO $$
BEGIN
    ALTER TABLE tenant.plan_tasks
        ADD CONSTRAINT plan_tasks_유형_check
        CHECK ("유형" IN ('기타','계약','비교견적'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN tenant.plan_tasks."유형" IS
    '캘린더 배지용 표시 축. 판정 4-way 와도 plan_tasks.상태 와도 무관하다.';


-- expense_plans.추가설명 — 폼이 못 담는 예외 맥락을 적는 자유 텍스트 칸.
-- 정규화에서는 용도 에 합류시키지만 원문은 여기 따로 남긴다.
ALTER TABLE tenant.expense_plans
    ADD COLUMN IF NOT EXISTS "추가설명" TEXT;

COMMENT ON COLUMN tenant.expense_plans."추가설명" IS
    '폼의 자유 텍스트 원문. 정규화 용도에 합류하되 원문은 여기 남긴다.';


-- corpus.check_items.유형 — 02_frontend.sql 에 이미 정의돼 있지만, IF NOT EXISTS 라
-- 테이블이 이미 있는 환경에서는 db/init/*.sql 을 다시 돌려도 이 컬럼이 생기지 않는다. 여기서 멱등하게 맞춘다.
ALTER TABLE corpus.check_items
    ADD COLUMN IF NOT EXISTS "유형" TEXT NOT NULL DEFAULT '기타';

DO $$
BEGIN
    ALTER TABLE corpus.check_items
        ADD CONSTRAINT check_items_유형_check
        CHECK ("유형" IN ('기타','계약','비교견적'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN corpus.check_items."유형" IS
    '캘린더 배지용 표시 축. plan_tasks."유형" 이 여기를 따라간다 — 마스터가 늘어도
    조용히 안 틀리려고 코드 분류표 대신 여기서 관리한다.';

-- 백필 — ADD COLUMN DEFAULT '기타' 는 기존 행을 전부 '기타' 로 채운다. WHERE 절로 멱등하게 건다.
UPDATE corpus.check_items SET "유형" = '비교견적'
    WHERE code = '비교견적준비' AND "유형" = '기타';
UPDATE corpus.check_items SET "유형" = '계약'
    WHERE code = '전대차아님확인' AND "유형" = '기타';


-- tenant.l3_documents.파싱품질 CHECK — 01_schema.sql 의 인라인 정의를 여기서 DROP 후 재정의해 멱등하게 맞춘다.
DO $$
BEGIN
    ALTER TABLE tenant.l3_documents DROP CONSTRAINT IF EXISTS l3_documents_파싱품질_check;
    ALTER TABLE tenant.l3_documents
        ADD CONSTRAINT l3_documents_파싱품질_check
        CHECK ("파싱품질" IN ('대기','pass','warn','fail'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- 안 만든 것 (일부러):
-- expense_plans.질문원문 NOT NULL 은 풀지 않는다 — 폼 경로는 폼 값을 문장으로 합성해 채운다.
--   합성 문장을 다시 LLM 입력으로 쓰지 않는다 — 필드→문장→필드 왕복은 정보를 잃는다.
-- expense_plans.용도·하위항목 컬럼 승격은 보류 — 정규화 JSONB 안에 있고, 쓰임이 생기면 그때 승격한다.
-- accounts.팀명은 손대지 않았다. tenant.accounts 테이블은 이미 존재(0행)하고, 없는 것은 API 배선이다.
