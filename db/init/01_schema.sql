-- 「써도돼요」 판정 엔진 스키마
-- 출처: Rag_Agent구현 파이프라인.md §5
-- 컨테이너를 처음 만들 때 자동 실행됩니다.

CREATE EXTENSION IF NOT EXISTS vector;

-- ════════════════════════════════════════════════════════════════
-- 1. documents : 문서 대장 (manifest.json 의 DB 표현)
-- ════════════════════════════════════════════════════════════════
CREATE TABLE documents (
    doc_id        TEXT PRIMARY KEY,
    layer         TEXT NOT NULL CHECK (layer IN ('L1','L2','L3','L4','L5','사례')),
    domain        TEXT,          -- 창업지원사업 | 연구비 | 대학혁신지원사업 | 기관운영
    기관ID        TEXT,          -- L4 전용. NULL 이면 전국 공통
    doc_type      TEXT,
    version       TEXT,
    시행일        DATE,
    status        TEXT NOT NULL CHECK (status IN ('active','superseded','reference')),
    apply_mode    TEXT NOT NULL DEFAULT 'apply' CHECK (apply_mode IN ('apply','compare')),
    parse_quality TEXT NOT NULL DEFAULT 'high' CHECK (parse_quality IN ('high','low')),
    src_path      TEXT NOT NULL,
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
    domain      TEXT,
    기관ID      TEXT,
    apply_mode  TEXT NOT NULL DEFAULT 'apply',
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
CREATE INDEX ix_chunks_filter ON chunks (status, apply_mode, layer, 기관ID);
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
    layer         TEXT NOT NULL,   -- L2 | L3 | L4
    기관ID        TEXT,            -- L4 전용
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
