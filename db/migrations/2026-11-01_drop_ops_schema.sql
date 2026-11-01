-- ops 스키마(ops.gpu_pod)를 제거한다. GPU 팟 주소를 담던 표이고, 읽던 코드가 삭제됐다.
-- 실행 시점: 수동 마이그레이션(선택). 운영 DB 에 ops 스키마가 없으면 아무것도 하지 않는다.
--   cd /opt/suddoe/deploy/ncp
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-01_drop_ops_schema.sql
-- 멱등: 다시 실행해도 같은 상태가 된다.

DROP SCHEMA IF EXISTS ops CASCADE;
