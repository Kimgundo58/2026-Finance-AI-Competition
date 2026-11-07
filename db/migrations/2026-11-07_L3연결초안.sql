-- 필수. L3 조문 연결 확인 화면(P4-5, docs/5_룰/5-6b_API와_화면_변경.md 「L3 업로드 연결」)이 쓰는 칸을 더한다.
--   l3_articles."연결초안"      TEXT[]  업로드 뒤 LLM 이 추천한 개념 ID(또는 이전 문서에서 조제목이 같은 조의 확인값).
--                                      NULL = 추천 없음 · '{}' = 공통으로 추천. 판정은 읽지 않는다(판정은 concept_ids 만 읽는다).
--   l3_articles."옛판문구"      JSONB   superseded L1 · L2 본문과 같고 active 본문에는 없는 문장 [{문장, 문서}]. 화면 경고용.
--   l3_documents."연결초안상태" TEXT    작성중 · 완료 · 일부실패 · 실패. NULL = 초안을 만들지 않는 문서(옛 경로뿐이거나 이전 문서).
--   l3_documents."연결초안시각" TIMESTAMPTZ 작성중으로 둔 시각. 30분 넘게 작성중이면 화면에 실패로 보인다(서버 재시작 등).
-- 소비자: server/routes_l3.py `_연결_정보`(GET /api/l3/current `연결`), scripts/l3_link.py(쓰기).
-- suddoe_app 은 tenant 전체 쓰기 권한(10b_app_grants.sql)과 org_isolation 으로 쓴다. 권한을 더 주지 않는다.
-- 멱등이다. 행은 바꾸지 않는다. 코드 배포 **전에** 적용한다(새 코드가 이 칸을 읽는다). api 를 멈추지 않는다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-07_L3연결초안.sql
--
-- 되돌리기: ALTER TABLE tenant.l3_articles DROP COLUMN "연결초안", DROP COLUMN "옛판문구";
--           ALTER TABLE tenant.l3_documents DROP COLUMN "연결초안상태", DROP COLUMN "연결초안시각";

ALTER TABLE tenant.l3_articles ADD COLUMN IF NOT EXISTS "연결초안" TEXT[];
ALTER TABLE tenant.l3_articles ADD COLUMN IF NOT EXISTS "옛판문구" JSONB;
ALTER TABLE tenant.l3_documents ADD COLUMN IF NOT EXISTS "연결초안상태" TEXT;
ALTER TABLE tenant.l3_documents ADD COLUMN IF NOT EXISTS "연결초안시각" TIMESTAMPTZ;
ALTER TABLE tenant.l3_documents DROP CONSTRAINT IF EXISTS "l3_documents_연결초안상태_check";
ALTER TABLE tenant.l3_documents ADD CONSTRAINT "l3_documents_연결초안상태_check"
    CHECK ("연결초안상태" IN ('작성중', '완료', '일부실패', '실패'));
