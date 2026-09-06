-- ════════════════════════════════════════════════════════════════════════════
-- 02_frontend.sql — 프론트 프로토타입을 붙이기 위한 신설 4 + 기존 수정 2
--   작성 2026-08-31.  근거: 아티팩트 "써도돼요 스키마 지도" · 프론트 연동 사양.md
--
--   🔴 corpus.chunks / doc_articles / refs 를 건드리지 않는다.
--      재파싱·재임베딩·BM25 재색인 없음. 신설은 전부 새 테이블이고,
--      ALTER 대상 2개(decisions·orgs)는 0행이라 ADD COLUMN 이 즉시 끝난다.
--
--   01_schema.sql 과 함께 이 디렉터리가 스키마 한 벌이다 (RAG.md §2-5).
--   docker-entrypoint 가 파일명 순서로 실행하므로 01 -> 02 로 재현된다.
-- ════════════════════════════════════════════════════════════════════════════


-- ════════════════════════════════════════════════════════════════
-- corpus.check_items : "결제 전 확인" 항목의 폐쇄 목록
--   🔴 이 테이블의 존재 이유는 목록 자체가 아니라 **code 라는 안정 식별자**다.
--      이게 없으면 LLM 이 매번 자유 문자열을 뱉는다 —
--      "과업 범위 확정" / "계약 범위 명확화" / "과업범위 문서화" 가 전부 다른 항목이 되고,
--      재판정 때 사용자가 체크해 둔 진행상황이 통째로 날아간다.
--      code 는 LLM 의 guided_json enum 으로 그대로 들어간다 (LLM.md §3-1 — 값을 열지 않는다).
--   재료는 이미 corpus.rules 19행 안에 있다: 사전승인_조건 8 · 금지예시 46 · 한도 7 · 증빙 129.
--   새로 파싱할 것이 없고, 그 재료를 항목 문장으로 압축하는 작업이다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS corpus.check_items (
    code          TEXT PRIMARY KEY,           -- 계약범위확정 / 비교견적준비 / 특수관계확인 ...
    사업명        TEXT,                       -- NULL = 전 사업 공통
    비목          TEXT,                       -- NULL = 전 비목 공통. rules 와 같은 조회 키
    구분          TEXT NOT NULL CHECK (구분 IN ('결제전','결제후')),
    항목          TEXT NOT NULL,              -- 기본 문안. LLM 이 다듬되 code 는 유지한다
    설명          TEXT,
    기본_오프셋일 INT,                        -- 집행일 기준 며칠 전. 코드가 초기 due_date 를 계산
    -- 캘린더 배지. 🔴 기본값 '기타' 라 코드가 새로 늘어도 조용히 틀리지 않는다.
    --    정본은 이 컬럼이다 — 서버가 항목 텍스트로 분류하면 마스터가 늘 때 갱신이 빠진다.
    유형          TEXT NOT NULL DEFAULT '기타' CHECK (유형 IN ('기타','계약','비교견적')),
    근거          JSONB,                      -- [{doc_id, 조번호}]
    verified      BOOLEAN NOT NULL DEFAULT false,
    검수자        TEXT,
    검수일        DATE
);
CREATE INDEX IF NOT EXISTS ix_check_items_key ON corpus.check_items (사업명, 비목, 구분);

COMMENT ON TABLE corpus.check_items IS
    '결제 전/후 확인 항목의 폐쇄 목록. code 가 재판정 간 진행상황을 잇는 키다.';


-- ════════════════════════════════════════════════════════════════
-- corpus.evidence_sources : 증빙 발급처 안내
--   `공유받은 파일/증빙서류 종합.csv` 124행이 그대로 들어온다. 전처리 없음.
--   ⚠️ CSV 의 증빙명 124종과 rules.증빙 49종은 문자열 완전일치가 23종뿐이다.
--      (a) 표기 차이는 별칭으로, (b) rules 쪽 복합 문자열
--      ("기술이전: 활용계획서·기술이전계약서·완료보고서")은 낱개로 쪼개 재적재해야 한다.
--      그 작업은 seed_rules.py 수정 + rules 19행 DELETE/INSERT 이고 chunks 와 무관하다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS corpus.evidence_sources (
    증빙명    TEXT PRIMARY KEY,               -- "세금계산서(신용카드 영수증)", "견적서" ...
    해당비목  TEXT[],
    패키지    TEXT[],
    세부정보  TEXT,                           -- 화면 툴팁
    발급처    TEXT                            -- 홈택스 / IRIS / K-스타트업 / 특허로 ...
);

-- 비목 정규화 결과. 🔴 `해당비목`(CSV 원문)을 덮어쓰지 않고 컬럼을 더한다 —
--   매핑 규칙이 틀렸을 때 되돌리려면 원본이 남아야 한다. `load_evidence.py::정본비목()` 산출.
--   분류: 정본 | 표기차이 | 지급수수료_세목 | R&D계통 | 주관기관비목 | 비목아님 | 미분류
--   🔴 R&D계통(TIPS)은 `해당비목_정본` 이 NULL 이다. 위임 계통이 다르고(「국가연구개발혁신법」
--      vs 통합관리지침 제36조 표-10) `rules` 에 TIPS 룰이 없어, 매핑하면 무음 0행 조인이 된다.
ALTER TABLE corpus.evidence_sources ADD COLUMN IF NOT EXISTS 해당비목_정본 TEXT[];
ALTER TABLE corpus.evidence_sources ADD COLUMN IF NOT EXISTS 해당비목_분류 TEXT[];

COMMENT ON TABLE corpus.evidence_sources IS
    '증빙서류별 발급처·설명. 출처 CSV 124행. 판정에 쓰이지 않고 안내에만 쓴다.';


-- ════════════════════════════════════════════════════════════════
-- tenant.expense_plans : 지출 계획
--   🔴 지금까지 백엔드에 "계획" 이라는 개체가 없었다. decisions(판정 로그)뿐이라
--      판정이 끝난 건만 남고 임시저장·지출명·최근수정일을 담을 데가 없었다.
--      프론트의 홈·목록·상세·새 계획 4화면이 전부 이 테이블 하나에 묶인다.
--   재판정은 decisions 에 append 하고 latest_decision_id 포인터만 옮긴다 —
--   덮어쓰면 "왜 그때는 가능이었나" 를 설명할 수 없다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tenant.expense_plans (
    plan_id     BIGSERIAL PRIMARY KEY,
    org_id      UUID REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,  -- NULL = 게스트
    제목        TEXT,                         -- 목록의 "지출명". 정규화가 문장에서 뽑아 채운다
    질문원문    TEXT NOT NULL,                -- 목록 검색이 뒤지는 값
    정규화      JSONB,                        -- ① 단계 출력 전체
    -- 아래 4개는 정렬·필터에 쓰는 값만 JSONB 에서 컬럼으로 승격한 것이다
    확정비목    TEXT,
    금액        NUMERIC,
    집행예정일  DATE,
    거래처      TEXT,
    -- 🔴 진행 상태이지 판정이 아니다. 4-way(가능/조건부/불가/판단불가)와 섞지 말 것
    상태        TEXT NOT NULL DEFAULT 'draft' CHECK (상태 IN ('draft','judged')),
    latest_decision_id BIGINT REFERENCES tenant.decisions(decision_id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_plans_org_time ON tenant.expense_plans (org_id, updated_at DESC);

COMMENT ON TABLE tenant.expense_plans IS
    '지출 계획. 상태는 진행(draft/judged)이지 판정이 아니다.';


-- ════════════════════════════════════════════════════════════════
-- tenant.plan_tasks : 할일 = 체크리스트 + 캘린더
--   🔴 두 화면이 같은 행을 다르게 거르는 것이지 서로 다른 레코드가 아니다.
--        체크리스트 = WHERE plan_id = ?          (전부)
--        캘린더     = WHERE due_date IS NOT NULL (날짜 있는 것만)
--      따로 두면 상세에서 체크했는데 캘린더는 "준비 필요" 로 남는 식으로 반드시 어긋난다.
--
--   ⚠️ org_id 를 plan_id 와 별도로 둔다.
--      스키마 지도에는 plan_id NULL(사용자가 직접 만든 일정)이 허용인데, 그러면
--      plan 경유 RLS 로는 그 행을 격리할 수 없다. l3_articles 가 org_id 를 중복
--      저장한 것과 같은 이유로 여기도 직접 건다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tenant.plan_tasks (
    task_id     BIGSERIAL PRIMARY KEY,
    org_id      UUID REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,  -- RLS 축
    plan_id     BIGINT REFERENCES tenant.expense_plans(plan_id) ON DELETE CASCADE,
                                              -- NULL = 계획과 무관한 사용자 일정
    decision_id BIGINT REFERENCES tenant.decisions(decision_id) ON DELETE SET NULL,
    -- 🔴 user 는 재판정이 절대 건드리지 않는다
    출처        TEXT NOT NULL CHECK (출처 IN ('ai','user')),
    코드        TEXT REFERENCES corpus.check_items(code),   -- 재판정 때 같은 항목인지 알아보는 키
    구분        TEXT NOT NULL CHECK (구분 IN ('결제전','결제후','집행')),
    항목        TEXT NOT NULL,
    설명        TEXT,
    due_date    DATE,                         -- NULL 이면 체크리스트에만, 값이 있으면 캘린더에도
    날짜_사용자수정 BOOLEAN NOT NULL DEFAULT false,  -- true 면 재판정이 날짜를 덮지 않는다
    -- 🔴 판정 4-way 와 다른 축이다. 코드가 관리한다
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


-- ════════════════════════════════════════════════════════════════
-- 기존 테이블 수정 2건 — 둘 다 0행이라 ADD COLUMN 이 즉시 끝난다
-- ════════════════════════════════════════════════════════════════

-- 판정을 계획에 종속시킨다. 없으면 어느 계획의 판정인지 알 수 없어 고아 로그가 된다.
ALTER TABLE tenant.decisions
    ADD COLUMN IF NOT EXISTS plan_id BIGINT
        REFERENCES tenant.expense_plans(plan_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_decisions_plan ON tenant.decisions (plan_id);

-- 프론트 기관 검색 결과가 "서울특별시 광진구 · 창업지원단" 을 표시 중인데 컬럼이 없었다.
ALTER TABLE tenant.orgs ADD COLUMN IF NOT EXISTS 주소 TEXT;
ALTER TABLE tenant.orgs ADD COLUMN IF NOT EXISTS 부서 TEXT;


-- ════════════════════════════════════════════════════════════════
-- decisions 확장 5건 (2026-08-31) — 판정을 처음 돌려보고 드러난 결손
--   D6 격리 테스트 72문항 실행 결과, 판정 출력을 스키마가 다 못 담았다.
--   `LLM.md` §3-4 에는 있는데 컬럼이 없던 것들이다. 근거는 `RAG.md` §2-2.
--
--   🔴 `강등사유` 가 가장 중요하다. 72건 중 **27건이 강등**됐는데 이유를 안 남기면
--      "왜 조건부로 내려갔나" 를 나중에 설명할 수 없다. 검증기가 내리는 판단
--      (인용이 컨텍스트 밖 / verified=false 룰 단독 '가능' / extraction='vlm')이
--      전부 여기 기록돼야 `Agent.md` §6 의 재현성 요건을 만족한다.
--
--   ⚠️ `인용`·`전제` 컬럼은 **검증기 출력**(인용목록·전제목록 — DB 원문까지 채운
--      객체 배열)을 담는다. LLM 원출력(S번호 문자열 배열)이 아니다.
-- ════════════════════════════════════════════════════════════════
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 요약        TEXT;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 버전스탬프  TEXT;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 참조사슬    JSONB;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 강등사유    TEXT[];
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS 미매핑전제  JSONB;


-- ════════════════════════════════════════════════════════════════
-- RLS — 신설 tenant 2개. 01_schema.sql 의 org_isolation 관례를 그대로 따른다.
-- ════════════════════════════════════════════════════════════════
ALTER TABLE tenant.expense_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.plan_tasks    ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_isolation ON tenant.expense_plans;
DROP POLICY IF EXISTS org_isolation ON tenant.plan_tasks;
CREATE POLICY org_isolation ON tenant.expense_plans USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.plan_tasks    USING (org_id = tenant.current_org());


-- ════════════════════════════════════════════════════════════════
-- updated_at 자동 갱신
--   목록의 "최근 수정일" 이 앱의 성실성에 의존하면 반드시 틀린다. DB 가 찍는다.
-- ════════════════════════════════════════════════════════════════
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


-- ════════════════════════════════════════════════════════════════
-- 권한 · 적재현황 뷰 갱신 (신설 4개를 포함시킨다)
-- ════════════════════════════════════════════════════════════════
REVOKE ALL ON ALL TABLES IN SCHEMA corpus, tenant FROM PUBLIC;

CREATE OR REPLACE VIEW corpus.v_적재현황 AS
SELECT '01. corpus.documents'      AS 테이블, count(*) AS 건수 FROM corpus.documents
UNION ALL SELECT '02. corpus.doc_articles',     count(*) FROM corpus.doc_articles
UNION ALL SELECT '03. corpus.chunks',           count(*) FROM corpus.chunks
UNION ALL SELECT '05. corpus.rules',            count(*) FROM corpus.rules
UNION ALL SELECT '06. corpus.precedence_rules', count(*) FROM corpus.precedence_rules
UNION ALL SELECT '07. corpus.refs',             count(*) FROM corpus.refs
UNION ALL SELECT '08. corpus.chunk_terms',      count(*) FROM corpus.chunk_terms
UNION ALL SELECT '09. corpus.chunk_len',        count(*) FROM corpus.chunk_len
UNION ALL SELECT '10. corpus.item_alias',       count(*) FROM corpus.item_alias
UNION ALL SELECT '11. corpus.xref_mismatch',    count(*) FROM corpus.xref_mismatch
UNION ALL SELECT '12. corpus.check_items',      count(*) FROM corpus.check_items
UNION ALL SELECT '13. corpus.evidence_sources', count(*) FROM corpus.evidence_sources
UNION ALL SELECT '14. tenant.orgs',             count(*) FROM tenant.orgs
UNION ALL SELECT '15. tenant.l3_documents',     count(*) FROM tenant.l3_documents
UNION ALL SELECT '16. tenant.l3_articles',      count(*) FROM tenant.l3_articles
UNION ALL SELECT '17. tenant.accounts',         count(*) FROM tenant.accounts
UNION ALL SELECT '18. tenant.f_profile',        count(*) FROM tenant.f_profile
UNION ALL SELECT '19. tenant.f_exec',           count(*) FROM tenant.f_exec
UNION ALL SELECT '20. tenant.f_personnel',      count(*) FROM tenant.f_personnel
UNION ALL SELECT '21. tenant.unmapped_premise', count(*) FROM tenant.unmapped_premise
UNION ALL SELECT '22. tenant.decisions',        count(*) FROM tenant.decisions
UNION ALL SELECT '23. tenant.expense_plans',    count(*) FROM tenant.expense_plans
UNION ALL SELECT '24. tenant.plan_tasks',       count(*) FROM tenant.plan_tasks
UNION ALL SELECT '25. eval.golden_set',         count(*) FROM eval.golden_set
ORDER BY 1;
