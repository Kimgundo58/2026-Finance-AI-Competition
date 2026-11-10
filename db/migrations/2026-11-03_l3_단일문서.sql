-- L3(기관 규정)를 기관당 한 문서로 맞춘다. 필수. 코드(scripts/l3_parse.py, server/routes_l3.py) 배포 전에 적용한다.
--
-- 배경: 새 L3 를 올려도 이전 문서가 active 로 남아 판정이 두 문서를 같이 읽었다. 2026-11-03 운영에서
--   QA 계정 기관(경상국립대학교 창업중심대학사업단)이 새 문서를 올렸는데 판정 4265 가 이전 경상국립대
--   문서를 인용했다. 코드는 이제 새 문서 파싱이 성공하면 이전 문서를 superseded 로 바꾼다.
--
-- 1. tenant.l3_documents."실패사유" 컬럼 추가(코드가 쓴다. 없으면 파싱 결과 기록이 실패한다).
-- 2. 이미 쌓인 문서를 같은 규칙으로 정리한다. 기관마다
--      - 적용 문서: active 이고 pass/warn 인 문서 중 최신 1건. 같은 분(minute)에 함께 올린 문서(시드 등)는
--        조문이 많은 쪽을 고른다(본문 문서와 양식이 같이 시드된 경우 양식이 남지 않게).
--        조문이 0건인 warn 문서는 조문이 있는 문서가 하나도 없을 때만 고른다.
--      - 적용 문서보다 먼저 올린 active 문서는 전부 superseded.
--      - 적용 문서보다 나중에 실패한 문서는 최신 1건만 남기고 superseded.
--    행과 조문은 지우지 않는다(status 만 바꾼다). 판정은 status='active' 만 읽는다.
-- 멱등이다. 다시 실행해도 같은 상태가 된다.
--
-- 실행(운영, 레포 루트 기준 경로는 db/migrations/README.md):
--   ./backup.sh
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
--     < ../../db/migrations/2026-11-03_l3_단일문서.sql
--
-- 되돌리기: 2번에서 바꾼 문서는 NOTICE 로 건수만 남는다. 필요하면 백업 덤프에서 status 를 복원한다.

BEGIN;

ALTER TABLE tenant.l3_documents ADD COLUMN IF NOT EXISTS "실패사유" TEXT;
COMMENT ON COLUMN tenant.l3_documents."실패사유" IS
    '파싱 실패 분류 코드(스캔본·보호문서·조문없음·추출실패 등). 실패가 아니면 NULL. 안내 문구는 server/routes_l3.py';

DO $$
DECLARE _이전 int; _실패 int;
BEGIN
    WITH 조수 AS (
        SELECT d.doc_id, d.org_id, d.created_at,
               (SELECT count(*) FROM tenant.l3_articles a WHERE a.doc_id = d.doc_id) AS n
          FROM tenant.l3_documents d
         WHERE d.status = 'active' AND d."파싱품질" IN ('pass', 'warn')
    ), 적용 AS (
        SELECT DISTINCT ON (org_id) org_id, doc_id, created_at
          FROM 조수
         ORDER BY org_id, (n > 0) DESC, date_trunc('minute', created_at) DESC, n DESC, created_at DESC
    )
    UPDATE tenant.l3_documents d SET status = 'superseded'
      FROM 적용 a
     WHERE d.org_id = a.org_id AND d.doc_id <> a.doc_id AND d.status = 'active'
       AND (d."파싱품질" IN ('pass', 'warn') OR d.created_at < a.created_at);
    GET DIAGNOSTICS _이전 = ROW_COUNT;

    WITH 최근실패 AS (
        SELECT DISTINCT ON (org_id) org_id, doc_id
          FROM tenant.l3_documents
         WHERE status = 'active' AND "파싱품질" = 'fail'
         ORDER BY org_id, created_at DESC
    )
    UPDATE tenant.l3_documents d SET status = 'superseded'
      FROM 최근실패 f
     WHERE d.org_id = f.org_id AND d.doc_id <> f.doc_id
       AND d.status = 'active' AND d."파싱품질" = 'fail';
    GET DIAGNOSTICS _실패 = ROW_COUNT;

    RAISE NOTICE 'L3 정리: 이전 문서 % 건, 오래된 실패 문서 % 건을 superseded 로 바꿨다', _이전, _실패;
END
$$;

-- 확인: 기관마다 active 이고 pass/warn 인 문서는 많아야 1건이어야 한다
DO $$
DECLARE _n int;
BEGIN
    SELECT count(*) INTO _n FROM (
        SELECT org_id FROM tenant.l3_documents
         WHERE status = 'active' AND "파싱품질" IN ('pass', 'warn')
         GROUP BY org_id HAVING count(*) > 1) x;
    IF _n > 0 THEN
        RAISE EXCEPTION '적용 문서가 2건 이상인 기관이 % 곳 남았다', _n;
    END IF;
END
$$;

COMMIT;
