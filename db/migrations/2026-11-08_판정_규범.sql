-- 필수. 판정에 쓴 규범 ID 를 판정 기록에 남긴다(5-15 ④, docs/5_룰/5-15_온톨로지와_입력_데이터_연결.md).
--   decisions."규범" JSONB  온톨로지 경로 판정의 규범 ID. 옛 경로 · 이 마이그레이션 전 행은 NULL.
--     {"코드판정": 규범ID | null, "조건": [조건 줄을 만든 규범ID], "문맥": [판정 프롬프트에 넣은 규범ID],
--      "자연어답": {규범ID: {"예": [조건 글], "아니오": [조건 글], "접힘": bool}}}
--   eval."v_규범별_판정"  규범 · 역할(코드판정 · 조건 · 문맥) · 판정별 건수. 규범별 적중 · 오판 집계(5-9)가 읽는다.
-- 소비자: scripts/orchestrate.py `_decisions_행`(쓰기), eval."v_규범별_판정"(읽기). 화면 변화 없음.
-- suddoe_app 은 tenant 전체 쓰기 권한(10b_app_grants.sql)으로 쓴다. 뷰는 postgres 소유라 집계는 postgres 로 읽는다.
-- 멱등이다. 행은 바꾸지 않는다. 코드 배포 **전에** 적용한다(새 코드가 이 칸에 쓴다). 옛 코드는 이 칸을 모른다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-08_판정_규범.sql
--
-- 되돌리기: DROP VIEW IF EXISTS eval."v_규범별_판정"; ALTER TABLE tenant.decisions DROP COLUMN "규범";

ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "규범" JSONB;

DO $migration$
BEGIN
IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'eval') THEN
EXECUTE $view$CREATE OR REPLACE VIEW eval."v_규범별_판정" AS
SELECT x."역할", x."규범ID", d."판정", d."경로", count(*) AS "건수", max(d.created_at) AS "최근"
  FROM tenant.decisions d
 CROSS JOIN LATERAL (
        SELECT '코드판정'::text AS "역할", d."규범"->>'코드판정' AS "규범ID" WHERE d."규범"->>'코드판정' IS NOT NULL
        UNION ALL
        SELECT '조건', jsonb_array_elements_text(COALESCE(d."규범"->'조건', '[]'::jsonb))
        UNION ALL
        SELECT '문맥', jsonb_array_elements_text(COALESCE(d."규범"->'문맥', '[]'::jsonb))
       ) x
 WHERE d."규범" IS NOT NULL
 GROUP BY x."역할", x."규범ID", d."판정", d."경로"$view$;
END IF;
END
$migration$;
