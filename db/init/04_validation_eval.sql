-- 판정 검증·평가용 스키마 변경: UNIQUE NULLS NOT DISTINCT 제약, decisions 검증코드·처리단계, incidents,
-- recheck_queue, programs, item_vocab, eval.runs/run_items/golden_chunks. 한 트랜잭션으로 적용한다.
-- 실행 시점: init. 멱등이라 다시 실행해도 같은 상태가 된다.
--   docker exec -i suddoe-db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < db/init/04_validation_eval.sql
-- tenant·corpus·eval 의 테이블을 차례로 락한다. lock_timeout 이 없으면 다른 커넥션의
-- idle-in-transaction 에 막혀 DB 가 몇 분씩 대기할 수 있다.
\set ON_ERROR_STOP on
SET lock_timeout = '10s';

BEGIN;

-- 정부지원 현물 제거 — 이 사업들에는 정부지원 현물이 없다.
-- 자기부담_현물(현물 총액)은 남긴다. 「현물은 총사업비의 20% 이하」 같은 비율 한도를 계산할 때 쓴다
-- (총사업비 = 정부지원_현금 + 자기부담_현금 + 자기부담_현물, migrations/2026-11-08_자기부담_현물.sql).

ALTER TABLE tenant.profile_agreements DROP COLUMN IF EXISTS "정부지원_현물";

-- 형태 컬럼이 사라지면 ix_exec_agg 도 같이 사라진다 — 재생성한다. 형태_check 는 컬럼과 함께 삭제된다.
ALTER TABLE tenant.profile_expenditures DROP COLUMN IF EXISTS "형태";
DROP INDEX IF EXISTS tenant.ix_exec_agg;
CREATE INDEX IF NOT EXISTS ix_exec_agg ON tenant.profile_expenditures (profile_id, "비목", "재원");


-- 제약 3건 — 전부 NULLS NOT DISTINCT (PostgreSQL 15+ 필요).
-- 기본 UNIQUE 는 NULL 을 서로 다른 값으로 봐서, 키 일부가 nullable 인 이 세 표는 ON CONFLICT 가 동작하지 않고 중복이 쌓인다.
--   rules: 기관id NULL(L1/L2 전역 룰) 중복 적재 / unmapped_premise: 사업명·비목이 NULL 이면 집계가 합쳐지지 않음
--   item_alias: 사업명 NULL(전 사업 공통 별칭)이 seed 재실행마다 늘어남

-- rules."사업명" 을 nullable 로 바꾼다 — (L1, 기관id=NULL, 사업명=NULL, 비목)으로 "전 사업 공통 L1 룰" 을 표현한다.
-- 유일성은 uq_rules_key 의 NULLS NOT DISTINCT 가 지킨다 — 비목당 L1 1행.
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

-- item_alias 에는 UNIQUE 가 없었다. 이미 적재된 행에 중복이 있으면 ADD CONSTRAINT 가 실패하므로 먼저 최소 alias_id 만 남긴다.
DELETE FROM corpus.item_alias a
 USING corpus.item_alias b
 WHERE a.alias_id > b.alias_id
   AND a."품목" = b."품목"
   AND a."사업명" IS NOT DISTINCT FROM b."사업명";

ALTER TABLE corpus.item_alias DROP CONSTRAINT IF EXISTS uq_item_alias_key;
ALTER TABLE corpus.item_alias
  ADD CONSTRAINT uq_item_alias_key
  UNIQUE NULLS NOT DISTINCT ("품목", "사업명");


-- decisions 4컬럼
-- 검증사유(사람이 읽는 문장)는 이미 있다. 검증코드는 기계가 집계하는 enum 배열이라
-- 따로 둔다 — 문장을 파싱해 지표를 내면 문구가 바뀔 때마다 지표가 끊긴다.
-- 처리단계 : 판정이 거친 단계를 + 로 이은 값 (정규화+검색+판정생성+검증, 금지목록일치, 캐시 등)
-- 실패단계 : 판단불가로 처리된 단계. NULL 이면 정상 완료.
-- 사용자답변 : 판정 전 확인 질문의 질문 문장과 답 값(예·아니오·모름) 목록. 질문 없이 판정하면 빈 배열.

ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "검증코드" TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "처리단계" TEXT;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "실패단계" TEXT;
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "사용자답변" JSONB NOT NULL DEFAULT '[]';
-- 경로 : 판정이 지난 경로. 옛 · 온톨로지(docs/5_룰/5-11_전환_계획.md). 전환 전 행은 NULL.
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "경로" TEXT;
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_경로_check";
ALTER TABLE tenant.decisions ADD CONSTRAINT "decisions_경로_check"
    CHECK ("경로" IN ('옛', '온톨로지'));
-- 온톨로지결과 : 결과 화면 값(분류 · 근거경로 · 코드판정 · 조건 · 용어풀이). 계획 다시 열기가 읽는다. 옛 경로는 NULL.
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "온톨로지결과" JSONB;
-- 규범 : 온톨로지 경로 판정에 쓴 규범 ID {코드판정, 조건, 문맥, 자연어답}(5-15 ④). 옛 경로는 NULL. 집계는 eval."v_규범별_판정"
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "규범" JSONB;
CREATE OR REPLACE VIEW eval."v_규범별_판정" AS
SELECT x."역할", x."규범ID", d."판정", d."경로", count(*) AS "건수", max(d.created_at) AS "최근"
  FROM tenant.decisions d
 CROSS JOIN LATERAL (
        SELECT '코드판정'::text AS "역할", d."규범"->>'코드판정' AS "규범ID" WHERE d."규범"->>'코드판정' IS NOT NULL
        UNION ALL
        SELECT '조건', jsonb_array_elements_text(COALESCE(d."규범"->'조건', '[]'::jsonb))
        UNION ALL
        SELECT '문맥', jsonb_array_elements_text(COALESCE(d."규범"->'문맥', '[]'::jsonb))
       ) x
 WHERE d."규범" IS NOT NULL
 GROUP BY x."역할", x."규범ID", d."판정", d."경로";

-- 검증코드 허용 목록. 목록 밖의 코드는 거부해 오타가 지표를 갈라놓지 못하게 한다.
-- 기준 목록은 scripts/llm_validate.py 의 검증코드_전체 이고, 이 CHECK 는 같은 순서로 맞춘다.
-- 코드를 추가하면 db/migrations/ 에 CHECK 교체 마이그레이션을 함께 낸다.
-- TASK_STATE_*/TASK_NUMBER_*/TASK_BASIS_* 는 해야할일 설명 대조용이고 판정을 바꾸지 않는다.
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_검증코드_check";
ALTER TABLE tenant.decisions
  ADD CONSTRAINT "decisions_검증코드_check"
  CHECK ("검증코드" <@ ARRAY[
    'INVALID_JUDGMENT','CITE_NOT_IN_MAP','CITE_DB_MISSING','CITE_PARAGRAPH_MISMATCH',
    'PREMISE_NO_BASIS','PREMISE_BASIS_NOT_IN_MAP','PREMISE_INVALID_OUTCOME','PREMISE_UNMAPPED',
    'PREMISE_DB_MISSING',
    'NO_CITATION','VLM_DOWNGRADE','B_GRADE_DOWNGRADE','UNVERIFIED_RULE',
    'TASK_CODE_INVALID','TASK_DB_MISSING',
    'L3_ONLY_DOWNGRADE','TENANT_LEAK','DANGLING_WARN',
    'DOMAIN_WARN','PRECEDENCE_OVERRIDE',
    'TASK_STATE_UNSOURCED','TASK_STATE_MISMATCH',
    'TASK_NUMBER_UNSOURCED','TASK_BASIS_NOT_IN_MAP',
    'ANSWER_UNCONFIRMED',
    'ONTOLOGY_BAN','LIMIT_EXCEEDED','VAT_BASIS_UNKNOWN'
  ]::text[]);

CREATE INDEX IF NOT EXISTS "ix_decisions_처리단계" ON tenant.decisions ("처리단계");


-- tenant.incidents (기관ID 유출) · corpus.recheck_queue (개정 대응 출력)

-- tenant.incidents : 판정 결과에 현재 기관이 아닌 기관ID 가 섞여 나온 사건 기록.
-- org_id 로 필터하면 감사가 안 되므로 RLS 를 걸지 않는다.
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
  해결          BOOLEAN     NOT NULL DEFAULT false,
  CONSTRAINT "incidents_종류_check"
    CHECK ("종류" IN ('TENANT_LEAK','INDEX_GUARD','SCHEMA_VIOLATION','ROUTING_BLOCK','OTHER'))
);
CREATE INDEX IF NOT EXISTS ix_incidents_time ON tenant.incidents ("발생시각" DESC);

-- corpus.recheck_queue : 법령 개정 대응 출력. 근거 조문이 개정·이동·삭제된 룰을 여기 쌓고 사람이 승인해 반영한다.
-- corpus.rules 를 직접 UPDATE 하지 않는다. 개정 감지는 이전판·신판 위치를 둘 다 저장해야 추적이 된다.
CREATE TABLE IF NOT EXISTS corpus.recheck_queue (
  queue_id   BIGSERIAL PRIMARY KEY,
  종류       TEXT NOT NULL,   -- 개정감지 | 엄격조항 | 기관답변 | 정산점검
  사유코드   TEXT NOT NULL,   -- BASIS_AMENDED | BASIS_RENUMBERED | BASIS_DELETED
                              -- | BASIS_STALE_DOC | STRICTER_L3 | ORG_ANSWER
  대상종류   TEXT NOT NULL,   -- rule | check_item | precedence_rule | item_alias | none
  -- 대상id 를 TEXT 로 두고 FK 를 걸지 않는다 — 대상이 rules(bigint)·check_items(code)·precedence_rules(bigint) 로 나뉘고,
  -- rules 재적재 시 rule_id 가 바뀌면 FK 가 큐를 깨뜨린다. 재적재 후에도 유지되는 키는 (사업명, 비목, doc_id, 조번호)다.
  대상id     TEXT,   -- 소문자 id. 프로젝트 관례(기관id·doc_id)와 맞춘다.
                      -- "대상ID" 로 두면 따옴표 없는 참조가 소문자 대상id 로 변환되어 컬럼을 찾지 못한다
  사업명     TEXT,
  비목       TEXT,
  doc_id     TEXT,            -- 신판 위치
  조번호     TEXT,
  이전판_doc_id TEXT,         -- 이전판 위치
  이전판_조번호 TEXT,
  변경유형   TEXT,
  유사도     NUMERIC,
  요약       TEXT,
  상세       JSONB NOT NULL DEFAULT '{}',
  상태       TEXT NOT NULL DEFAULT '대기',
  발견일     TIMESTAMPTZ NOT NULL DEFAULT now(),
  처리자     TEXT,
  처리일     DATE,
  CONSTRAINT "recheck_queue_종류_check"
    CHECK ("종류" IN ('개정감지','엄격조항','기관답변','정산점검')),
  CONSTRAINT "recheck_queue_대상종류_check"
    CHECK ("대상종류" IN ('rule','check_item','precedence_rule','item_alias','none')),
  CONSTRAINT "recheck_queue_상태_check"
    CHECK ("상태" IN ('대기','검토중','반영','기각'))
);
CREATE INDEX IF NOT EXISTS ix_recheck_open
  ON corpus.recheck_queue ("상태", "발견일" DESC);
-- 개정 감지는 주기적으로 반복 실행된다. 같은 개정이 중복 적재되지 않게 UNIQUE 를 둔다.
-- NULLS NOT DISTINCT 라 위치 일부가 NULL 이어도 중복으로 판단된다.
ALTER TABLE corpus.recheck_queue DROP CONSTRAINT IF EXISTS uq_recheck_key;
ALTER TABLE corpus.recheck_queue
  ADD CONSTRAINT uq_recheck_key
  UNIQUE NULLS NOT DISTINCT
    ("종류", "사유코드", "대상종류", "대상id", doc_id, "조번호", "이전판_doc_id", "이전판_조번호");


-- tenant.l3_documents."출처" — 합성 L3 픽스처가 실제 기관 자체규정과 섞이지 않게 막는 라벨.

ALTER TABLE tenant.l3_documents
  ADD COLUMN IF NOT EXISTS "출처" TEXT NOT NULL DEFAULT '기관업로드';
ALTER TABLE tenant.l3_documents DROP CONSTRAINT IF EXISTS "l3_documents_출처_check";
ALTER TABLE tenant.l3_documents
  ADD CONSTRAINT "l3_documents_출처_check"
  CHECK ("출처" IN ('기관업로드','테스트픽스처'));
COMMENT ON COLUMN tenant.l3_documents."출처" IS
  '테스트픽스처 = 합성 L3(L3 상위 규정 확인·RLS 검증용). 판정 코퍼스 통계·실기관 규정과 분리하는 라벨.';


-- corpus.check_items."기본_오프셋일" 부호 규약.
-- due_date = 집행예정일 + 기본_오프셋일 (분기 없는 단일 공식). 결제전 → 음수 이하, 결제후 → 양수 이상, NULL → 기한 없음.
-- 구분으로 분기하지 않는다. 분기가 프론트와 판정 두 곳에 생기면 서로 어긋난다.

COMMENT ON COLUMN corpus.check_items."기본_오프셋일" IS
  '집행예정일 기준 부호 있는 일수. due_date = 집행예정일 + 이 값. '
  '결제전은 음수(집행 전), 결제후는 양수(집행 후), NULL 은 기한 없음. '
  '구분으로 분기하지 않는다 — 부호가 방향을 나타낸다 (2026-08-31 결정).';

ALTER TABLE corpus.check_items DROP CONSTRAINT IF EXISTS "ck_check_items_오프셋부호";
ALTER TABLE corpus.check_items
  ADD CONSTRAINT "ck_check_items_오프셋부호" CHECK (
    "기본_오프셋일" IS NULL
    OR ("구분" = '결제전' AND "기본_오프셋일" <= 0)
    OR ("구분" = '결제후' AND "기본_오프셋일" >= 0));


-- corpus.programs · corpus.item_vocab
-- programs 를 뒤의 golden_set FK 보다 먼저 만든다.

CREATE TABLE IF NOT EXISTS corpus.programs (
  사업명          TEXT PRIMARY KEY,
  별칭            TEXT[]  NOT NULL DEFAULT '{}',
  전문기관        TEXT,
  비목체계        TEXT    NOT NULL DEFAULT '창업',   -- 창업 | RND(TIPS)
  우선규범        TEXT,          -- 이 사업의 상위 규정. NULL = 통합관리지침(기본)
  정부지원상한    NUMERIC,       -- 근거 확인 전에는 NULL. 추측 금지
  상한_단위       TEXT,
  상한_근거       JSONB   NOT NULL DEFAULT '[]',
  트랙범위        TEXT,          -- 사업 안에서 이 서비스가 다루는 범위
  비고            TEXT,
  활성            BOOLEAN NOT NULL DEFAULT true,
  CONSTRAINT "programs_비목체계_check" CHECK ("비목체계" IN ('창업','RND'))
);

-- 표준 사업명은 corpus.chunks."사업명"(text[]) 의 값과 같다.
-- 별칭은 폴더명·golden_set.사업명·corpus.rules.사업명 에서만 가져온다.
-- 우선규범은 corpus.precedence_rules."우선규범" 을 그대로 옮긴다.
INSERT INTO corpus.programs ("사업명","별칭","전문기관","비목체계","우선규범","트랙범위","비고") VALUES
  ('예비창업패키지',          ARRAY['예비창업','예창패'],                       '창업진흥원','창업', NULL, NULL, NULL),
  ('초기창업패키지',          ARRAY['초기창업','초창패'],                       '창업진흥원','창업', NULL, NULL, NULL),
  ('재도전성공패키지',        ARRAY['재도전','재도전성공'],                     '창업진흥원','창업', NULL, NULL,
     '2026-08-31 현재 corpus.rules 0행. 이후 적재. 구판(2025)이 active 상태였음'),
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
     'RND 비목체계(R&D 비목표 26종)라 창업 비목체계 어휘집 10종과 다르다. 우선순위 조항 없음(precedence_rules 0행). 설계 결정 대기')
ON CONFLICT ("사업명") DO UPDATE SET
  "별칭"     = EXCLUDED."별칭",
  "전문기관" = EXCLUDED."전문기관",
  "비목체계" = EXCLUDED."비목체계",
  "우선규범" = EXCLUDED."우선규범",
  "트랙범위" = EXCLUDED."트랙범위",
  "비고"     = EXCLUDED."비고";
-- 정부지원상한 은 전 행 NULL 이다. 확정 근거를 코퍼스에서 찾지 못했고 추측으로 채우지 않는다.
-- 채울 때는 상한_근거 에 doc_id·조번호를 함께 넣는다.


-- 비목 어휘집. 창업 비목표의 비목이 표준 목록이다 (corpus.rules · check_items 의 비목과 일치).
-- 지급수수료 는 하위항목(기술이전비·멘토링비·사무실임차료 …)을 모두 한 비목으로 묶는다.
CREATE TABLE IF NOT EXISTS corpus.item_vocab (
  비목        TEXT    NOT NULL,
  비목체계    TEXT    NOT NULL DEFAULT '창업',
  별칭        TEXT[]  NOT NULL DEFAULT '{}',
  하위항목    TEXT[]  NOT NULL DEFAULT '{}',
  정렬        INT,
  비고        TEXT,
  CONSTRAINT item_vocab_pkey PRIMARY KEY ("비목", "비목체계"),
  CONSTRAINT "item_vocab_비목체계_check" CHECK ("비목체계" IN ('창업','RND'))
);

INSERT INTO corpus.item_vocab ("비목","비목체계","별칭","하위항목","정렬","비고") VALUES
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
     6, '창업 비목표 6~22 가 이 한 비목으로 묶인다. 하위항목별 한도가 달라 rules 는 하위까지 봐야 한다'),
  ('여비','창업', ARRAY['출장비'], '{}', 7, NULL),
  ('교육훈련비','창업', ARRAY['교육 훈련비'], '{}', 8,
     '통합관리지침 제44조②(본인부담금)·③(4대보험)이 전 사업에 적용된다'),
  ('광고선전비','창업', ARRAY['광고 선전비','마케팅비'], '{}', 9, NULL),
  ('창업활동비','창업', '{}', '{}', 10,
     'corpus.rules 에는 있으나 check_items 에는 없다. 사업별 유무가 갈린다')
ON CONFLICT ("비목", "비목체계") DO UPDATE SET
  "별칭"     = EXCLUDED."별칭",
  "하위항목" = EXCLUDED."하위항목",
  "정렬"     = EXCLUDED."정렬",
  "비고"     = EXCLUDED."비고";
-- TIPS(RND 비목체계) 비목은 이 파일에서 넣지 않는다. 추가할 때는 비목체계='RND' 로 INSERT 만 하면 된다(DDL 변경 불필요).


-- golden_set."사업명" 의미 분리 → 그 뒤 FK.
-- '공통(지침 제14차)' 같은 값은 사업 이름이 아니라 적용범위 표시다. 분리하지 않고 programs FK 를 걸면 실패한다.
-- 원표기를 그대로 "적용범위" 로 옮긴다(정규화하지 않는다). '(지침 제14차)' 는 근거 판본 정보라 버리면 복구가 안 된다.

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


-- eval.runs · eval.run_items · eval.golden_chunks

-- eval.runs : 한 번의 평가 실행. 종류로 e2e / retrieval / judge 를 구분한다.
-- 지표를 jsonb 로 두는 이유: 종류마다 지표가 다르다(hit@5, 일치율, 판단불가율). 컬럼으로 두면 새 지표마다 DDL 이 필요하다.
CREATE TABLE IF NOT EXISTS eval.runs (
  run_id       BIGSERIAL PRIMARY KEY,
  종류         TEXT        NOT NULL,
  시작         TIMESTAMPTZ NOT NULL DEFAULT now(),
  종료         TIMESTAMPTZ,
  코퍼스버전   TEXT,
  git커밋      TEXT,
  설정         JSONB       NOT NULL DEFAULT '{}',   -- 하이퍼파라미터 전체. 재현의 근거
  문항수       INT,
  지표         JSONB       NOT NULL DEFAULT '{}',
  라벨         TEXT,
  비고         TEXT,
  CONSTRAINT "runs_종류_check" CHECK ("종류" IN ('e2e','retrieval','judge'))
);
CREATE INDEX IF NOT EXISTS "ix_runs_종류" ON eval.runs ("종류", "시작" DESC);

-- 문항 단위 결과. 실패한 문항을 찾아 원인을 분석하는 데 쓴다.
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

-- eval.golden_chunks : 골든셋 정답근거와 chunk_id 의 고정 매핑. 매번 원문 부분일치로 재구성하지 않도록 저장한다.
-- (gold_id, 근거순번) 을 PK 로 두면 안 된다. 조문 하나가 여러 청크로 나뉘어 근거 1건이 청크 N개에 대응할 수 있다.
-- 매칭에 실패한 행은 chunk_id 가 NULL 이다. 대리키 + UNIQUE NULLS NOT DISTINCT 로 둘 다 담는다.
CREATE TABLE IF NOT EXISTS eval.golden_chunks (
  gc_id       BIGSERIAL PRIMARY KEY,
  gold_id     BIGINT NOT NULL REFERENCES eval.golden_set(gold_id) ON DELETE CASCADE,
  근거순번    INT    NOT NULL,          -- golden_set."정답근거" 배열 인덱스(0-based)
  chunk_id    BIGINT REFERENCES corpus.chunks(chunk_id) ON DELETE SET NULL,
  article_id  BIGINT REFERENCES corpus.doc_articles(article_id) ON DELETE SET NULL,
  doc_id      TEXT,
  조번호      TEXT,
  매칭방법    TEXT NOT NULL,            -- 원문일치 | 조번호 | 조제목 | 수동 | 실패
  실패사유    TEXT,                     -- 매칭방법='실패' 일 때만. 코퍼스 누락 vs 표기 불일치
  매칭일      TIMESTAMPTZ NOT NULL DEFAULT now(),
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

COMMENT ON COLUMN eval.golden_chunks."조번호" IS
  '골든셋 정답근거의 원표기 그대로(제20조① · [붙임2] …). 청크의 조번호가 아니다 — '
  '비교할 때는 chunk_id 로 corpus.chunks 를 조인해 그쪽 조번호를 쓴다 (2026-09-01 결정).';

COMMIT;
