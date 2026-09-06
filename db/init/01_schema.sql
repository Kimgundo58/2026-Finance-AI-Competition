-- 「써도돼요」 판정 엔진 스키마
-- 정본 사양: RAG.md §2 (스키마 2분할·DDL) · rule_base.md §2~3 · 서비스 아키텍쳐.md §3
-- 컨테이너를 "처음" 만들 때만 자동 실행된다.
--   ⚠️ 이 파일을 고쳤으면 반드시  docker compose down -v  후  up -d
--      (/docker-entrypoint-initdb.d 는 볼륨이 비었을 때만 돈다. 재시작으로는 절대 반영되지 않는다)
--
-- ════════════════════════════════════════════════════════════════════════════
-- 2026-08-31 개정 — 스키마 3분할 + 컬럼 4건
-- ════════════════════════════════════════════════════════════════════════════
-- (1) 🔴 스키마 분할. 이전까지 전 테이블이 public 에 있었다.
--     Supabase 에서 public 은 PostgREST 가 자동으로 REST API 로 노출하는 스키마다.
--     anon 키만 있으면 브라우저에서  GET /rest/v1/golden_set  으로 정답지가 나간다.
--     index_guard.py 는 인덱스 투입을 막지 API 노출을 막지 않는다 — 방어선이 여기서 뚫린다.
--
--       corpus      공개 규범·룰·refs.  전 사용자 공통, RLS 없음.  PostgREST 미노출
--       tenant      L3·계정·F축·판정로그. org 별 격리, RLS 필수.   PostgREST 미노출
--       eval        골든셋.  🔴 Supabase 로 아예 올리지 않는다 (pg_dump 대상 제외)
--       extensions  pgvector.  Supabase 와 같은 배치라 덤프가 그대로 복원된다
--       public      비워둔다
--
--     정책이 필요한 것만 tenant 한 곳에 모아 전부 건다 — 벡터 검색에 정책 평가가 붙지 않는다.
--     골든셋을 eval 로 뺀 이유: corpus 미노출 설정으로 막는 것보다 안 올리는 게 낫다.
--     앱은 런타임에 골든셋을 읽지 않는다. 노출면 자체를 없앤다.
--
-- (2) 컬럼 4건
--     corpus.doc_articles.삭제          stage0_run 이 붙이는 `제N조 삭제 <날짜>` 플래그.
--                                       빈 조가 아니라 원문 사실이다 (V3 게이트 제외 대상)
--     corpus.precedence_rules.우선규범  'L1 > L2' 를 뭉뚱그리면 안 된다. 상위 규범이 사업마다
--                                       다르다 (모두의창업 = 운영요령). rule_base.md §7
--     corpus.refs.src_layer             엣지의 출처 레이어. 폐포 수집 시 레이어별 상한·집계에 쓴다
--     corpus.documents.retrieval_scope  🔑 진입점 노브 (아래)
--
-- (3) 🔑 retrieval_scope — 검색 진입점을 "적재 단계"가 아니라 "필터 단계"에서 좁힌다
--     index=true 229 규범의 조 수를 실측하면 재정경제부 5,819 · 법무부 4,734(민법 1,193 ·
--     상법 1,184 · 형사소송법 610) … 중기부 소관은 866 뿐이다. 세법·민상법이 판정 인덱스의
--     검색 후보로 들어가 있다. 이들은 refs 폐포의 도착지이지 검색 진입점이 아니다.
--
--     그렇다고 임베딩에서 빼버리면 되돌리는 데 재임베딩이 든다. 그래서 데이터는 전량 넣고
--     이 컬럼으로 WHERE 절에서 켜고 끈다 — 골든셋 검색 15문항 Recall@5 로 A/B 한 뒤 확정한다.
--     기본값은 넓은 쪽('진입점')이다. 조용히 좁아지는 방향(근거 누락)이 위험하기 때문이다.
--     ⚠️ 부처 메타데이터는 관련성의 대리 지표가 못 된다 — 국고보조금 통합관리지침·보조금법은
--        기획예산처 소관이지만 적대적 골든셋 A26 의 정답 근거다.
-- ════════════════════════════════════════════════════════════════════════════

CREATE SCHEMA corpus;
CREATE SCHEMA tenant;
CREATE SCHEMA eval;
CREATE SCHEMA extensions;

-- Supabase 는 pgvector 를 extensions 스키마에 둔다. 같은 배치로 만들어야
-- pg_dump 산출물의 `extensions.vector(1024)` 가 그대로 복원된다.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

SET search_path = corpus, tenant, eval, extensions, public;

-- 기존 스크립트(build_index·seed_rules·smoke_search …)는 테이블명을 스키마 없이 쓴다.
-- DB 기본 search_path 를 박아 두면 그대로 돈다.
DO $do$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET search_path = corpus, tenant, eval, extensions, public',
        current_database());
END
$do$;


-- ════════════════════════════════════════════════════════════════
-- corpus 1. documents : 문서 대장
-- ════════════════════════════════════════════════════════════════
CREATE TABLE corpus.documents (
    doc_id        TEXT PRIMARY KEY,
    layer         TEXT NOT NULL CHECK (layer IN ('L1','L2','L3','사례')),
    domain        TEXT,          -- 창업지원사업 | 연구비 | 대학혁신지원사업 | 기관운영
    기관ID        TEXT,          -- L3(주관기관) 전용. NULL 이면 전국 공통
    doc_type      TEXT,
    version       TEXT,
    시행일        DATE,
    status        TEXT NOT NULL CHECK (status IN ('active','superseded','reference')),
    -- 2026-08-28 제거된 컬럼: 근거가 두 번 갈아끼워졌고 두 번째도 무너졌다.
    --   옛 근거(타 대학 24건 제외)      -> scripts/archive/eval/index_guard.py 가 코드로 막는다
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
    -- 🔴 `roles TEXT[]` 삭제 (2026-09-01). `index_target` 의 완전한 복사본이었다 —
    --    적재기가 `['judgment_index'] if index_target else []` 로 넣었을 뿐이고,
    --    주석이 예고한 rule_source·diff_only 를 쓰는 코드는 끝내 없었다.
    --    같은 사실을 두 컬럼에 적어 두면 필터를 어느 쪽으로 걸지가 매번 흔들린다.
    index_target  BOOLEAN NOT NULL DEFAULT FALSE, -- chunks 에 넣을 문서인가
    -- 2026-08-31 추가 (서문 (3)). 검색 진입점인가, 참조 폐포의 도착지일 뿐인가.
    --   진입점    = 임베딩·BM25 검색 후보. RAG.md §4-2 pre-filter 통과
    --   폐포전용  = doc_articles + refs 로만 도달. 인용은 정상 작동(검색이 아니라 참조로 온다)
    retrieval_scope TEXT NOT NULL DEFAULT '진입점'
                    CHECK (retrieval_scope IN ('진입점','폐포전용')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE corpus.documents IS '원본 문서 대장. 인덱서는 status=active 만 읽는다.';
COMMENT ON COLUMN corpus.documents.retrieval_scope IS
  '진입점 = 검색 후보 / 폐포전용 = refs 로만 도달. 좁히기는 측정(Recall@5) 후 UPDATE 로 확정한다.';

-- ════════════════════════════════════════════════════════════════
-- corpus 2. doc_articles : 조(條) 단위 원문. diff 전용·폐포 전용 문서도 여기까지는 온다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE corpus.doc_articles (
    article_id  BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES corpus.documents(doc_id) ON DELETE CASCADE,
    조번호      TEXT NOT NULL,
    조제목      TEXT,
    조번호_int  INT,             -- 정렬 및 단조성 검증용
    본문        TEXT NOT NULL,
    페이지      INT,
    -- 2026-08-31 추가. `제11조 삭제 <2003.8.26>` · `[별지 제9호서식] 삭제 <1996.3.30>`.
    -- 실측: L1 219건의 빈 조 1,964 중 1,667(85%)이 삭제 조문이었다. 빈 조가 아니라 원문 사실이라
    -- V3(빈 조 비율) 게이트에서 제외하고, 검색·청킹에서도 뺀다. 산출: stage0_run.py
    삭제        BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (doc_id, 조번호)
);
CREATE INDEX ix_articles_doc ON corpus.doc_articles (doc_id, 조번호_int);
-- 폐포 조회(refs.dst → 원문)의 진입 경로
CREATE INDEX ix_articles_lookup ON corpus.doc_articles (doc_id, 조번호);

-- ════════════════════════════════════════════════════════════════
-- corpus 3. chunks : 판정 인덱스 (A등급 근거)
-- ════════════════════════════════════════════════════════════════
CREATE TABLE corpus.chunks (
    chunk_id    BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES corpus.documents(doc_id) ON DELETE CASCADE,
    article_id  BIGINT REFERENCES corpus.doc_articles(article_id) ON DELETE CASCADE,
    layer       TEXT NOT NULL,
    기관ID      TEXT,
    -- 저품질 파싱은 판정에서 제외한다. 구 apply_mode='compare' 의 실제 역할이었다.
    -- domain 은 documents 에만 둔다 (인용 시 join). 청크마다 들고 다닐 이유가 없다.
    parse_quality TEXT NOT NULL DEFAULT 'high' CHECK (parse_quality IN ('high','low')),
    version     TEXT,
    status      TEXT NOT NULL,
    -- documents 에서 내려온 사본. §4-2 pre-filter 를 단일 테이블 스캔으로 유지하기 위한 비정규화
    -- (layer·기관ID·version·status·parse_quality 와 같은 취급)
    retrieval_scope TEXT NOT NULL DEFAULT '진입점'
                    CHECK (retrieval_scope IN ('진입점','폐포전용')),
    조번호      TEXT,
    조제목      TEXT,
    항호        TEXT,
    페이지      INT,
    사업명      TEXT[],          -- NULL 이면 전 사업 공통
    -- Stage 0.5 태깅이 채운다. 검색은 항상 적용대상 IN ('창업기업','공통') 필터 (2026-08-30 추가)
    적용대상    TEXT CHECK (적용대상 IN ('주관기관','창업기업','공통') OR 적용대상 IS NULL),
    text        TEXT NOT NULL,   -- 원문 그대로. 인용 검증 대상이므로 절대 가공 금지
                                 -- 임베딩·BM25 입력은 text 가 아니라 [컨텍스트 헤더]+text (RAG.md §3-5)
    embedding   extensions.vector(1024)     -- KURE-v1
);
-- pre-filter 가 검색보다 먼저 (구버전·타사업·타기관·폐포전용 차단) — RAG.md §4-2
CREATE INDEX ix_chunks_filter ON corpus.chunks
    (status, parse_quality, retrieval_scope, layer, 적용대상, 기관ID);
CREATE INDEX ix_chunks_사업   ON corpus.chunks USING GIN (사업명);
COMMENT ON COLUMN corpus.chunks.text IS '원문 문자열 그대로. 인용 검증이 이 값과 대조한다.';
-- ⚠️ ANN 인덱스(HNSW/IVFFlat)를 만들지 않는다.
--    근사 검색의 리콜 손실 = 인용 누락 = 오답. 1만 청크 초과 시 재검토.
--    (retrieval_scope 로 진입점을 좁히면 1만 미만으로 떨어질 수 있다 — 그래서 먼저 잰다)

-- ════════════════════════════════════════════════════════════════
-- corpus 4. (비어 있음) — case_chunks 는 2026-09-06 오너 결정으로 «드랍했다»
--   판단불가 경로에 사례를 붙이지 않는다. 193행은
--   scratchpad/_백업_case_chunks_0906.json 에 백업돼 있다.
--   🔴 «물리 분리 원칙 자체는 남는다» — 판정 인덱스는 여전히 L1·L2 + 현재 기관 L3 뿐이고,
--      타 기관 규정(L4)·정답셋은 절대 넣지 않는다. 사라진 것은 «사례 테이블» 이지 «경계» 가 아니다.
-- ════════════════════════════════════════════════════════════════

-- ════════════════════════════════════════════════════════════════
-- corpus 5. rules : 룰 테이블 (fast path). 벡터 없음.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE corpus.rules (
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
CREATE INDEX ix_rules_lookup ON corpus.rules (사업명, 비목);

-- 🔴 2026-09-06 오너 결정 — corpus.rules 의 TRUNCATE 를 «막는다».
--   `scripts/archive/seed/seed_rules.py` 가 TRUNCATE + 재삽입이고 스스로 "유일한 소유자" 라
--   선언하는데, 그 파일의 rows() 는 «2026-09-02 스냅샷» 이다. 이후 DB 변경(배열 append 121건 ·
--   L1 단서 7행 · 사전승인_조건 UNION · 골든셋 판정 5건)이 거기 «없다». 재적재하면 되돌릴 수
--   없이 사라진다. 그 파일은 archive/ 라 읽기 전용이므로 «DB 쪽에» 관문을 둔다 —
--   어느 경로로 들어오든(적재 스크립트 · 검수툴 · 수기 SQL) 똑같이 막힌다.
--   🔴 정말 재적재해야 하면 사람이 이 트리거를 «의도적으로» 떨군다:
--      DROP TRIGGER trg_rules_truncate_금지 ON corpus.rules;
CREATE OR REPLACE FUNCTION corpus.f_rules_truncate_금지() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION
    '🔴 corpus.rules TRUNCATE 는 막혀 있다 (2026-09-06 오너 결정). '
    'seed_rules.py 의 rows() 는 2026-09-02 스냅샷이고 이후 DB 변경이 거기 없다. '
    '재적재하면 되돌릴 수 없이 사라진다. '
    '정말 필요하면 사람이 DROP TRIGGER trg_rules_truncate_금지 ON corpus.rules; 를 «의도적으로» 친다.';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rules_truncate_금지
  BEFORE TRUNCATE ON corpus.rules
  FOR EACH STATEMENT EXECUTE FUNCTION corpus.f_rules_truncate_금지();
COMMENT ON COLUMN corpus.rules.verified IS 'false 인 룰만으로 "가능" 판정 금지. 조문 인용 동반 필수.';
-- ⚠️ 미결: layer='L3' 행은 기관 데이터다 (rule_base.md §3-1 overlay).
--    corpus 는 "전 사용자 공통" 스키마인데 여기에 기관별 행이 섞인다.
--    tenant 이관 여부는 오너 결정 대기 — 현재는 설계 문서(rule_base.md §3-1)를 따라 corpus 에 둔다.

-- ════════════════════════════════════════════════════════════════
-- corpus 5-b. precedence_rules : 우선순위 조항 (2026-08-27 추가)
--      "어느 계층이 이기는가" 를 각 문서의 제3조 부근에서 파싱해 등록한다.
--      비목 룰이 아니라 충돌 해소 룰이다. 상세: rule_base.md §3
--      🔴 "아래가 엄격하면 이긴다" 가 아니다 — 8개 사업 중 6개가 L2 > L3 를 명시한다.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE corpus.precedence_rules (
    prec_id     BIGSERIAL PRIMARY KEY,
    사업명      TEXT NOT NULL,
    우선계층    TEXT NOT NULL CHECK (우선계층 IN ('L1','L2','L3')),
    열위계층    TEXT NOT NULL CHECK (열위계층 IN ('L1','L2','L3')),
    범위        TEXT NOT NULL DEFAULT 'all'
                CHECK (범위 IN ('all','unspecified_only')),  -- unspecified_only: 상위에 미규정인 사항만 하위 적용
    -- 2026-08-31 추가. 'L1 > L2' 를 뭉뚱그리면 안 된다 — 상위 규범이 사업마다 다르다.
    -- 모두의창업(일반·기술트랙) = 「중소기업창업 지원사업 운영요령」 / 초격차 = 요령 및 지침.
    -- 뭉뚱그리면 통합관리지침을 상위로 잘못 적용한다. rule_base.md §7
    우선규범    TEXT,
    근거        JSONB NOT NULL DEFAULT '[]',   -- [{doc_id, 조번호}]
    원문        TEXT NOT NULL,                 -- 조항 원문 그대로 (화면 7에서 인용)
    해석        TEXT,                          -- 사람이 읽는 한 줄 (build_precedence.py 산출)
    verified    BOOLEAN NOT NULL DEFAULT FALSE,
    검수자      TEXT,
    검수일      DATE,
    UNIQUE (사업명, 우선계층, 열위계층)
);
CREATE INDEX ix_prec_lookup ON corpus.precedence_rules (사업명);
COMMENT ON TABLE corpus.precedence_rules IS
  '우선순위 조항. 없으면 폴백(상위 규범 우선 + 엄격한 값 우선). 사업별로 문구가 달라 사람 검수 대상.';
COMMENT ON COLUMN corpus.precedence_rules.범위 IS
  'all = 항상 우선계층이 이김(재도전성공패키지 제3조) / unspecified_only = 상위에 없는 사항만 하위 적용(초격차 제3조)';
-- ⚠️ 조회 키가 사업명이다. 모두의창업 로컬트랙(제3편)은 위임 계통이 다른데 같은 키를 쓰므로
--    범위 밖으로 잘라낸다 (build_precedence.범위밖_구간). CLAUDE.md 사업 스코프

-- ════════════════════════════════════════════════════════════════
-- corpus 5-c. refs : 참조 그래프 (2026-08-27 추가)
--      "어느 조가 어느 조를 가리키는가". 한 행 = 엣지 하나.
--      그래프 DB(Neo4j 등) 불필요 — 깊이 3, 재귀 CTE 로 밀리초.
--      RAG 의 존재 이유가 여기다: 사용자 문서만으로 답이 안 될 때
--      "제33조에 따른다" 를 따라가 실제 조항에 닿는다. RAG.md §4-3
-- ════════════════════════════════════════════════════════════════
CREATE TABLE corpus.refs (
    ref_id      BIGSERIAL PRIMARY KEY,
    src_doc_id  TEXT NOT NULL,
    src_조번호  TEXT NOT NULL,
    -- 2026-08-31 추가. 엣지의 출처 레이어. build_refs.py 가 이미 산출한다.
    -- 폐포가 세법 계열로 번지는 것을 막는 레이어별 상한·집계의 축.
    src_layer   TEXT CHECK (src_layer IN ('L1','L2','L3','사례') OR src_layer IS NULL),
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
CREATE INDEX ix_refs_src ON corpus.refs (src_doc_id, src_조번호);
CREATE INDEX ix_refs_dst ON corpus.refs (dst_doc_id, dst_조번호);   -- 역참조("누가 나를 인용했나")
CREATE INDEX ix_refs_dangling ON corpus.refs (해소상태) WHERE 해소상태 = 'dangling';

COMMENT ON TABLE  corpus.refs IS
  '참조 그래프. 진입점에서 깊이 3까지 폐포를 수집해 LLM 컨텍스트에 싣는다. LLM 은 이 표를 보지 않는다 — 코드가 조회해 텍스트로 먹인다.';
COMMENT ON COLUMN corpus.refs.해소상태 IS
  'resolved=정상 / shifted=조번호가 구판이라 조제목으로 재매칭함 / dangling=코퍼스에 대상 없음(판단불가 예고)';
COMMENT ON COLUMN corpus.refs.관계 IS
  '미규정위임 = "이 지침에서 정하지 아니한 사항은 ~에 따름". 건국대 지침 실측 문형. L3 게이팅(Agent.md §3-2)의 근거가 된다.';

-- ════════════════════════════════════════════════════════════════
-- corpus 5-d. BM25 역색인 (2026-08-30 추가 — RAG.md §2-4)
--      kiwipiepy 분석은 앱이 색인 시점에 하고, DB 는 결과 토큰을 세고 정렬만 한다.
--      재인덱싱 = TRUNCATE; INSERT; REFRESH MATERIALIZED VIEW; COMMIT (트랜잭션 하나)
-- ════════════════════════════════════════════════════════════════
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

-- ════════════════════════════════════════════════════════════════
-- corpus 6. item_alias : 상품명 → 비목 매핑 사전. 여기만 벡터가 필요.
-- ════════════════════════════════════════════════════════════════
CREATE TABLE corpus.item_alias (
    alias_id    BIGSERIAL PRIMARY KEY,
    상품명      TEXT NOT NULL,
    비목        TEXT NOT NULL,   -- 비목 어휘집 enum (rule_base.md §1-b)
    사업명      TEXT,
    출처        TEXT,            -- seed | 사용자질문 | 센터답변
    embedding   extensions.vector(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_alias_name ON corpus.item_alias (상품명);

-- ════════════════════════════════════════════════════════════════
-- corpus 7. xref_mismatch : 크로스 레퍼런스 불일치 (RAG.md §3-3)
-- ════════════════════════════════════════════════════════════════
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


-- ════════════════════════════════════════════════════════════════════════════
-- tenant : 사용자 것. RAG.md §2-3 이 정본.
--   RLS 는 2차 방어선이다. 1차는 코드 — 앱이 항상 org_id 를 명시적으로 건다.
--   ⚠️ 로컬 postgres 는 슈퍼유저라 RLS 를 우회한다. 정책은 Supabase(anon/authenticated)에서 문다.
-- ════════════════════════════════════════════════════════════════════════════

-- Supabase(PostgREST) 는 request.jwt.claims 를, 로컬은 SET app.org_id 를 쓴다.
-- 한 함수로 흡수해 정책 문구를 양쪽에서 동일하게 유지한다.
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
    status       TEXT NOT NULL CHECK (status IN ('active','superseded')),
    -- 'hwpx'/'hwp': L3 사용자 업로드 파서 경로 (2026-08-30 채택).
    -- 조달분(L1·L2)의 한컴 1회 수동 변환 원칙과는 별개다 — RAG.md §3-1
    extraction   TEXT NOT NULL DEFAULT 'native'
                 CHECK (extraction IN ('native','dedupe','hancom','vlm','hwpx','hwp')),
    -- 🔴 '대기' = 접수만 됐고 파싱 평가 전. 접수 시점에 'warn' 을 넣으면 «평가했는데
    --    미심쩍다» 와 «아직 안 봤다» 가 같은 값이 되어 구분이 영원히 사라진다.
    파싱품질     TEXT NOT NULL CHECK (파싱품질 IN ('대기','pass','warn','fail')),
    dangling수   INT NOT NULL DEFAULT 0,   -- 업로드 시점에 알린다 (판정 시점 아님)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 벡터 컬럼이 없다. 검색하지 않기 때문이다 (통째 로드 — RAG.md §4-1).
-- corpus.chunks 와 다른 테이블이라 멀티테넌시 누수가 구조적으로 불가능하다.
CREATE TABLE tenant.l3_articles (
    article_id  BIGSERIAL PRIMARY KEY,
    doc_id      UUID NOT NULL REFERENCES tenant.l3_documents(doc_id) ON DELETE CASCADE,
    org_id      UUID NOT NULL,          -- RLS 를 위해 비정규화
    조번호      TEXT NOT NULL,
    조제목      TEXT,
    조번호_int  INT,
    장          TEXT,                   -- 사업비 관련 장만 로드한다 (RAG.md §4-1)
    본문        TEXT NOT NULL,
    페이지      INT,
    UNIQUE (doc_id, 조번호)
);
CREATE INDEX ix_l3_org ON tenant.l3_articles (org_id, doc_id, 조번호_int);

-- 사용자 축. 이름 저장 금지 원칙 — 인력은 역할·수치만 (서비스 아키텍쳐.md §6).
CREATE TABLE tenant.accounts (
    account_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    email       TEXT NOT NULL UNIQUE,
    -- 🔴 NOT NULL 해제 (2026-09-03, S2 인증). Supabase 가 비밀번호를 들고 우리는
    --    (email → org_id) 만 든다. 우리 쪽 pw_hash 는 «있으면 안 되는» 값이다.
    --    자체 로그인을 되살릴 때만 다시 채운다 — 그때 NOT NULL 을 되걸 것.
    pw_hash     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- F1 협약 + F2 요약. 사업(협약) 1건 = 1행.
CREATE TABLE tenant.f_profile (
    profile_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    사업명          TEXT NOT NULL,
    협약시작일      DATE,
    협약종료일      DATE,
    정부지원_현금   NUMERIC, 정부지원_현물 NUMERIC,   -- F1 = 재원 x 형태 4분면
    자기부담_현금   NUMERIC, 자기부담_현물 NUMERIC,
    과업범위요약    TEXT,          -- F2. 원문은 저장하지 않는다
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, 사업명)
);

-- F3 집행내역. 집계 축 3개(비목별·거래처별·인별월별)가 이 한 테이블에서 나온다.
CREATE TABLE tenant.f_exec (
    exec_id     BIGSERIAL PRIMARY KEY,
    profile_id  UUID NOT NULL REFERENCES tenant.f_profile(profile_id) ON DELETE CASCADE,
    비목        TEXT NOT NULL,                 -- 비목 어휘집 enum (rule_base.md §1-b)
    재원        TEXT NOT NULL CHECK (재원 IN ('정부지원','자기부담')),
    형태        TEXT NOT NULL CHECK (형태 IN ('현금','현물')),
    거래처      TEXT,                          -- 동일 거래처 누적(2천만원 심의)용
    인력역할    TEXT,                          -- 인별월별 집계용. 이름 아님
    귀속월      DATE,
    금액        NUMERIC NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_exec_agg ON tenant.f_exec (profile_id, 비목, 재원, 형태);

-- F4 인력. 참여율 상한이 소속기관 유형(100%/130%)으로 갈린다.
CREATE TABLE tenant.f_personnel (
    person_id     BIGSERIAL PRIMARY KEY,
    profile_id    UUID NOT NULL REFERENCES tenant.f_profile(profile_id) ON DELETE CASCADE,
    역할          TEXT NOT NULL,               -- 대표자 | 신규채용 | 기존직원 ...
    고용형태      TEXT,
    타사업참여율  NUMERIC,
    소속기관유형  TEXT,
    겸직          BOOLEAN
);

-- 결핍 루프 (서비스 아키텍쳐.md §5). 주간 집계로 상위 항목을 F 필드로 승격한다.
CREATE TABLE tenant.unmapped_premise (
    id           BIGSERIAL PRIMARY KEY,
    premise_text TEXT NOT NULL,
    근거조항     JSONB,
    사업명       TEXT,
    비목         TEXT,
    발생횟수     INT NOT NULL DEFAULT 1,
    최초         TIMESTAMPTZ NOT NULL DEFAULT now(),
    최근         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (premise_text, 사업명, 비목)
);

-- ════════════════════════════════════════════════════════════════
-- tenant. decisions : 판정 로그
--   🔴 corpus 가 아니라 tenant 다 — 전제·검색스냅샷에 F축 흔적이 남는다.
--      corpus 에 두면 RLS 없이 남는다 (저장 계층화, 서비스 아키텍쳐.md §6).
--   게스트 판정은 org_id IS NULL 로 남고 클라이언트에서는 읽히지 않는다 (서버 롤만).
-- ════════════════════════════════════════════════════════════════
CREATE TABLE tenant.decisions (
    decision_id BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    org_id      UUID REFERENCES tenant.orgs(org_id) ON DELETE SET NULL,  -- NULL = 게스트
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
    모델        JSONB,           -- {정규화: ..., 조립: ...}
    -- 재현성: 이 세 컬럼이 없으면 "판정 이력을 나중에 설명" 이 불가능하다 (Agent.md §6)
    전제        JSONB,           -- 🔴 [2겹] 이다. 1겹이 아니다 (2026-09-03 정정)
                                 --    orchestrate.py:762 가 넣는 값은 llm_validate.검증()
                                 --    «통과 후» 다. 실물 키가 [근거조항·매핑·미매핑·미충족시·사실]
                                 --    인데 `미매핑` 은 llm_validate.py:428 이 «코드로» 붙인다 —
                                 --    1겹 스키마엔 없는 키다. 이게 직접 증거.
                                 --    🔴 그래서 「LLM 출력 스키마의 키를 바꾸면 DB 가 깨진다」는
                                 --       거짓이다. 2겹에서 이름이 다시 세워진다 (LLM.md §3-4)
    검색스냅샷  JSONB,           -- S번호→(chunk_id|article_id, 항호) 매핑 + top-k 목록 (LLM.md §3-7)
    코퍼스버전  TEXT             -- 판정 시점의 인덱스 버전 스탬프
);
CREATE INDEX ix_decisions_time ON tenant.decisions (created_at DESC);

-- ── RLS ──────────────────────────────────────────────────────────
ALTER TABLE tenant.orgs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.l3_documents    ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.l3_articles     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.accounts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.f_profile       ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.f_exec          ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.f_personnel     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.unmapped_premise ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant.decisions       ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation ON tenant.orgs          USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.l3_documents  USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.l3_articles   USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.accounts      USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.f_profile     USING (org_id = tenant.current_org());
CREATE POLICY org_isolation ON tenant.decisions     USING (org_id = tenant.current_org());
-- profile 경유 격리
CREATE POLICY org_isolation ON tenant.f_exec USING (
    profile_id IN (SELECT profile_id FROM tenant.f_profile WHERE org_id = tenant.current_org()));
CREATE POLICY org_isolation ON tenant.f_personnel USING (
    profile_id IN (SELECT profile_id FROM tenant.f_profile WHERE org_id = tenant.current_org()));
-- 결핍 집계는 org 축이 없다(전사 통계). 클라이언트에는 열지 않는다 — 정책 없이 RLS 만 켠다.


-- ════════════════════════════════════════════════════════════════════════════
-- eval : 평가 정답지.  🔴 Supabase 로 올리지 않는다.
--   pg_dump 는 --schema=corpus --schema=tenant 로만 뜬다.
--   "인덱스 투입 금지"(index_guard.py)와 "API 노출 금지"는 다른 방어선이다.
--   여기 있는 것을 안 올리면 두 번째 방어선이 설정이 아니라 사실이 된다.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE eval.golden_set (
    gold_id     BIGSERIAL PRIMARY KEY,
    세트        TEXT NOT NULL,   -- 별첨4 | 직접작성 | 적대적 | 연구재단홀드아웃
    no          TEXT,            -- 원본 문항 번호 (적대적은 A1~A27)
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


-- ════════════════════════════════════════════════════════════════════════════
-- 권한 — public 은 비워 두고, corpus/tenant/eval 은 PUBLIC 롤에서 회수한다.
--   Supabase 에서는 여기에 더해 expose_schemas 에서 corpus·tenant 를 빼야 한다.
--   (앱은 서비스 롤로 직접 접속한다 — PostgREST 를 경유하지 않는다)
-- ════════════════════════════════════════════════════════════════════════════
REVOKE ALL ON SCHEMA corpus, tenant, eval FROM PUBLIC;


-- ════════════════════════════════════════════════════════════════
-- 확인용 뷰 : pgweb 에서 한눈에 적재 현황 보기
-- ════════════════════════════════════════════════════════════════
CREATE VIEW corpus.v_적재현황 AS
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
UNION ALL SELECT '12. tenant.orgs',             count(*) FROM tenant.orgs
UNION ALL SELECT '13. tenant.l3_documents',     count(*) FROM tenant.l3_documents
UNION ALL SELECT '14. tenant.l3_articles',      count(*) FROM tenant.l3_articles
UNION ALL SELECT '15. tenant.accounts',         count(*) FROM tenant.accounts
UNION ALL SELECT '16. tenant.f_profile',        count(*) FROM tenant.f_profile
UNION ALL SELECT '17. tenant.f_exec',           count(*) FROM tenant.f_exec
UNION ALL SELECT '18. tenant.f_personnel',      count(*) FROM tenant.f_personnel
UNION ALL SELECT '19. tenant.unmapped_premise', count(*) FROM tenant.unmapped_premise
UNION ALL SELECT '20. tenant.decisions',        count(*) FROM tenant.decisions
UNION ALL SELECT '21. eval.golden_set',         count(*) FROM eval.golden_set
ORDER BY 1;
