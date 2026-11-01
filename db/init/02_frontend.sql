-- 프론트엔드 화면용 테이블(check_items, evidence_sources, expense_plans, plan_tasks)과 decisions·orgs 컬럼 추가.
-- 실행 시점: init (01_schema.sql 다음). corpus.chunks / doc_articles / refs 는 건드리지 않는다.


-- corpus.check_items : 결제 전/후 확인 항목의 허용 목록. code 는 재판정 사이에 진행 상황을 이어 주는 고정 식별자다.
CREATE TABLE IF NOT EXISTS corpus.check_items (
    code          TEXT PRIMARY KEY,           -- 계약범위확정 / 비교견적준비 / 특수관계확인 ...
    사업명        TEXT,                       -- NULL = 전 사업 공통
    비목          TEXT,                       -- NULL = 전 비목 공통. rules 와 같은 조회 키
    구분          TEXT NOT NULL CHECK (구분 IN ('결제전','결제후')),
    항목          TEXT NOT NULL,              -- 기본 문안. LLM 이 다듬되 code 는 유지한다
    설명          TEXT,
    기본_오프셋일 INT,                        -- 집행일 기준 며칠 전. 코드가 초기 due_date 를 계산
    -- 캘린더 배지 유형. 기본값이 '기타' 라 새 코드가 추가돼도 잘못된 유형으로 표시되지 않는다.
    유형          TEXT NOT NULL DEFAULT '기타' CHECK (유형 IN ('기타','계약','비교견적')),
    근거          JSONB,                      -- [{doc_id, 조번호}]
    verified      BOOLEAN NOT NULL DEFAULT false,
    검수자        TEXT,
    검수일        DATE
);
CREATE INDEX IF NOT EXISTS ix_check_items_key ON corpus.check_items (사업명, 비목, 구분);

COMMENT ON TABLE corpus.check_items IS
    '결제 전/후 확인 항목의 고정 목록. code 가 재판정 간 진행상황을 잇는 키다.';


-- corpus.evidence_sources : 증빙 발급처 안내. CSV 원본을 그대로 적재한다 (전처리 없음).
CREATE TABLE IF NOT EXISTS corpus.evidence_sources (
    증빙명    TEXT PRIMARY KEY,               -- "세금계산서(신용카드 영수증)", "견적서" ...
    해당비목  TEXT[],
    사업명    TEXT[],
    세부정보  TEXT,                           -- 화면 툴팁
    발급처    TEXT                            -- 홈택스 / IRIS / K-스타트업 / 특허로 ...
);

-- 비목 정규화 결과 컬럼. 원문 `해당비목` 은 덮지 않고 컬럼을 더한다.
-- 분류 값: 표준(표준 표기) | 표기차이 | 지급수수료_세목 | RND | 주관기관비목 | 비목아님 | 미분류
-- TIPS(RND)는 해당비목_표준 이 NULL 이다. 창업 비목으로 매핑하면 조인 결과가 경고 없이 0행이 된다.
ALTER TABLE corpus.evidence_sources ADD COLUMN IF NOT EXISTS 해당비목_표준 TEXT[];
ALTER TABLE corpus.evidence_sources ADD COLUMN IF NOT EXISTS 해당비목_분류 TEXT[];

COMMENT ON TABLE corpus.evidence_sources IS
    '증빙서류별 발급처·설명. 출처 CSV 124행. 판정에 쓰이지 않고 안내에만 쓴다.';


-- tenant.expense_plans : 지출 계획. 프론트 홈·목록·상세·새 계획 화면이 이 테이블에 묶인다.
-- 재판정은 decisions 에 행을 추가하고 latest_decision_id 만 바꾼다. 덮어쓰면 이력이 사라진다.
CREATE TABLE IF NOT EXISTS tenant.expense_plans (
    plan_id     BIGSERIAL PRIMARY KEY,
    org_id      UUID REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,  -- NULL = 게스트
    제목        TEXT,                         -- 목록의 "지출명". 정규화가 문장에서 뽑아 채운다
    질문원문    TEXT NOT NULL,                -- 목록 검색 대상
    정규화      JSONB,                        -- 정규화 단계 출력 전체
    -- 아래 4개는 정렬·필터에 쓰는 값만 JSONB 에서 컬럼으로 승격한 것이다
    확정비목    TEXT,
    금액        NUMERIC,
    집행예정일  DATE,
    거래처      TEXT,
    -- 진행 상태다. 4종 판정(가능/조건부/불가/판단불가)과 별개 값이다
    상태        TEXT NOT NULL DEFAULT 'draft' CHECK (상태 IN ('draft','judged')),
    latest_decision_id BIGINT REFERENCES tenant.decisions(decision_id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_plans_org_time ON tenant.expense_plans (org_id, updated_at DESC);

COMMENT ON TABLE tenant.expense_plans IS
    '지출 계획. 상태는 진행(draft/judged)이지 판정이 아니다.';


-- tenant.plan_tasks : 할일 = 체크리스트 + 캘린더. 같은 행을 필터만 다르게 거른다.
--   체크리스트 = WHERE plan_id = ? (전부) / 캘린더 = WHERE due_date IS NOT NULL (날짜 있는 것만)
-- org_id 를 plan_id 와 별도로 둔다. plan_id 가 NULL(사용자 직접 일정)이면 plan 을 거치는 RLS 로는 격리할 수 없다.
CREATE TABLE IF NOT EXISTS tenant.plan_tasks (
    task_id     BIGSERIAL PRIMARY KEY,
    org_id      UUID REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,  -- RLS 정책이 보는 컬럼
    plan_id     BIGINT REFERENCES tenant.expense_plans(plan_id) ON DELETE CASCADE,
                                              -- NULL = 계획과 무관한 사용자 일정
    decision_id BIGINT REFERENCES tenant.decisions(decision_id) ON DELETE SET NULL,
    -- 출처가 user 인 행은 재판정이 건드리지 않는다
    출처        TEXT NOT NULL CHECK (출처 IN ('ai','user')),
    코드        TEXT REFERENCES corpus.check_items(code),   -- 재판정 때 같은 항목인지 알아보는 키
    구분        TEXT NOT NULL CHECK (구분 IN ('결제전','결제후','집행')),
    항목        TEXT NOT NULL,
    설명        TEXT,
    due_date    DATE,                         -- NULL 이면 체크리스트에만, 값이 있으면 캘린더에도
    날짜_사용자수정 BOOLEAN NOT NULL DEFAULT false,  -- true 면 재판정이 날짜를 덮지 않는다
    -- 4종 판정과 별개 값. 코드가 관리한다
    상태        TEXT NOT NULL DEFAULT '준비필요'
                CHECK (상태 IN ('준비필요','집행예정','완료')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tasks_plan     ON tenant.plan_tasks (plan_id);
CREATE INDEX IF NOT EXISTS ix_tasks_calendar ON tenant.plan_tasks (org_id, due_date)
    WHERE due_date IS NOT NULL;               -- 캘린더 조회 전용 부분 인덱스

COMMENT ON TABLE tenant.plan_tasks IS
    '집행 준비 체크리스트와 캘린더가 공유하는 한 테이블. due_date 유무로 갈린다.';


-- 기존 테이블 ALTER 2건 (decisions, orgs)

-- 판정을 계획에 연결한다. 없으면 어느 계획의 판정인지 알 수 없다.
ALTER TABLE tenant.decisions
    ADD COLUMN IF NOT EXISTS plan_id BIGINT
        REFERENCES tenant.expense_plans(plan_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_decisions_plan ON tenant.decisions (plan_id);

-- 주소·부서: 기관 검색 결과 표시용.
ALTER TABLE tenant.orgs ADD COLUMN IF NOT EXISTS 주소 TEXT;
ALTER TABLE tenant.orgs ADD COLUMN IF NOT EXISTS 부서 TEXT;


-- decisions 확장 컬럼 5개.
-- 검증사유: 검증 단계가 판정을 하향한 이유(인용이 컨텍스트 밖 / verified=false 룰 단독 '가능' / extraction='vlm')를 남긴다.
-- 인용·전제 컬럼은 검증 단계 출력(객체 배열)이다. LLM 원출력(문장 ID 문자열 배열)이 아니다.
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 요약        TEXT;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 규정버전    TEXT;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 참조관계    JSONB;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 검증사유    TEXT[];
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 미매핑전제  JSONB;


-- RLS — 신설 tenant 테이블 2개. 01_schema.sql 의 org_isolation 관례를 따른다.
ALTER TABLE tenant.expense_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.plan_tasks    ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_isolation ON tenant.expense_plans;
DROP POLICY IF EXISTS org_isolation ON tenant.plan_tasks;
CREATE POLICY org_isolation ON tenant.expense_plans USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.plan_tasks    USING (org_id = tenant.current_org());


-- updated_at 자동 갱신 트리거. 앱이 아니라 DB 가 찍는다.
CREATE OR REPLACE FUNCTION tenant.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_touch ON tenant.expense_plans;
DROP TRIGGER IF EXISTS trg_touch ON tenant.plan_tasks;
CREATE TRIGGER trg_touch BEFORE UPDATE ON tenant.expense_plans
    FOR EACH ROW EXECUTE FUNCTION tenant.touch_updated_at();
CREATE TRIGGER trg_touch BEFORE UPDATE ON tenant.plan_tasks
    FOR EACH ROW EXECUTE FUNCTION tenant.touch_updated_at();


-- 권한 회수 (신설 테이블 포함)
REVOKE ALL ON ALL TABLES IN SCHEMA corpus, tenant FROM PUBLIC;
