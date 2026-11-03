-- L3 스캔본을 OCR 로 읽은 문서를 tenant.l3_documents.extraction = 'ocr' 로 기록할 수 있게 한다. 필수.
-- 코드(scripts/l3_parse.py, scripts/clova_ocr.py) 배포 전에 적용한다. 적용하지 않으면 OCR 로 읽은 문서의
-- 결과 기록이 CHECK 위반으로 실패한다.
--
-- 'ocr' 문서의 조문을 인용하면 신뢰등급이 B 가 되고 '가능' 은 조건부로 낮춘다(scripts/llm_validate.py).
-- 데이터는 바꾸지 않는다. 멱등이다.
--
-- 실행(운영, 레포 루트 기준 경로는 db/migrations/README.md):
--   ./backup.sh
--   docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
--     < ../../db/migrations/2026-11-03_l3_ocr.sql

BEGIN;

ALTER TABLE tenant.l3_documents DROP CONSTRAINT IF EXISTS l3_documents_extraction_check;
ALTER TABLE tenant.l3_documents ADD CONSTRAINT l3_documents_extraction_check
    CHECK (extraction IN ('native','dedupe','hancom','vlm','hwpx','hwp','ocr'));

COMMIT;
