-- 04_agent.sql — Agent 층 스키마 (2026-08-31, D 세션)
--
-- 계약: 0831_최종구현.md §8-D (D1-a ~ D1-f, D2)
-- DDL 전권은 D 세션에만 있다. 다른 세션은 이 파일을 고치지 않는다 (BLOCKED 로 D 에게).
--
-- 전 구간 멱등(idempotent) 이다. 여러 번 실행해도 같은 상태가 된다.
-- 실행:  docker exec -i suddoe-db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f -  < db/init/04_agent.sql

-- ⚠️ 이 파일은 한 트랜잭션에서 tenant·corpus·eval 세 스키마의 표를 줄줄이 잠근다.
--    다른 세션이 DB 를 쓰고 있으면 `deadlock detected` 로 통째로 롤백된다 (실측 2026-08-31).
--    한가할 때 돌리거나, 이미 머지된 뒤 한 블록만 고쳤다면 그 블록만 따로 쳐라.
--    lock_timeout 이 있어야 남의 idle-in-transaction 에 걸렸을 때 10초 만에 되돌아온다
--    (없으면 DB 를 6분씩 세운다 — 2026-08-31 실측. PG 가 못 보는 앱 레벨 교착이었다).
\set ON_ERROR_STOP on
SET lock_timeout = '10s';

BEGIN;

-- ════════════════════════════════════════════════════════════════════════
-- D1-a. 현물 제거
-- ────────────────────────────────────────────────────────────────────────
-- 근거: 현물 계상은 지출이 아니다. 이 서비스는 "이 돈 써도 되나"를 판정한다.
--       현물은 협약 시점에 산정되는 회계 항목이지 집행 승인 대상이 아니다.
--
-- 🔴 손실 1건 (숨기지 않는다):
--   자기부담금 구성비율 검증(현금 5% / 현물 20% 등 사업별 하한)을
--   더는 할 수 없다. f_profile.자기부담_현물 이 사라지므로
--   "자기부담 현금 비중이 규정 하한에 미달" 을 잡아낼 근거가 없다.
--   → 되살리려면 f_profile 에 현물 총액 1컬럼만 다시 두면 된다(집행 형태는 불필요).
--   → 오늘은 해당 골든셋 문항이 0건이라 판정 지표에 영향이 없다.
--
-- 실측(2026-08-31 적용 직전): f_profile 0행 · f_exec 0행 → 데이터 손실 0
-- ════════════════════════════════════════════════════════════════════════

ALTER TABLE tenant.f_profile DROP COLUMN IF EXISTS "정부지원_현물";
ALTER TABLE tenant.f_profile DROP COLUMN IF EXISTS "자기부담_현물";

-- f_exec.형태 는 ix_exec_agg 의 마지막 키다. 컬럼을 지우면 인덱스가 같이 사라지므로
-- DROP 뒤에 (profile_id, 비목, 재원) 로 재생성한다.  형태_check 는 컬럼과 함께 소멸.
ALTER TABLE tenant.f_exec   DROP COLUMN IF EXISTS "형태";
DROP INDEX IF EXISTS tenant.ix_exec_agg;
CREATE INDEX IF NOT EXISTS ix_exec_agg ON tenant.f_exec (profile_id, "비목", "재원");


-- ════════════════════════════════════════════════════════════════════════
-- D1-b. 제약 3건 — 전부 NULLS NOT DISTINCT
-- ────────────────────────────────────────────────────────────────────────
-- 기본 UNIQUE 는 NULL 을 서로 다른 값으로 본다. 세 표 모두 키의 일부가 nullable 이라
-- 기본 동작이면 ON CONFLICT 가 영영 안 걸리고 중복이 조용히 쌓인다.
--   rules            : 기관id NULL (= L1/L2 전역 룰) 이 중복 적재된다
--   unmapped_premise : 사업명·비목 NULL 이면 발생횟수+1 이 안 걸려 결핍 루프가 죽는다  🔴
--   item_alias       : 사업명 NULL (= 전 사업 공통 별칭) 이 seed 재실행마다 불어난다
-- PostgreSQL 15+ 필요. 현재 17.10.
-- ════════════════════════════════════════════════════════════════════════

-- 🔴 rules."사업명" 을 nullable 로 내린다 (G 세션 BLOCKED 요청 · G3 L1 룰 분리의 선결).
--    (L1, 기관id=NULL, 사업명=NULL, 비목) = "전 사업 공통 L1 룰" 을 표현할 자리가 없었다.
--    사업별 L1 행 8개를 만드는 대안은 G3 을 반대로 만든다 — 고칠 곳이 6곳에서 8곳으로 는다.
--    D2 가 골든셋 27행을 사업명=NULL 로 내렸으므로 그 27문항(전체의 35%)의 룰 조회 키도
--    사업명 NULL 이다. 받을 그릇이 없으면 그 27문항의 룰 조회가 통째로 0행이 된다.
--    유일성은 아래 uq_rules_key 의 NULLS NOT DISTINCT 가 그대로 지킨다 — 비목당 L1 1행.
ALTER TABLE corpus.rules ALTER COLUMN "사업명" DROP NOT NULL;

ALTER TABLE corpus.rules DROP CONSTRAINT IF EXISTS "rules_layer_기관id_사업명_비목_key";
ALTER TABLE corpus.rules DROP CONSTRAINT IF EXISTS uq_rules_key;
ALTER TABLE corpus.rules
  ADD CONSTRAINT uq_rules_key
  UNIQUE NULLS NOT DISTINCT (layer, "기관id", "사업명", "비목");

ALTER TABLE tenant.unmapped_premise
  DROP CONSTRAINT IF EXISTS "unmapped_premise_premise_text_사업명_비목_key";
ALTER TABLE tenant.unmapped_premise DROP CONSTRAINT IF EXISTS uq_unmapped_premise_key;
ALTER TABLE tenant.unmapped_premise
  ADD CONSTRAINT uq_unmapped_premise_key
  UNIQUE NULLS NOT DISTINCT (premise_text, "사업명", "비목");

-- item_alias 는 UNIQUE 자체가 없었다. seed_item_alias.py(B) 재실행이 중복을 낳는다.
-- 선적재분에 중복이 있으면 ADD CONSTRAINT 가 실패하므로 먼저 최소 alias_id 만 남긴다.
DELETE FROM corpus.item_alias a
 USING corpus.item_alias b
 WHERE a.alias_id > b.alias_id
   AND a."상품명" = b."상품명"
   AND a."사업명" IS NOT DISTINCT FROM b."사업명";

ALTER TABLE corpus.item_alias DROP CONSTRAINT IF EXISTS uq_item_alias_key;
ALTER TABLE corpus.item_alias
  ADD CONSTRAINT uq_item_alias_key
  UNIQUE NULLS NOT DISTINCT ("상품명", "사업명");


-- ════════════════════════════════════════════════════════════════════════
-- D1-c. decisions 3컬럼
-- ────────────────────────────────────────────────────────────────────────
-- 강등사유(사람이 읽는 문장)는 이미 있다. 강등코드는 기계가 집계하는 enum 배열이라
-- 따로 둔다 — 문장을 파싱해 지표를 내면 문구가 바뀔 때마다 지표가 끊긴다.
-- 경로     : 판정이 지나온 분기 (게이트 A~D · L3단독 · 판단불가 등)
-- 실패단계 : 판단불가로 떨어진 지점. NULL 이면 정상 완주.
-- ════════════════════════════════════════════════════════════════════════

ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "강등코드" TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "경로"     TEXT;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "실패단계" TEXT;

-- 강등코드 22종. 매핑표 밖의 코드는 거부한다 — 오타가 지표를 조용히 갈라놓지 못하게.
-- 2026-09-01: TASK_* 4종 추가 (18 → 22). `해야할일[].설명` 대조용이고 판정을 바꾸지 않는다
-- (설명만 떨어뜨린다). 정본 철자는 `llm_validate.py` 에 박힌 문자열이다.
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_강등코드_check";
ALTER TABLE tenant.decisions
  ADD CONSTRAINT "decisions_강등코드_check"
  CHECK ("강등코드" <@ ARRAY[
    'INVALID_JUDGMENT','CITE_NOT_IN_MAP','CITE_DB_MISSING','CITE_HANG_MISMATCH',
    'PREMISE_NO_BASIS','PREMISE_BASIS_NOT_IN_MAP','PREMISE_ENUM','PREMISE_UNMAPPED',
    'NO_CITATION','VLM_DOWNGRADE','B_GRADE_DOWNGRADE','UNVERIFIED_RULE',
    'TASK_CODE_INVALID','L3_ONLY_DOWNGRADE','TENANT_LEAK','DANGLING_WARN',
    'DOMAIN_WARN','PRECEDENCE_FLIP',
    -- 해야할일 대조 (llm_validate.py · ai-ba)
    'TASK_STATE_UNSOURCED','TASK_STATE_MISMATCH',
    'TASK_NUMBER_UNSOURCED','TASK_BASIS_NOT_IN_MAP'
  ]::text[]);

CREATE INDEX IF NOT EXISTS "ix_decisions_경로" ON tenant.decisions ("경로");


-- ════════════════════════════════════════════════════════════════════════
-- D1-d. tenant.incidents (기관ID 누수) · corpus.recheck_queue (H1 출력)
-- ════════════════════════════════════════════════════════════════════════

-- 판정 결과에 현재 기관이 아닌 기관ID 가 섞여 나온 사건.
-- A3 이 TENANT_LEAK 을 발행하며 판정을 폐기하고 여기에 남긴다.
-- 🔴 이 표는 사고 기록이라 org_id 로 필터하면 감사가 안 된다 → RLS 걸지 않는다.
CREATE TABLE IF NOT EXISTS tenant.incidents (
  incident_id   BIGSERIAL PRIMARY KEY,
  발생시각      TIMESTAMPTZ NOT NULL DEFAULT now(),
  종류          TEXT        NOT NULL,
  decision_id   BIGINT      REFERENCES tenant.decisions(decision_id) ON DELETE SET NULL,
  org_id        UUID        REFERENCES tenant.orgs(org_id)           ON DELETE SET NULL,
  기대_기관id   TEXT,
  발견_기관id   TEXT,
  질문원문      TEXT,
  상세          JSONB       NOT NULL DEFAULT '{}',
  해소          BOOLEAN     NOT NULL DEFAULT false,
  CONSTRAINT "incidents_종류_check"
    CHECK ("종류" IN ('TENANT_LEAK','INDEX_GUARD','SCHEMA_VIOLATION','ROUTING_BLOCK','기타'))
);
CREATE INDEX IF NOT EXISTS ix_incidents_time ON tenant.incidents ("발생시각" DESC);

-- H1(A4 개정 대응) 의 출력. 🔴 H 는 corpus.rules 를 직접 UPDATE 하지 않는다 (G와 충돌).
-- 개정으로 근거 조문이 흔들린 룰을 여기에 쌓고, 사람이 승인해 반영한다.
-- 컬럼 구성은 H 세션이 제시한 형태를 채택했다 (2026-08-31). 내 초안보다 정확하다 —
-- 개정 감지는 **구판 좌표와 신판 좌표를 둘 다** 들고 있어야 무엇이 어디로 갔는지가 남는다.
CREATE TABLE IF NOT EXISTS corpus.recheck_queue (
  queue_id   BIGSERIAL PRIMARY KEY,
  종류       TEXT NOT NULL,   -- A4개정 | A2엄격조항 | A5기관답변 | H4정산리허설
  사유코드   TEXT NOT NULL,   -- BASIS_AMENDED | BASIS_RENUMBERED | BASIS_DELETED
                              -- | BASIS_STALE_DOC | STRICTER_L3 | ORG_ANSWER
  대상종류   TEXT NOT NULL,   -- rule | check_item | precedence_rule | item_alias | none
  -- 🔴 대상ID 를 TEXT 로 두고 FK 를 걸지 않는다. 이유 둘:
  --    ① 대상이 rules(bigint) · check_items(code) · precedence_rules(bigint) 로 갈린다
  --    ② G 가 corpus.rules 를 TRUNCATE RESTART IDENTITY 로 재적재해 rule_id 가 통째로 갈린다.
  --       FK 를 걸면 재적재가 큐를 날리거나 TRUNCATE 자체가 막힌다.
  --    → 재적재 후에도 살아남는 식별자는 (사업명, 비목, doc_id, 조번호) 다. 같이 채울 것.
  대상id     TEXT,   -- 소문자 id. 프로젝트 관례(기관id·doc_id)와 맞춘다 —
                      -- "대상ID" 로 두면 따옴표 없는 참조가 대상id 로 접혀 못 찾는다
  사업명     TEXT,
  비목       TEXT,
  doc_id     TEXT,            -- 신판 좌표
  조번호     TEXT,
  구doc_id   TEXT,            -- 구판 좌표
  구조번호   TEXT,
  변경유형   TEXT,
  유사도     NUMERIC,
  요약       TEXT,
  상세       JSONB NOT NULL DEFAULT '{}',
  상태       TEXT NOT NULL DEFAULT '대기',
  발견일     TIMESTAMPTZ NOT NULL DEFAULT now(),
  처리자     TEXT,
  처리일     DATE,
  CONSTRAINT "recheck_queue_종류_check"
    CHECK ("종류" IN ('A4개정','A2엄격조항','A5기관답변','H4정산리허설')),
  CONSTRAINT "recheck_queue_대상종류_check"
    CHECK ("대상종류" IN ('rule','check_item','precedence_rule','item_alias','none')),
  CONSTRAINT "recheck_queue_상태_check"
    CHECK ("상태" IN ('대기','검토중','반영','기각'))
);
CREATE INDEX IF NOT EXISTS ix_recheck_open
  ON corpus.recheck_queue ("상태", "발견일" DESC);
-- 🔴 A4 는 주 1회 배치다. 같은 개정을 반복 적재하면 큐가 쓰레기가 된다.
--    NULLS NOT DISTINCT 라 좌표 일부가 NULL 이어도 중복이 접힌다.
ALTER TABLE corpus.recheck_queue DROP CONSTRAINT IF EXISTS uq_recheck_key;
ALTER TABLE corpus.recheck_queue
  ADD CONSTRAINT uq_recheck_key
  UNIQUE NULLS NOT DISTINCT
    ("종류", "사유코드", "대상종류", "대상id", doc_id, "조번호", "구doc_id", "구조번호");


-- ════════════════════════════════════════════════════════════════════════
-- D1-d 부록. tenant.l3_documents."출처"  (E 세션 BLOCKED 요청 · 2026-08-31)
-- ────────────────────────────────────────────────────────────────────────
-- 계약 §8-E1 이 "l3_documents.출처='테스트픽스처' 라벨 필수" 라고 못박았는데
-- 01/02 스키마 어디에도 컬럼이 없었다. 합성 L3 픽스처가 실기관 규정과 섞이지 않게
-- 막는 유일한 라벨이라 넣는다. l3_documents 0행이라 기본값 채우기 비용 0.
-- ════════════════════════════════════════════════════════════════════════

ALTER TABLE tenant.l3_documents
  ADD COLUMN IF NOT EXISTS "출처" TEXT NOT NULL DEFAULT '기관업로드';
ALTER TABLE tenant.l3_documents DROP CONSTRAINT IF EXISTS "l3_documents_출처_check";
ALTER TABLE tenant.l3_documents
  ADD CONSTRAINT "l3_documents_출처_check"
  CHECK ("출처" IN ('기관업로드','테스트픽스처'));
COMMENT ON COLUMN tenant.l3_documents."출처" IS
  '테스트픽스처 = 합성 L3(게이팅·RLS 검증용). 판정 코퍼스 통계·실기관 규정과 분리하는 라벨.';


-- ════════════════════════════════════════════════════════════════════════
-- D1-d 부록 2. corpus.check_items."기본_오프셋일" 부호 규약 (E 세션 요청 · E5)
-- ────────────────────────────────────────────────────────────────────────
-- 뜻이 "집행일 기준 며칠 전" 이었는데 그러면 결제후 항목이 전부 과거로 꽂힌다.
-- `자산등록 30` 은 지침 제39조 "취득일부터 1개월 이내" 를 옮긴 값인데
-- "30일 전" 으로 읽혀 물건 사기 한 달 전에 자산등록을 하라는 일정이 나왔다.
-- 숫자가 틀린 게 아니라 **부호가 없었다.**
--
--   due_date = 집행예정일 + 기본_오프셋일     ← 분기 없는 단일 공식
--     결제전 → 음수 또는 0 · 결제후 → 양수 또는 0 · NULL → 기한 없음
--
-- 🔴 구분을 보고 분기하지 않는 게 요점이다. 분기가 프론트·판정 두 군데 생기면 어긋난다.
--    코드는 seed_check_items.due_date계산() 하나만 쓴다. 여기 CHECK 는 그 못이다.
-- ════════════════════════════════════════════════════════════════════════

COMMENT ON COLUMN corpus.check_items."기본_오프셋일" IS
  '집행예정일 기준 부호 있는 일수. due_date = 집행예정일 + 이 값. '
  '결제전은 음수(집행 전), 결제후는 양수(집행 후), NULL 은 기한 없음. '
  '구분으로 분기하지 않는다 — 부호가 방향이다 (2026-08-31 E5).';

ALTER TABLE corpus.check_items DROP CONSTRAINT IF EXISTS "ck_check_items_오프셋부호";
ALTER TABLE corpus.check_items
  ADD CONSTRAINT "ck_check_items_오프셋부호" CHECK (
    "기본_오프셋일" IS NULL
    OR ("구분" = '결제전' AND "기본_오프셋일" <= 0)
    OR ("구분" = '결제후' AND "기본_오프셋일" >= 0));


-- ════════════════════════════════════════════════════════════════════════
-- D1-f. corpus.programs · corpus.item_vocab
-- ────────────────────────────────────────────────────────────────────────
-- programs 를 golden_set FK 보다 먼저 만든다 (D2 가 이걸 참조한다).
-- ════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS corpus.programs (
  사업명          TEXT PRIMARY KEY,
  별칭            TEXT[]  NOT NULL DEFAULT '{}',
  전문기관        TEXT,
  비목계통        TEXT    NOT NULL DEFAULT '창업',   -- 창업 | RND(TIPS)
  우선규범        TEXT,          -- 이 사업의 상위 규범. NULL = 통합관리지침(기본)
  정부지원상한    NUMERIC,       -- 근거 확인 전에는 NULL. 추측 금지
  상한_단위       TEXT,
  상한_근거       JSONB   NOT NULL DEFAULT '[]',
  트랙범위        TEXT,          -- 사업 안에서 우리가 다루는 범위
  비고            TEXT,
  활성            BOOLEAN NOT NULL DEFAULT true,
  CONSTRAINT "programs_비목계통_check" CHECK ("비목계통" IN ('창업','RND'))
);

-- 사업명 정본은 corpus.chunks."사업명"(text[]) 에 실제로 박혀 있는 8종이다.
-- 별칭은 실측 재료에서만 가져온다:
--   폴더명(2026_Finance_DATA_FOR_RAG/창진원/*) · golden_set."사업명" · corpus.rules."사업명"
-- 우선규범은 corpus.precedence_rules."우선규범" 을 그대로 옮긴다 (오너 검수 완료 행).
INSERT INTO corpus.programs ("사업명","별칭","전문기관","비목계통","우선규범","트랙범위","비고") VALUES
  ('예비창업패키지',          ARRAY['예비창업','예창패'],                       '창업진흥원','창업', NULL, NULL, NULL),
  ('초기창업패키지',          ARRAY['초기창업','초창패'],                       '창업진흥원','창업', NULL, NULL, NULL),
  ('재도전성공패키지',        ARRAY['재도전','재도전성공'],                     '창업진흥원','창업', NULL, NULL,
     '2026-08-31 현재 corpus.rules 0행. G1 이 적재. 구판(2025)이 active 자리를 차지한 상태였음'),
  ('창업도약패키지',          ARRAY['창업도약','도약패키지'],                   '창업진흥원','창업', NULL, NULL, NULL),
  ('창업중심대학',            ARRAY['창업중심대학사업'],                        '창업진흥원','창업', NULL, NULL, NULL),
  ('초격차 스타트업 프로젝트',ARRAY['초격차','초격차 스타트업','초격차스타트업'],'창업진흥원','창업',
     '중소기업창업 지원사업 운영요령 · 중소기업창업 지원사업 통합관리지침', NULL, NULL),
  ('모두의 창업 프로젝트',    ARRAY['모두의창업','모두의 창업'],                '창업진흥원','창업',
     '중소기업창업 지원사업 운영요령 · 중소기업창업 지원사업 통합관리지침',
     '제1편 총칙 + 제2편 일반·기술트랙만. 제3편 로컬트랙은 범위 밖(상위가 신사업창업사관학교 운영지침)',
     NULL),
  ('TIPS',                    ARRAY['민관공동창업자발굴육성','민관공동창업자발굴육성(TIPS)','팁스'],
     '창업진흥원','RND', NULL, NULL,
     'R&D 계통 비목(표B 26종)이라 창업 계통 어휘집 10종과 다르다. 우선순위 조항 없음(precedence_rules 0행). G2 설계 결정 대기')
ON CONFLICT ("사업명") DO UPDATE SET
  "별칭"     = EXCLUDED."별칭",
  "전문기관" = EXCLUDED."전문기관",
  "비목계통" = EXCLUDED."비목계통",
  "우선규범" = EXCLUDED."우선규범",
  "트랙범위" = EXCLUDED."트랙범위",
  "비고"     = EXCLUDED."비고";
-- 🔴 정부지원상한 은 전 행 NULL 이다. 사업별 최대 지원금액은 공고문마다 다르고
--    코퍼스(규정 문서)에서 확정 근거를 찾지 못했다. 추측으로 채우지 않는다.
--    채울 때는 반드시 상한_근거 에 doc_id·조번호를 같이 박는다.


-- 비목 어휘집. 표A 창업 계통 10종이 정본이다 (corpus.rules · check_items 실측과 일치).
-- 지급수수료 는 하위 12종(기술이전비·멘토링비·사무실임차료 …)이 전부 한 비목으로 접힌다.
CREATE TABLE IF NOT EXISTS corpus.item_vocab (
  비목        TEXT PRIMARY KEY,
  계통        TEXT    NOT NULL DEFAULT '창업',
  별칭        TEXT[]  NOT NULL DEFAULT '{}',
  하위항목    TEXT[]  NOT NULL DEFAULT '{}',
  정렬        INT,
  비고        TEXT,
  CONSTRAINT "item_vocab_계통_check" CHECK ("계통" IN ('창업','RND'))
);

INSERT INTO corpus.item_vocab ("비목","계통","별칭","하위항목","정렬","비고") VALUES
  ('재료비','창업', ARRAY['재료 및 원료비','원재료비'], '{}', 1,
     '재도전성공패키지에는 정부지원 비목으로 없다(자기부담 현물 세부항목으로만 등장)'),
  ('외주용역비','창업', ARRAY['외주 용역비','용역비'], '{}', 2, NULL),
  ('기계장치','창업',
     ARRAY['기계장치(공구·기구, 비품, SW 등)','기계장치비','공구기구비','비품비'],
     '{}', 3, '모두의 창업 프로젝트는 재료비와 통합 규정'),
  ('특허권등무형자산취득비','창업',
     ARRAY['특허권 등 무형자산 취득비','무형자산취득비','특허권등무형자산취득비'], '{}', 4, NULL),
  ('인건비','창업', ARRAY['인 건 비'], '{}', 5, NULL),
  ('지급수수료','창업', ARRAY['지급 수수료'],
     ARRAY['기술이전비','학회·세미나 참가비','전시회·박람회 참가비','시험·인증비','멘토링비',
           '기자재임차비','장비 수리비','사무실임차료','운반비','보험료','보관료',
           '세무·회계비','회계감사비','법인설립비','기술보호비','규제애로 해소 법률컨설팅비',
           '고영향 AI 인증·검증'],
     6, '표A 6~22 가 이 한 비목으로 접힌다. 하위항목별 한도가 달라 rules 는 하위까지 봐야 한다'),
  ('여비','창업', ARRAY['출장비'], '{}', 7, NULL),
  ('교육훈련비','창업', ARRAY['교육 훈련비'], '{}', 8,
     '통합관리지침 제44조②(본인부담금)·③(4대보험)이 전 사업에 걸린다 — G4'),
  ('광고선전비','창업', ARRAY['광고 선전비','마케팅비'], '{}', 9, NULL),
  ('창업활동비','창업', '{}', '{}', 10,
     'corpus.rules 에는 있으나 check_items 에는 없다. 사업별 유무가 갈린다')
ON CONFLICT ("비목") DO UPDATE SET
  "계통"     = EXCLUDED."계통",
  "별칭"     = EXCLUDED."별칭",
  "하위항목" = EXCLUDED."하위항목",
  "정렬"     = EXCLUDED."정렬",
  "비고"     = EXCLUDED."비고";
-- 🔴 TIPS(RND 계통) 비목 26종은 넣지 않았다. G2 가 "매핑표 vs 별도 enum" 을 결정한다.
--    결정되면 계통='RND' 로 INSERT 만 하면 된다 — DDL 변경 불필요.


-- ════════════════════════════════════════════════════════════════════════
-- D2. golden_set."사업명" 의미 분리  → 그 뒤 FK
-- ────────────────────────────────────────────────────────────────────────
-- '공통(지침 제14차)' 26행 + '공통' 1행 = 27행은 사업 이름이 아니라 **적용범위 표시**다.
-- 가르지 않고 programs FK 를 걸면 그 자리에서 깨진다.
--
-- 원표기를 그대로 "적용범위" 로 옮긴다(정규화하지 않는다) — '(지침 제14차)' 는
-- 근거 판본 정보라 버리면 복구가 안 된다. 사업명은 NULL.
-- 이후 "공통 27 / 사업지정 50" 분해는 `적용범위 IS NOT NULL` 로 가른다.
-- ════════════════════════════════════════════════════════════════════════

ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS "적용범위" TEXT;

UPDATE eval.golden_set
   SET "적용범위" = "사업명",
       "사업명"   = NULL
 WHERE "사업명" LIKE '공통%'
   AND "적용범위" IS NULL;

-- 사업명이 남아 있으면 반드시 programs 에 있는 이름이어야 한다.
ALTER TABLE eval.golden_set DROP CONSTRAINT IF EXISTS "golden_set_사업명_fkey";
ALTER TABLE eval.golden_set
  ADD CONSTRAINT "golden_set_사업명_fkey"
  FOREIGN KEY ("사업명") REFERENCES corpus.programs("사업명")
  ON UPDATE CASCADE ON DELETE RESTRICT;


-- ════════════════════════════════════════════════════════════════════════
-- D1-e. eval.runs · eval.run_items · eval.golden_chunks
-- ════════════════════════════════════════════════════════════════════════

-- 한 번의 평가 실행. 종류로 e2e / retrieval / judge 를 가른다.
-- 🔴 지표를 jsonb 로 두는 이유: 종류마다 지표가 다르다(hit@5 vs 일치율 vs 판단불가율).
--    컬럼으로 박으면 새 지표마다 DDL 이 필요해지고 DDL 은 D 만 칠 수 있다.
CREATE TABLE IF NOT EXISTS eval.runs (
  run_id       BIGSERIAL PRIMARY KEY,
  종류         TEXT        NOT NULL,
  시작         TIMESTAMPTZ NOT NULL DEFAULT now(),
  종료         TIMESTAMPTZ,
  코퍼스버전   TEXT,
  git커밋      TEXT,
  설정         JSONB       NOT NULL DEFAULT '{}',   -- 하이퍼파라미터 전건. 재현의 근거
  문항수       INT,
  지표         JSONB       NOT NULL DEFAULT '{}',
  라벨         TEXT,
  비고         TEXT,
  CONSTRAINT "runs_종류_check" CHECK ("종류" IN ('e2e','retrieval','judge'))
);
CREATE INDEX IF NOT EXISTS "ix_runs_종류" ON eval.runs ("종류", "시작" DESC);

-- 문항 단위 결과. 실패 문항을 지목할 수 있어야 "무엇을 고칠지" 가 나온다.
CREATE TABLE IF NOT EXISTS eval.run_items (
  item_id   BIGSERIAL PRIMARY KEY,
  run_id    BIGINT  NOT NULL REFERENCES eval.runs(run_id) ON DELETE CASCADE,
  gold_id   BIGINT  REFERENCES eval.golden_set(gold_id) ON DELETE CASCADE,
  예측      TEXT,
  정답      TEXT,
  적중      BOOLEAN,
  원출력    JSONB NOT NULL DEFAULT '{}',
  CONSTRAINT uq_run_items UNIQUE (run_id, gold_id)
);
CREATE INDEX IF NOT EXISTS ix_run_items_run ON eval.run_items (run_id);
CREATE INDEX IF NOT EXISTS ix_run_items_miss ON eval.run_items (run_id) WHERE "적중" IS NOT TRUE;

-- 골든셋 정답근거 ↔ 실제 chunk_id 고정 매핑.
-- 🔴 지금은 평가할 때마다 원문 부분일치로 되짚는다 → 문자열이 조금만 달라도 지표가 흔들린다.
--    한 번 박아두면 평가가 결정적이 되고, 역추적 실패가
--    "매칭 버그" 인지 "코퍼스 결손" 인지 갈린다.
-- 🔴 (gold_id, 근거순번) 을 PK 로 두면 안 된다. 조문 하나가 여러 청크로 쪼개지므로
--    근거 1건이 정답 청크 N개를 갖는 게 정상이고, 역추적 실패 행은 chunk_id 가 NULL 이다.
--    → 대리키 + UNIQUE NULLS NOT DISTINCT 로 둘 다 담는다.
CREATE TABLE IF NOT EXISTS eval.golden_chunks (
  gc_id       BIGSERIAL PRIMARY KEY,
  gold_id     BIGINT NOT NULL REFERENCES eval.golden_set(gold_id) ON DELETE CASCADE,
  근거순번    INT    NOT NULL,          -- golden_set."정답근거" 배열 인덱스(0-based)
  chunk_id    BIGINT REFERENCES corpus.chunks(chunk_id) ON DELETE SET NULL,
  article_id  BIGINT REFERENCES corpus.doc_articles(article_id) ON DELETE SET NULL,
  doc_id      TEXT,
  조번호      TEXT,
  매칭방법    TEXT NOT NULL,            -- 원문일치 | 조번호 | 조제목 | 수동 | 실패
  실패사유    TEXT,                     -- 매칭방법='실패' 일 때만. 코퍼스 결손 vs 표기 불일치
  고정일      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_golden_chunks
    UNIQUE NULLS NOT DISTINCT (gold_id, "근거순번", chunk_id),
  CONSTRAINT "golden_chunks_매칭방법_check"
    CHECK ("매칭방법" IN ('원문일치','조번호','조제목','수동','실패')),
  -- 실패면 chunk_id 가 없어야 하고 사유가 있어야 한다. 반대도 마찬가지.
  CONSTRAINT "golden_chunks_실패_check"
    CHECK (("매칭방법" = '실패' AND chunk_id IS NULL AND "실패사유" IS NOT NULL)
        OR ("매칭방법" <> '실패' AND chunk_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_golden_chunks_chunk ON eval.golden_chunks (chunk_id);
CREATE INDEX IF NOT EXISTS ix_golden_chunks_gold  ON eval.golden_chunks (gold_id);

COMMIT;
