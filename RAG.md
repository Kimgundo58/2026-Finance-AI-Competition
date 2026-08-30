# RAG.md — 근거를 어떻게 찾아오나

작성 2026-08-28 · **개정 2026-08-30** (L3 분리 확정 · 청킹 토큰 기준 · 컨텍스트 헤더 · BM25 토큰화 정책 · 스키마 개정) · 대상 저장소 **Supabase (Postgres 17 + pgvector)**

> **이 문서의 범위**: 코퍼스 경계 · 저장소 스키마 · 인덱싱 · 검색 · 참조확장.
> 룰 테이블은 [`rule_base.md`], 프롬프트·모델은 [`LLM.md`], 이 셋을 엮는 순서는 [`Agent.md`].
> 상위 문서는 `서비스 아키텍쳐.md` — 충돌 시 그쪽이 이긴다.

## 0. RAG가 존재하는 이유

**사용자 문서(L3)만으로 답이 안 될 때 참조를 따라가는 것**이다.
"관련 조항 검색"으로 이해하면 설계가 어긋난다.

```
(1) L3 먼저 조회 (가장 구체적)
     없음 / "~에 따른다"만  -> (2)
     불가 / 조건부          -> L3 단독 결론 가능 (틀려도 "틀린 불가" = 안전)
     가능                   -> 상위 확인 강제 (L3 는 추가 제약만 적는다)
(2) L1 / L2 조회 + refs 로 깊이 3까지 폐포 수집
(3) precedence_rules 로 효력 결정 -- L3 가 항상 이기는 게 아니다
```

---

## 1. 코퍼스 경계 (절대 규칙)

**판정 인덱스에 들어가는 것은 L1 · L2 + 현재 기관의 L3 1벌뿐이다.**

| 축 | 원천 | 실측 | 인덱싱 |
|---|---|---:|---|
| L1 | `2026_Finance_DATA_FOR_RAG/중기부/` 배포본 | 9 | O |
| L1 | `법령 PDF/L1_법령/` 중 `index:true` | 219 규범 | O |
| L2 | `2026_Finance_DATA_FOR_RAG/창진원/` 세부관리기준 | 41 | O |
| B급 | `사례집/` + `kosmes_faq.json` | 10 + 194 | O (사례 인덱스) |
| L3 | 사용자 업로드 | **샘플 0** | O (해당 org 전용) |
| — | `PMS/` 화면 매뉴얼 | 42 | **X** — 규범이 아니다 |
| — | 별표 PDF 103건 | 판정 유효 **4건** | 4건만 |
| X | `archive/` · `_골든셋/` · `_테스트_L3/` · `_범위밖_보류/` | — | **조건 없이 거부** |

> ⚠️ **법령 219건은 중기부 것만이 아니다.** 레이어는 **발행주체**로 정하므로 법률·시행령·
> 행정규칙은 누가 참조하든 전부 L1 이다. 조달 경로(누가 참조해서 찾았나)는 별개 축이고
> `_law_sources.json` 의 `sources` 배열에 있다 — **창진원 계통이 96건**(단독 41 + 공통 55)이다.
> 예: 공무원여비규정(숙박비 상한), 국가연구개발사업 연구개발비 사용기준은 창진원 단독 참조다.

거부는 문장이 아니라 코드다 — `scripts/index_guard.py`, 회귀 테스트 `tests/test_index_guard.py`.
**새 인덱싱 경로를 만들면 이 게이트를 반드시 태운다.**

> `PMS/` 42건은 버리는 게 아니다. **F축 입력 UX 설계 자료**다 —
> F1·F3 이 PMS 화면에 이미 있으므로 무엇을 물을지 여기서 정한다 (`서비스 아키텍쳐.md` §2-4).

---

## 2. 저장소 설계 — Supabase

### 2-1. 스키마를 둘로 가른다

```
schema corpus   공개 규범.  전 사용자가 같은 것을 본다.  RLS 없음
schema tenant   사용자 것.  org 별로 격리.              RLS 필수
```

**왜 나누나** — RLS 를 전 테이블에 걸면 ① 벡터 검색마다 정책 평가가 붙어 느려지고
② "이 테이블은 왜 안 걸려 있지?" 를 매번 확인해야 한다.
공개 코퍼스에는 정책이 필요 없다. **정책이 필요한 것만 한 스키마에 모아 전부 건다.**

### 2-2. `corpus` — 기존 스키마 유지

`db/init/01_schema.sql` 의 테이블을 그대로 옮긴다. 변경점은 §2-3 하나뿐.

| 테이블 | 내용 | 벡터 |
|---|---|---|
| `documents` | 문서 대장. `index_target` 이 인덱싱 여부 | |
| `doc_articles` | 조 단위 원문. diff 전용 문서도 여기까지 | |
| `chunks` | **판정 인덱스 (A등급)** | `vector(1024)` |
| `case_chunks` | **사례 인덱스 (B등급)** | `vector(1024)` |
| `rules` / `precedence_rules` | [`rule_base.md`] 참조 | |
| `refs` | 참조 그래프 1,136 엣지 | |
| `item_alias` | 상품명 -> 비목 별칭 사전 | |
| `golden_set` | 평가용. **인덱스 투입 금지** | |

**`chunks` 와 `case_chunks` 는 물리적으로 분리한다.** 프롬프트가 아니라 함수 레벨에서 강제 —
검색 함수는 `chunks` 만 SELECT 한다. 사례집은 사용자 질문과 말투가 닮아 한 인덱스에 두면
스코어 경쟁에서 규정 원문을 이긴다.

**ANN 인덱스(HNSW/IVFFlat)를 만들지 않는다.** 근사 검색의 리콜 손실 = 인용 누락 = 오답.

### 2-2-b. 스키마 개정 목록 (2026-08-30 — `01_schema.sql` 에 반영)

문서가 전제하는데 정본 스키마에 없던 것들을 반영했다. 기존 컨테이너는 재적재 필요
(`docker compose down -v` 후 `up -d`).

| 대상 | 변경 | 왜 |
|---|---|---|
| `chunks.적용대상` | 컬럼 추가 (`주관기관\|창업기업\|공통`, Stage 0.5 가 채움) | §4-2 필터의 전제인데 스키마에 없었다 |
| `chunk_terms` / `chunk_len` / `term_df` | 신설 (§2-5-1 DDL) | BM25 를 DB 로 옮긴 결정의 물리화 |
| `decisions.전제` (JSONB) | 컬럼 추가 | LLM 출력 [1겹]의 전제[]를 로그에 남긴다 |
| `decisions.검색스냅샷` (JSONB) | 컬럼 추가 — S번호→(chunk_id, 항호) 매핑 + top-k 목록 | 재현성(§7 Agent.md): 이게 없으면 "판정 이력을 나중에 설명" 이 불가능 |
| `decisions.코퍼스버전` (TEXT) | 컬럼 추가 | 판정 시점의 인덱스 버전 스탬프 |

### 2-3. L3 를 `chunks` 에서 빼낸다 (✅ 2026-08-30 확정)

현재 스키마는 L3 를 `chunks` 에 `기관ID` 컬럼으로 섞고, 검색에서
`layer IN ('L1','L2') OR (layer='L3' AND 기관ID=:현재기관)` 로 거른다.

**이걸 별도 테이블 `tenant.l3_articles` 로 뺄 것을 제안한다.**

| | 섞어 두기 (현행) | 빼내기 (제안) |
|---|---|---|
| 누수 위험 | 필터를 한 번만 빠뜨려도 샌다 | **다른 테이블이라 구조적으로 불가능** |
| RLS | `chunks` 전체에 정책이 걸린다 | `tenant` 에만 걸린다 |
| 벡터 | L3 도 임베딩해야 함 | **불필요** — 통째로 로드하므로 |
| 근거 | | CLAUDE.md "L3 는 검색하지 않고 통째로 로드한다" 와 일치 |

이미 확정된 "L3 는 검색하지 않는다" 를 저장소까지 밀어낸 것뿐이다.
**2026-08-30 확정 — CLAUDE.md 의 WHERE 절 표현도 같이 갱신했다** (검색은 L1·L2 만,
L3 는 `tenant.l3_articles` 통째 로드).

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
                 CHECK (extraction IN ('native','dedupe','hancom','vlm')),
    파싱품질     TEXT NOT NULL CHECK (파싱품질 IN ('pass','warn','fail')),
    dangling수   INT NOT NULL DEFAULT 0,   -- 업로드 시점에 알린다 (판정 시점 아님)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 벡터 컬럼이 없다. 검색하지 않기 때문이다.
CREATE TABLE tenant.l3_articles (
    article_id  BIGSERIAL PRIMARY KEY,
    doc_id      UUID NOT NULL REFERENCES tenant.l3_documents(doc_id) ON DELETE CASCADE,
    org_id      UUID NOT NULL,          -- RLS 를 위해 비정규화한다
    조번호      TEXT NOT NULL,
    조제목      TEXT,
    조번호_int  INT,
    본문        TEXT NOT NULL,
    페이지      INT,
    UNIQUE (doc_id, 조번호)
);
CREATE INDEX ix_l3_org ON tenant.l3_articles (org_id, doc_id, 조번호_int);

-- ── 사용자 축 테이블 (2026-08-30 추가 — 문서 그림에만 있고 DDL 이 없던 것들) ──
-- 이름 저장 금지 원칙(서비스 아키텍쳐.md §6): 인력은 역할·수치만 둔다.

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
    -- F1 은 재원 x 형태 4분면 (서비스 아키텍쳐.md §2-3)
    정부지원_현금   NUMERIC, 정부지원_현물 NUMERIC,
    자기부담_현금   NUMERIC, 자기부담_현물 NUMERIC,
    과업범위요약    TEXT,          -- F2. 원문은 저장하지 않는다
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, 사업명)
);

-- F3 집행내역. 집계 축 3개(비목별·거래처별·인별월별)가 전부 이 한 테이블에서 나온다.
CREATE TABLE tenant.f_exec (
    exec_id     BIGSERIAL PRIMARY KEY,
    profile_id  UUID NOT NULL REFERENCES tenant.f_profile(profile_id) ON DELETE CASCADE,
    비목        TEXT NOT NULL,                 -- 비목 어휘집(LLM.md §3-7)의 enum
    재원        TEXT NOT NULL CHECK (재원 IN ('정부지원','자기부담')),
    형태        TEXT NOT NULL CHECK (형태 IN ('현금','현물')),
    거래처      TEXT,                          -- 거래처별 누적(외주 2천만원 합산)용
    인력역할    TEXT,                          -- 인별월별 집계용. 이름 아님
    귀속월      DATE,
    금액        NUMERIC NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_exec_agg ON tenant.f_exec (profile_id, 비목, 재원, 형태);

-- F4 인력. 참여율 판정에 소속기관 유형이 필요하다 (130% 분기).
CREATE TABLE tenant.f_personnel (
    person_id     BIGSERIAL PRIMARY KEY,
    profile_id    UUID NOT NULL REFERENCES tenant.f_profile(profile_id) ON DELETE CASCADE,
    역할          TEXT NOT NULL,               -- 대표자 | 신규채용 | 기존직원 ... 이름 아님
    고용형태      TEXT,
    타사업참여율  NUMERIC,
    소속기관유형  TEXT,                        -- 인건비 확보 여부 분기 (rule_base.md §2)
    겸직          BOOLEAN
);

-- 결핍 루프 (서비스 아키텍쳐.md §5-4)
CREATE TABLE tenant.unmapped_premise (
    id           BIGSERIAL PRIMARY KEY,
    premise_text TEXT NOT NULL,
    근거조항     JSONB,                        -- {doc_id, 조번호}
    사업명       TEXT,
    비목         TEXT,
    발생횟수     INT NOT NULL DEFAULT 1,
    최초         TIMESTAMPTZ NOT NULL DEFAULT now(),
    최근         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (premise_text, 사업명, 비목)
);
```

### 2-4. RLS 는 2차 방어선이다

```sql
ALTER TABLE tenant.l3_articles ENABLE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON tenant.l3_articles
    USING (org_id = (auth.jwt() ->> 'org_id')::uuid);
```

**1차는 코드다.** FastAPI 가 psycopg 로 직접 붙으므로 서비스 롤이면 RLS 가 우회된다.
따라서 트랜잭션마다 사용자 JWT 클레임을 심어야 정책이 산다:

```sql
SET LOCAL request.jwt.claims = '{"org_id":"..."}';
```

이게 커넥션 풀과 잘 안 맞는다는 걸 감안해, **앱이 항상 `org_id` 를 명시적으로 거는 것을 1차로,
RLS 를 그물망 2차로** 둔다. `index_guard.py` 와 같은 철학 — "실수로 안 하기" 가 아니라 두 겹.

### 2-5. Supabase 가 **못 하는 것** — 설계에 미리 반영

| 못 하는 것 | 왜 | 우리 대응 |
|---|---|---|
| KURE-v1 임베딩 | 확장이 아니라 모델이다 | **앱 서버가 인코딩**해 vector 를 파라미터로 보낸다 |
| 한국어 **형태소 분석** | `pg_bigm` · `mecab-ko` · `kiwi` 설치 불가 | **앱이 분석하고 결과 토큰만 적재** (§2-5-1) |
| LLM 호출 | | 앱 서버의 어댑터가 부른다 |

### 2-5-1. BM25 를 DB 안으로 끌어온다 (2026-08-28 변경)

**기존 결정 폐기.** `Rag_Agent구현 파이프라인.md` §4.4 는 `bm25s` 인덱스를 **앱 메모리**에
직렬화 파일로 두기로 했었다. Supabase 로 가면 이게 문제가 된다:

```
Supabase                       앱 서버 메모리
  chunks / embedding             bm25s 인덱스 (파일)
      |                              |
      +-- 재인덱싱 시 원자적으로 못 바꾼다 --+
          한쪽만 갱신되면 조용히 틀린 검색을 한다
```

부수 피해 — 워커마다 사본을 들어야 하므로 **KURE-v1(1.5GB)과 RAM 을 다투고**, 앱이 상태를
가지므로 스케일아웃이 막힌다.

**숨은 가정을 걷어낸다.** "Supabase 가 한국어 분석기를 못 깐다" 는 맞다. 그런데 거기에
*"형태소 분석을 DB 가 해야 한다"* 는 가정이 붙어 있었다. **안 해도 된다** —
분석은 앱이 색인 시점에 하고, **결과 토큰만 넣으면** DB 는 세고 정렬하기만 하면 된다.

#### 검토한 3안

| | 방식 | 원자성 | 랭킹 | 구현비 |
|---|---|---|---|---|
| A | 현행 + 버전 스탬프 대조 | X (감지만) | BM25 정확 | 최소 |
| B | 사전 토큰화 -> `tsvector` + `ts_rank_cd` | O | **IDF 없음** | 낮음 |
| **C** | **BM25 를 SQL 테이블로** | **O** | **BM25 정확** | 중간 |

**C 채택.** B 를 버린 이유는 `ts_rank_cd` 에 IDF 가 없기 때문이다. 이 도메인은
"범용성" · "직접 연관성" 같은 **희귀어가 결정적**이라 IDF 를 버리면 BM25 를 쓰는 이유의
절반이 날아간다. A 는 문제를 **감지만 하고 해결하지 않는다.**

#### 스키마

```sql
CREATE TABLE corpus.chunk_terms (           -- kiwipiepy 분석 결과를 적재
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

#### 검색 (k1=1.2, b=0.75)

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

**규모**: 1만 청크 x 고유 term 평균 80 = 80만 행. 쿼리 term 10개면 인덱스로 수천 행만 건드린다.

#### 무엇이 해결되나

```
원자성      재인덱싱이 트랜잭션 하나가 된다
              TRUNCATE chunk_terms; INSERT; REFRESH MATERIALIZED VIEW term_df; COMMIT
            -> 버전 스탬프 대조도, 기동 거부 로직도 필요 없어진다
RAM         워커마다 들던 BM25 사본이 사라진다 -> KURE-v1 워커를 더 띄울 수 있다
스케일아웃  앱 서버가 상태를 안 가진다
디버깅      "왜 이 조항이 안 걸렸지" 를 SQL 로 직접 캐물을 수 있다
            (앱 메모리 안의 bm25s 는 들여다볼 수 없다)
```

#### 남는 것

- **`kiwipiepy` 는 여전히 앱에 있다** — 색인 시(오프라인) + 쿼리 토큰화 시(온라인).
  다만 **인덱스가 아니라 분석기만** 들고 있으므로 메모리가 훨씬 가볍다
- ⚠️ **`bm25s` 와 같은 순위가 나오는지 검증이 필요하다.** 같은 코퍼스 · 같은 쿼리로
  top-20 겹침률을 잰다. **골든셋 평가보다 먼저** 할 것 — 검색이 흔들리면 평가가 무의미하다

#### 토큰화 정책 (2026-08-30 확정 — 색인과 쿼리에 동일 적용)

이게 없으면 위 동등성 검증 자체가 재현이 안 된다.

| 항목 | 값 |
|---|---|
| 분석기 | kiwipiepy 기본 모델 |
| 채택 품사 | 체언 NNG·NNP·NNB / 용언 어간 VV·VA / 어근 XR / 외국어 SL / 숫자 SN / 한자 SH |
| 정규화 | 영문 소문자화. 그 외 원형 유지 |
| 불용어 | 별도 목록 없음 — IDF 가 고빈도어를 자연 감쇠시킨다 |
| 복합명사 | kiwi 기본 분해를 따른다 ("범용성" 같은 결정어가 쪼개지는지 골든셋에서 확인) |

### 2-6. Storage

L3 업로드 원본은 Supabase Storage 비공개 버킷에 둔다.
`서비스 아키텍쳐.md` §6 원칙("문서 원문은 파싱 후 버리고 구조화된 결과만 남긴다")에 따라
**파싱 성공 시 원본을 삭제**한다. 다만 L3 는 재파싱 수요가 있어 예외 여지가 있다 — 미결.

### 2-7. 마이그레이션 경로

```
현행                          이행
docker compose + pgweb   ->   supabase CLI (로컬 = supabase start)
db/init/01_schema.sql    ->   supabase/migrations/0001_corpus.sql
                              supabase/migrations/0002_tenant_rls.sql
```

스키마는 **한 벌만** 둔다. 로컬과 원격이 갈리는 순간 어느 쪽이 진짜인지 알 수 없게 된다.

### 2-8. 앱 서버 호스팅 — 사양이 곧 동시성 한계다

병목은 DB 가 아니라 **임베딩 모델의 메모리**다 (KURE-v1 워커당 ~1.5GB).

| 안 | 사양 | 비용 | 워커 |
|---|---|---|---|
| VM (B2s급) | 2 vCPU / **4GB** | 유료 | **1~2개** |
| HF Spaces (Docker) | 2 vCPU / **16GB** / 디스크 50GB(비영속) | **PRO $9/월** | 여유 |

🔴 **`Rag_Agent구현 파이프라인.md` §9 의 "HF Spaces 무료" 전제는 틀렸다.**
2026-07-15 경 **Gradio·Docker Space 는 유료 플랜(PRO $9/월)** 이 있어야 생성된다.
Static Space 만 무료다. 사양(2 vCPU / 16GB)은 맞다.

→ 6주 MVP 기준 $9 는 무시할 수 있는 비용이고, **16GB 면 워커 병목이 사라진다.**
`서비스 아키텍쳐.md` §3 의 "VM 1대 B2s" 토폴로지를 재검토할 것 (미결).
DB 를 Supabase 로 빼는 이상 앱 서버에 Postgres 를 얹을 이유도 없어졌다.

---

## 3. 인덱싱 파이프라인 (오프라인)

```
원문 (XML / PDF / HWP->PDF)
  |
  +- Stage 0    파싱, 조 단위 분해                -> corpus.doc_articles
  |    pdftext.py 경유 필수 (문자중복 레이어)
  |    표 추출을 텍스트 추출과 별도로 돌린다 -- 미구현
  |      L2 세부관리기준 뒤 [참고N] 에 비목 카탈로그 / 증빙 매핑이 표로 있다
  |      실측 표 수: 예비 13 / 초기 14 / 창업중심대학 40 / 초격차 48 / 도약 42
  |      raw text 로만 뽑으면 "증빙표가 없다" 는 오판을 낳는다
  |    다단 레이아웃 5건: 좌표 기반 컬럼 분리 (초격차는 4분면)
  |    TIPS 는 조 체계가 아님 -> outline_numbered 전략 (1. -> 가. -> 1))
  |
  +- Stage 0.5  적용대상 태깅                     -> corpus.chunks.적용대상
  |    {주관기관 | 창업기업 | 공통}
  |    절(節) 헤딩이 이미 선언한다 -> LLM 은 절 밖 조문만
  |
  +- Stage 0.7  참조 그래프 (정규식)              -> corpus.refs
  |    1,288 엣지 / resolved 1,059 / shifted 41 / dangling 188   (2026-08-30)
  |
  +- Stage 0.8  우선순위 조항                     -> corpus.precedence_rules
  |    -> rule_base.md
  |
  +- Stage 1    룰 컴파일                         -> corpus.rules
  |    -> rule_base.md
  |
  +- Stage 2    청킹 + 임베딩 + BM25              -> corpus.chunks / bm25 파일
```

**적용대상 태깅은 임베딩보다 먼저다.** 청크 적재 시 필드가 채워져 있어야 필터가 걸린다.
조 단위로 태깅하면 항으로 쪼개도 상속되므로 **청킹 단위 결정을 기다릴 필요가 없다.**

### 3-1. 조문 재조립 (Stage 0 핵심 사양)

> 2026-08-28 `Rag_Agent구현 파이프라인.md` §2.3-0d 에서 이관.

평문 -> 조 단위 분할. **구조가 문서마다 다르므로 섹션 분리 후 3단 fallback 을 적용한다.**

**Step 1 — 섹션 분리 (선행 필수)**

| 섹션 | 처리 |
|---|---|
| 붙임 · 별표 · 별지 · 서식 | **각각 독립 조로 분리.** 조번호 = `붙임2`, `별표1` … |
| 부칙 | 조번호에 `부칙 ` 접두사. 단조성 검증에서 제외 |
| 본칙 | 아래 3단 fallback |

> 🔴 **이 단계를 빠뜨리면 인용 위치가 틀린다.** 실측: 부칙의 `제1조(시행일)` 가 본칙 `제1조(목적)` 와
> 번호가 겹쳐 덮어썼고, 그 조가 문서 끝까지(붙임1·붙임2 포함) 삼켜 **7,103자**가 됐다.
> **붙임2 내용이 "제1조"로 인용되는 사고**다. 섹션 분리 후 제1조는 정상적으로 **115자**가 됐다.
>
> 붙임/별표 헤더는 본문 참조(`[붙임 2]에서 정하는 바에 따른다`)와 구분해야 한다.
> 🔴 **"문서 후반부(45% 이후)" 조건은 2026-08-30 폐기했다.** 양방향으로 틀렸다:
> 창업중심대학 `[참고1]`(33%)을 놓쳐 **부칙이 비목 정의·집행기준을 5,268자 삼켰고**,
> 모두의창업 본문의 `[별지2]을 준수하여…`(56%)를 섹션으로 오인해 **23,729자짜리 가짜 붙임**을 만들었다.
> → **순서: 목차 컷 → 부칙 → 붙임(부칙 뒤에서만).** 실측 8건에서 진짜 섹션 헤더 21개는
> 전부 부칙 뒤, 오탐 4개는 전부 부칙 앞이었다.
> 부칙 판별은 **뒤 300자의 "시행" 언명**으로 한다 — 위치 비율(초격차 27%)도 조 개수(TIPS 1개)도 못 쓴다.
> 목차는 점선 지도 10회 이상 + 마지막이 앞 30% 안이면 컷 (TIPS 96 / 도약 51 / 모두의창업 38회).

**Step 2 — 4단 fallback**

| 우선순위 | 전략 | 패턴 | 적용 문서 |
|---|---|---|---|
| 1 | `jo_titled` | `제(\d+)조\(제목\)` | 법령·지침·규정·규칙 (실측 29/48) |
| 2 | `outline_numbered` | `제N장` → `N.` → `가.` | **TIPS 총괄 운영지침 계열 (5/48)** |
| 3 | `jo_bare` | 제목 없는 `제(\d+)조` | 일부 조례 |
| 4 | `jang` | `제(\d+)장` | 매뉴얼·가이드라인 |
| 5 | `paragraph` | 빈 줄 2개 기준 단락 분할 | 위 전부 실패 시 |

🔴 **`outline_numbered` 가 `jo_bare` 보다 앞이다.** TIPS 본문에는 타 법령 인용
(`「중소기업창업지원법」 제29조`)이 널려 있어 `jo_bare` 로 떨어뜨리면 **인용을 조 헤딩으로 오인**한다.
실측: 옛 코드에서 TIPS 6건이 `jo_bare` 로 가 조 32개·역전 7건이 나왔다. 지금은 0건이다.

중첩 열거(`1. 과제수행과 관련이 없거나 …`)도 표제와 표기가 같아 정규식으로는 못 가른다.
**번호 단조성**으로 고른다 — 표제 번호는 장을 넘어 이어지고(제2장이 29~31), 중첩 열거는 1부터 다시 돈다.
실측(TIPS 2026): 후보 335개 → 표제 35개(1..35, 역전 0), 목차와 정확히 일치.

**Step 2-b — 조번호 중복은 목차 중복이 아니다**

같은 조번호가 두 번 나오는 경우가 둘인데 처리가 정반대다.

| 경우 | 판별 | 처리 |
|---|---|---|
| 목차 중복 | 조제목이 **같다** | 긴 쪽 하나만 남긴다 |
| **원문 오류** | 조제목이 **다르다** | **둘 다 남긴다.** 뒤엣것에 `[2]` |

> 🔴 실측(창업중심대학 2025): 원문에 제35조가 둘이다 — 제35조(이의신청) · 제35조(권리 의무 이전).
> 조번호만으로 묶어 "긴 쪽 채택" 하면 **조제목은 '이의신청' 인데 본문은 '권리 의무 이전'** 인
> 레코드가 만들어지고 이의신청 조문은 코퍼스에서 사라진다. 인용하면 완전한 오답이다.
> `UNIQUE(doc_id, 조번호)` 라 접미 없이는 둘을 같이 넣을 수도 없다.
> 이때 생기는 조번호 역전은 파싱 실패가 아니므로 V1 이 아니라 `원문오류` 플래그로 따로 센다.

fallback 2·3 으로 떨어진 문서는 `parse_quality='low'` 이고 **판정 인덱스에서 제외**한다.
구조가 없으면 인용 단위가 성립하지 않는다.

**Step 3 — 문자 위생 (Postgres 적재 전 필수)**

| 문자 | 문제 | 처리 |
|---|---|---|
| `NUL` (0x00) | Postgres text 에 넣을 수 없음 -> `DataError` | 제거 |
| 짝 없는 서로게이트 (U+D800~DFFF) | UTF-8 인코딩 오류 | 제거 |

실측: 협성대 대학혁신지원사업 지침 PDF 에 NUL, HWP 별표 파일에 서로게이트가 있었다.

### 3-2. 파싱 검증 게이트

통과하지 못하면 인덱싱하지 않는다.

| # | 검사 | 실패 시 |
|---|---|---|
| V1 | 조 번호 **단조 증가** | 플래그 -> 사람 확인 (예비 세부관리기준 제30->65->31 이 실측 케이스) |
| V2 | 조 개수 >= 5 | 파싱 실패로 간주 |
| V3 | 빈 조(본문 50자 미만) 비율 < 10% | 텍스트 추출 실패 의심 |
| V4 | **크로스 레퍼런스 검증** (§3-3) | 조번호 참조 불일치 리포트 |

> 구 V4(manifest `status`)·V5(HWP 변환본 대조)는 2026-08-28 삭제.
> manifest 는 폐기됐고 HWP 변환은 완료됐다.

### 3-3. 크로스 레퍼런스 — 참조가 실제로 깨져 있다

세부관리기준 제22조①: *"창업기업 사업비 비목은 **지침 제33조부터 제42조까지** 정하는 바에 따른다"*

| 판본 | 창업기업등 사업비 비목 | 재료비 | 외주용역비 | 기계장치 | 광고선전비 |
|---|---|---|---|---|---|
| 제12차 | **제33조** | 제34조 | 제35조 | 제36조 | **제42조** |
| 제13차 | 제39조 | 제40조 | 제41조 | 제42조 | — |
| **제14차 (현행)** | **제36조** | **제37조** | **제38조** | **제39조** | **제45조** |

**"제33조~제42조" 는 제12차 조번호와 정확히 일치한다.** 2025년판 세부관리기준이
두 세대 전 지침을 참조하고 있다.

| 결과 | 대응 |
|---|---|
| "지침 제33조" 를 그대로 따라가면 제14차 **제33조(회의비)** 를 가져온다 | **조번호가 아니라 조 제목(비목명)으로 매칭** |
| L3 -> L2 참조가 일반적으로 깨질 수 있다 | 모든 `지침 제N조` 참조를 추출해 현행 조 제목과 대조, 불일치를 `xref_mismatch` 에 기록 |

`build_refs.py` 가 이미 구현했다 — **shifted 35건**이 조제목 매칭으로 자동 보정된 실측이다.
`Agent.md` §9-1 A4 개정 대응의 실증 소재이기도 하다.

### 3-4. 청킹 정책

**단위는 조(條).** 인용 단위가 "제15조 제2항" 이므로 청크 경계와 인용 경계가 일치해야
화면 7 의 원문 하이라이트가 성립한다. **512토큰 고정 청킹 금지.**

| 규칙 | 값 |
|---|---|
| 기본 단위 | 1 조 = 1 청크 |
| 분할 임계 | 조 본문 **KURE 토크나이저 900토큰 초과** -> 항(①②③) 단위 |
| 재분할 임계 | 항도 900토큰 초과 -> 호(1.2.3.) 단위 |
| 병합 | 청크 **50자 미만** -> 직전 청크에 병합 (조 제목만 있는 껍데기 방지) |
| 오버랩 | **없음.** 조 경계가 곧 의미 경계 |
| 표 | 청크에 **넣지 않음.** 룰 테이블로 ([`rule_base.md`]) |

🔴 **분할 임계를 문자수(구 3,000자)에서 토큰수로 재정의했다** (2026-08-30).
한국어 3,000자는 이 토크나이저로 약 1,500~2,000토큰이라 `max_seq_length` 1024 를 넘고,
**긴 조문의 꼬리(단서·예외가 몰리는 곳)가 임베딩에서 조용히 잘리고 있었다.**
900 은 §3-5 헤더 분량을 뺀 안전 마진이다. **Stage 2 게이트**: 임베딩 직전 전 청크를
토크나이즈해 **1,024토큰 초과 0건**을 확인한다 — 초과가 나오면 분할 로직 버그다.

⚠️ **조 vs 항은 아직 미확정이다** (§5 미결 #1). 위 값은 "조 기본 + 초과 시 항" 안이다.
임계의 **정의(토큰 기준)** 는 확정이고, 기본 단위의 선택만 남았다.

### 3-5. 임베딩

| 항목 | 값 |
|---|---|
| 모델 | `nlpai-lab/KURE-v1` |
| 차원 | **1024** (bge-m3 기반이라 스키마 호환) |
| 정규화 | L2 정규화 후 저장 -> 코사인 = 내적 |
| `max_seq_length` | 🔴 **1024 로 낮출 것** (기본값 8192) — §3-4 분할 임계(900토큰)와 짝 |
| **임베딩 입력** | 🆕 **컨텍스트 헤더 + 본문** (아래) |
| 실행 | 앱 서버 CPU. 인제스천 1회 + 쿼리당 1회(~150ms) |
| 교체 비용 | 재임베딩 1회. 골든셋에서 열세면 bge-m3 로 전환 |

> 🔴 `max_seq_length` 기본값 8192 는 조 단위 청크에 과도하고
> 긴 청크에서 attention 비용이 급증한다. 잘림 방지는 §3-4 의 토큰 기준 분할이 맡는다.

#### 컨텍스트 헤더 (2026-08-30 신설)

조문 하나만 임베딩하면 "어느 사업의 무슨 문서인가" 정보가 벡터에 없다 —
"제39조 ②" 는 문서 밖에서 아무 의미가 없다. **검색 대상 텍스트 앞에 구조 헤더를 붙인다:**

```
[L2 | 예비창업패키지 | 세부관리기준(2025) | 제4장 사업비 | 제39조(기계장치)]
① 사업계획서 상의 사업화를 위해 ...
```

- 헤더는 **임베딩·BM25 입력 전용**이다. `chunks.text`(저장 원문·인용 검증 대상)는 **불변**
- 결정론적 조립(메타데이터 연결)이다 — LLM 생성 문장을 헤더로 쓰는 안(Anthropic Contextual
  Retrieval)은 인덱스에 환각을 심는 경로라 채택하지 않는다. 법령은 구조 헤더로 충분하다
- BM25 쪽 헤더 토큰(사업명 등)은 전 청크에 공통이라 IDF 가 자연 감쇠시킨다
- 헤더 분량(~50토큰)이 §3-4 분할 임계 900 의 마진에 이미 반영돼 있다

**실측 소요 (Core Ultra 7 255H, 16코어, CPU only)** — 판정 2,096 + 사례 242 청크,
약 **1건/초**, 총 **35~40분**. 초기 추정 "2~5분" 은 틀렸다.
**재임베딩이 필요한 변경(청킹 정책·모델 교체)은 그만큼 비용이 든다.**

**사례 인덱스도 같은 모델**을 쓴다(비교 가능성). 단 청킹이 다르다:

| 항목 | 값 |
|---|---|
| 단위 | Q&A 한 쌍 |
| 임베딩 대상 | **질문 텍스트** (답변 아님) |
| 이유 | 사용자 질문 <-> 사례집 질문 매칭이 질문 <-> 답변보다 정확 |
| 필수 필드 | `출처도메인` (R&D / 보조금 / 창업) |
| 홀드아웃 | 골든셋으로 뺀 Q&A 는 **인덱싱 전에 제거** |

---

## 4. 검색 — 요청 (3) 단계

```
쿼리 (코드가 (1)의 JSON 필드로 조립. LLM 이 검색어를 만들지 않는다)
  |
  +- KURE-v1 -> 1024차원  -> pgvector 코사인  --+
  |                                             +- RRF 융합 -> top-8
  +- kiwi 토큰화 -> BM25 (SQL, §2-5-1)  -------+
  |
  +- 필터 (없으면 recall 을 아무리 올려도 무의미)
       사업명 && / status='active' / 적용대상 IN ('창업기업','공통')
  |
  +- 참조 확장 (코드, LLM 0회, ~10ms)
       정의 / 별표 / 위임 사슬 / 우선순위 / L3 대응 조항
```

**적용대상 필터가 핵심이다.** 없으면 창업팀의 "노트북 사도 되나요?" 에
주관기관 전담인력 인건비 조항이 검색된다. 8개 사업 실측에서 주관기관 언급이
창업기업 계열보다 많은 사업이 4개였다.

### 4-1. L3 는 검색하지 않는다

```sql
-- 검색 없음. 해당 기관의 사업비 관련 장을 통째로 가져온다.
SELECT 조번호, 조제목, 본문
  FROM tenant.l3_articles          -- §2-3 확정안 (2026-08-30). L3 는 chunks 에 없다
 WHERE org_id = :org AND 장 IN (:사업비관련장)   -- 인제스천 시점에 정해둔 정적 선택
 ORDER BY 조번호_int;
```

**왜 검색하지 않는가** — L3 조항은 30~80개, L1·L2 는 수천~만 개다.
한 풀에 넣고 top-N 을 뽑으면 **사용자가 올린 문서가 숫자에 밀려 한 건도 안 뽑힐 수 있다.**
스코어 튜닝이나 쿼터로 막을 게 아니라 **애초에 경쟁시키지 않는 것**이 맞다.

| | 효과 |
|---|---|
| L3 가 컨텍스트에서 밀릴 확률 | **0** — 구조적으로 보장 |
| 검색 리콜 손실 | **없음** — 전문을 넣으므로 |
| 스키마 분리 필요성 | **없음** — 다른 경로면 충분 |

> 🔑 **전문이 아니라 "사업비 집행 관련 장" 만 넣는다.**
> 규모 실측(건국대): 붙임1 10쪽 13,304자 / 붙임2 30쪽 34,888자.
> 전문 투입은 로컬 소형 모델에 부담이다. 이 선택은 **인제스천 시점에 1회 정하는 정적 값**이지
> 런타임 판단이 아니다.

### 4-2. L1·L2 하이브리드 — pre-filter 가 검색보다 먼저

구버전·타사업 조항이 검색되는 사고는 **스코어 튜닝이 아니라 필터로** 막는다.

```sql
WHERE status = 'active'
  AND parse_quality = 'high'
  AND layer IN ('L1','L2')            -- L3 제외: §4-1 에서 통째로 들어옴
  AND 적용대상 IN ('창업기업','공통')
  AND (사업명 IS NULL OR :사업 = ANY(사업명))
```

top-8 -> **top-5** 로 줄인다. L3 가 별도 경로로 들어오므로 컨텍스트 예산을 나눈다.

### 4-3. 참조 폐포 — RAG 의 존재 이유

검색이 찾은 건 **진입점**이지 답이 아니다. 진입점이 가리키는 곳을 따라가야 근거가 완성된다.

```
건국대 지침 제12조 --"지침 제33조~제42조"--> 통합관리지침 제39조 --"별표2"--> 증빙 4종
     ^ 4-1 로 들어옴          (제12차 표기)          ^ 4-3 이 끌어옴      ^ 4-3
```

**제39조를 검색이 못 뽑아도, 별표2를 검색이 못 뽑아도, 그래프가 끌고 온다.**

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
     WHERE p.depth < 3                     -- 위임 추적과 같은 깊이 제한
)
SELECT DISTINCT * FROM 폐포;
```

- **깊이 3** — 법령 위임 추적(`build_delegation_paths.py`)과 같은 규칙. 더 가면 전체 법령으로 번진다
- **`dangling` 엣지는 제외**하되 그 사실을 판정에 싣는다 -> 근거 불완전 신호이므로 판단불가 쪽으로 기운다
- **`shifted` 엣지**(구판 조번호)는 보정된 `dst` 를 쓰되 **원래 표기도 함께 전달**한다.
  화면 7 에서 *"귀 기관 규정은 제33조라 하지만 현행 제14차 기준 제39조입니다"* 를 보여줘야 한다

### 4-4. RRF 결합

```python
K = 60
def rrf(dense_ranked, sparse_ranked, w_dense=0.6, w_sparse=0.4):
    score = defaultdict(float)
    for rank, cid in enumerate(dense_ranked, 1):
        score[cid] += w_dense / (K + rank)
    for rank, cid in enumerate(sparse_ranked, 1):
        score[cid] += w_sparse / (K + rank)
    return sorted(score, key=score.get, reverse=True)[:8]
```

한쪽에만 등장한 청크는 **그 항만 0** 으로 처리한다 (51등을 51로 넣지 않는다).
가중합이 아니라 RRF 인 이유는 **스케일 정규화 문제 회피**다.

**임계치**: 1등 RRF 점수가 임계치 미만이면 판정을 건너뛰고 판단불가로 간다.
값은 골든셋으로 정한다. 예외는 `Agent.md` §3-2 (3).

---

## 5. 미결

| # | 미결 | 왜 지금 못 정하나 |
|---|---|---|
| 1 | **청킹 기본 단위 조 vs 항** | 재인덱싱 전 결정 필요. `구현.md` §1-1. 분할 **임계의 정의(토큰 900)** 는 2026-08-30 확정 (§3-4) |
| 3 | 1만 청크 초과 시 ANN | L1 219규범 + L2 42문서면 경계선. **실측 후 판단** |
| 4 | 표 추출 미구현 | Stage 0 에 `extract_tables()` 경로 신설 필요 |
| 5 | L3 원본 보관 여부 | 재파싱 수요 vs §6 저장 최소화 원칙 |
| 6 | Supabase 무료 티어 한도 | 벡터 1만 x 1024차원 = 약 40MB. 확인 필요 |

**해소 이력**: #2 L3 테이블 분리 — ✅ 2026-08-30 확정 (§2-3, CLAUDE.md 동시 갱신).

## 6. 이 문서를 쓰며 발견한 충돌 (✅ 2026-08-30 해소)

🔴 **`서비스 아키텍쳐.md` §3 의 "데이터가 밖으로 나가는 유일한 경계 = LLM API" 가 깨진다.**
Supabase 는 관리형 3자 인프라다. 서울 리전이라도 계정 · F1 · F3 · F4 · F2요약 · decisions 가
그쪽에 저장된다. §6 의 저장 계층화(원문은 버리고 구조화 결과만) 덕분에 **영업비밀과 개인식별정보는
여전히 나가지 않지만**, "유일한 경계" 라는 문장은 사실이 아니게 된다.
→ ✅ `서비스 아키텍쳐.md` §3 에 Supabase 경계 주석을 넣어 해소했다 (2026-08-30).
