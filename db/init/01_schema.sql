-- 써도돼요 판정 엔진의 기본 스키마. 스키마 4개와 핵심 테이블, RLS 정책을 만든다.
--   corpus      공개 규정·룰·참조 그래프. 전 사용자 공통, RLS 없음
--   tenant      L3·계정·기업 프로필·판정 로그. org 별 격리, RLS 필수
--   eval        골든셋과 평가 결과. 운영 DB 로 옮기지 않는다
--   extensions  pgvector. public 은 비워 둔다
-- 실행 시점: init. /docker-entrypoint-initdb.d 는 볼륨이 비어 있을 때만 실행된다.

CREATE SCHEMA corpus;
CREATE SCHEMA tenant;
CREATE SCHEMA eval;
CREATE SCHEMA extensions;

-- pgvector 를 extensions 스키마에 둔다. pg_dump 의 `extensions.vector(1024)` 가 그대로 복원된다.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

SET search_path = corpus, tenant, eval, extensions, public;

-- 스크립트가 테이블명을 스키마 없이 쓰므로 DB 기본 search_path 를 설정한다.
DO $do$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET search_path = corpus, tenant, eval, extensions, public',
        current_database());
END
$do$;


-- corpus.documents : 문서 대장
CREATE TABLE corpus.documents (
    doc_id        TEXT PRIMARY KEY,
    layer         TEXT NOT NULL CHECK (layer IN ('L1','L2','L3','사례')),
    domain        TEXT,          -- 창업지원사업 | 연구비 | 대학혁신지원사업 | 기관운영
    기관ID        TEXT,          -- L3(주관기관) 전용. NULL 이면 전국 공통
    doc_type      TEXT,
    version       TEXT,
    시행일        DATE,
    status        TEXT NOT NULL CHECK (status IN ('active','superseded','reference')),
    parse_quality TEXT NOT NULL DEFAULT 'high' CHECK (parse_quality IN ('high','low')),
    -- 텍스트를 어떻게 얻었나. native/dedupe/hancom 은 A등급 인용 가능, vlm(스캔본 VLM 추출)은 A등급 인용 금지
    extraction    TEXT NOT NULL DEFAULT 'native'
                  CHECK (extraction IN ('native','dedupe','hancom','vlm')),
    src_path      TEXT NOT NULL,
    index_target  BOOLEAN NOT NULL DEFAULT FALSE, -- chunks 에 넣을 문서인가
    -- 검색대상 = 임베딩·BM25 검색 후보 / 참조확장전용 = 참조 확장으로만 들어감(검색 후보 아님). doc_articles + refs 로만 도달하고 인용은 가능하다
    retrieval_scope TEXT NOT NULL DEFAULT '검색대상'
                    CHECK (retrieval_scope IN ('검색대상','참조확장전용')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE corpus.documents IS '원본 문서 대장. 인덱서는 status=active 만 읽는다.';
COMMENT ON COLUMN corpus.documents.retrieval_scope IS
  '검색대상 = 검색 후보 / 참조확장전용 = refs 로만 도달. 범위를 줄일 때는 측정(Recall@5) 후 UPDATE 로 확정한다.';

-- corpus.doc_articles : 조(條) 단위 원문. 참조 확장 전용 문서도 여기에는 들어간다.
CREATE TABLE corpus.doc_articles (
    article_id  BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES corpus.documents(doc_id) ON DELETE CASCADE,
    조번호      TEXT NOT NULL,
    조제목      TEXT,
    조순번      INT,             -- 정렬 및 조 번호 증가 순서 검사용
    본문        TEXT NOT NULL,
    페이지      INT,
    -- `제N조 삭제 <날짜>` 플래그. 빈 조가 아니라 원문 사실이라 검색·청킹에서 뺀다
    삭제        BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (doc_id, 조번호)
);
CREATE INDEX ix_articles_doc ON corpus.doc_articles (doc_id, 조순번);
-- 참조 확장 조회(refs.dst → 원문)용 인덱스
CREATE INDEX ix_articles_lookup ON corpus.doc_articles (doc_id, 조번호);

-- corpus.chunks : 검색 인덱스(A등급 근거). L1·L2 만 넣는다. L3·골든셋은 넣지 않는다.
CREATE TABLE corpus.chunks (
    chunk_id    BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES corpus.documents(doc_id) ON DELETE CASCADE,
    article_id  BIGINT REFERENCES corpus.doc_articles(article_id) ON DELETE CASCADE,
    layer       TEXT NOT NULL,
    기관ID      TEXT,
    -- 저품질 파싱은 판정에서 제외한다. domain 은 documents 에만 둔다 (인용 시 join)
    parse_quality TEXT NOT NULL DEFAULT 'high' CHECK (parse_quality IN ('high','low')),
    version     TEXT,
    status      TEXT NOT NULL,
    -- documents 에서 복사한 값. 사전 필터를 테이블 하나만 스캔해 처리하려는 비정규화
    retrieval_scope TEXT NOT NULL DEFAULT '검색대상'
                    CHECK (retrieval_scope IN ('검색대상','참조확장전용')),
    조번호      TEXT,
    조제목      TEXT,
    항호        TEXT,
    페이지      INT,
    사업명      TEXT[],          -- NULL 이면 전 사업 공통
    -- 태깅 단계가 채운다. 검색은 항상 적용대상 IN ('창업기업','공통') 필터
    적용대상    TEXT CHECK (적용대상 IN ('주관기관','창업기업','공통') OR 적용대상 IS NULL),
    text        TEXT NOT NULL,   -- 원문 그대로. 인용 검증 대상이므로 가공 금지
                                 -- 임베딩·BM25 입력은 text 가 아니라 [컨텍스트 헤더]+text
    embedding   extensions.vector(1024)     -- KURE-v1
);
-- 검색 전에 사전 필터로 구버전·타사업·타기관·참조 확장 전용 청크를 제외한다
CREATE INDEX ix_chunks_filter ON corpus.chunks
    (status, parse_quality, retrieval_scope, layer, 적용대상, 기관ID);
CREATE INDEX ix_chunks_사업명 ON corpus.chunks USING GIN (사업명);
COMMENT ON COLUMN corpus.chunks.text IS '원문 문자열 그대로. 인용 검증이 이 값과 대조한다.';
-- ANN 인덱스(HNSW/IVFFlat)를 만들지 않는다 — 근사 검색의 재현율 저하는 곧 인용 누락이다. 1만 청크 초과 시 재검토.

-- corpus.rules : 룰 테이블. 검색 없이 코드가 (사업명, 비목)으로 바로 조회한다. 벡터 없음.
CREATE TABLE corpus.rules (
    rule_id       BIGSERIAL PRIMARY KEY,
    layer         TEXT NOT NULL CHECK (layer IN ('L1','L2','L3')),
    기관ID        TEXT,            -- L3(주관기관) 전용
    사업명        TEXT NOT NULL,
    비목          TEXT NOT NULL,
    허용          TEXT NOT NULL CHECK (허용 IN ('가능','조건부','불가')),
    사전승인      BOOLEAN NOT NULL DEFAULT FALSE,
    -- 한 행이 조건을 둘 이상 담는다 (L1·L2 병합, 룰 제안 append). NULL 은 NULL 로 둔다
    사전승인_조건 TEXT[],
    한도_유형     TEXT CHECK (한도_유형 IN ('비율','금액','개수') OR 한도_유형 IS NULL),
    한도_값       NUMERIC,
    한도_단위     TEXT,
    증빙          TEXT[] NOT NULL DEFAULT '{}',
    금지예시      TEXT[],
    허용예시      TEXT[],
    근거          JSONB NOT NULL DEFAULT '[]',   -- [{doc_id, 조번호}]
    출처도메인    TEXT,            -- R&D 추가분 표시
    verified      BOOLEAN NOT NULL DEFAULT FALSE,
    검수자        TEXT,
    검수일        DATE,
    UNIQUE (layer, 기관ID, 사업명, 비목)
);
CREATE INDEX ix_rules_lookup ON corpus.rules (사업명, 비목);

-- TRUNCATE 금지 트리거와 저장 전 검증 트리거는 16_rules_guard.sql 에 있다.
COMMENT ON COLUMN corpus.rules.verified IS 'false 인 룰만으로 "가능" 판정 금지. 조문 인용 동반 필수.';
-- layer='L3' 행은 기관 데이터지만 상위 룰에 L3 룰을 병합하는 설계(L3 병합)라 corpus 에 둔다.

-- corpus.precedence_rules : 우선순위 조항. 어느 계층이 우선하는지를 담는 충돌 해결 룰이다(비목 룰 아님).
CREATE TABLE corpus.precedence_rules (
    prec_id     BIGSERIAL PRIMARY KEY,
    사업명      TEXT NOT NULL,
    우선계층    TEXT NOT NULL CHECK (우선계층 IN ('L1','L2','L3')),
    후순위계층  TEXT NOT NULL CHECK (후순위계층 IN ('L1','L2','L3')),
    범위        TEXT NOT NULL DEFAULT 'all'
                CHECK (범위 IN ('all','unspecified_only')),  -- unspecified_only: 상위에 미규정인 사항만 하위 적용
    -- 상위 규정이 사업마다 다르다 (모두의창업 = 운영요령). 'L1 > L2' 로 일반화하지 않는다
    우선규범    TEXT,
    근거        JSONB NOT NULL DEFAULT '[]',   -- [{doc_id, 조번호}]
    원문        TEXT NOT NULL,                 -- 조항 원문 그대로 (화면에서 인용)
    해석        TEXT,                          -- 사람이 읽는 한 줄 (build_precedence.py 산출)
    verified    BOOLEAN NOT NULL DEFAULT FALSE,
    검수자      TEXT,
    검수일      DATE,
    UNIQUE (사업명, 우선계층, 후순위계층)
);
CREATE INDEX ix_prec_lookup ON corpus.precedence_rules (사업명);
COMMENT ON TABLE corpus.precedence_rules IS
  '우선순위 조항. 없으면 폴백(상위 규정 우선 + 엄격한 값 우선). 사업별로 문구가 달라 사람 검수 대상.';
COMMENT ON COLUMN corpus.precedence_rules.범위 IS
  'all = 항상 우선계층을 적용(재도전성공패키지 제3조) / unspecified_only = 상위에 없는 사항만 하위 적용(초격차 제3조)';
-- 조회 키가 사업명이다. 모두의창업 로컬트랙(제3편)은 위임 관계가 달라 범위에서 제외한다.

-- corpus.refs : 참조 그래프. 한 행 = 엣지 하나 ("어느 조가 어느 조를 가리키는가"). 재귀 CTE 로 조회하고, 참조 확장 깊이는 1이다(scripts/retrieve.py 깊이).
CREATE TABLE corpus.refs (
    ref_id      BIGSERIAL PRIMARY KEY,
    src_doc_id  TEXT NOT NULL,
    src_조번호  TEXT NOT NULL,
    -- 참조를 건 조의 규정 계층. build_refs.py 가 계층별 해결 상태 집계에 쓴다
    src_layer   TEXT CHECK (src_layer IN ('L1','L2','L3','사례') OR src_layer IS NULL),
    참조문자열  TEXT NOT NULL,   -- 원문 표기 그대로: "통합관리지침 제33조부터 제42조까지"
    관계        TEXT NOT NULL CHECK (관계 IN ('위임','준용','별표참조','인용','미규정위임')),
    dst_doc_id  TEXT,            -- dangling 이면 NULL
    dst_조번호  TEXT,
    해결상태    TEXT NOT NULL CHECK (해결상태 IN ('resolved','remapped','dangling')),
    해결근거    TEXT,            -- remapped 일 때: "조제목 '기계장치, 공구·기구' 로 재매칭"
    depth_hint  INT,             -- 위임 관계를 재구성할 때의 단계 (있으면)
    발견일      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_doc_id, src_조번호, 참조문자열)
);
CREATE INDEX ix_refs_src ON corpus.refs (src_doc_id, src_조번호);
CREATE INDEX ix_refs_dst ON corpus.refs (dst_doc_id, dst_조번호);   -- 역참조("누가 나를 인용했나")
CREATE INDEX ix_refs_dangling ON corpus.refs (해결상태) WHERE 해결상태 = 'dangling';

COMMENT ON TABLE  corpus.refs IS
  '참조 그래프. 검색된 조에서 깊이 1까지 참조 확장 대상을 모아 LLM 컨텍스트에 넣는다. LLM 은 이 표를 보지 않는다 — 코드가 조회해 텍스트로 넣는다.';
COMMENT ON COLUMN corpus.refs.해결상태 IS
  'resolved=정상 / remapped=조번호가 구판이라 조제목으로 재매칭함 / dangling=코퍼스에 대상 없음(판단불가가 될 수 있음)';
COMMENT ON COLUMN corpus.refs.관계 IS
  '미규정위임 = "이 지침에서 정하지 아니한 사항은 ~에 따름". 건국대 지침에서 확인한 문장 형식. L3 상위 규정 확인(docs/5_룰/5-2_효력_결정.md 3절)의 근거가 된다.';

-- BM25 역색인. kiwipiepy 분석은 앱이 색인 시점에 하고, DB 는 토큰을 세고 정렬만 한다.
-- 재인덱싱 = TRUNCATE; INSERT; REFRESH MATERIALIZED VIEW; COMMIT (트랜잭션 하나)
CREATE TABLE corpus.chunk_terms (
    chunk_id BIGINT NOT NULL REFERENCES corpus.chunks(chunk_id) ON DELETE CASCADE,
    term     TEXT   NOT NULL,
    tf       INT    NOT NULL,
    PRIMARY KEY (chunk_id, term)
);
CREATE INDEX ix_terms_term ON corpus.chunk_terms (term);

CREATE TABLE corpus.chunk_len (
    chunk_id BIGINT PRIMARY KEY REFERENCES corpus.chunks(chunk_id) ON DELETE CASCADE,
    dl       INT NOT NULL
);

CREATE MATERIALIZED VIEW corpus.term_df AS
    SELECT term, count(*)::int AS df FROM corpus.chunk_terms GROUP BY term;

-- corpus.item_alias : 품목 → 비목 매핑 사전. 여기만 벡터가 필요.
CREATE TABLE corpus.item_alias (
    alias_id    BIGSERIAL PRIMARY KEY,
    품목        TEXT NOT NULL,
    비목        TEXT NOT NULL,   -- 비목 어휘집 enum
    사업명      TEXT,
    -- 별칭을 얻은 곳: 규정발췌_* | 비목명종합 | 금지예시 | 허용예시 | item_vocab | 삭제후보_* | 복원후보_* | 보류_*
    출처        TEXT,
    embedding   extensions.vector(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 별칭의 근거. 원문 대조가 안 된 행은 NULL 로 둔다
    원문발췌  TEXT,
    doc_id    TEXT,
    조번호    TEXT
);
CREATE INDEX ix_alias_name ON corpus.item_alias (품목);

-- corpus.xref_mismatch : 교차 참조 불일치
CREATE TABLE corpus.xref_mismatch (
    id          BIGSERIAL PRIMARY KEY,
    src_doc_id  TEXT,
    src_조번호  TEXT,
    참조문자열  TEXT,            -- 예: "지침 제33조"
    해석_조제목 TEXT,            -- 현행에서 그 번호의 조 제목
    기대_조제목 TEXT,            -- 문맥상 기대되는 제목
    현행_조번호 TEXT,            -- 제목으로 재매칭한 결과
    상태        TEXT NOT NULL DEFAULT 'mismatch' CHECK (상태 IN ('mismatch','resolved')),
    발견일      TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- tenant : 기관별 데이터. RLS 는 2차 방어선이고 1차는 코드(앱이 항상 org_id 조건을 명시한다).
--   로컬 postgres 는 슈퍼유저라 RLS 를 우회한다. 정책은 비특권 롤(suddoe_app)에만 적용된다.

-- PostgREST 계열은 request.jwt.claims 를, 앱은 GUC app.org_id 를 쓴다. 한 함수에서 둘 다 읽는다.
CREATE FUNCTION tenant.current_org() RETURNS uuid
LANGUAGE plpgsql STABLE AS $fn$
DECLARE v text;
BEGIN
    BEGIN
        v := current_setting('request.jwt.claims', true)::json ->> 'org_id';
    EXCEPTION WHEN others THEN
        v := NULL;
    END;
    IF v IS NULL OR v = '' THEN
        v := nullif(current_setting('app.org_id', true), '');
    END IF;
    RETURN v::uuid;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$fn$;

-- 계정 단위 격리용. 앱은 GUC app.account_id 를 app.org_id 와 같은 트랜잭션에 설정한다. 없으면 NULL(데모 세션·게스트).
-- 기업 데이터(프로필·지출계획·할일·판정·캐시)는 org 와 account 가 둘 다 맞아야 보인다. L3 는 org 단위다.
CREATE FUNCTION tenant.current_account() RETURNS uuid
LANGUAGE plpgsql STABLE AS $fn$
DECLARE v text;
BEGIN
    BEGIN
        v := current_setting('request.jwt.claims', true)::json ->> 'account_id';
    EXCEPTION WHEN others THEN
        v := NULL;
    END;
    IF v IS NULL OR v = '' THEN
        v := nullif(current_setting('app.account_id', true), '');
    END IF;
    RETURN v::uuid;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$fn$;

CREATE TABLE tenant.orgs (
    org_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    기관명      TEXT NOT NULL,
    사업명      TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenant.l3_documents (
    doc_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    원본파일명   TEXT NOT NULL,
    version      TEXT,
    시행일       DATE,
    -- 기관당 판정에 쓰는 문서는 하나다. 새 문서 파싱이 성공하면 이전 문서를 superseded 로 바꾼다
    -- (scripts/l3_parse.적용문서_정리). active 로 남는 것은 적용 중 1건, 처리 중, 최근 실패 1건이다.
    status       TEXT NOT NULL CHECK (status IN ('active','superseded')),
    -- hwpx/hwp 는 L3 사용자 업로드 파서 경로. ocr 은 스캔본 PDF 를 CLOVA OCR 로 읽은 문서(인용 신뢰등급 B)
    extraction   TEXT NOT NULL DEFAULT 'native'
                 CHECK (extraction IN ('native','dedupe','hancom','vlm','hwpx','hwp','ocr')),
    -- 'pending' = 접수만 됐고 파싱 평가 전. 'warn'(평가했는데 의심스러움)과 구분한다
    파싱품질     TEXT NOT NULL CHECK (파싱품질 IN ('pending','pass','warn','fail')),
    미해결참조수 INT NOT NULL DEFAULT 0,   -- 업로드 시점에 알린다 (판정 시점 아님)
    -- 파싱 실패 분류 코드(스캔본·보호문서·조문없음 등, scripts/l3_parse.py). 안내 문구는 server/routes_l3.py
    실패사유     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 벡터 컬럼이 없다. 검색하지 않고 전체를 로드한다. corpus.chunks 와 다른 테이블이라 기관 간 데이터 유출이 구조적으로 불가능하다.
CREATE TABLE tenant.l3_articles (
    article_id  BIGSERIAL PRIMARY KEY,
    doc_id      UUID NOT NULL REFERENCES tenant.l3_documents(doc_id) ON DELETE CASCADE,
    org_id      UUID NOT NULL,          -- RLS 를 위해 비정규화
    조번호      TEXT NOT NULL,
    조제목      TEXT,
    조순번      INT,
    장          TEXT,                   -- 사업비 관련 장만 로드한다
    본문        TEXT NOT NULL,
    페이지      INT,
    UNIQUE (doc_id, 조번호)
);
CREATE INDEX ix_l3_org ON tenant.l3_articles (org_id, doc_id, 조순번);

-- 로그인 계정. 개인 이름은 저장하지 않는다 — 인력은 역할·수치만 둔다.
CREATE TABLE tenant.accounts (
    account_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    email       TEXT NOT NULL UNIQUE,
    -- 비밀번호는 Supabase Auth 가 관리한다. 이 테이블은 (email → org_id) 만 담는다. 자체 로그인을 쓸 때만 채운다
    pw_hash     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 기업 프로필: 협약 정보와 과업범위 요약. 계정의 사업(협약) 1건 = 1행.
CREATE TABLE tenant.profile_agreements (
    profile_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    -- NULL = 계정 없는 기관(데모 세션). 같은 기관의 계정마다 따로 저장한다
    account_id      UUID REFERENCES tenant.accounts(account_id) ON DELETE CASCADE,
    사업명          TEXT NOT NULL,
    협약시작일      DATE,
    협약종료일      DATE,
    -- 재원(정부지원·자기부담) x 형태(현금·현물). 정부지원_현물은 04_validation_eval.sql 이 지운다.
    -- 자기부담_현물 = 현물 총액(NULL = 미입력). 총사업비 = 정부지원_현금 + 자기부담_현금 + 자기부담_현물
    정부지원_현금   NUMERIC, 정부지원_현물 NUMERIC,
    자기부담_현금   NUMERIC, 자기부담_현물 NUMERIC,
    과업범위요약    TEXT,          -- 사업계획서의 과업범위 요약. 원문은 저장하지 않는다
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_profile_agreements_key UNIQUE NULLS NOT DISTINCT (org_id, account_id, 사업명)
);

-- 기업 프로필: 집행내역. 집계 기준 3개(비목별·거래처별·인별월별)가 이 한 테이블에서 나온다.
CREATE TABLE tenant.profile_expenditures (
    exec_id     BIGSERIAL PRIMARY KEY,
    profile_id  UUID NOT NULL REFERENCES tenant.profile_agreements(profile_id) ON DELETE CASCADE,
    비목        TEXT NOT NULL,                 -- 비목 어휘집 enum
    재원        TEXT NOT NULL CHECK (재원 IN ('정부지원','자기부담')),
    형태        TEXT NOT NULL CHECK (형태 IN ('현금','현물')),
    거래처      TEXT,                          -- 동일 거래처 누적(2천만원 심의)용
    인력역할    TEXT,                          -- 인별월별 집계용. 이름 아님
    귀속월      DATE,
    금액        NUMERIC NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_exec_agg ON tenant.profile_expenditures (profile_id, 비목, 재원, 형태);

-- 기업 프로필: 참여인력. 참여율 상한이 소속기관 유형(100%/130%)에 따라 다르다.
CREATE TABLE tenant.profile_personnel (
    person_id     BIGSERIAL PRIMARY KEY,
    profile_id    UUID NOT NULL REFERENCES tenant.profile_agreements(profile_id) ON DELETE CASCADE,
    역할          TEXT NOT NULL,               -- 대표자 | 신규채용 | 기존직원 ...
    고용형태      TEXT,
    타사업참여율  NUMERIC,
    소속기관유형  TEXT,
    겸직          BOOLEAN
);

-- 미매핑 전제 집계. 자주 나오는 항목을 프로필 필드로 추가하기 위한 표다. 현재 앱은 쓰지 않는다.
CREATE TABLE tenant.unmapped_premise (
    id           BIGSERIAL PRIMARY KEY,
    premise_text TEXT NOT NULL,
    근거조항     JSONB,
    사업명       TEXT,
    비목         TEXT,
    발생횟수     INT NOT NULL DEFAULT 1,
    최초발생     TIMESTAMPTZ NOT NULL DEFAULT now(),
    최근발생     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (premise_text, 사업명, 비목)
);

-- tenant.decisions : 판정 로그. corpus 가 아니라 tenant 다 — 전제·검색스냅샷에 기업 프로필 정보가 남는다.
--   게스트 판정은 org_id IS NULL 로 남고 클라이언트에서는 읽히지 않는다 (서버 롤만).
CREATE TABLE tenant.decisions (
    decision_id BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    org_id      UUID REFERENCES tenant.orgs(org_id) ON DELETE SET NULL,  -- NULL = 게스트
    -- NULL = 게스트 또는 계정 없는 기관(데모 세션). 계정을 지워도 판정 기록은 남긴다
    account_id  UUID REFERENCES tenant.accounts(account_id) ON DELETE SET NULL,
    사업명      TEXT,
    기관ID      TEXT,
    질문원문    TEXT NOT NULL,
    정규화      JSONB,           -- 정규화 단계 출력
    비목        TEXT,
    금액        NUMERIC,
    판정        TEXT CONSTRAINT "decisions_판정_check" CHECK (판정 IN ('가능','확인 필요','불가') OR 판정 IS NULL),   -- 3값(2026-11-12_판정_3값.sql)
    신뢰등급    TEXT CHECK (신뢰등급 IN ('A','B') OR 신뢰등급 IS NULL),
    인용        JSONB,
    해야할일    JSONB,
    지연ms      JSONB,           -- 단계별 소요시간
    모델        JSONB,           -- LLM 호출 정보 {정규화, 판정, 호출수, 판정지연ms, 프롬프트버전}
    -- 재현성: 아래 세 컬럼이 없으면 판정 이력을 나중에 설명할 수 없다
    전제        JSONB,           -- 검증 단계를 통과한 전제 목록 (LLM 원출력이 아니다)
    검색스냅샷  JSONB,           -- 문장맵(문장 ID→chunk_id|article_id, 항호) + top-k 목록
    코퍼스버전  TEXT             -- 판정 시점의 인덱스 버전 문자열
);
CREATE INDEX ix_decisions_time ON tenant.decisions (created_at DESC);

-- RLS
ALTER TABLE tenant.orgs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.l3_documents    ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.l3_articles     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.accounts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.profile_agreements   ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.profile_expenditures ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.profile_personnel    ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.unmapped_premise ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.decisions       ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation ON tenant.orgs          USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.l3_documents  USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.l3_articles   USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.accounts      USING (org_id = tenant.current_org());
-- 기업 데이터는 계정 단위다. 데모 세션은 account_id 가 NULL 이라 IS NOT DISTINCT FROM 으로 비교한다
CREATE POLICY account_isolation ON tenant.profile_agreements USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());
CREATE POLICY account_isolation ON tenant.decisions USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());
-- profile_id 를 거쳐 격리한다
CREATE POLICY account_isolation ON tenant.profile_expenditures USING (
    profile_id IN (SELECT profile_id FROM tenant.profile_agreements
                    WHERE org_id = tenant.current_org()
                      AND account_id IS NOT DISTINCT FROM tenant.current_account()));
CREATE POLICY account_isolation ON tenant.profile_personnel USING (
    profile_id IN (SELECT profile_id FROM tenant.profile_agreements
                    WHERE org_id = tenant.current_org()
                      AND account_id IS NOT DISTINCT FROM tenant.current_account()));
-- unmapped_premise 는 org 컬럼이 없는 전체 통계라 정책 없이 RLS 만 켠다. suddoe_app 은 읽기 0행, 쓰기 거부다.


-- eval : 골든셋과 평가 결과. 운영 DB 로 옮기지 않는다 (운영 덤프는 corpus·tenant 스키마만 포함한다).
CREATE TABLE eval.golden_set (
    gold_id     BIGSERIAL PRIMARY KEY,
    세트        TEXT NOT NULL,   -- 기본 | 함정질문 | 유형보충 | 공식질의응답 | L3
    no          TEXT,            -- 원본 문항 번호 (함정질문은 A1~)
    사업명      TEXT,
    질문        TEXT NOT NULL,
    정답판정    TEXT NOT NULL CHECK (정답판정 IN ('가능','조건부','불가','판단불가')),
    정답근거    JSONB,
    근거원문    TEXT,
    해야할일    JSONB,
    채점대상    TEXT[] NOT NULL DEFAULT '{판정일치율,인용정확도}',
    verified    BOOLEAN NOT NULL DEFAULT FALSE,
    검수메모    TEXT
);
COMMENT ON TABLE eval.golden_set IS
  '절대 corpus.chunks 에 넣지 말 것 (정답 유출). Supabase 덤프 대상도 아니다.';


-- 권한: public 은 비워 두고, corpus/tenant/eval 은 PUBLIC 롤에서 회수한다.
--   앱 롤 권한은 10b_app_grants.sql 이 부여한다.
REVOKE ALL ON SCHEMA corpus, tenant, eval FROM PUBLIC;

-- 적재 현황 확인용 뷰 corpus.v_적재현황 은 모든 테이블이 생긴 뒤 17_check_items_questions.sql 에서 만든다.
