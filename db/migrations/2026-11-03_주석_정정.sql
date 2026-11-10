-- 주석 정정: corpus.refs 의 COMMENT 두 개를 db/init/01_schema.sql 과 맞춘다. 선택(주석만 바꾼다).
--   1. 테이블 COMMENT 의 참조 확장 깊이를 "깊이 3" → "깊이 1" 로 고친다. 실제 깊이는 scripts/retrieve.py 의 `깊이 = 1` 이다.
--   2. 해결상태 컬럼 COMMENT 의 "(판단불가 예고)" 를 "(판단불가가 될 수 있음)" 으로 바꾼다.
-- 스키마·데이터는 바꾸지 않는다. 멱등이다(다시 실행해도 같은 COMMENT 가 된다).
-- 현재 db/init/ 으로 새로 만든 DB 는 이미 이 문구라 적용할 필요가 없다.
--
-- 실행 (NCP VM 의 deploy/ncp 디렉터리):
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
--     < ../../db/migrations/2026-11-03_주석_정정.sql

BEGIN;

COMMENT ON TABLE  corpus.refs IS
  '참조 그래프. 검색된 조에서 깊이 1까지 참조 확장 대상을 모아 LLM 컨텍스트에 넣는다. LLM 은 이 표를 보지 않는다 — 코드가 조회해 텍스트로 넣는다.';
COMMENT ON COLUMN corpus.refs.해결상태 IS
  'resolved=정상 / remapped=조번호가 구판이라 조제목으로 재매칭함 / dangling=코퍼스에 대상 없음(판단불가가 될 수 있음)';

COMMIT;
