-- 프론트엔드 요구사항 반영: plan_tasks.유형, expense_plans.추가설명, check_items.유형, l3_documents.파싱품질 CHECK.
-- 실행 시점: init. 전부 멱등이다.


-- plan_tasks.유형 — 캘린더 일정 유형 배지. 판정(4종), plan_tasks.상태 와 다른 값이다.
ALTER TABLE tenant.plan_tasks
    ADD COLUMN IF NOT EXISTS "유형" TEXT NOT NULL DEFAULT '기타';

DO $$
BEGIN
    ALTER TABLE tenant.plan_tasks
        ADD CONSTRAINT plan_tasks_유형_check
        CHECK ("유형" IN ('기타','계약','비교견적','사전승인','사전확인','증빙'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN tenant.plan_tasks."유형" IS
    '캘린더 배지용 표시 값. 판정(4종)과도 plan_tasks.상태 와도 무관하다.';


-- expense_plans.추가설명 — 폼이 못 담는 예외 맥락을 적는 자유 텍스트 칸.
-- 정규화 단계에서는 용도에 합치지만 원문은 여기 따로 남긴다.
ALTER TABLE tenant.expense_plans
    ADD COLUMN IF NOT EXISTS "추가설명" TEXT;

COMMENT ON COLUMN tenant.expense_plans."추가설명" IS
    '폼의 자유 텍스트 원문. 정규화 용도에 합류하되 원문은 여기 남긴다.';


-- corpus.check_items.유형 — 02_frontend.sql 에도 정의돼 있지만 CREATE TABLE IF NOT EXISTS 라
-- 테이블이 이미 있는 환경에서는 그 정의가 적용되지 않는다. 여기서 멱등하게 맞춘다.
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
    '캘린더 배지용 표시 값. plan_tasks."유형" 이 이 값을 따른다 — 항목이 추가돼도 분류가 어긋나지 않도록 '
    '코드 분류표 대신 여기서 관리한다.';

-- 백필. ADD COLUMN DEFAULT '기타' 는 기존 행을 모두 '기타' 로 채운다. WHERE 절로 멱등하게 만든다.
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
        CHECK ("파싱품질" IN ('pending','pass','warn','fail'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN tenant.l3_documents."파싱품질" IS
    'pending = 업로드만 접수됐고 파싱 평가 전. pass/warn/fail 은 평가를 마친 뒤에만 쓴다 — '
    '접수 시점에 warn 을 넣으면 "평가 결과 의심" 과 "아직 평가 안 함" 이 같은 값이 되어 구분할 수 없다.';


-- 의도적으로 하지 않은 것:
-- expense_plans.질문원문 NOT NULL 은 유지한다. 폼 경로는 폼 값을 문장으로 합성해 채운다.
--   합성 문장을 다시 LLM 입력으로 쓰지 않는다. 필드→문장→필드 왕복은 정보를 잃는다.
-- expense_plans.용도·하위항목 은 정규화 JSONB 안에 두고 컬럼으로 따로 빼지 않는다.
