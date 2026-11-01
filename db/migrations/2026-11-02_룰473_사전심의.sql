-- 2026-11-02_룰473_사전심의.sql  (보류: 운영·로컬 모두 적용하지 않음, 데이터 수정)
--
-- corpus.rules 473(모두의 창업 프로젝트 · 기계장치)의 한도 필드를 비운다.
--
-- 왜
--   근거 조문은 모두의 창업 프로젝트 세부관리기준(개정본) 제34조③이다:
--   "시제품 제작에 직‧간접적으로 활용되는 기구, 비품, 기계 등의 구입이 가능하나 취득가액이 500만원 이상
--    구입할 경우, 사업운영위원회 사전심의를 받은 건에 한하여 집행이 가능하다."
--   500만원은 지출 상한이 아니라 사전심의를 받아야 하는 기준이다. 그런데 이 행에는 한도_유형='금액',
--   한도_값=5000000 이 들어 있어, 500만원 이상 구입이면 룰요약문(B4)에 "한도 비교 결과 한도를 넘는다"가
--   들어가 판정이 불가로 기울었다(정답: 사전심의 조건부).
--   사전승인=true 와 사전승인_조건(위 원문 요지)은 그대로 둔다. 500만원 기준은 사전승인_조건 문장에 남는다.
--   docs/9_백로그.md 의 "룰 473 데이터" 항목이다.
--
-- 보류 사유 (2026-11-02 로컬 측정, 골든 v2 의 모두의 창업 · 기계장치 17문항을 수정 전 1회 · 수정 후 2회 판정)
--   일치 수는 7 → 6 / 8 로 실행 간 흔들림(같은 상태 두 번에 3문항이 다름)과 구분되지 않았다.
--   대신 수정 후 2회째에 기계장치_12(공급가 470만원 + 부가세 47만원, 정답 판단불가: 500만원 기준이 부가세
--   포함인지 L1·L2 에 정의가 없음)가 '가능'으로 나왔다. 수정 전에는 틀린 한도 초과 문장 때문에 '불가'였다.
--   틀린 '가능'이 가장 나쁜 결과라서, 이 문항 유형을 막는 장치(예: 금액 기준의 부가세 포함 여부가 정의되지 않은
--   경우 판단불가) 없이 적용하지 않는다. 적용하려면 골든 v2 전체 평가로 틀린 '가능' 수를 확인한 뒤 결정한다.
--   실수로 적용되지 않도록 -v apply=1 을 주지 않으면 아무것도 바꾸지 않고 끝난다.
--
-- 멱등: 이미 비어 있으면 아무것도 바꾸지 않는다. 바꾸기 전 행은 backup_dq_20261102.rules 에 남긴다.
-- 실행 (운영, deploy/ncp): ./backup.sh 후
--   docker compose exec -T db psql -U postgres -d suddoe -X -v ON_ERROR_STOP=1 -v apply=1 -f - < ../../db/migrations/2026-11-02_룰473_사전심의.sql

\set ON_ERROR_STOP on
\if :{?apply}
\else
\echo '룰 473: 보류 중인 수정이라 적용하지 않았다(-v apply=1 필요). 파일 머리말의 보류 사유를 확인한다.'
\quit
\endif
BEGIN;
CREATE SCHEMA IF NOT EXISTS backup_dq_20261102;
REVOKE ALL ON SCHEMA backup_dq_20261102 FROM PUBLIC;
CREATE TABLE IF NOT EXISTS backup_dq_20261102.rules (LIKE corpus.rules);
INSERT INTO backup_dq_20261102.rules
SELECT r.* FROM corpus.rules r
 WHERE r.rule_id = 473 AND r."한도_유형" IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM backup_dq_20261102.rules b WHERE b.rule_id = r.rule_id);

UPDATE corpus.rules
   SET "한도_유형" = NULL, "한도_값" = NULL, "한도_단위" = NULL,
       "검수자" = "검수자" || '; 2026-11-02 한도 필드 제거(제34조③ 500만원은 사전심의 기준), Claude 데이터 정정(사람 미검수)',
       "검수일" = DATE '2026-11-02'
 WHERE rule_id = 473
   AND "사업명" = '모두의 창업 프로젝트' AND "비목" = '기계장치'
   AND "한도_유형" = '금액' AND "한도_값" = 5000000;

DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM corpus.rules WHERE rule_id = 473;
  IF NOT FOUND THEN
    RAISE EXCEPTION '룰 473 이 없다';
  END IF;
  IF r."사업명" <> '모두의 창업 프로젝트' OR r."비목" <> '기계장치' THEN
    RAISE EXCEPTION '룰 473 이 예상한 행(모두의 창업 프로젝트 · 기계장치)이 아니다: % · %', r."사업명", r."비목";
  END IF;
  IF r."한도_유형" IS NOT NULL OR r."한도_값" IS NOT NULL OR r."한도_단위" IS NOT NULL THEN
    RAISE EXCEPTION '룰 473 의 한도 필드가 남아 있다';
  END IF;
  IF NOT r."사전승인" OR coalesce(array_length(r."사전승인_조건", 1), 0) = 0 THEN
    RAISE EXCEPTION '룰 473 의 사전승인 조건이 비어 있다';
  END IF;
  RAISE NOTICE '룰 473: 확인 통과';
END $$;
COMMIT;
