-- 사업마다 「L3(기관 규정)를 올리는 주체」가 다른 역할 이름으로 불린다 — 모두의창업·TIPS 는 「주관기관」이 아예 없다.
-- 「주관기관」으로 뭉개지 않고, 사업마다 어느 역할이 L3 발행 주체인지를 값으로 둔다.

ALTER TABLE corpus.programs ADD COLUMN IF NOT EXISTS "L3발행역할" TEXT;

COMMENT ON COLUMN corpus.programs."L3발행역할" IS
    '이 사업에서 L3(기관 규정)를 발행하는 역할. tenant.org_programs.역할 과 같은 어휘';

-- 6개 사업 — 원문이 「주관기관」이라 쓰고, 협약 상대이자 사업비 집행 관리 주체다
UPDATE corpus.programs SET "L3발행역할" = '주관기관'
 WHERE "사업명" IN ('예비창업패키지','초기창업패키지','재도전성공패키지',
                    '창업도약패키지','창업중심대학','초격차 스타트업 프로젝트');

-- TIPS — 운영사가 창업기업을 보육하고 사업비를 관리한다.
-- 협력기관은 아니다 — 원문 정의는 「보육공간 제공·공동투자·기술개발 지원」이고, 「사전승인」의 주어는 전문기관·업무지원기관이다.
UPDATE corpus.programs SET "L3발행역할" = '운영사' WHERE "사업명" = 'TIPS';

-- 모두의 창업 프로젝트 — 운영기관. 이 값은 추정이다 — 「운영기관이 규정을 낸다」는 조문을 아직 원문에서 못 찾았다.
-- 확실한 건 멘토기관은 아니라는 것이다 — 멘토는 보육을 돕지 사업비 규정을 내지 않는다. 조문 근거가 나오면 다시 본다.
UPDATE corpus.programs SET "L3발행역할" = '운영기관' WHERE "사업명" = '모두의 창업 프로젝트';


-- 기관 선택 화면이 읽을 것.
-- v_기관명부_최신 은 모든 역할을 담아 규정을 내지 않는 기관도 섞여 나온다.
-- L3 를 올릴 기관을 고르는 화면은 이 뷰(v_L3발행기관_최신)를 읽어야 한다.
CREATE OR REPLACE VIEW tenant."v_L3발행기관_최신" AS
SELECT op.*
  FROM tenant.org_programs op
  JOIN corpus.programs p        ON p."사업명" = op."사업명"
                              AND p."L3발행역할" = op."역할"
  JOIN (SELECT "사업명", "역할", max("기준연도") AS "연도"
          FROM tenant.org_programs GROUP BY "사업명", "역할") m
    ON m."사업명" = op."사업명" AND m."역할" = op."역할" AND m."연도" = op."기준연도";

COMMENT ON VIEW tenant."v_L3발행기관_최신" IS
    'L3(기관 규정)를 올리는 기관만, 사업별 최신 연도로. 🔴 org_id 를 그대로 내보내지 마라';
