# RAG.md — 근거를 어떻게 찾아오나

저장소 **Supabase (Postgres 17 + pgvector)**. 범위: 코퍼스 경계 · 스키마 · 인덱싱 · 검색 · 참조확장.
룰은 `rule_base.md`, 프롬프트·모델은 `LLM.md`, 순서는 `Agent.md`. 상위 문서는 `서비스 아키텍쳐.md`.

## 0. RAG의 역할

**사용자 문서(L3)만으로 답이 안 될 때 참조를 따라가는 것**이다. "관련 조항 검색"이 아니다.

```
(1) L3 먼저 조회 (가장 구체적)
     없음 / "~에 따른다"만  → (2)
     불가 / 조건부          → L3 단독 결론 가능 (틀려도 "틀린 불가" = 안전)
     가능                   → 상위 확인 강제 (L3 는 추가 제약만 적는다)
(2) L1·L2 조회 + refs 로 깊이 3까지 폐포 수집
(3) precedence_rules 로 효력 결정 — L3 가 항상 이기는 게 아니다
```

## 1. 코퍼스 경계 (절대 규칙)

**판정 인덱스 = L1 · L2 + 현재 기관의 L3 1벌뿐.**

| 축 | 원천 | 인덱싱 |
|---|---|---|
| L1 | `2026_Finance_DATA_FOR_RAG/중기부/` + `법령 PDF/L1_법령/` 중 `index:true` (219 규범) | O |
| L2 | `2026_Finance_DATA_FOR_RAG/창진원/` 세부관리기준 (41) | O |
| B급 | `사례집/` + `kosmes_faq.json` | O — **사례 인덱스** (판단불가 경로 전용) |
| L3 | 사용자 업로드 | O — 해당 org 전용, `tenant` 스키마 |
| — | `PMS/` 매뉴얼 42건 | X — 규범이 아님. F축 입력 UX 설계 자료 |
| — | 별표 PDF 103건 | 판정 유효 4건만 |
| X | `archive/` · `_골든셋/` · `_테스트_L3/` · `_범위밖_보류/` · L4·L5 | **조건 없이 거부** |

- 레이어는 **발행주체** 기준 — 법률·시행령·행정규칙은 누가 참조하든 L1. 조달 경로는
  `_law_sources.json` 의 `sources` 배열이 별도로 든다
- 거부는 문장이 아니라 코드다 — `scripts/index_guard.py` (`stage0_ingest.py` 가 `index_target`
  직전에 통과). 회귀 테스트 `tests/test_index_guard.py`. **새 인덱싱 경로는 반드시 이 게이트를 태운다**

## 2. 저장소 — Supabase

### 2-1. 스키마 2분할

```
schema corpus      공개 규범·룰·refs. 전 사용자 공통. RLS 없음
schema tenant      사용자 것 (L3·계정·F축·판정로그). org 별 격리. RLS 필수
schema eval        골든셋. 🔴 Supabase 로 올리지 않는다 (덤프 대상 제외)
schema extensions  pgvector. Supabase 와 같은 배치 — 덤프가 그대로 복원된다
schema public      비워둔다
```

정책이 필요한 것만 tenant 한 곳에 모아 전부 건다 — 벡터 검색에 정책 평가가 붙지 않는다.

🔴 **`public` 을 비우는 것이 골든셋 방어의 2차선이다.** Supabase 에서 `public` 은 PostgREST 가
자동으로 REST API 로 노출하는 스키마다 — anon 키만 있으면 `GET /rest/v1/golden_set` 이 된다.
`index_guard.py` 는 인덱스 투입을 막지 API 노출을 막지 않는다. 골든셋은 앱이 런타임에 읽지
않으므로 `eval` 로 빼고 **덤프에서 제외**한다 (설정으로 막는 것보다 안 올리는 게 낫다).

### 2-2. corpus (정본 DDL: `db/init/01_schema.sql`)

| 테이블 | 내용 | 비고 |
|---|---|---|
| `documents` | 문서 대장 | `index_target` · `retrieval_scope`(§4-2) · `extraction`(native/dedupe/hancom/**vlm**) · `parse_quality` |
| `doc_articles` | 조 단위 원문 | diff 전용 문서도 여기까지 |
| `chunks` | **판정 인덱스 (A등급)** | `vector(1024)` · `적용대상` 컬럼 · `text` 는 원문 그대로(가공 금지) |
| `case_chunks` | **사례 인덱스 (B등급)** | 물리 분리. 판단불가 경로 전용 — 검색 함수는 `chunks` 만 SELECT |
| `rules` / `precedence_rules` | `rule_base.md` | |
| `refs` | 참조 그래프 (조→조 엣지) | resolved / shifted / dangling |
| `chunk_terms` / `chunk_len` / `term_df` | BM25 역색인 (§2-4) | |
| `item_alias` | 상품명 → 비목 별칭 | 컨펌 로그로 성장 |
| `xref_mismatch` | 크로스 레퍼런스 불일치 | §3-3 |

`decisions`(판정 로그)는 corpus 가 아니라 **tenant** 다 — `전제`·`검색스냅샷` 에 F축 흔적이
남는다. `golden_set` 은 **eval**. 정본 DDL 은 `db/init/01_schema.sql`.

**ANN 인덱스(HNSW/IVFFlat) 없음** — 근사 검색의 리콜 손실 = 인용 누락 = 오답. 1만 청크 초과 시 재검토.

### 2-3. tenant — L3 와 사용자 축

**L3 는 `chunks` 에 넣지 않는다.** 별도 테이블이라 멀티테넌시 누수가 구조적으로 불가능하고,
통째 로드하므로 벡터도 불필요하다.

```sql
CREATE SCHEMA tenant;

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
    extraction   TEXT NOT NULL DEFAULT 'native'
                 CHECK (extraction IN ('native','dedupe','hancom','vlm','hwpx','hwp')),
                 -- 'hwpx'/'hwp': L3 사용자 업로드 파서 경로 (2026-08-30 채택)
    파싱품질     TEXT NOT NULL CHECK (파싱품질 IN ('pass','warn','fail')),
    dangling수   INT NOT NULL DEFAULT 0,   -- 업로드 시점에 알린다 (판정 시점 아님)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 벡터 컬럼이 없다. 검색하지 않기 때문이다.
CREATE TABLE tenant.l3_articles (
    article_id  BIGSERIAL PRIMARY KEY,
    doc_id      UUID NOT NULL REFERENCES tenant.l3_documents(doc_id) ON DELETE CASCADE,
    org_id      UUID NOT NULL,          -- RLS 를 위해 비정규화
    조번호      TEXT NOT NULL,
    조제목      TEXT,
    조번호_int  INT,
    본문        TEXT NOT NULL,
    페이지      INT,
    UNIQUE (doc_id, 조번호)
);
CREATE INDEX ix_l3_org ON tenant.l3_articles (org_id, doc_id, 조번호_int);

-- 사용자 축. 이름 저장 금지 원칙 — 인력은 역할·수치만.
CREATE TABLE tenant.accounts (
    account_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    email       TEXT NOT NULL UNIQUE,
    pw_hash     TEXT NOT NULL,
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

-- 결핍 루프 (서비스 아키텍쳐.md §5)
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
```

**RLS 는 2차 방어선이다.** 1차는 코드 — 앱이 항상 `org_id` 를 명시적으로 건다.

```sql
ALTER TABLE tenant.l3_articles ENABLE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON tenant.l3_articles
    USING (org_id = (auth.jwt() ->> 'org_id')::uuid);
```

### 2-4. BM25 — DB 안에서 SQL 로

형태소 분석은 앱이 색인 시점에 하고 **결과 토큰만 적재**한다. 재인덱싱이 트랜잭션 하나가 되고
(`TRUNCATE; INSERT; REFRESH MATERIALIZED VIEW; COMMIT`), 워커가 상태를 안 가진다.

```sql
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
```

검색 (k1=1.2, b=0.75):

```sql
WITH q(term) AS (SELECT unnest(%(terms)s::text[])),      -- 앱이 kiwi 로 토큰화해 넘긴다
     s AS (SELECT count(*)::numeric n, avg(dl)::numeric avgdl FROM corpus.chunk_len)
SELECT ct.chunk_id,
       sum( ln(1 + (s.n - df.df + 0.5) / (df.df + 0.5))
            * (ct.tf * 2.2) / (ct.tf + 1.2 * (0.25 + 0.75 * cl.dl / s.avgdl)) ) AS score
FROM q
JOIN corpus.chunk_terms ct ON ct.term = q.term
JOIN corpus.term_df     df ON df.term = q.term
JOIN corpus.chunk_len   cl ON cl.chunk_id = ct.chunk_id
CROSS JOIN s
GROUP BY ct.chunk_id
ORDER BY score DESC LIMIT 50;
```

**토큰화 정책 (색인·쿼리 동일 적용)** — 이게 있어야 동등성 검증이 재현된다:

| 항목 | 값 |
|---|---|
| 분석기 | kiwipiepy 기본 모델 |
| 채택 품사 | 체언 NNG·NNP·NNB / 용언 어간 VV·VA / 어근 XR / 외국어 SL / 숫자 SN / 한자 SH |
| 정규화 | 영문 소문자화. 불용어 목록 없음(IDF 가 감쇠) |

⚠️ `bm25s` 와 top-20 겹침률 검증을 **골든셋 평가보다 먼저** 한다.

### 2-5. 운영 메모

- 임베딩(KURE-v1)·형태소 분석·LLM 호출은 전부 **앱 서버** 몫 — Supabase 는 저장·SQL 만
- L3 업로드 원본은 비공개 버킷 → 파싱 성공 시 삭제 (보관 여부 미결)
- 마이그레이션: `db/init/01_schema.sql` → `supabase/migrations/0001_corpus.sql` ·
  `0002_tenant_rls.sql`. **스키마는 한 벌만 둔다**

## 3. 인덱싱 파이프라인 (오프라인)

```
원문 (XML / PDF / HWP→PDF · L3 업로드는 HWPX/HWP 직파싱)
  ├─ Stage 0    파싱·조 분해            → corpus.doc_articles
  ├─ Stage 0.5  적용대상 태깅           → corpus.chunks.적용대상
  │              절(節) 헤딩이 선언하면 상속, LLM 은 절 밖 조문만
  ├─ Stage 0.7  참조 그래프 (정규식)    → corpus.refs
  ├─ Stage 0.8  우선순위 조항           → corpus.precedence_rules
  ├─ Stage 1    룰 컴파일               → corpus.rules        (rule_base.md)
  └─ Stage 2    청킹·헤더·임베딩·BM25   → corpus.chunks / chunk_terms
```

적용대상 태깅은 임베딩보다 먼저다 — 청크 적재 시 필드가 차 있어야 필터가 걸린다.

### 3-1. Stage 0 — 조문 재조립 규칙

**텍스트 추출은 반드시 `scripts/pdftext.py::extract()` 경유** (문자중복 레이어 dedupe ·
다단/4분면 좌표 컬럼 분리 내장). `pdfplumber.extract_text()` 직접 호출 금지 — 훅이 경고한다.

**L3 사용자 업로드는 HWPX·HWP 도 받는다** (2026-08-30 채택 — 조달분 L1·L2 의 한컴 수동
변환 원칙과 별개다).

| L3 포맷 | 추출 | 비고 |
|---|---|---|
| HWPX | zip/XML 직파싱 | 최우선 — 논리 구조가 있어 PDF 좌표 재조립보다 정확. `.hwp` 확장자여도 실제 HWPX 인 경우가 많다(매직바이트로 판별) |
| HWP 5.0 | `scripts/hwp_extract.py` 계열 텍스트 추출 | 표 정밀도 낮음 — L3 는 조문 텍스트 위주라 허용. `extraction='hwp'` 태깅 |
| PDF | `pdftext.py::extract()` | 기존 경로 |
| 배포용(DRM) HWP | 파싱 실패 처리 | PDF 변환 후 재업로드 안내 |

추출 이후는 포맷 공통 — Step 1 섹션 분리부터 게이트 V1~V4 까지 동일하게 탄다.

**Step 1 — 섹션 분리 (선행 필수). 순서: 목차 컷 → 부칙 → 붙임(부칙 뒤에서만).**

| 규칙 | 값 |
|---|---|
| 목차 컷 | 점선 지도(`·····`) 10회 이상 + 마지막이 앞 30% 안이면 목차로 보고 제거 |
| 부칙 판별 | **뒤 300자의 "시행" 언명** (위치 비율·조 개수는 판별 기준으로 못 쓴다) |
| 붙임·별표·별지·서식 | **부칙 뒤에서만** 탐지. 각각 독립 조로 분리 (조번호 = `붙임2` 등) |
| 부칙 조 | 조번호에 `부칙 ` 접두사. 단조성 검증 제외 |

붙임 헤더와 본문 참조(`[붙임 2]에서 정하는 바에 따른다`)를 혼동하면 가짜 섹션이 생긴다.

**Step 2 — 5단 fallback**

| 순위 | 전략 | 패턴 | 대상 |
|---|---|---|---|
| 1 | `jo_titled` | `제(\d+)조(제목)` | 법령·지침·규정·규칙 (대부분) |
| 2 | `outline_numbered` | `제N장` → `N.` → `가.` | TIPS 총괄 운영지침 계열 |
| 3 | `jo_bare` | 제목 없는 `제(\d+)조` | 일부 조례 |
| 4 | `jang` | `제(\d+)장` | 매뉴얼 |
| 5 | `paragraph` | 빈 줄 2개 단락 분할 | 전부 실패 시 |

- `outline_numbered` 가 `jo_bare` 보다 **앞**이다 — 개요형 문서 본문의 타 법령 인용
  (`「…법」 제29조`)을 조 헤딩으로 오인하는 것을 막는다. 표제는 **번호 단조성**으로 고른다
  (표제 번호는 장을 넘어 이어지고, 중첩 열거는 1부터 다시 돈다)
- 순위 3 이하로 떨어진 문서는 `parse_quality='low'` — **판정 인덱스 제외**

**Step 2-b — 조번호 중복 2종 (처리가 정반대)**

| 경우 | 판별 | 처리 |
|---|---|---|
| 목차 중복 | 조제목이 같다 | 긴 쪽 하나만 |
| 원문 오류 (한 문서에 같은 조번호 2개) | 조제목이 다르다 | **둘 다 보존**, 뒤엣것에 `[2]` 접미 |

**Step 3 — 문자 위생 (적재 전 필수)**: `NUL`(0x00) 제거 · 짝 없는 서로게이트(U+D800~DFFF) 제거.

### 3-2. 파싱 검증 게이트 — 실패 시 인덱싱 금지

| # | 검사 | 실패 시 |
|---|---|---|
| V1 | 조 번호 단조 증가 | 플래그 → 사람 확인 (원문 오류는 `원문오류` 로 따로 센다) |
| V2 | 조 개수 ≥ 5 | 파싱 실패 |
| V3 | 빈 조(50자 미만) 비율 < 10% | 추출 실패 의심 |
| V4 | 크로스 레퍼런스 검증 (§3-3) | 불일치 리포트 |

### 3-3. 크로스 레퍼런스 — 조번호가 아니라 조제목으로 재매칭

하위 문서의 `지침 제N조` 참조는 구판 조번호일 수 있다 (개정 시 조번호가 밀린다).
**조번호 매칭이 실패하면 조제목(비목명)으로 재매칭**하고, `refs.해소상태='shifted'` 로 기록해
원래 표기와 보정 근거를 함께 남긴다. 불일치는 `xref_mismatch` 에 적재.

### 3-4. 청킹

| 규칙 | 값 |
|---|---|
| 기본 단위 | 1 조 = 1 청크 (조 vs 항은 미결 — 재인덱싱 전 확정) |
| 분할 임계 | 조 본문 **KURE 토크나이저 900토큰 초과** → 항(①②③) 단위, 항도 초과 → 호 |
| 병합 | 50자 미만 청크 → 직전에 병합 |
| 오버랩 | 없음 — 조 경계가 의미 경계 |
| 표 | 청크에 넣지 않음 → 룰 테이블 (`rule_base.md`) |
| 첨부(별표·별지·서식·붙임) | 청크에 넣지 않음 — 표와 같은 취급. 실측 4,141건 28.9M자로 L1·L2 본문의 74%인데 대부분 세무 서식·감독 표다 |
| 범위 컷 | 모두의창업 **제3편(로컬트랙)은 청크에서 제외**. `doc_articles` 에는 남긴다 — 조회 키가 `사업명` 이라 남겨두면 일반·기술트랙 판정에 딸려온다 |
| Stage 2 게이트 | 임베딩 직전 전 청크 토크나이즈 — **1,024토큰 초과 0건** 확인 |

임계가 문자수가 아니라 **토큰수**인 이유: 잘림 방지. 한국어 3,000자 ≈ 1,500~2,000토큰이라
`max_seq_length` 1024 를 넘어 조문 꼬리(단서·예외)가 임베딩에서 소실된다.

### 3-5. 임베딩

| 항목 | 값 |
|---|---|
| 모델 | `nlpai-lab/KURE-v1` (1024차원, bge-m3 계열) |
| 정규화 | L2 정규화 후 저장 → 코사인 = 내적 |
| `max_seq_length` | **1024** (§3-4 분할 임계와 짝) |
| 임베딩 입력 | **컨텍스트 헤더 + 본문** (아래) |
| 실행 | 앱 서버 CPU. 전량 재임베딩 ~40분 — 청킹·모델 변경 비용 |
| 교체 | 골든셋에서 열세면 bge-m3 |

**컨텍스트 헤더** — 조문 단독 임베딩에는 "어느 사업 어느 문서인가"가 없다. 검색 대상 텍스트
앞에 결정론적 구조 헤더를 붙인다 (LLM 생성 문장 금지 — 인덱스에 환각을 심는 경로다):

```
[L2 | 예비창업패키지 | 세부관리기준(2025) | 제4장 사업비 | 제39조(기계장치)]
① 사업계획서 상의 사업화를 위해 ...
```

헤더는 임베딩·BM25 입력 전용 — `chunks.text`(인용 검증 대상)는 불변. 헤더 분량(~50토큰)은
분할 임계 900 의 마진에 반영돼 있다.

**사례 인덱스**: 같은 모델, 단위는 Q&A 한 쌍, **질문 텍스트를 임베딩**(답변 아님).
골든셋 홀드아웃은 인덱싱 전에 제거.

## 4. 검색 — 요청 (3) 단계

쿼리는 코드가 ① 의 JSON 필드로 조립한다. **LLM 이 검색어를 만들지 않는다.**

### 4-1. L3 — 검색하지 않고 통째 로드

```sql
SELECT 조번호, 조제목, 본문
  FROM tenant.l3_articles
 WHERE org_id = :org AND 장 IN (:사업비관련장)   -- 인제스천 시점에 정하는 정적 선택
 ORDER BY 조번호_int;
```

L3 조항(30~80개)을 수천 개짜리 풀과 경쟁시키면 밀려서 안 뽑힐 수 있다 — 경쟁 자체를 없앤다.
전문이 아니라 **사업비 집행 관련 장만** 넣는다 (소형 모델 컨텍스트 예산).

### 4-2. L1·L2 — pre-filter 가 검색보다 먼저

```sql
WHERE status = 'active'
  AND parse_quality = 'high'
  AND retrieval_scope = '진입점'          -- 아래 참조
  AND layer IN ('L1','L2')
  AND 적용대상 IN ('창업기업','공통')      -- NULL 은 통과하지 못한다. 아래
  AND (사업명 IS NULL OR :사업 = ANY(사업명))
```

🔴 **`적용대상` 은 적재 시점에 NULL 이 남아 있으면 안 된다.** SQL 의 `NULL` 은 `IN` 을
통과하지 못하므로 비워 둔 조는 조용히 검색에서 사라진다. Stage 0.5 태깅은 현행 9문서
422조만 걸고, 나머지 L1·L2 264문서 23,324조는 태깅 대상이 아니다 — 「산업안전보건법
시행규칙」에 주관기관/창업기업 구분은 존재하지 않는다. 그래서 **태깅 대상 밖과 부칙·붙임의
기본값은 `공통`** 이다. 태깅 대상 **안**의 NULL 만 2단 LLM 대기이고, 그것만 인덱스에서
제외한다. Stage 2 는 `tag_apply_target.적용대상_of()` 를 거쳐 둘을 가른다.

**`retrieval_scope` — 검색 진입점과 폐포 도착지를 가른다.** `index=true` 229 규범의 조 수를
실측하면 재정경제부 5,819 · 법무부 4,734(민법 1,193 · 상법 1,184 · 형사소송법 610) 인데
중기부 소관은 866 이다. 세법·민상법은 세부관리기준이 인용하니 **코퍼스에 있어야** 하지만,
그건 refs 폐포의 **도착지**이지 검색 **진입점**이 아니다 (§0 — RAG 의 역할은 참조 해소).

- `폐포전용` 문서도 `doc_articles` + `refs` 에는 전량 들어간다. 인용은 정상 작동한다 —
  검색이 아니라 참조로 온다
- 좁히기를 **적재가 아니라 필터로** 한다. 임베딩에서 빼면 되돌리는 데 재임베딩이 든다
- 기본값은 넓은 쪽(`진입점`). 조용히 좁아지는 방향(근거 누락)이 위험하다
- 값 부여 기준은 **미확정** — 골든셋 검색 15문항 Recall@5 로 A/B 후 확정 (§5)
- ⚠️ 부처 메타데이터는 관련성의 대리 지표가 못 된다 — 국고보조금 통합관리지침·보조금법은
  기획예산처 소관이지만 적대적 골든셋 A26 의 정답 근거다

임베딩 코사인 top-50 + BM25 top-50 → RRF → **top-5** (L3 가 별도 유입되므로 8이 아니라 5).

### 4-3. 참조 폐포 — RAG 의 존재 이유

검색이 찾은 건 진입점이지 답이 아니다. 진입점이 가리키는 정의·별표·위임 사슬을 그래프가 끌어온다.

```sql
WITH RECURSIVE 폐포 AS (
    SELECT dst_doc_id, dst_조번호, 1 AS depth, ref_id
      FROM corpus.refs
     WHERE (src_doc_id, src_조번호) IN :진입점집합
       AND 해소상태 <> 'dangling'
    UNION ALL
    SELECT r.dst_doc_id, r.dst_조번호, p.depth + 1, r.ref_id
      FROM corpus.refs r JOIN 폐포 p
        ON (r.src_doc_id, r.src_조번호) = (p.dst_doc_id, p.dst_조번호)
     WHERE p.depth < 3
)
SELECT DISTINCT * FROM 폐포;
```

- 깊이 3 — 더 가면 전체 법령으로 번진다
- `dangling` 은 제외하되 그 사실을 판정에 싣는다 → 근거 불완전 → 판단불가 쪽으로 기운다.
  판정 인덱스 **안의** dangling 만 신호로 취급 (밖은 정상)
- `shifted` 는 보정된 dst 를 쓰되 원래 표기도 전달 — 화면 7 이
  "귀 기관 규정은 제33조라 하지만 현행 기준 제39조입니다" 를 그린다

### 4-4. RRF 결합

```python
K = 60
def rrf(dense_ranked, sparse_ranked, w_dense=0.6, w_sparse=0.4):
    score = defaultdict(float)
    for rank, cid in enumerate(dense_ranked, 1):
        score[cid] += w_dense / (K + rank)
    for rank, cid in enumerate(sparse_ranked, 1):
        score[cid] += w_sparse / (K + rank)
    return sorted(score, key=score.get, reverse=True)[:5]
```

한쪽에만 등장한 청크는 그 항만 0. 1등 점수가 임계치 미만이면 판정을 건너뛰고 판단불가
(예외: L3 게이팅 (3) — `Agent.md` §3-2). 임계치는 골든셋으로 정한다.

## 5. 미결

| # | 항목 | 조건 |
|---|---|---|
| 1 | 청킹 기본 단위 조 vs 항 | **조 기준 + 900토큰 초과 시 항 으로 확정**(2026-08-31). 인용 검증 단위가 조라서 `chunks.text` 와 어긋나지 않는다 |
| 1-b | `retrieval_scope` 값 부여 기준 | 골든셋 Recall@5 A/B (§4-2) |
| 2 | 1만 청크 초과 시 ANN | **발동했다.** 실측 L1·L2 조문 19,660조 → 약 22,400청크(첨부 제외). 적재 후 정확검색 지연을 재고 결정 |
| 3 | L3 원본 보관 여부 | 재파싱 수요 vs 저장 최소화 |
| 4 | Supabase 무료 티어 한도 | 벡터 22,400 × 1024 float4 ≈ 92MB + 본문·refs. 500MB 안에 드는지 적재 후 실측 |
| 5 | **스키마가 `public` 에 있다** | Supabase 는 `public` 을 PostgREST 로 자동 노출한다 — anon 키로 `golden_set` 이 읽힌다. §2-1 대로 `corpus`/`tenant` 로 옮기고 `public` 은 비운다 |
