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
(2) L1·L2 조회 + refs 로 **깊이 1** 폐포 수집 (조 지정된 참조만 — §4-3 실측)
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

✅ **분리 완료** (2026-08-31 실측: `public` 테이블 0개). 정본 DDL 은 `db/init/` 디렉터리 한 벌이다 —
`01_schema.sql`(기존 21테이블) + `02_frontend.sql`(프론트용 신설 4 + 수정 2)
+ `03_input_fields.sql`(입력 필드 3건).
`docker-entrypoint` 가 파일명 순서로 실행하므로 새 컨테이너에서 01 -> 02 -> 03 으로 재현된다.

🔴 **`db/init/` 은 컨테이너를 처음 만들 때만 실행된다.** 살아있는 DB 에 반영하려면 `psql` 로
직접 적용해야 한다. `docker compose down -v` 로 반영하려 들면 적재된 20,518청크가 날아간다.

**02_frontend.sql 신설분** (프론트 프로토타입 연결용. `chunks`·`refs`·`doc_articles` 미접촉 =
재파싱·재임베딩 없음):

| 테이블 | 무엇 | 비고 |
|---|---|---|
| `corpus.check_items` | "결제 전/후 확인" 항목의 **폐쇄 목록** | `code` 가 재판정 간 진행상황을 잇는 키다. 자유 문자열이면 재판정 때 사용자 체크가 날아간다. LLM guided_json enum 이 여기서 생성된다. **39행 적재 완료** (결제전 30 / 결제후 9 · 전량 `verified` · `기본_오프셋일` 전량 · 전부 `사업명 IS NULL`(전 사업 공통)) |
| `corpus.evidence_sources` | 증빙 발급처 안내 | CSV 124행 적재 완료. 판정에 안 쓰고 안내에만 |
| `tenant.expense_plans` | 지출 계획 | 홈·목록·상세·새계획 4화면이 여기 묶인다. 재판정은 `decisions` 에 append 하고 `latest_decision_id` 만 옮긴다 |
| `tenant.plan_tasks` | 체크리스트 = 캘린더 | 같은 행을 `due_date` 유무로 갈라 본다. **`org_id` 를 직접 둔다** — `plan_id` 가 NULL 인 사용자 일정은 plan 경유 RLS 로 격리가 안 되기 때문 |
| `tenant.decisions` **+`plan_id`** | 판정을 계획에 종속 | 없으면 고아 로그가 된다 |
| `tenant.orgs` **+`주소`·`부서`** | 기관 검색 결과 표시값 | |

`expense_plans`·`plan_tasks` 는 `org_isolation` RLS + `updated_at` 트리거를 건다 —
"최근 수정일" 이 앱의 성실성에 의존하면 반드시 틀린다.

**03_input_fields.sql 변경분** (2026-08-31 · 프론트 입력 필드 전수 대조에서 나온 3건.
전부 `IF NOT EXISTS`/존재 검사라 재실행 안전. 라이브 DB 에도 `psql` 로 적용 완료):

| 변경 | 왜 |
|---|---|
| `tenant.expense_plans` **+`사업명`** | 🔴 룰 조회 키가 `사업 x 비목` 인데 계획에 사업을 담을 자리가 없었다. `orgs.사업명[]`·`f_profile.사업명` 은 둘 다 로그인 사용자 것이라 **게스트(`org_id IS NULL`)는 사업을 저장할 데가 아예 없다** — 오케스트레이터가 붙는 순간 게스트 경로가 막힌다. JSONB 안이 아니라 컬럼인 이유는 룰 조회가 매 판정마다 꺼내 쓰기 때문 |
| `tenant.f_personnel.역할` **CHECK** | `대표자 \| 신규채용 \| 기존직원` 으로 닫았다. 제약 없는 TEXT 였어서 "대표"/"대표자"/"CEO" 가 다 다른 값이 될 수 있었고, 그러면 인원수 집계가 조용히 깨진다 — 그 인원수가 **"PC 1인 1대" 한도의 분모**다. 0행일 때가 공짜 |
| `eval.golden_set` **+`비목`·`입력필드`** | 77문항이 전부 자연어라 **폼 경로를 재면 입력이 문항마다 즉흥으로 만들어진다.** `비목` 만 컬럼인 이유는 채점 축이 하나 늘기 때문(판정일치율·인용정확도 옆에 **비목분류 정확도**) |

⚠️ **`tenant.f_exec.인력역할` 은 일부러 열어 뒀다.** 역할만으로는 개인을 식별하지 못해
(신규채용 3명이면 세 행이 같다) 인건비 검증에 실제로 쓰이지 못하는 컬럼이고, 프론트에도
입력칸을 만들지 않기로 했다. `person_id` 로 교체할지는 F3·F4 화면을 만들 때 함께 정한다.

⚠️ `expense_plans.사업명` 에 CHECK 를 걸지 않은 이유 — `rules.사업명`·`decisions.사업명`·
`golden_set.사업명` 이 전부 제약 없는 TEXT 라 여기만 닫으면 적재 경로가 비대칭이 된다.
사업명 8종 강제는 비목 어휘집처럼 한 번에 정리할 일이다.

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

#### `tenant.decisions` 확장 5건 (2026-08-31) — 판정을 처음 돌려보고 드러났다

D6 격리 테스트(72문항)를 실행하니 **판정 출력을 스키마가 다 못 담았다.** 설계 문서에는
있는데 컬럼이 없던 것들이다:

| 컬럼 | 근거 | 없으면 |
|---|---|---|
| `요약` TEXT | `LLM.md` §3-4 [1겹] 필수 필드 | 화면에 띄울 한 문장이 사라진다 |
| `버전스탬프` TEXT | §3-4 [2겹] 명시 | "제14차, 2025.12.23 기준" 을 못 쓴다 |
| `참조사슬` JSONB | §3-4 [2겹] 명시 | 화면 7 "이게 왜 나에게 적용되나" 의 재료가 없다 |
| **`강등사유`** TEXT[] | (신규) | 🔴 **72건 중 27건이 강등됐는데 이유가 통째로 사라진다** |
| `미매핑전제` JSONB | §3-4 — `unmapped_premise` 로깅 대상 | 결핍 루프가 재료를 못 받는다 |

🔴 **`강등사유` 가 가장 중요하다.** 검증기가 "인용 S번호가 컨텍스트 밖" · "verified=false
룰만으로 가능" · "extraction='vlm' 이라 A등급 금지" 같은 이유로 판정을 내린다.
그 사유를 안 남기면 **"왜 조건부로 내려갔나" 를 나중에 설명할 수 없다** —
`Agent.md` §6 이 재현성을 요건으로 못박은 도메인에서 이건 결함이다.

⚠️ 이름 주의: LLM 원출력은 `인용`·`전제`(S번호 배열)이고 검증기 출력은
`인용목록`·`전제목록`(DB 원문까지 채운 객체 배열)이다. **컬럼은 후자를 담는다.**
채점기가 이 차이를 몰라 "인용 0건" 으로 잘못 집계한 적이 있다 (같은 날 수정).

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
    -- 🔴 현물 2컬럼 DROP (2026-08-31 · db/init/04_agent.sql D1-a).
    --    현물 계상은 지출이 아니다 — 이 서비스는 "이 돈 써도 되나"를 판정한다.
    --    현물은 협약 시점에 산정되는 회계 항목이지 집행 승인 대상이 아니다.
    --    손실 1건: 자기부담금 구성비율 검증(현금 5% 이상 / 현물 20% 이하 등)이
    --    불가능해졌다. 되살리려면 현물 총액 1컬럼만 다시 두면 된다.
    정부지원_현금   NUMERIC,
    자기부담_현금   NUMERIC,
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
    -- 🔴 형태 DROP (2026-08-31 D1-a). 현물 집행은 판정 대상이 아니다.
    --    집계 축은 재원 하나로 줄었다.
    거래처      TEXT,                          -- 동일 거래처 누적(2천만원 심의)용
    인력역할    TEXT,                          -- 인별월별 집계용. 이름 아님
    귀속월      DATE,
    금액        NUMERIC NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_exec_agg ON tenant.f_exec (profile_id, 비목, 재원);   -- 형태 제거로 재생성

-- F4 인력. 참여율 상한이 소속기관 유형(100%/130%)으로 갈린다.
CREATE TABLE tenant.f_personnel (
    person_id     BIGSERIAL PRIMARY KEY,
    profile_id    UUID NOT NULL REFERENCES tenant.f_profile(profile_id) ON DELETE CASCADE,
    -- 폐쇄 목록이다 (03_input_fields.sql). 인원수가 "PC 1인 1대" 한도의 분모라
    -- "대표"/"대표자"/"CEO" 가 섞이면 집계가 조용히 깨진다.
    역할          TEXT NOT NULL
                  CHECK (역할 IN ('대표자','신규채용','기존직원')),
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
| 기본 단위 | 1 조 = 1 청크 — ✅ **확정** (2026-08-31). 900토큰 초과 시 항. 재인덱싱 완료: `corpus.chunks` **20,518행** (232문서 · 진입점 1,591 / 폐포전용 18,927) |
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

✅ **2단 LLM 태깅은 2026-08-31 에 완결됐다.** 로컬트랙 컷 후 현행 9문서 **377조 전량 결정 ·
미결 0** (`_apply_target.json.요약.미결=0`, `_apply_target_todo.json.건수=0`). 적재된
20,518청크에 `적용대상` NULL 은 없다 — 공통 20,149 / 주관기관 205 / 창업기업 164.
위 NULL 배제 로직은 폐기가 아니라 **회귀 방어**로 남긴다.

**`retrieval_scope` — 검색 진입점과 폐포 도착지를 가른다.** `index=true` 229 규범의 조 수를
실측하면 재정경제부 5,819 · 법무부 4,734(민법 1,193 · 상법 1,184 · 형사소송법 610) 인데
중기부 소관은 866 이다. 세법·민상법은 세부관리기준이 인용하니 **코퍼스에 있어야** 하지만,
그건 refs 폐포의 **도착지**이지 검색 **진입점**이 아니다 (§0 — RAG 의 역할은 참조 해소).

- `폐포전용` 문서도 `doc_articles` + `refs` 에는 전량 들어간다. 설계상 인용은 참조로 온다
  🔴 **그런데 2026-08-31 실측 결과 실제로는 오지 않는다 — §4-3 하단 참조**
- 좁히기를 **적재가 아니라 필터로** 한다. 임베딩에서 빼면 되돌리는 데 재임베딩이 든다
- 기본값은 넓은 쪽(`진입점`). 조용히 좁아지는 방향(근거 누락)이 위험하다
- ⚠️ 부처 메타데이터는 관련성의 대리 지표가 못 된다 — 국고보조금 통합관리지침·보조금법은
  기획예산처 소관이지만 적대적 골든셋 A26 의 정답 근거다

✅ **값 부여 기준 확정 + 적용 완료 (2026-08-31).** `scripts/retag_scope.py`.

기준은 하나다 — **"이 돈 써도 되나요"의 답이 그 문서 안에 문장으로 적혀 있는가.**
적혀 있으면 `진입점`, 정의·절차만 공급하면 `폐포전용`. 부처로 가르지 않는다.

진입점 3축: (A) 창업지원사업 자체 규율(통합관리지침·세부관리기준·중기부 고시/훈령)
(B) 보조금·사업비 집행/정산/환수 일반(보조금법·국고보조금 통합관리지침·공공재정환수법)
(C) 비목 한도를 직접 규정(공무원 여비규정·연구개발비 사용기준).

```
documents  진입점 84 / 폐포전용 199        chunks  진입점 1,461 / 폐포전용 18,594
판정 후보 청크  19,835 -> 1,252  (-93.7%)
```

**골든셋 65문항 실측 — 15개 지표 전부 상승, 하락 없음:**

| 검색기 | | hit@1 | hit@5 | hit@10 | hit@20 | hit@50 | MRR |
|---|---|---:|---:|---:|---:|---:|---:|
| dense | 전 | 27.7% | 41.5% | 49.2% | 50.8% | 53.8% | 0.340 |
| dense | **후** | **30.8%** | **50.8%** | **60.0%** | **63.1%** | **69.2%** | **0.401** |
| BM25 | 전 | 15.4% | 40.0% | 46.2% | 53.8% | 55.4% | 0.251 |
| BM25 | **후** | **18.5%** | **43.1%** | **52.3%** | **56.9%** | **63.1%** | **0.282** |
| RRF | 전 | 24.6% | 44.6% | 55.4% | 61.5% | 66.2% | 0.336 |
| RRF | **후** | **29.2%** | **49.2%** | **60.0%** | **67.7%** | **69.2%** | **0.390** |

dense hit@50 이 +15.4%p 로 가장 크게 올랐다 — 임베딩 top-50 자리를 민상법·세법이
차지하고 있었다는 뜻이다. 경계 판정 근거는 `retag_scope.py` 주석에 문서별로 남겼다.

🔴 **다만 이 재태깅의 안전망이 지금 꺼져 있다 — §4-3 을 볼 것.**

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

🔴 **조 지정 없는 참조는 확장하지 않는다** (`dst_조번호 IS NOT NULL`) — 2026-08-31 실측.

resolved 17,386건 중 **8,287건(48%)이 「소득세법」처럼 조 없이 법률명만 인용**한 것이다
(2026-08-31 실측. 약칭 해소 후 resolved 는 32,043 으로 늘었으나 조 없는 인용의 성질은 같다).
이걸 "문서 전체"로 해석하면 근로기준법 하나가 6,026청크를 끌고 온다.
실측: 폐포가 끌어온 20,696청크 중 **17,394개(84%)가 조번호 NULL 로 딸려온 문서 통째**였다.

| | 중앙 청크 | 최대 | 중앙 토큰 | hit@5+폐포 |
|---|---:|---:|---:|---:|
| 조 NULL 도 확장 (초판) | 63 | 1,050 | 60,335 | 48.6% |
| **조 지정된 것만** | **19** | **42** | **15,824** | **48.6%** |

**커버리지 손실 0 · 토큰 1/4.** 조 없는 법률명 인용은 "이 법 어딘가"라는 뜻이라
근거로 쓸 수 없다 — 인용은 S번호 추출이므로 조가 특정돼야 한다.

🔴 **깊이는 3 이 아니라 1 이면 충분하다** (2026-08-31 실측).
깊이 1·2·3 의 hit@5+폐포가 **전부 48.6% 로 같다** — 2단계 이상은 커버리지에 1건도
기여하지 않으면서 토큰만 늘린다 (1.6만 -> 3.8만). 코퍼스가 바뀌면 다시 잰다.

- ~~깊이 3~~ -> **깊이 1** — 더 가도 커버리지가 안 오르고 전체 법령으로 번진다
- `dangling` 은 제외하되 그 사실을 판정에 싣는다 → 근거 불완전 → 판단불가 쪽으로 기운다.
  판정 인덱스 **안의** dangling 만 신호로 취급 (밖은 정상)
- `shifted` 는 보정된 dst 를 쓰되 원래 표기도 전달 — 화면 7 이
  "귀 기관 규정은 제33조라 하지만 현행 기준 제39조입니다" 를 그린다

#### ✅ 폐포 도달 복구 (2026-08-31) — `scripts/normalize_refs.py`

`폐포전용`(§4-2)의 안전 근거는 "검색에 안 걸려도 참조로 도달한다" 인데, 실측하면 **도달하지
않는다.**

```
진입점 문서발 resolved 참조                3,907건
  그중 dst 가 documents 에 실재            2,343건
  그중 dst 가 폐포전용 문서                    0건   <- 하나도 없다
전체 dangling 39.3% · 진입점발만 보면 24.4% (1,279/5,233)
```

원인은 **`refs.dst_doc_id` 가 `documents.doc_id` 로 정규화돼 있지 않은 것**이다.
값이 세 갈래로 섞여 있다 — 파일 경로(`법령 PDF/L1_법령/법인세법시행령`),
약칭(`L1_통합관리지침_제14차`), 정상 doc_id.

**결과였던 것: 폐포전용 18,594청크가 검색으로도 참조로도 도달 불가.**

✅ **정규화로 해소했다** (`scripts/normalize_refs.py --apply`):

```
미해소 dst 198종 / 8,715건  ->  189종 8,673건 매핑 (99.5%) · 모호 0 · UPDATE 8,673행
남은 42건은 코퍼스 밖 법령(장애인복지법·기초연금법 등) — 정상 dangling
진입점 -> 폐포전용 resolved 참조   0건  ->  658건
폐포 재귀 CTE 실행 확인: 시작 20노드에서 폐포전용 문서 7개 노드 도달
refs 28,701 · resolved 17,386 · dangling 11,268 불변 (dst_doc_id 값만 고쳤다)
※ 이 표는 2026-08-31 시점. 현재 값은 아래 4-3b
```

매칭은 3단이다 — ① 손으로 정한 약칭 별칭(근거 주석 필수) ② `doc_id` 직접 일치
③ `L1_<이름>_<날짜>` 규칙. **후보가 2개 이상이면 매칭하지 않고 보고한다** —
억지로 붙이면 엉뚱한 조문이 근거로 인용된다.

✅ **근본 수정 완료 (2026-09-01)** — `build_refs.py` 가 처음부터 `doc_id` 를 쓴다.
`normalize_refs.py` 는 멱등 안전망으로 남되 이제 고칠 것이 0건이다.

#### 4-3b. 약칭 해소 · 경계 검사 · 계열 가드 (2026-09-01) — 현재 값

| | 전 | 후 |
|---|---:|---:|
| 엣지 | 28,701 | **44,855** |
| resolved | 17,386 (60.6%) | **32,043 (71.4%)** |
| dangling | 11,268 | 12,769 |
| shifted | 47 | 43 |
| 조 지정 dangling — **진입점 문서발** | 29 | **19** |
| 조 지정 dangling — 폐포전용 문서발 | 66 | 1,142 |
| 중복 엣지(같은 src·문자열·dst) | 미측정 | **0** |

레이어별 (현재): L1 resolved 28,985 / dangling 12,052 · L2 2,500 / 579 · 사례 558 / 138.

- 🔴 **약칭은 문서가 스스로 정의한다.** `법|시행령|시행규칙` 을 "어느 법인지 확정 불가" 로
  무조건 dangling 처리하던 것을 고쳤다. 제1·2조가 `「중소기업창업 지원법」(이하 "법")` 로
  적어둔다 — **문서별** 약칭표를 걷어 해소한다. 전역 별칭 사전 하드코딩은 기각
  (문서마다 「법」이 다르다). 정의 보유 142/283 문서 · 약칭으로 15,204건 해소
- 약칭표는 **문서 전체에서 한 번** 걷는다. 정의는 제1·2조, 사용은 제20조·제51조라
  조 단위 스캔 중에는 만나지 못한다
- 🔴 **경계 검사** — 공백 제거 사본에서 토큰 `법` 이 「부가가치세법」 꼬리에도 걸린다.
  원문 좌표로 되돌려 앞 글자가 한글이면 버린다. `지침|요령|관리기준|기준` 에는 적용하지
  않는다 (「세부관리기준」처럼 앞에 한글이 붙는 것이 정상)
- **범위 문형을 조 문형보다 먼저** 태운다 — `법 제28조부터 제31조까지` 가 조 문형에도
  걸려 같은 참조가 엣지 둘이 됐다
- 🔴 **계열 가드** — 「법인세법」이 「법인세법시행령」에 붙어 있었다(길이차 3). 본법이
  코퍼스에 없어서 생긴 대체다. **없는 것을 없다고 해야 수집 결손이 드러난다.**
  폐포전용발 dangling 이 66 → 1,142 로 는 것은 덮여 있던 결손이 드러난 것이다.
  본법 미수집 상위: 법인세법 425 · 자본시장법 334 · 전자정부법 293 · 지방세법 107.
  전부 폐포전용 계열이라 사업비 판정 영향은 낮다 — 수집 여부는 사람이 정할 문제
- **모법 유도 fallback 은 기각** — 시행령·시행규칙 8건 중 1건이 오매칭
  (`공공기록물…시행규칙` → **시행령**). 이득 8건에 오답 위험 1건은 안 맞는다
- 🔴 **`load_db.py` 를 쓰면 안 된다** — `TRUNCATE corpus.documents CASCADE` 가 청크
  20,518과 임베딩까지 날린다. refs 만 갈아끼우려면 `TRUNCATE corpus.refs` + INSERT
- ⚠️ **`eval.runs` 의 코퍼스버전이 바뀐다** — run 185~189 는 `r28701` 이다.
  refs 가 44,855 인 지금과 **폐포 재료가 다르므로** 검색·판정 지표를 직접 비교하면 안 된다

### 4-4. RRF 결합

✅ **가중치 확정 (2026-08-31)** — 골든셋 70문항 스윕. `K` 는 5·10·20·60·120 을 함께 봤다.

| w_dense | w_sparse | hit@1 | hit@5 | hit@10 | hit@50 |
|---:|---:|---:|---:|---:|---:|
| 1.0 | 0.0 | 25.7% | 47.1% | 57.1% | 67.1% |
| **0.9** | **0.1** | 24.3% | **48.6%** | 57.1% | **70.0%** |
| 0.8 | 0.2 | 21.4% | 45.7% | 57.1% | 70.0% |
| ~~0.6~~ | ~~0.4~~ | 21.4% | 41.4% | 52.9% | 71.4% |

🔴 **초판 사양 0.6/0.4 는 최악에 가까웠다** — hit@5 가 0.9/0.1 보다 **7.2%p 낮다.**
BM25 를 많이 섞으면 정답을 밀어낸다. 소량만 보완재로 쓰는 게 맞다. `K=60` 은 유지 —
5·10·20·120 을 다 재봤고 60 이 가장 좋았다.

⚠️ **과적합 주의.** 70문항에서 1문항 = 1.4%p 다. 0.9/0.1 과 dense 단독의 hit@5 차이는
**1문항**이라 통계적으로 유의하다고 말할 수 없다. 골든셋이 커지면 다시 스윕한다.

```python
K = 60
def rrf(dense_ranked, sparse_ranked, w_dense=0.9, w_sparse=0.1):
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
| ~~1~~ | ~~청킹 기본 단위 조 vs 항~~ | ✅ **종결** — 조 기준 + 900토큰 초과 시 항 으로 확정하고 **재인덱싱까지 마쳤다** (2026-08-31 확정 · 현재 `corpus.chunks` **20,518행** 전량 임베딩). 인용 검증 단위가 조라서 `chunks.text` 와 어긋나지 않는다 |
| ~~1-b~~ | ~~`retrieval_scope` 값 부여 기준~~ | ✅ **종결** — 기준 확정 + 적용 완료 (2026-08-31, `retag_scope.py`). 진입점 84 / 폐포전용 199. 골든셋 65문항 15개 지표 전부 상승, 하락 0 (§4-2 표) |
| ~~1-c~~ | ~~`refs.dst_doc_id` 정규화~~ | ✅ **종결** (2026-08-31, `normalize_refs.py`): 8,673행 정규화, 진입점->폐포전용 resolved 참조 **0 -> 658건**, 폐포 도달 실행 확인. 남은 42건은 코퍼스 밖 법령. ⚠️ `build_refs.py` 재실행 시 되돌아가므로 이어서 `--apply` 필요 (§4-3) |
| **1-d** | **검색 품질 — 랭킹은 손대지 마라** | 2026-09-01 실측: 판정 경로 실효 hit@5 **55.4%**(74문항·필터 on) / 회귀 기준선 **52.9%**(70문항·필터 off). 🔴 **미스 33건 분해 = 필터밖 31.1% / 랭킹 9.5% / 후보밖 4.1%.** 검색기 튜닝으로 고칠 수 있는 건 9.5% 뿐이고, 180조합 스윕이 최고 +1문항(노이즈)이었던 이유가 이것이다. **RRF 가중을 만지지 말 것** — 실제 병목은 필터밖 = 주관기관 화자 지원 여부(`서비스 아키텍쳐.md` §11-14)라는 제품 정의 결정이다 |
| ~~2~~ | ~~1만 청크 초과 시 ANN~~ | ✅ **종결 — ANN 을 쓰지 않는다** (2026-08-31 `scripts/bench_ann.py` 실측). 적재 실측은 예측 22,400 이 아니라 **20,055청크**(현 20,518). 전량스캔 134ms · recall 100% 로 예산(top-50 < 200ms) 안이고, HNSW 는 켜지는 구간(ef≤100)에서 recall 82~91%(최악 30%), 안 켜지는 구간(ef≥200)에서는 플래너가 인덱스를 버린다 — 중간이 없다. 코퍼스가 크게 늘면 같은 스크립트로 재측정 |
| 3 | L3 원본 보관 여부 | 재파싱 수요 vs 저장 최소화 |
| 4 | Supabase 무료 티어 한도 | **적재 후 실측 완료 (2026-08-31): 로컬 DB 478MB · `corpus.chunks` 302MB(20,055행 시점 · 현 20,518).** 500MB 를 거의 다 채운다 — 무료 티어로 갈지는 호스팅 결정(#3 · `서비스 아키텍쳐.md` §11-5)과 함께 **남는다** |
| ~~5~~ | ~~스키마가 `public` 에 있다~~ | ✅ **종결** — `corpus`/`tenant`/`eval` 로 분리 완료, **`public` 테이블 0개** (2026-08-31 실측: `information_schema.tables` where `table_schema='public'`). `golden_set` 은 `eval` 스키마라 anon 키 노출 경로가 없다 |
