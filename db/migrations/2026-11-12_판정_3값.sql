-- 2026-11-12 판정 3값 전환 (필수)
-- 판정은 가능 · 확인 필요 · 불가 세 가지다(판정 아키텍쳐 재설계_1111.md, 사용자 결정 2026-11-11).
-- 옛 조건부 · 판단불가는 확인 필요로 합친다.
--
-- 하는 일
--   1. tenant.decisions.판정 의 옛 CHECK(4값)를 이름과 상관없이 찾아 지운다
--   2. 저장된 조건부 · 판단불가 행을 확인 필요로 바꾼다(그대로 두면 새 CHECK 를 걸 수 없다)
--   3. 새 CHECK decisions_판정_check (가능 · 확인 필요 · 불가 · NULL) 를 건다
--   4. 판정 응답 캐시(judge_cache 종류 judge)를 비운다. 옛 4값 판정이 캐시에서 나가지 않게 한다
-- 하지 않는 일: eval.golden_set.정답판정(4값 유지), corpus.rules.허용(룰 속성, 판정 값 아님)
--
-- 멱등: 다시 실행해도 같은 상태가 된다(두 번째 실행은 UPDATE 0행 · DELETE 0행).
-- 순서: 3값 코드와 함께 적용한다. 옛 코드는 조건부 · 판단불가를 저장하려다 새 CHECK 에 걸리고,
--       새 코드는 옛 CHECK 에 걸린다. api 를 멈춘 사이에 적용하고 새 이미지를 띄운다.
--
-- 실행(운영, NCP VM, postgres 롤):
--   cd /opt/suddoe/deploy/ncp
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-12_판정_3값.sql

BEGIN;

-- 1. 옛 CHECK 찾기 · 지우기. 판정 CHECK 만 '판단불가' 를 담고 있다(검증코드 · 신뢰등급 CHECK 는 다른 값)
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = 'tenant.decisions'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%판단불가%'
    LOOP
        RAISE NOTICE '옛 CHECK 지움: % = %', c.conname, c.def;
        EXECUTE format('ALTER TABLE tenant.decisions DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

-- 2. 저장된 행 바꾸기
UPDATE tenant.decisions SET 판정 = '확인 필요' WHERE 판정 IN ('조건부', '판단불가');

-- 3. 새 CHECK (다시 실행할 때를 위해 먼저 지운다)
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_판정_check";
ALTER TABLE tenant.decisions ADD CONSTRAINT "decisions_판정_check"
    CHECK (판정 IN ('가능', '확인 필요', '불가') OR 판정 IS NULL);

-- 4. 판정 응답 캐시 비우기
DELETE FROM tenant.judge_cache WHERE 종류 = 'judge';

-- 확인: 판정 값별 행 수 (조건부 · 판단불가가 없어야 한다)
SELECT 판정, count(*) FROM tenant.decisions GROUP BY 판정 ORDER BY 판정;

COMMIT;
