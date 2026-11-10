-- 2026-11-01_시퀀스_재설정.sql  (필수)
--
-- 열이 소유한 모든 시퀀스를 그 열의 최대값 이상으로 올린다. 데이터를 id 째 옮겨 넣으면
-- 시퀀스가 따라 올라가지 않아, 다음 INSERT 가 기존 행과 같은 id 를 받아 UniqueViolation 이 난다.
-- 운영에서 확인한 상태(2026-11-01 23:35):
--   tenant.l3_articles_article_id_seq  318 < max 515    → 기관 규정(L3) 업로드가 "l3_articles 적재 실패" 로 끝났다
--   corpus.item_alias_alias_id_seq     273 < max 326
--   corpus.chunks_chunk_id_seq       20677 < max 20832
-- 시퀀스는 내리지 않는다(greatest). 다시 실행해도 같은 상태가 된다(멱등).
--
-- 실행:
--   cd /opt/suddoe/deploy/ncp && ./backup.sh
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
--     < ../../db/migrations/2026-11-01_시퀀스_재설정.sql

DO $$
DECLARE
    r record;
    최대 bigint;
    현재 bigint;
BEGIN
    FOR r IN
        SELECT sn.nspname AS 시퀀스스키마, s.relname AS 시퀀스,
               tn.nspname AS 표스키마, t.relname AS 표, a.attname AS 열
          FROM pg_class s
          JOIN pg_namespace sn ON sn.oid = s.relnamespace
          JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
          JOIN pg_class t ON t.oid = d.refobjid
          JOIN pg_namespace tn ON tn.oid = t.relnamespace
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
         WHERE s.relkind = 'S'
    LOOP
        EXECUTE format('SELECT max(%I) FROM %I.%I', r.열, r.표스키마, r.표) INTO 최대;
        EXECUTE format('SELECT last_value FROM %I.%I', r.시퀀스스키마, r.시퀀스) INTO 현재;
        IF 최대 IS NOT NULL AND 최대 > 현재 THEN
            PERFORM setval(format('%I.%I', r.시퀀스스키마, r.시퀀스), 최대, true);
            RAISE NOTICE '%.%: % -> % (%.%.% 최대값)', r.시퀀스스키마, r.시퀀스, 현재, 최대, r.표스키마, r.표, r.열;
        END IF;
    END LOOP;
END $$;
