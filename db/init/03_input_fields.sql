-- 판정 입력 필드 3건: expense_plans.사업명, f_personnel.역할 CHECK, golden_set.비목·입력필드.
-- 실행 시점: init. 전부 IF NOT EXISTS 라 다시 실행해도 같은 상태가 된다.


-- 1. tenant.expense_plans.사업명
--    룰 조회 키가 `사업 x 비목` 인데, 게스트(org_id IS NULL)는 orgs.사업명[]·f_profile.사업명 이 없어
--    사업을 저장할 곳이 없다. 판정 조회와 목록 필터가 매번 쓰므로 정규화 JSONB 가 아니라 컬럼으로 둔다.
--    CHECK 는 걸지 않는다. rules.사업명 · decisions.사업명 · golden_set.사업명 도 제약 없는 TEXT 다.
ALTER TABLE tenant.expense_plans ADD COLUMN IF NOT EXISTS 사업명 TEXT;

COMMENT ON COLUMN tenant.expense_plans.사업명 IS
    '룰 조회 키(사업 x 비목)의 절반. 게스트는 f_profile 이 없어 여기가 유일한 저장처다.';


-- 2. tenant.f_personnel.역할 — 허용 목록(CHECK)으로 제한한다.
--    제약 없는 TEXT 면 "대표"/"대표자"/"CEO" 가 전부 다른 값으로 들어가 인원수 집계가 깨진다.
--    tenant.f_exec.인력역할 은 제한하지 않는다. 역할만으로는 개인을 식별하지 못해 인건비 검증에 쓰지 않는다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'tenant.f_personnel'::regclass
          AND conname  = 'f_personnel_역할_check'
    ) THEN
        ALTER TABLE tenant.f_personnel
            ADD CONSTRAINT f_personnel_역할_check
            CHECK (역할 IN ('대표자', '신규채용', '기존직원'));
    END IF;
END
$$;

COMMENT ON COLUMN tenant.f_personnel.역할 IS
    '대표자 | 신규채용 | 기존직원. CHECK 로 닫혀 있다 — 인원수 집계가 한도의 분모라서.';


-- 3. eval.golden_set — 폼 경로 입력의 기준값. 문항마다 폼 값을 붙여 평가 입력과 운영 입력을 같은 형태로 측정한다.
--    비목은 비목분류 정확도 채점에 쓰므로 컬럼으로 두고, 나머지는 JSONB 로 둔다.
--    eval 스키마는 운영 DB 에 없고 앱이 읽지 않는다 (정답 유출 방지).
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS 비목     TEXT;
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS 입력필드 JSONB;

COMMENT ON COLUMN eval.golden_set.비목 IS
    '폼 경로 입력의 정본 + 비목분류 정확도 채점 축. 비목 어휘집 guided_json_enum 10종.';
COMMENT ON COLUMN eval.golden_set.입력필드 IS
    '폼 경로 재현용 입력값 {품목, 금액, 용도, 집행예정일, F5...}. NULL = 미작성.';


-- 적용 확인
DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM information_schema.columns
     WHERE (table_schema, table_name, column_name) IN
           (('tenant','expense_plans','사업명'),
            ('eval','golden_set','비목'),
            ('eval','golden_set','입력필드'));
    RAISE NOTICE '03_input_fields : 컬럼 %/3 · 역할 CHECK %',
        n,
        (SELECT count(*) FROM pg_constraint
          WHERE conrelid = 'tenant.f_personnel'::regclass
            AND conname  = 'f_personnel_역할_check');
END
$$;
