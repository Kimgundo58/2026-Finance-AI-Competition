-- 필수(데이터). 모두의 창업 세부관리기준의 corpus.documents.시행일 이 NULL 이다. 부칙을 PDF 로 읽어 채운다.
-- 근거: 모두의 창업 프로젝트 세부관리기준(개정본).pdf (sha256 b48599…) PDF p.33(인쇄 30쪽)
--   「부 칙 < 2026. 06. 23. 개정 >」 · 「제1조(시행일) 본 관리기준은 2026년 6월 23일로부터 시행한다.」
--   표지(p.1 ’26.6.23) · 제개정 표기(p.4 제정 ’26.05.21. 개정 ’26.06.23.)와 맞는다.
--   대조 기록: ontology/추출/모두의_창업/PDF대조/차이목록.md
-- 이 값을 읽는 코드:
--   scripts/l3_load.py:297 _상위문서_찾기 ORDER BY (status='active') DESC, 시행일 DESC NULLS LAST (후보가 여럿일 때 순서만)
--   scripts/corpus_ingest.py:945 · 969 · 986 → server/routes_ingest.py:119 · 143 (규정 적재 API 응답의 시행일)
-- 읽지 않는 곳: 판정 응답의 규정버전은 version 을 읽는다(scripts/llm_validate.py:219 · 225 · 548 → server/judge_response.py:50).
--   이 문서의 version 은 NULL 이고 여기서 고치지 않는다. 이 파일만으로는 판정 · 화면이 바뀌지 않는다.
-- 멱등이다. 이미 같은 값이면 0행이다. 문서가 없는 DB 에서도 0행이다.
-- 데이터 수정이라 db/init 은 고치지 않는다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-06_세부관리기준_시행일.sql

UPDATE corpus.documents
   SET 시행일 = DATE '2026-06-23'
 WHERE doc_id = '모두의 창업 프로젝트 세부관리기준(개정본)'
   AND 시행일 IS DISTINCT FROM DATE '2026-06-23';
