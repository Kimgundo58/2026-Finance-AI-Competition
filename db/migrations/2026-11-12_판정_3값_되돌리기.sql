-- 2026-11-12 판정 3값 되돌리기 (되돌릴 때만)
-- 2026-11-12_판정_3값.sql 을 적용한 뒤 옛 4값 이미지(가능 · 조건부 · 판단불가 · 불가)로 되돌릴 때 적용한다.
-- deploy.sh 가 새 컨테이너 unhealthy 로 suddoe-api:rollback-auto 에 자동으로 되돌린 경우도 같다.
--
-- 하는 일
--   1. 3값 CHECK decisions_판정_check 와, 이름과 상관없이 tenant.decisions.판정 을 검사하는 CHECK('확인 필요' 를 담은 것)를 지운다
--   2. 옛 4값 + 확인 필요를 모두 받는 CHECK decisions_판정_check 를 건다
--      (옛 코드는 조건부 · 판단불가를 저장하고, 이미 바뀐 행과 새 코드가 쓴 행은 확인 필요다. 어느 이미지로도 저장이 실패하지 않는다)
--   3. 응답 캐시 tenant.judge_cache 의 judge · normalize · questions 를 비운다(새 코드의 응답 모양이 옛 코드로 나가지 않게)
-- 되돌릴 수 없는 것: 2026-11-12_판정_3값.sql 이 확인 필요로 바꾼 행이 원래 조건부였는지 판단불가였는지는 남아 있지 않다.
--   그 행들은 확인 필요로 남는다(옛 화면은 확인 필요를 모르는 값으로 보일 수 있다). 지운 캐시도 돌아오지 않는다.
-- 하지 않는 일: eval.golden_set.정답판정 · corpus.rules.허용(3값 마이그레이션도 바꾸지 않았다)
--
-- 멱등: 다시 실행해도 같은 상태가 된다(두 번째 실행은 같은 CHECK 를 지우고 다시 건다 · DELETE 0행).
-- 다시 3값으로 갈 때는 2026-11-12_판정_3값.sql 을 그대로 다시 적용한다(이 CHECK 도 '판단불가' 를 담고 있어 1단계가 지운다).
--
-- 실행(운영, NCP VM, postgres 롤. api 를 멈춘 사이에 적용하고 옛 이미지를 띄운다):
--   cd /opt/suddoe/deploy/ncp
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-12_판정_3값_되돌리기.sql

BEGIN;

-- 1. 판정 CHECK 찾기 · 지우기. 판정 CHECK 만 '확인 필요' 를 담고 있다(검증코드 · 신뢰등급 CHECK 는 다른 값)
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = 'tenant.decisions'::regclass
          AND contype = 'c'
          AND (conname = 'decisions_판정_check' OR pg_get_constraintdef(oid) LIKE '%확인 필요%')
    LOOP
        RAISE NOTICE '판정 CHECK 지움: % = %', c.conname, c.def;
        EXECUTE format('ALTER TABLE tenant.decisions DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

-- 2. 옛 4값 + 확인 필요를 모두 받는 CHECK
ALTER TABLE tenant.decisions ADD CONSTRAINT "decisions_판정_check"
    CHECK (판정 IN ('가능', '조건부', '판단불가', '확인 필요', '불가') OR 판정 IS NULL);

-- 3. 응답 캐시 비우기
DELETE FROM tenant.judge_cache WHERE 종류 IN ('judge', 'normalize', 'questions');

-- 확인: 판정 CHECK
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
WHERE conrelid = 'tenant.decisions'::regclass AND conname = 'decisions_판정_check';

COMMIT;
