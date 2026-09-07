-- 사업×연도별 기관 명부. tenant.orgs 는 건드리지 않고 옆에 새 테이블을 세운다.
-- tenant.orgs.사업명(TEXT[]) 은 "어느 사업을 하는가" 까지만 담고 연도를 못 담아 분리한다 — 사업마다 최신 연도가 다르다.


-- tenant.org_programs
-- 역할 은 사업마다 부르는 이름이 다르다: 6개 사업 → 주관기관 / 모두의 창업 프로젝트 → 운영기관·멘토기관 / TIPS → 운영사·협력기관.
-- 사업명 은 corpus.programs(PK) 참조다 — 폴더명·프론트 표기가 정본과 어긋나 조용히 0행이 되는 사고를 DB 가 막는다.
CREATE TABLE IF NOT EXISTS tenant.org_programs (
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    사업명      TEXT NOT NULL REFERENCES corpus.programs("사업명")
                ON UPDATE CASCADE ON DELETE RESTRICT,
    기준연도    INT  NOT NULL,
    역할        TEXT NOT NULL,

    -- 원문 섹션명 그대로 (일반형/특화분야/IP전략형/교육형/기술분야/차수 …).
    -- PK 의 일부다 — 한 기관이 같은 사업·같은 해에 분야를 달리해 두 번 나올 수 있다.
    -- 구분을 PK 에서 빼면 뒤엣것이 앞엣것을 덮어 기관 하나가 조용히 사라진다.
    -- NOT NULL DEFAULT '' 인 이유는 PostgreSQL PK 가 NULL 을 못 담기 때문이다.
    구분        TEXT NOT NULL DEFAULT '',

    연락처      TEXT,
    주소        TEXT,          -- 공고 붙임에는 주소가 아예 없다. NULL 이 정상이다

    -- 연도의 출처를 남긴다 — 파일명과 본문의 연도가 어긋나는 사례가 있다. 본문(대상년도)이 파일명보다 우선한다.
    연도출처    TEXT NOT NULL CHECK (연도출처 IN ('대상년도','파일명','수집기록')),

    출처표기    TEXT,          -- 원문이 쓴 기관명 그대로 (정규화 전). 되짚기용
    출처파일    TEXT NOT NULL, -- 레포 기준 상대경로
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (org_id, 사업명, 기준연도, 역할, 구분),

    -- 어휘를 열어두지 않는다 — 새 역할이 나오면 적재가 시끄럽게 실패해야 표기 흔들림을 놓치지 않는다.
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

-- tenant.orgs.사업명 은 이제 파생값이다. 정본은 이 테이블이다. 읽는 코드가 남아 있어 아직 지우지 않는다.
COMMENT ON COLUMN tenant.orgs."사업명" IS
    '파생값. 정본은 tenant.org_programs 다 (2026-09-02). 연도를 못 담아서 분리했다';


-- RLS — 다른 tenant 표와 다르다. org_isolation 을 그대로 걸면 기관 선택 화면이 죽는다 —
-- 사용자가 자기 기관을 고르는 시점엔 org 문맥이 없다. 그래서 읽기는 전부 열고 쓰기는 막는다.
-- 담긴 것은 정부 공개 명부지 남의 tenant 데이터가 아니다. 다만 org_id 는 자기신고 파라미터다 —
-- 기관 목록 API 는 org_id 를 그대로 내보내지 마라.
ALTER TABLE tenant.org_programs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_programs_read_all ON tenant.org_programs;
CREATE POLICY org_programs_read_all ON tenant.org_programs
    FOR SELECT USING (true);


-- 「몇 년 기준」 뷰. 프론트에 「2025 기준」 표시용 값을 내려준다. 사업마다 최신 연도가 달라 「올해」로 뭉뚱그리면 틀린다.
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


-- 기관 선택 화면이 실제로 읽을 것 — 사업별 최신 연도의 명부만.
-- 「올해」로 거르지 않는다 — 사업마다 최신이 다르고, 없는 해를 물으면 빈 목록이 돼 사용자가 진행을 못 한다.
CREATE OR REPLACE VIEW tenant."v_기관명부_최신" AS
SELECT op.*
  FROM tenant.org_programs op
  JOIN (SELECT "사업명", max("기준연도") AS "연도"
          FROM tenant.org_programs GROUP BY "사업명") m
    ON m."사업명" = op."사업명" AND m."연도" = op."기준연도";

COMMENT ON VIEW tenant."v_기관명부_최신" IS
    '사업별 최신 연도의 기관 명부. 기관 선택 화면용. 🔴 org_id 를 그대로 내보내지 마라 — 사칭 축이다';


-- tenant.orgs 공개열람 — 기관 선택 화면이 기관명을 보여주려면 orgs 도 조인해야 하는데
-- org_isolation 만 걸려 있으면 org 문맥이 없는 시점에 0행이 된다. 정부 공개 명부(기관명·주소·부서)만 담겨 있다.
-- org_id 는 자기신고 파라미터지 인증된 신원이 아니다 — 기관 목록 API 는 org_id 를 그대로 내보내면 안 된다.
ALTER TABLE tenant.orgs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS orgs_read_all ON tenant.orgs;
CREATE POLICY orgs_read_all ON tenant.orgs FOR SELECT USING (true);
