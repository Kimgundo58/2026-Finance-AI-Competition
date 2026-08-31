-- ════════════════════════════════════════════════════════════════
-- 03_input_fields.sql — 입력 필드 명세 반영 (2026-08-31)
--
--   01_schema.sql · 02_frontend.sql 을 건드리지 않고 파일명 순서로 이어진다.
--   전부 IF NOT EXISTS / 존재 검사라 여러 번 돌려도 안전하다.
--
--   🔴 db/init/ 은 컨테이너를 "처음" 만들 때만 실행된다.
--      살아있는 DB 에는 psql 로 직접 적용했다.
--      `docker compose down -v` 로 반영하려 들면 20,525청크가 날아간다.
--
--   근거: 프론트 입력 필드 전수 대조 (2026-08-31). 여기 담은 것은
--         "안 넣으면 판정 배선이 막히는" 3건뿐이고, 값 채우기는 별건이다.
-- ════════════════════════════════════════════════════════════════


-- ════════════════════════════════════════════════════════════════
-- 1. tenant.expense_plans.사업명
--    🔴 룰 조회 키가 `사업 x 비목` 인데 지출 계획에 사업을 담을 자리가 없었다.
--       orgs.사업명[] · f_profile.사업명 은 둘 다 로그인 사용자 것이라
--       게스트(org_id IS NULL)는 사업을 저장할 데가 **아예 없다**.
--       오케스트레이터가 붙는 순간 게스트 경로가 통째로 막힌다.
--
--    정규화 JSONB 안이 아니라 컬럼으로 둔다 — 룰 조회가 매 판정마다 꺼내 쓰고
--    목록 화면이 이 값으로 거른다. JSONB 안에 두면 둘 다 매번 파싱해야 한다.
--
--    ⚠️ CHECK 는 걸지 않는다. rules.사업명 · decisions.사업명 · golden_set.사업명
--       이 전부 제약 없는 TEXT 라 여기만 닫으면 적재 경로가 비대칭이 된다.
--       사업명 8종 강제는 어휘집처럼 한 번에 정리할 일이다.
-- ════════════════════════════════════════════════════════════════
ALTER TABLE tenant.expense_plans ADD COLUMN IF NOT EXISTS 사업명 TEXT;

COMMENT ON COLUMN tenant.expense_plans.사업명 IS
    '룰 조회 키(사업 x 비목)의 절반. 게스트는 f_profile 이 없어 여기가 유일한 저장처다.';


-- ════════════════════════════════════════════════════════════════
-- 2. tenant.f_personnel.역할 — 폐쇄 목록으로 닫는다
--    지금까지 제약 없는 TEXT 였다. 주석에 `대표자 | 신규채용 | 기존직원 ...` 라고만
--    적혀 있어서 "대표" / "대표자" / "CEO" 가 전부 다른 값으로 들어갈 수 있었고,
--    그러면 인원수 집계가 조용히 깨진다 — 그 인원수가 "PC 1인 1대" 한도의 분모다.
--
--    0행일 때 거는 것이 공짜다. 값이 쌓인 뒤에는 정제 작업이 된다.
--    넓혀야 하면 DROP CONSTRAINT 후 재생성 한 줄이다.
--
--    ⚠️ tenant.f_exec.인력역할 은 **일부러 열어 둔다.** 역할만으로는 개인을 식별하지
--       못해(신규채용 3명이면 세 행이 같다) 인건비 검증에 쓰이지 못하는 컬럼이고,
--       프론트에도 입력칸을 만들지 않기로 했다. F3·F4 화면을 실제로 만들 때
--       `person_id` 로 교체할지와 함께 정한다.
-- ════════════════════════════════════════════════════════════════
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


-- ════════════════════════════════════════════════════════════════
-- 3. eval.golden_set — 폼 경로 입력의 정본
--    77문항이 전부 자연어 질문이다. 프론트가 폼 입력으로 가면 평가 입력과
--    프로덕션 입력의 형태가 달라져 "골든셋 점수 = 서비스 품질" 등식이 깨진다.
--    문항마다 폼 값의 정본을 붙여 두면 두 경로를 같은 문항으로 잴 수 있다.
--
--    비목 만 컬럼으로 뺀다 — 채점 축이 하나 늘기 때문이다
--    (판정일치율 · 인용정확도 옆에 **비목분류 정확도**). 나머지는 JSONB.
--
--    🔴 이 테이블은 eval 스키마다. Supabase 덤프 대상이 아니고 앱이 런타임에
--       읽지 않는다 (정답 유출 방어). 컬럼이 늘어도 그 원칙은 그대로다.
-- ════════════════════════════════════════════════════════════════
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS 비목     TEXT;
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS 입력필드 JSONB;

COMMENT ON COLUMN eval.golden_set.비목 IS
    '폼 경로 입력의 정본 + 비목분류 정확도 채점 축. 비목 어휘집 guided_json_enum 10종.';
COMMENT ON COLUMN eval.golden_set.입력필드 IS
    '폼 경로 재현용 입력값 {품목, 금액, 용도, 집행예정일, F5...}. NULL = 미작성.';


-- ════════════════════════════════════════════════════════════════
-- 적용 확인
-- ════════════════════════════════════════════════════════════════
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
