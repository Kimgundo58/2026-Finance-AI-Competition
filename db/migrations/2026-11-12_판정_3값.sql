-- 2026-11-12 판정 3값 전환 (필수)
-- 판정은 가능 · 확인 필요 · 불가 세 가지다(판정 아키텍쳐 재설계_1111.md, 사용자 결정 2026-11-11).
-- 옛 조건부 · 판단불가는 확인 필요로 합친다.
--
-- 하는 일
--   1. tenant.decisions.판정 의 옛 CHECK(4값)를 이름과 상관없이 찾아 지운다
--   2. 저장된 조건부 · 판단불가 행을 확인 필요로 바꾼다(그대로 두면 새 CHECK 를 걸 수 없다)
--   3. 새 CHECK decisions_판정_check (가능 · 확인 필요 · 불가 · NULL) 를 건다
--   4. 응답 캐시 tenant.judge_cache 의 세 종류(judge · normalize · questions, 13_judge_cache.sql CHECK 의 전부)를 비운다.
--      judge: 옛 4값 판정이 나가지 않게. normalize: 캐시 키에 응답 모양 버전이 없어(server/routes_normalize.py) `품목확인` 이
--      없는 옛 결과가 TTL(기본 900초) 동안 나가면 화면이 새 경로 사업에도 옛 비목 고르기를 보인다. questions: 옛 흐름이 만든
--      질문세트(TTL 24시간)에는 조건 줄 키(조건키)가 없고 서버 분류(③) 전 노드결정이 들어 있어, 새 판정이 옛 질문 · 결정을 섞어 읽지 않게 한다.
--      진행 중이던 질문세트를 잃은 판정은 답변유실로 처리된다(확인 필요 쪽, routes_judge 답변 검증)
-- 하지 않는 일: eval.golden_set.정답판정(4값 유지), corpus.rules.허용(룰 속성, 판정 값 아님)
--
-- 멱등: 다시 실행해도 같은 상태가 된다(두 번째 실행은 UPDATE 0행 · DELETE 0행).
-- 순서: 3값 코드와 함께 적용한다. 옛 코드는 조건부 · 판단불가를 저장하려다 새 CHECK 에 걸리고,
--       새 코드는 옛 CHECK 에 걸린다. api 를 멈춘 사이에 적용하고 새 이미지를 띄운다.
-- 되돌리기: 옛 이미지로 돌아가면(deploy.sh 자동 되돌림 포함) 먼저 2026-11-12_판정_3값_되돌리기.sql 을 적용한다.
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

-- 4. 응답 캐시 비우기(판정 · 정규화 · 질문세트. 응답 모양 · 내용이 옛 흐름의 것)
DELETE FROM tenant.judge_cache WHERE 종류 IN ('judge', 'normalize', 'questions');

-- 확인: 판정 값별 행 수 (조건부 · 판단불가가 없어야 한다)
SELECT 판정, count(*) FROM tenant.decisions GROUP BY 판정 ORDER BY 판정;

COMMIT;
