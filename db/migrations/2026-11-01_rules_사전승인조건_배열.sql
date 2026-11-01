-- 2026-11-01_rules_사전승인조건_배열.sql  (필수)
--
-- corpus.rules.사전승인_조건 을 스키마(db/init/01_schema.sql)와 같은 TEXT[] 로 맞춘다.
-- 운영 DB 에서만 이 컬럼이 text 로 남아 있었고 값은 '{"문장1","문장2"}' 배열 표기 문자열이었다.
-- 그 결과:
--   - server/response_cache.py 설정 해시 쿼리의 unnest(사전승인_조건) 가 판정마다 42883 으로 실패해
--     해시가 'unknown' 으로 고정됐다(룰·코퍼스를 바꿔도 판정 캐시가 무효화되지 않음).
--   - rule_lookup 이 문자열 하나를 조건 한 문장으로 받아, 여러 문장 조건이 중괄호·따옴표째 한 줄로 나왔다.
-- 값은 모두 올바른 배열 표기라 ::text[] 로 그대로 변환된다. 이미 TEXT[] 이면 아무것도 하지 않는다(멱등).
--
-- 적용 후 API 를 재시작한다. 풀 연결에 준비된 문장(prepared statement)이 결과 타입 변경으로
-- "cached plan must not change result type" 을 낼 수 있다.
--
-- 실행:
--   cd /opt/suddoe/deploy/ncp && ./backup.sh
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
--     < ../../db/migrations/2026-11-01_rules_사전승인조건_배열.sql
--   docker compose restart api

DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
         WHERE table_schema = 'corpus' AND table_name = 'rules'
           AND column_name = '사전승인_조건') = 'text' THEN
        ALTER TABLE corpus.rules
            ALTER COLUMN 사전승인_조건 TYPE text[] USING 사전승인_조건::text[];
        RAISE NOTICE 'corpus.rules.사전승인_조건: text -> text[]';
    ELSE
        RAISE NOTICE 'corpus.rules.사전승인_조건: 이미 text[] (변경 없음)';
    END IF;
END $$;
