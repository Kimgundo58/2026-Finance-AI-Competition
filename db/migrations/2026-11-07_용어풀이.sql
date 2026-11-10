-- 필수. 용어 풀이 테이블 corpus.terminology 를 만든다(docs/6_LLM/6-5_용어_풀이.md 「저장」, 작업 P4-6).
--   term_id · 용어 · 풀이 · concept_id(재적재로 개념이 사라지면 NULL) · 적용사업(NULL = 공통) · 검수단계.
--   근거(doc_id · 조번호 · 발췌)는 corpus.provenance(owner_kind '용어', 칸 '근거')에 둔다.
--   검수단계 값은 concepts · norms 와 같은 다섯 가지다(6-5 예시는 AI검증 을 빠뜨렸고 원천 · 화면 기준이 AI검증 이다).
-- 소비자: scripts/ontology_terms.py `풀이_목록`(확인 질문 프롬프트 · 응답 `용어풀이`, 판정 응답 `용어풀이`),
--         server/response_cache.py 데이터 버전 해시(풀이가 바뀌면 판정 · 질문 캐시가 무효), scripts/corpus_ingest.py `_재검토_하향`.
-- 쓰기: scripts/ontology_load.py(롤 suddoe_ingest). 읽기: suddoe_app.
-- 코드 배포 **전에** 적용한다. 새 코드의 데이터 버전 해시가 이 테이블을 읽어, 테이블이 없으면 해시가 "unknown" 이 되어
-- 데이터가 바뀌어도 캐시가 적중한다. 적용 뒤 `python scripts/ontology_load.py` 로 ontology/용어풀이.json 을 넣는다.
-- 멱등이다. 다른 행은 바꾸지 않는다. api 를 멈추지 않는다. 스키마는 db/init/19_ontology.sql 과 같다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-07_용어풀이.sql
--
-- 되돌리기: DELETE FROM corpus.provenance WHERE owner_kind = '용어'; DROP TABLE corpus.terminology;

CREATE TABLE IF NOT EXISTS corpus.terminology (
    term_id    TEXT PRIMARY KEY,
    용어       TEXT NOT NULL,
    풀이       TEXT NOT NULL,
    concept_id TEXT REFERENCES corpus.concepts ON DELETE SET NULL,
    적용사업   TEXT REFERENCES corpus.programs ("사업명"),
    검수단계   TEXT NOT NULL,
    CHECK (검수단계 IN ('AI초안','AI대조','AI검증','사람확정','재검토')),
    UNIQUE NULLS NOT DISTINCT (용어, 적용사업)
);

GRANT SELECT ON corpus.terminology TO suddoe_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON corpus.terminology TO suddoe_ingest;
