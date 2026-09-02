-- ════════════════════════════════════════════════════════════════
-- 06_org_programs.sql — 사업×연도별 기관 명부 (2026-09-02)
--
-- 🔴 왜 새 파일인가: `tenant.orgs` 는 다른 세션 소유고 FK 가 4벌 걸려 있다.
--    기존 테이블을 고치지 않고 **옆에 세운다.** 되돌릴 때도 이 파일만 본다.
--
-- 왜 필요한가: `tenant.orgs.사업명`(TEXT[]) 은 «이 기관이 어느 사업을 한다» 까지만
--    말한다. **몇 년도 명부인지**를 못 담는다. 사업마다 최신 연도가 다르다는 게
--    실측으로 확정됐다 (재도전은 2026 이 있고 창업중심대학은 2025 가 마지막이다).
--    사용자에게 「2025 기준」이라고 밝히려면 연도가 값이어야 한다.
-- ════════════════════════════════════════════════════════════════


-- ── ① tenant.org_programs ────────────────────────────────────────
--
-- 🔴 `역할` 이 왜 별도 칸인가 — 사업마다 부르는 이름이 다르다 (2026-09-02 실측).
--    6개 사업(예비·초기·재도전·도약·중심대학·초격차) → 「주관기관」
--    모두의 창업 프로젝트                            → 「운영기관」·「멘토기관」
--    TIPS                                            → 「운영사」 (2025~ 「협력기관」 신설)
--    전부 「주관기관」으로 뭉개면 **없는 개념을 있다고 적는 것**이다. 모두의창업에서
--    「주관기관」이 실제로 나오는 유일한 자리는 로컬트랙인데 그건 우리 범위 밖이다.
--
-- 🔴 `사업명` 은 `corpus.programs`(PK) 참조다. 폴더명·프론트 표기가 들어올 수
--    없게 **DB 가 막는다.** 표기 흔들림을 코드 규율에 맡기지 않는다 —
--    오늘 프론트 `"2026 민관공동 창업자 발굴·육성"` 이 정본과 안 맞아 조용히 0행이
--    되는 경로가 실제로 발견됐다. 같은 사고를 이 테이블에선 구조적으로 막는다.
CREATE TABLE IF NOT EXISTS tenant.org_programs (
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    사업명      TEXT NOT NULL REFERENCES corpus.programs("사업명")
                ON UPDATE CASCADE ON DELETE RESTRICT,
    기준연도    INT  NOT NULL,
    역할        TEXT NOT NULL,

    -- 원문 섹션명 그대로. 일반형 / 특화분야 / IP전략형 / 교육형 / 기술분야 / 차수 …
    -- 🔴 값으로 남기는 이유 ①: 2026-09-02 에 재도전 2024 에서 기관 1곳을 통째로 잃었는데
    --    원인이 **섹션 경계**였다. 섹션을 값으로 남기면 다음에 또 잃어도 표에 보인다.
    -- 🔴 값으로 남기는 이유 ②: **PK 의 일부다.** 한 기관이 같은 사업·같은 해에 분야를
    --    달리해 두 번 나온다 — 실측 3건 (초격차 2026):
    --      한국과학기술원      로보틱스 042-350-7187 · 첨단제조 042-350-7154
    --      한국항공우주연구원  방산 042-870-3683 · 우주항공 042-879-4412
    --      한국방송통신전파진흥원 보안·네트워크 061-350-1428 · 콘텐츠 061-350-1416
    --    중복이 아니라 **서로 다른 배정**이고 연락처가 다르다. 구분을 PK 에서 빼면
    --    뒤엣것이 앞엣것을 덮어 기관 하나가 조용히 사라진다.
    --    NOT NULL DEFAULT '' 인 이유는 PostgreSQL PK 가 NULL 을 못 담기 때문이다.
    구분        TEXT NOT NULL DEFAULT '',

    연락처      TEXT,
    주소        TEXT,          -- 공고 붙임에는 주소가 아예 없다. NULL 이 정상이다

    -- 🔴 연도의 출처를 남긴다. 파일명과 본문이 어긋나는 실물이 있다 —
    --    「초기창업패키지 주관기관 현황(2025년_2026-03-26갱신본).hwp」 는 파일명이 2025 인데
    --    본문이 「( 대상년도 : 2026년 )」 이다. 본문이 이긴다. 그 판단 근거를 값으로 남긴다.
    연도출처    TEXT NOT NULL CHECK (연도출처 IN ('대상년도','파일명','수집기록')),

    출처표기    TEXT,          -- 원문이 쓴 기관명 그대로 (정규화 전). 되짚기용
    출처파일    TEXT NOT NULL, -- 레포 기준 상대경로
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (org_id, 사업명, 기준연도, 역할, 구분),

    -- 🔴 어휘를 열어두지 않는다. 새 역할이 나오면 **적재가 시끄럽게 실패**해야 한다.
    --    조용히 받아들이면 「운영기관」이 「운영 기관」으로 들어와도 아무도 모른다.
    --    목록은 원문에서 실제로 관측된 것만이다 — 추측으로 늘리지 않는다.
    CONSTRAINT "org_programs_역할_check"
        CHECK (역할 IN ('주관기관','운영기관','멘토기관','운영사','협력기관')),

    -- 명부에 없는 연도가 들어오면 파싱 사고다. 넓게 잡되 열어두지는 않는다.
    CONSTRAINT "org_programs_기준연도_check"
        CHECK (기준연도 BETWEEN 2015 AND 2100)
);

CREATE INDEX IF NOT EXISTS "ix_org_programs_사업연도"
    ON tenant.org_programs ("사업명", "기준연도");

COMMENT ON TABLE  tenant.org_programs IS
    '사업×연도별 기관 명부. 창진원 공개자료 원본에서 추출. L3 규정 본문이 아니라 «누가 그 사업의 기관인가» 만 담는다';
COMMENT ON COLUMN tenant.org_programs.역할 IS
    '원문이 쓴 말 그대로. 사업마다 다르다 — 주관기관/운영기관/멘토기관/운영사/협력기관';
COMMENT ON COLUMN tenant.org_programs.연도출처 IS
    '기준연도를 무엇에서 읽었나. 대상년도(본문) > 파일명 순으로 신뢰한다';

-- 🔴 `tenant.orgs.사업명` 은 이제 파생값이다. 정본은 이 테이블이다.
--    지금 지우지 않는다 — 읽는 코드를 아직 아무도 전수로 세지 않았다.
--    (`scripts/agent_a2.py:171` 이 읽는 것까지는 확인했다)
COMMENT ON COLUMN tenant.orgs."사업명" IS
    '파생값. 정본은 tenant.org_programs 다 (2026-09-02). 연도를 못 담아서 분리했다';


-- ── ② RLS — 🔴 다른 tenant 표와 **일부러** 다르다 ────────────────
--
-- `tenant.*` 의 나머지는 `org_isolation USING (org_id = tenant.current_org())` 다.
-- 이 표에 같은 정책을 걸면 **기관 선택 화면이 죽는다** — 사용자가 자기 기관을 고르는
-- 시점엔 아직 org 문맥이 없다. 그래서 읽기는 전부 열고 쓰기는 막는다.
-- 이 표에 담긴 것은 정부가 공개한 명부지 남의 tenant 데이터가 아니다.
--
-- ⚠️ 다만 이건 **누출이 아니라 설계**라는 걸 못박아 둔다. 빠뜨린 게 아니다.
--    그리고 이 표가 `org_id` 를 내보내는 순간 사칭 방어 문제와 만난다 —
--    org_id 는 자기신고 파라미터지 신원이 아니다(`격리감사_0902.md`).
--    **기관 목록 API 는 org_id 를 그대로 내보내지 마라.**
ALTER TABLE tenant.org_programs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_programs_read_all ON tenant.org_programs;
CREATE POLICY org_programs_read_all ON tenant.org_programs
    FOR SELECT USING (true);


-- ── ③ 「몇 년 기준」 뷰 ───────────────────────────────────────────
--
-- 사용자 요구: 「2025 문서가 가장 최신이면 프론트에 작은 글씨로 «2025 기준»」.
-- 🔴 사업마다 최신 연도가 다르다. 하나의 「올해」로 뭉뚱그리면 틀린다.
-- 🔴 프론트에 이 값을 띄울 «자리»가 아직 없다 (2026-09-02 번들 실측:
--    기준연도·연도·「년 기준」·asOf·lastUpdated 전부 0건). 프론트 작업이 따로 필요하다.
CREATE OR REPLACE VIEW tenant."v_사업_기준연도" AS
SELECT "사업명",
       max("기준연도")                            AS "최신연도",
       min("기준연도")                            AS "최초연도",
       count(DISTINCT "기준연도")                 AS "연도수",
       array_agg(DISTINCT "역할" ORDER BY "역할") AS "역할들",
       count(*)                                   AS "총건수"
FROM tenant.org_programs
GROUP BY "사업명";

COMMENT ON VIEW tenant."v_사업_기준연도" IS
    '사업별 명부의 최신 연도. 프론트 「2025 기준」 표시의 출처. 사업마다 다르다';


-- 기관 선택 화면이 실제로 읽을 것 — **사업별 최신 연도의 명부만**.
-- 🔴 「올해」로 거르지 않는다. 사업마다 최신이 다르고, 없는 해를 물으면 빈 목록이 된다.
--    빈 목록은 「기관이 없다」로 읽혀서 사용자가 진행을 못 한다.
CREATE OR REPLACE VIEW tenant."v_기관명부_최신" AS
SELECT op.*
  FROM tenant.org_programs op
  JOIN (SELECT "사업명", max("기준연도") AS "연도"
          FROM tenant.org_programs GROUP BY "사업명") m
    ON m."사업명" = op."사업명" AND m."연도" = op."기준연도";

COMMENT ON VIEW tenant."v_기관명부_최신" IS
    '사업별 최신 연도의 기관 명부. 기관 선택 화면용. 🔴 org_id 를 그대로 내보내지 마라 — 사칭 축이다';


-- ── ④ 🔴 tenant.orgs 공개열람 — 2026-09-02 추가 ──────────────────
--
-- ②에서 `org_programs` 만 열어놨는데 **`orgs` 를 안 봤다.** 기관 선택 화면은
-- `기관명` 을 보여줘야 하니 두 표를 조인한다. `orgs` 에는 `org_isolation
-- USING (org_id = tenant.current_org())` 만 걸려 있어서, RLS 를 실제로 켜는 날
-- **기관 목록이 통째로 0행이 된다** — 사용자가 자기 기관을 고르는 시점엔 org 문맥이 없다.
--
-- `orgs` 가 담은 것은 기관명·주소·부서다. 정부가 공개한 명부에서 뽑았고 원본 PDF 에
-- 다 실려 있다. 남의 tenant 데이터(계획·판정·L3 문서)는 다른 표에 있고 그쪽은 계속 막힌다.
--
-- ⚠️ 🔴 그래도 이게 사칭 문제를 푸는 건 아니다. `org_id` 는 인증된 신원이 아니라
--    **자기신고 쿼리 파라미터**다. 값을 알면 그 기관인 척할 수 있다.
--    이 정책은 「목록을 볼 수 있게」 할 뿐이고, **기관 목록 API 는 `org_id` 를 그대로
--    내보내면 안 된다.** 근본 해법은 org_id 를 파라미터에서 없애고 세션으로 옮기는 것이다.
ALTER TABLE tenant.orgs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS orgs_read_all ON tenant.orgs;
CREATE POLICY orgs_read_all ON tenant.orgs FOR SELECT USING (true);
