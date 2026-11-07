-- 필수(데이터). 창업중심대학 세부관리기준2025년 개정의 corpus.documents.시행일 이 NULL 이다(로컬 suddoe_ontology, 2026-11-08). 부칙을 PDF 로 읽어 채운다.
-- 근거: 2026_Finance_DATA_FOR_RAG/창진원/창업중심대학/창업중심대학 세부관리기준2025년 개정.pdf (sha256 3a6df32f040a…, 가로 2쪽 모아찍기 22장)
--   PDF 8장 왼쪽(인쇄 15쪽) 마지막 부칙 「부칙 < 2025. 5. 30. >」 · 「제1조(시행일) 본 관리기준은 2025년 5월 30일로부터 시행한다.」
--   표지(PDF 1장 왼쪽) 제개정 표기 마지막 줄 「개정 < 2025.5.30. >」 와 맞는다.
--   DB 의 부칙 행에는 이 시행일 줄이 없다(2022 · 2023 부칙 조각만 있다). 대조 기록: ontology/추출/창업중심대학/PDF대조/차이목록.md
-- 이 값을 읽는 코드:
--   scripts/l3_load.py:297 _상위문서_찾기 ORDER BY (status='active') DESC, 시행일 DESC NULLS LAST (후보가 여럿일 때 순서만.
--     창업중심대학 2023 · 2024 판은 superseded 라 active 우선에서 이미 갈린다)
--   scripts/corpus_ingest.py:993 문서_상태 · :1034 문서_목록 → server/routes_ingest.py:352 · 343 (규정 적재 API 응답의 시행일)
-- 읽지 않는 곳: 판정 응답의 규정버전은 version 을 읽는다(scripts/llm_validate.py:225 · 556). 이 문서의 version 은 비어 있고
--   여기서 고치지 않는다. 이 파일만으로는 판정 · 화면이 바뀌지 않는다.
-- 멱등이다. 이미 같은 값이면 0행이다. 문서가 없는 DB 에서도 0행이다.
-- 데이터 수정이라 db/init 은 고치지 않는다. 로컬 suddoe_ontology 에만 적용했다(2026-11-08). 운영 미적용.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-08_창업중심대학_시행일.sql

UPDATE corpus.documents
   SET 시행일 = DATE '2025-05-30'
 WHERE doc_id = '창업중심대학 세부관리기준2025년 개정'
   AND 시행일 IS DISTINCT FROM DATE '2025-05-30';
