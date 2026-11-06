-- 필수. tenant.plan_tasks."유형" CHECK 에 사전승인·사전확인·증빙을 더한다.
-- 사용자가 고른 일정 유형이 새로고침 뒤에도 남게 하려는 변경이다. 값을 넓히기만 해서 기존 행과 이전 코드에 영향이 없다.
-- 코드 배포 전에 적용한다(없으면 해당 유형의 일정 저장이 실패한다). api 를 멈추지 않는다.
-- 실행: docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-06_일정_유형.sql
BEGIN;
ALTER TABLE tenant.plan_tasks DROP CONSTRAINT IF EXISTS "plan_tasks_유형_check";
ALTER TABLE tenant.plan_tasks ADD CONSTRAINT "plan_tasks_유형_check"
    CHECK ("유형" IN ('기타','계약','비교견적','사전승인','사전확인','증빙'));
COMMIT;
