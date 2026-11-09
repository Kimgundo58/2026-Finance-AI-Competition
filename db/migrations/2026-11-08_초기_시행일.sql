-- 필수(데이터). 초기창업패키지 세부관리기준(2025년)의 corpus.documents.시행일 이 NULL 이다(로컬 suddoe_ontology, 2026-11-08). 부칙을 PDF 로 읽어 채운다.
-- 근거: _hwp변환/.../초기창업패키지 세부관리기준(2025년).pdf (sha256 ccbcb219c962…, 가로 2쪽 모아찍기 9장 + 세로 1장)
--   PDF 7장 왼쪽(인쇄 12쪽) 「부칙」 · 「제1조(시행일) 본 관리기준은 2025년 7월 2일로부터 시행한다.」
--   표지(PDF 1장 왼쪽 「2025. 7.」) · 제개정 표기(PDF 1장 오른쪽 「개정 2025.07.02.」, 마지막 개정)와 맞는다.
--   대조 기록: ontology/추출/초기/PDF대조/차이목록.md
-- 이 값을 읽는 코드:
--   scripts/l3_load.py:297 _상위문서_찾기 ORDER BY (status='active') DESC, 시행일 DESC NULLS LAST (후보가 여럿일 때 순서만.
--     초기 2023 · 2024 판은 superseded 라 active 우선에서 이미 갈린다)
--   scripts/corpus_ingest.py:993 문서_상태 · :1034 문서_목록 → server/routes_ingest.py:352 · 343 (규정 적재 API 응답의 시행일)
-- 읽지 않는 곳: 판정 응답의 규정버전은 version 을 읽는다(scripts/llm_validate.py:225 · 556). 이 문서의 version 은 '2025년' 이고
--   여기서 고치지 않는다. 이 파일만으로는 판정 · 화면이 바뀌지 않는다.
-- 멱등이다. 이미 같은 값이면 0행이다. 문서가 없는 DB 에서도 0행이다.
-- 데이터 수정이라 db/init 은 고치지 않는다. 로컬 suddoe_ontology 에 적용했고 운영에는 적용하지 않았다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-08_초기_시행일.sql

UPDATE corpus.documents
   SET 시행일 = DATE '2025-07-02'
 WHERE doc_id = '초기창업패키지 세부관리기준(2025년)'
   AND 시행일 IS DISTINCT FROM DATE '2025-07-02';
