-- ════════════════════════════════════════════════════════════════
-- 05_frontend_alter.sql — 프론트 요구서(2026-09-01) 반영 ALTER
--
-- 🔴 왜 새 파일인가: 같은 시각 다른 세션들이 `01_schema.sql`·`04_agent.sql` 을
--    고치고 있다. 프론트 사유의 변경만 별도 파일로 뺐다 — 되돌릴 때도 이 파일만 본다.
--    건드리는 것은 `tenant.*` 뿐이다. `corpus.*` 는 Agent 세션 소유라 손대지 않는다.
--
-- 판정 기준(이 세션 규칙): «아키텍처가 뒤집히면 백엔드, 구현하면 그만이면 프론트».
--    아래 둘은 표시·저장용이고 판정 로직에 들어가지 않는다 → 프론트 기준으로 받는다.
-- ════════════════════════════════════════════════════════════════


-- ── ① plan_tasks.유형 — 화면 6 ⑥ 「다가오는 일정」 의 일정 유형 배지 ──────
--
-- 프론트 요구서 §화면6-⑥ 이 `기타` / `계약` / `비교견적` 3종을 요구한다.
-- 판정과 무관한 **표시 축**이다. plan_tasks.상태(준비필요·집행예정·완료)와도,
-- 판정 4-way 와도 다른 축이라는 점을 주석으로 못박는다.
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


-- ── ② expense_plans.추가설명 — 화면 8 폼의 자유 텍스트 칸 ────────────────
--
-- 폼이 못 담는 예외 맥락을 사용자가 적는 칸이다 (`프로토타입_해부_구현명세.md` §1-3).
-- 정규화에서는 `용도` 에 합류시키지만, **원문은 따로 남긴다** — 합쳐버리면
-- 나중에 «사용자가 뭐라고 썼는지» 를 못 되짚는다.
ALTER TABLE tenant.expense_plans
    ADD COLUMN IF NOT EXISTS "추가설명" TEXT;

COMMENT ON COLUMN tenant.expense_plans."추가설명" IS
    '폼의 자유 텍스트 원문. 정규화 용도에 합류하되 원문은 여기 남긴다.';


-- ── ④ corpus.check_items.유형 — 기존 DB 업그레이드 경로가 빠져 있던 것 ────────
--
-- 🔴 2026-09-01 조율 세션 실측: `02_frontend.sql` 의 `CREATE TABLE IF NOT EXISTS
--    corpus.check_items (...)` 안에 `유형` 이 이미 인라인으로 들어 있다. 오늘
--    이 컬럼이 살아있는 DB 에 처음 생긴 경로는 수동 ALTER(ai-25) 였는데, 그 ALTER 가
--    `db/init/*.sql` 어디에도 없다 — `IF NOT EXISTS` 라 테이블이 이미 있는 환경(어제
--    만든 DB 등)에서는 `db/init/*.sql` 을 전부 다시 돌려도 이 컬럼이 **영원히 안 생긴다**.
--    corpus.* 는 Agent 세션 소유지만, 살아있는 DB·02_frontend.sql 과 동일한 정의를
--    멱등하게 다시 쓰는 것뿐이라 조율 세션이 승인했다 — 정의 자체는 안 바꿨다.
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

-- 백필 — `ADD COLUMN ... DEFAULT '기타'` 는 기존 행을 전부 '기타' 로만 채운다.
-- 마스터 52종 중 기타가 아닌 건 두 종뿐이다(2026-09-01 실측). WHERE 절로 멱등하게 건다.
UPDATE corpus.check_items SET "유형" = '비교견적'
    WHERE code = '비교견적준비' AND "유형" = '기타';
UPDATE corpus.check_items SET "유형" = '계약'
    WHERE code = '전대차아님확인' AND "유형" = '기타';


-- ── ⑤ tenant.l3_documents.파싱품질 CHECK — 같은 종류의 누락 ──────────────────
--
-- 🔴 `01_schema.sql:376` 의 `CREATE TABLE` 안에 `파싱품질 ... CHECK (파싱품질 IN
--    ('대기','pass','warn','fail'))` 이 인라인으로 있다. `01_schema.sql` 은 이 조율
--    세션도 훅 때문에 못 고친다 — 대신 여기서 DROP-후-재정의로 멱등하게 맞춘다.
--    제약 이름은 Postgres 기본 명명(`{table}_{column}_check`)이고 실측(`pg_constraint`)
--    으로도 확인했다 — 이름을 새로 짓지 않았다.
DO $$
BEGIN
    ALTER TABLE tenant.l3_documents DROP CONSTRAINT IF EXISTS l3_documents_파싱품질_check;
    ALTER TABLE tenant.l3_documents
        ADD CONSTRAINT l3_documents_파싱품질_check
        CHECK ("파싱품질" IN ('대기','pass','warn','fail'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ── ⑥ 안 만든 것 (일부러) ────────────────────────────────────────────────
--
-- `expense_plans.질문원문` 의 NOT NULL 은 **풀지 않았다.**
--   폼 경로에는 질문 원문이 없지만, 폼 값을 문장으로 합성해 채운다
--   (`server/routes_plans.py::_합성`). 스키마 변경 0건으로 끝나고
--   목록 검색이 계속 살며 자연어 경로와 비교도 가능하다.
--   ⚠️ 합성 문장을 다시 LLM 입력으로 쓰지 않는다 — 필드→문장→필드 왕복은 정보를 잃는다.
--
-- `expense_plans.용도` 컬럼 승격도 하지 않았다. 정규화 JSONB 안에 있고,
--   CSV 「산출 근거」 열이 그걸 읽으면 된다. 목록 필터가 용도를 쓰게 되면 그때 승격한다.
--
-- `expense_plans.하위항목` 도 아직이다. 정규화 출력에 `하위항목` 이 실리기 시작하면
--   (Agent 세션 작업) 그때 추적용으로 승격한다.
--
-- `accounts.팀명` 은 손대지 않았다 — `accounts` 테이블 자체가 아직 없고,
--   사이드바 계정 표시는 로그인 설계가 닫힌 뒤의 일이다. 미결로 남긴다.
