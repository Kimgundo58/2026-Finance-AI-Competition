-- 「써도돼요」 판정 엔진 스키마
-- 출처: RAG.md §2 (구 Rag_Agent구현 파이프라인.md §5)
-- 컨테이너를 처음 만들 때 자동 실행됩니다.
--
-- ⚠️ 2026-08-27 개정 — 계층 체계가 L1~L5 에서 L1/L2/L3 로 바뀌었다.
--    L1 중소벤처기업부(총괄기관) / L2 창업진흥원(전문기관) / L3 주관기관(사용자 업로드)
--    구 표기의 L3(공고·세부관리기준)와 현 L3(주관기관 규정)는 **의미가 다르다.**
--    구 L4·L5 로 적재된 기존 데이터가 있으면 재적재해야 한다.
--    이 파일을 고쳤으면 반드시  docker compose down -v  후  up -d  (init 은 최초 1회만 실행됨)

CREATE EXTENSION IF NOT EXISTS vector;

-- ════════════════════════════════════════════════════════════════
-- 1. documents : 문서 대장 (manifest.json 의 DB 표현)
-- ════════════════════════════════════════════════════════════════
CREATE TABLE documents (
    doc_id        TEXT PRIMARY KEY,
    layer         TEXT NOT NULL CHECK (layer IN ('L1','L2','L3','사례')),
    domain        TEXT,          -- 창업지원사업 | 연구비 | 대학혁신지원사업 | 기관운영
    기관ID        TEXT,          -- L3(주관기관) 전용. NULL 이면 전국 공통
    doc_type      TEXT,
    version       TEXT,
    시행일        DATE,
    status        TEXT NOT NULL CHECK (status IN ('active','superseded','reference')),
    -- 2026-08-28 제거된 컬럼: 근거가 두 번 갈아끼워졌고 두 번째도 무너졌다.
    --   옛 근거(타 대학 24건 제외)      -> scripts/index_guard.py 가 코드로 막는다
    --   새 근거(멀티테넌시)             -> org_id + tenant 스키마 분리 + RLS (RAG.md §2-3)
    --   실사용(저품질 파싱 판정 제외)   -> parse_quality 가 원래 그 필드다
    parse_quality TEXT NOT NULL DEFAULT 'high' CHECK (parse_quality IN ('high','low')),
    -- 텍스트를 어떻게 얻었나. 2026-08-27 추가 — 원칙 4(인용은 생성이 아니라 추출)의 방어선.
    --   native  = PDF/XML 텍스트 레이어 그대로            → A등급 인용 가능
    --   dedupe  = 문자중복 레이어를 dedupe_chars() 로 해소  → A등급 인용 가능 (제14차 지침 등)
    --   hancom  = HWP → 한컴 PDF 변환                     → A등급 인용 가능
    --   vlm     = 스캔 이미지 판독                        → 🔴 A등급 인용 금지. 경고 문구 강제
    extraction    TEXT NOT NULL DEFAULT 'native'
                  CHECK (extraction IN ('native','dedupe','hancom','vlm')),
    src_path      TEXT NOT NULL,
    roles         TEXT[] NOT NULL DEFAULT '{}',   -- manifest 의 role (judgment_index, rule_source, golden_set ...)
    index_target  BOOLEAN NOT NULL DEFAULT FALSE, -- chunks 에 넣을 문서인가
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE documents IS '원본 문서 대장. 인덱서는 status=active 만 읽는다.';

-- ════════════════════════════════════════════════════════════════
-- 2. doc_articles : 조(條) 단위 원문. diff 전용 문서도 여기까지만.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE doc_articles (
    article_id  BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    조번호      TEXT NOT NULL,
    조제목      TEXT,
    조번호_int  INT,             -- 정렬 및 단조성 검증용
    본문        TEXT NOT NULL,
    페이지      INT,
    UNIQUE (doc_id, 조번호)
);
CREATE INDEX ix_articles_doc ON doc_articles (doc_id, 조번호_int);

-- ════════════════════════════════════════════════════════════════
-- 3. chunks : 판정 인덱스 (A등급 근거)
-- ════════════════════════════════════════════════════════════════
CREATE TABLE chunks (
    chunk_id    BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    article_id  BIGINT REFERENCES doc_articles(article_id) ON DELETE CASCADE,
    layer       TEXT NOT NULL,
    기관ID      TEXT,
    -- 저품질 파싱은 판정에서 제외한다. 구 apply_mode='compare' 의 실제 역할이었다.
    -- domain 은 documents 에만 둔다 (인용 시 join). 청크마다 들고 다닐 이유가 없다.
    parse_quality TEXT NOT NULL DEFAULT 'high' CHECK (parse_quality IN ('high','low')),
    version     TEXT,
    status      TEXT NOT NULL,
    조번호      TEXT,
    조제목      TEXT,
    항호        TEXT,
    페이지      INT,
    사업명      TEXT[],          -- NULL 이면 전 사업 공통
    text        TEXT NOT NULL,   -- 원문 그대로. 인용 검증 대상이므로 절대 가공 금지
    embedding   vector(1024)     -- KURE-v1
);
-- pre-filter 가 검색보다 먼저 (구버전·타사업·타기관 차단)
CREATE INDEX ix_chunks_filter ON chunks (status, parse_quality, layer, 기관ID);
CREATE INDEX ix_chunks_사업   ON chunks USING GIN (사업명);
COMMENT ON COLUMN chunks.text IS '원문 문자열 그대로. 인용 검증이 이 값과 대조한다.';
-- ⚠️ ANN 인덱스(HNSW/IVFFlat)를 만들지 않는다.
--    근사 검색의 리콜 손실 = 인용 누락 = 오답. 1만 청크 초과 시 재검토.

-- ════════════════════════════════════════════════════════════════
-- 4. case_chunks : 사례 인덱스 (B등급). 물리적으로 분리한다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE case_chunks (
    case_id     BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    출처도메인  TEXT NOT NULL,   -- R&D | 보조금 | 창업
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    embedding   vector(1024)     -- question 을 임베딩한다 (answer 아님)
);
COMMENT ON TABLE case_chunks IS '판단불가 경로에서만 조회. 판정 근거로 인용 금지.';

-- ════════════════════════════════════════════════════════════════
-- 5. rules : 룰 테이블 (fast path). 벡터 없음.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE rules (
    rule_id       BIGSERIAL PRIMARY KEY,
    layer         TEXT NOT NULL CHECK (layer IN ('L1','L2','L3')),
    기관ID        TEXT,            -- L3(주관기관) 전용
    사업명        TEXT NOT NULL,
    비목          TEXT NOT NULL,
    허용          TEXT NOT NULL CHECK (허용 IN ('가능','조건부','불가')),
    사전승인      BOOLEAN NOT NULL DEFAULT FALSE,
    사전승인_조건 TEXT,
    한도_유형     TEXT CHECK (한도_유형 IN ('비율','금액','개수') OR 한도_유형 IS NULL),
    한도_값       NUMERIC,
    한도_단위     TEXT,
    증빙          TEXT[] NOT NULL DEFAULT '{}',
    금지예시      TEXT[],
    허용예시      TEXT[],
    근거          JSONB NOT NULL DEFAULT '[]',   -- [{doc_id, 조번호}]
    출처도메인    TEXT,            -- R&D 보강분 표시
    verified      BOOLEAN NOT NULL DEFAULT FALSE,
    검수자        TEXT,
    검수일        DATE,
    UNIQUE (layer, 기관ID, 사업명, 비목)
);
CREATE INDEX ix_rules_lookup ON rules (사업명, 비목);
COMMENT ON COLUMN rules.verified IS 'false 인 룰만으로 "가능" 판정 금지. 조문 인용 동반 필수.';

-- ════════════════════════════════════════════════════════════════
-- 5-b. precedence_rules : 우선순위 조항 (2026-08-27 추가)
--      "어느 계층이 이기는가" 를 각 문서의 제3조 부근에서 파싱해 등록한다.
--      비목 룰이 아니라 충돌 해소 룰이다. 상세: rule_base.md §3
-- ════════════════════════════════════════════════════════════════
CREATE TABLE precedence_rules (
    prec_id     BIGSERIAL PRIMARY KEY,
    사업명      TEXT NOT NULL,
    우선계층    TEXT NOT NULL CHECK (우선계층 IN ('L1','L2','L3')),
    열위계층    TEXT NOT NULL CHECK (열위계층 IN ('L1','L2','L3')),
    범위        TEXT NOT NULL DEFAULT 'all'
                CHECK (범위 IN ('all','unspecified_only')),  -- unspecified_only: 상위에 미규정인 사항만 하위 적용
    근거        JSONB NOT NULL DEFAULT '[]',   -- [{doc_id, 조번호}]
    원문        TEXT NOT NULL,                 -- 조항 원문 그대로 (화면 7에서 인용)
    verified    BOOLEAN NOT NULL DEFAULT FALSE,
    검수자      TEXT,
    검수일      DATE,
    UNIQUE (사업명, 우선계층, 열위계층)
);
CREATE INDEX ix_prec_lookup ON precedence_rules (사업명);
COMMENT ON TABLE precedence_rules IS
  '우선순위 조항. 없으면 폴백(상위 규범 우선 + 엄격한 값 우선). 사업별로 문구가 달라 사람 검수 대상.';
COMMENT ON COLUMN precedence_rules.범위 IS
  'all = 항상 우선계층이 이김(재도전성공패키지 제3조) / unspecified_only = 상위에 없는 사항만 하위 적용(초격차 제3조)';

-- ════════════════════════════════════════════════════════════════
-- 5-c. refs : 참조 그래프 (2026-08-27 추가)
--      "어느 조가 어느 조를 가리키는가". 한 행 = 엣지 하나.
--      그래프 DB(Neo4j 등) 불필요 — 깊이 3, 재귀 CTE 로 밀리초.
--      RAG 의 존재 이유가 여기다: 사용자 문서만으로 답이 안 될 때
--      "제33조에 따른다" 를 따라가 실제 조항에 닿는다. 상세: 파이프라인 §6.3
-- ════════════════════════════════════════════════════════════════
CREATE TABLE refs (
    ref_id      BIGSERIAL PRIMARY KEY,
    src_doc_id  TEXT NOT NULL,
    src_조번호  TEXT NOT NULL,
    참조문자열  TEXT NOT NULL,   -- 원문 표기 그대로: "통합관리지침 제33조부터 제42조까지"
    관계        TEXT NOT NULL CHECK (관계 IN ('위임','준용','별표참조','인용','미규정위임')),
    dst_doc_id  TEXT,            -- dangling 이면 NULL
    dst_조번호  TEXT,
    해소상태    TEXT NOT NULL CHECK (해소상태 IN ('resolved','shifted','dangling')),
    보정근거    TEXT,            -- shifted 일 때: "조제목 '기계장치, 공구·기구' 로 재매칭"
    depth_hint  INT,             -- 위임 계통 복원 시의 단계 (있으면)
    발견일      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_doc_id, src_조번호, 참조문자열)
);
CREATE INDEX ix_refs_src ON refs (src_doc_id, src_조번호);
CREATE INDEX ix_refs_dst ON refs (dst_doc_id, dst_조번호);   -- 역참조("누가 나를 인용했나")
CREATE INDEX ix_refs_dangling ON refs (해소상태) WHERE 해소상태 = 'dangling';

COMMENT ON TABLE  refs IS
  '참조 그래프. 진입점에서 깊이 3까지 폐포를 수집해 LLM 컨텍스트에 싣는다. LLM 은 이 표를 보지 않는다 — 코드가 조회해 텍스트로 먹인다.';
COMMENT ON COLUMN refs.해소상태 IS
  'resolved=정상 / shifted=조번호가 구판이라 조제목으로 재매칭함 / dangling=코퍼스에 대상 없음(판단불가 예고)';
COMMENT ON COLUMN refs.관계 IS
  '미규정위임 = "이 지침에서 정하지 아니한 사항은 ~에 따름". 건국대 지침 실측 문형. 게이팅(파이프라인 §6.2)의 근거가 된다.';

-- ════════════════════════════════════════════════════════════════
-- 6. item_alias : 상품명 → 비목 매핑 사전. 여기만 벡터가 필요.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE item_alias (
    alias_id    BIGSERIAL PRIMARY KEY,
    상품명      TEXT NOT NULL,
    비목        TEXT NOT NULL,
    사업명      TEXT,
    출처        TEXT,            -- seed | 사용자질문 | 센터답변
    embedding   vector(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_alias_name ON item_alias (상품명);

-- ════════════════════════════════════════════════════════════════
-- 7. decisions : 판정 로그 (A5 · A6 대시보드의 데이터 원천)
-- ════════════════════════════════════════════════════════════════
CREATE TABLE decisions (
    decision_id BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    사업명      TEXT,
    기관ID      TEXT,
    질문원문    TEXT NOT NULL,
    정규화      JSONB,           -- ① 단계 출력
    비목        TEXT,
    금액        NUMERIC,
    판정        TEXT CHECK (판정 IN ('가능','조건부','불가','판단불가') OR 판정 IS NULL),
    신뢰등급    TEXT CHECK (신뢰등급 IN ('A','B') OR 신뢰등급 IS NULL),
    인용        JSONB,
    해야할일    JSONB,
    지연ms      JSONB,           -- 단계별 소요시간
    모델        JSONB            -- {정규화: ..., 조립: ...}
);
CREATE INDEX ix_decisions_time ON decisions (created_at DESC);

-- ════════════════════════════════════════════════════════════════
-- 8. golden_set : 평가용 정답지. 인덱스에서 격리한다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE golden_set (
    gold_id     BIGSERIAL PRIMARY KEY,
    출처        TEXT NOT NULL,   -- 별첨4 | 직접작성 | 연구재단홀드아웃
    사업명      TEXT,
    질문        TEXT NOT NULL,
    정답판정    TEXT NOT NULL,
    정답근거    JSONB,
    채점대상    TEXT[] NOT NULL DEFAULT '{판정일치율,인용정확도}'
);
COMMENT ON TABLE golden_set IS '절대 chunks / case_chunks 에 넣지 말 것. 정답 유출.';

-- ════════════════════════════════════════════════════════════════
-- 9. xref_mismatch : 크로스 레퍼런스 불일치 (파이프라인 §2.5)
-- ════════════════════════════════════════════════════════════════
CREATE TABLE xref_mismatch (
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

-- ════════════════════════════════════════════════════════════════
-- 확인용 뷰 : pgweb 에서 한눈에 적재 현황 보기
-- ════════════════════════════════════════════════════════════════
CREATE VIEW v_적재현황 AS
SELECT '1. documents'   AS 테이블, count(*) AS 건수 FROM documents
UNION ALL SELECT '2. doc_articles',  count(*) FROM doc_articles
UNION ALL SELECT '3. chunks',        count(*) FROM chunks
UNION ALL SELECT '4. case_chunks',   count(*) FROM case_chunks
UNION ALL SELECT '5. rules',         count(*) FROM rules
UNION ALL SELECT '6. item_alias',    count(*) FROM item_alias
UNION ALL SELECT '7. decisions',     count(*) FROM decisions
UNION ALL SELECT '8. golden_set',    count(*) FROM golden_set
UNION ALL SELECT '9. xref_mismatch', count(*) FROM xref_mismatch
ORDER BY 1;
