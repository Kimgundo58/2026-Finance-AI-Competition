-- 필수. 기업 프로필 협약에 자기부담 현물 총액 칸을 되살린다(5-15 ②, docs/5_룰/5-15_온톨로지와_입력_데이터_연결.md).
--   profile_agreements."자기부담_현물" NUMERIC  협약서의 자기부담 현물 총액(원). NULL = 입력하지 않음(0 과 다르다).
--   총사업비 = 정부지원_현금 + 자기부담_현금 + 자기부담_현물. 「현물은 총사업비의 20% 이하」 같은 비율 한도를 계산할 때 쓴다.
--   정부지원 현물은 이 사업들에 없어 되살리지 않는다(04_validation_eval.sql 이 계속 지운다).
-- 소비자: server/routes_profile.py(GET · PUT /api/profile), server/response_cache.py `프로필_해시`(판정 캐시키),
--         scripts/orchestrate.py `_프로필값_읽기`(판정 프롬프트 B5 프로필 줄 · 전제 경로 F1.자기부담.현물).
-- suddoe_app 은 tenant 전체 쓰기 권한(10b_app_grants.sql)과 account_isolation 으로 쓴다. 권한 · 정책을 더 주지 않는다.
-- 멱등이다. 행은 바꾸지 않는다(기존 행은 NULL = 미입력). 코드 배포 **전에** 적용한다(새 코드가 이 칸을 읽고 쓴다).
-- 옛 코드는 이 칸을 모르므로 영향이 없다. api 를 멈추지 않는다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-08_자기부담_현물.sql
--
-- 되돌리기: ALTER TABLE tenant.profile_agreements DROP COLUMN "자기부담_현물";

ALTER TABLE tenant.profile_agreements ADD COLUMN IF NOT EXISTS "자기부담_현물" NUMERIC;
