-- 필수(데이터). 창업도약패키지 세부관리기준(2025년)의 corpus.documents.시행일 이 NULL 이다(로컬 suddoe_ontology, 2026-11-08). 부칙을 PDF 로 읽어 채운다.
-- 근거: 2026_Finance_DATA_FOR_RAG/창진원/창업도약패키지/창업도약패키지 세부관리기준(2025년).pdf (sha256 265f00f43a80…, 34쪽)
--   PDF 25쪽(인쇄 20쪽) 마지막 부칙 「부 칙<2025.6.9. 개정>」 · 「제1조(시행일) 본 관리기준은 2025년 6월 9일로부터 시행한다.」
--   표지(PDF 1쪽 「2025. 6.」)와 맞는다. 표지는 월까지만 적혀 있다. 이 PDF 에는 제개정 표기 쪽이 없다.
--   같은 쪽의 앞 부칙 5개(2019 · 2020 · 2021 · 2022 · 2024)는 옛 개정의 시행일이다.
--   대조 기록: ontology/추출/도약/PDF대조/차이목록.md
-- 이 값을 읽는 코드:
--   scripts/l3_load.py:297 _상위문서_찾기 ORDER BY (status='active') DESC, 시행일 DESC NULLS LAST (후보가 여럿일 때 순서만.
--     도약 2022 · 2024 판은 superseded 라 active 우선에서 이미 갈린다)
--   scripts/corpus_ingest.py:993 문서_상태 · :1034 문서_목록 → server/routes_ingest.py:352 · 343 (규정 적재 API 응답의 시행일)
-- 읽지 않는 곳: 판정 응답의 규정버전은 version 을 읽는다(scripts/llm_validate.py:225 · 556). 이 문서의 version 은 '2025년' 이고
--   여기서 고치지 않는다. 이 파일만으로는 판정 · 화면이 바뀌지 않는다.
-- 멱등이다. 이미 같은 값이면 0행이다. 문서가 없는 DB 에서도 0행이다.
-- 데이터 수정이라 db/init 은 고치지 않는다. 로컬 suddoe_ontology 에만 적용했다(2026-11-08). 운영 미적용.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-08_도약_시행일.sql

UPDATE corpus.documents
   SET 시행일 = DATE '2025-06-09'
 WHERE doc_id = '창업도약패키지 세부관리기준(2025년)'
   AND 시행일 IS DISTINCT FROM DATE '2025-06-09';
