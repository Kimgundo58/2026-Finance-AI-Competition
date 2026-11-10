-- 사업×연도별 기관 목록 tenant.org_programs 와 기관 선택 화면용 뷰, 공개 열람 RLS 정책.
-- 실행 시점: init. tenant.orgs.사업명(TEXT[]) 은 연도를 담지 못해 별도 테이블로 둔다(사업마다 최신 연도가 다르다).


-- tenant.org_programs
-- 역할 은 사업마다 부르는 이름이 다르다: 6개 사업 → 주관기관 / 모두의 창업 프로젝트 → 운영기관·멘토기관 / TIPS → 운영사·협력기관.
-- 사업명 은 corpus.programs(PK) 참조다. 폴더명·프론트 표기가 표준 사업명과 달라 조회가 0행이 되는 문제를 FK 로 막는다.
CREATE TABLE IF NOT EXISTS tenant.org_programs (
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    사업명      TEXT NOT NULL REFERENCES corpus.programs("사업명")
                ON UPDATE CASCADE ON DELETE RESTRICT,
    기준연도    INT  NOT NULL,
    역할        TEXT NOT NULL,

    -- 원문 섹션명 그대로 (일반형/특화분야/IP전략형/교육형/기술분야/차수 …).
    -- PK 의 일부다 — 한 기관이 같은 사업·같은 해에 분야를 달리해 두 번 나올 수 있다.
    -- 구분을 PK 에서 빼면 뒤의 행이 앞의 행을 덮어 기관 하나가 경고 없이 사라진다.
    -- NOT NULL DEFAULT '' 인 이유는 PostgreSQL PK 가 NULL 을 못 담기 때문이다.
    구분        TEXT NOT NULL DEFAULT '',

    연락처      TEXT,
    주소        TEXT,          -- 공고 붙임에는 주소가 아예 없다. NULL 이 정상이다

    -- 연도의 출처를 남긴다 — 파일명과 본문의 연도가 어긋나는 사례가 있다. 본문(대상연도)이 파일명보다 우선한다.
    연도출처    TEXT NOT NULL CHECK (연도출처 IN ('대상연도','파일명','수집기록')),

    출처표기    TEXT,          -- 원문이 쓴 기관명 그대로 (정규화 전). 출처 추적용
    출처파일    TEXT NOT NULL, -- 레포 기준 상대경로
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (org_id, 사업명, 기준연도, 역할, 구분),

    -- 허용 목록으로 제한한다. 새 역할 표기가 나오면 적재가 실패해야 표기 차이를 놓치지 않는다.
    CONSTRAINT "org_programs_역할_check"
        CHECK (역할 IN ('주관기관','운영기관','멘토기관','운영사','협력기관')),

    -- 범위 밖 연도는 파싱 오류다. 범위를 넓게 잡되 제한은 둔다.
    CONSTRAINT "org_programs_기준연도_check"
        CHECK (기준연도 BETWEEN 2015 AND 2100)
);

CREATE INDEX IF NOT EXISTS "ix_org_programs_사업연도"
    ON tenant.org_programs ("사업명", "기준연도");

COMMENT ON TABLE  tenant.org_programs IS
    '사업×연도별 기관 목록. 창진원 공개자료 원본에서 추출. L3 규정 본문이 아니라 ''누가 그 사업의 기관인가'' 만 담는다';
COMMENT ON COLUMN tenant.org_programs.역할 IS
    '원문이 쓴 말 그대로. 사업마다 다르다 — 주관기관/운영기관/멘토기관/운영사/협력기관';
COMMENT ON COLUMN tenant.org_programs.연도출처 IS
    '기준연도를 무엇에서 읽었나. 대상연도(본문) > 파일명 순으로 신뢰한다';

-- tenant.orgs.사업명 은 파생값이고 기준 데이터는 이 테이블이다. 읽는 코드가 있어 컬럼은 유지한다.
COMMENT ON COLUMN tenant.orgs."사업명" IS
    '파생값. 기준 데이터는 tenant.org_programs 다 (2026-09-02). 연도를 못 담아서 분리했다';


-- RLS 는 다른 tenant 테이블과 다르다. org_isolation 을 적용하면 기관 선택 화면이 동작하지 않는다.
-- 사용자가 기관을 고르는 시점에는 org 가 정해지지 않았기 때문이다. 그래서 읽기는 모두 허용하고 쓰기는 막는다.
-- 담긴 데이터는 정부 공개 목록이다. 다만 org_id 를 알면 ?org_id= 파라미터로 사칭할 수 있으므로
-- 기관 목록 API 는 org_id 를 그대로 내보내지 않는다(HMAC slug 를 쓴다).
ALTER TABLE tenant.org_programs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_programs_read_all ON tenant.org_programs;
CREATE POLICY org_programs_read_all ON tenant.org_programs
    FOR SELECT USING (true);


-- 사업별 기준 연도 뷰. 프론트의 「2025 기준」 표시에 쓴다. 사업마다 최신 연도가 달라 「올해」로 일반화하면 틀린다.
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
    '사업별 기관 목록의 최신 연도. 프론트 「2025 기준」 표시의 출처. 사업마다 다르다';


-- 기관 선택 화면이 실제로 읽는 뷰 — 사업별 최신 연도의 기관 목록만.
-- 「올해」로 거르지 않는다. 사업마다 최신 연도가 다르고, 없는 연도로 조회하면 빈 목록이 되어 사용자가 진행할 수 없다.
CREATE OR REPLACE VIEW tenant."v_기관명부_최신" AS
SELECT op.*
  FROM tenant.org_programs op
  JOIN (SELECT "사업명", max("기준연도") AS "연도"
          FROM tenant.org_programs GROUP BY "사업명") m
    ON m."사업명" = op."사업명" AND m."연도" = op."기준연도";

COMMENT ON VIEW tenant."v_기관명부_최신" IS
    '사업별 최신 연도의 기관 목록. 기관 선택 화면용. 주의: org_id 를 그대로 내보내지 않는다 — 기관 사칭에 쓰일 수 있다';


-- tenant.orgs 공개 열람. 기관 선택 화면이 기관명을 보여주려면 orgs 를 조인해야 하는데
-- org_isolation 만 있으면 org 가 정해지지 않은 시점에 0행이 된다. 정부 공개 목록(기관명·주소·부서)만 담겨 있다.
-- org_id 는 인증된 신원이 아니므로 기관 목록 API 는 org_id 를 그대로 내보내지 않는다.
ALTER TABLE tenant.orgs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS orgs_read_all ON tenant.orgs;
CREATE POLICY orgs_read_all ON tenant.orgs FOR SELECT USING (true);
