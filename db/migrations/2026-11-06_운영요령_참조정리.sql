-- 필수(데이터). 운영요령 doc_id 가 2개(active · superseded)이고 모두의 창업 · 지침 제14차의 참조 41행이 superseded 를 가리킨다.
-- 참조 확장은 도착 문서가 active 일 때만 따라가므로(scripts/retrieve.py 참조확장SQL) 이 행들은 지금 판정에 쓰이지 않는다.
-- 41행의 도착 문서만 active 로 옮긴다. superseded 문서 행과 조문은 지우지 않는다.
--   src 가 superseded 인 3행(공고 2 · 복사본 자기 참조 1)은 판정 경로에 없어 고치지 않는다.
-- 조번호 있는 35행은 조마다 active 문서에 있는지 확인한다. 하나라도 없으면 멈춘다.
-- 멱등이다. 41행이 아니면 멈추고, 0행이면 이미 적용했거나 데이터가 없는 DB 다.
-- 데이터 수정이라 db/init 은 고치지 않는다. 원천은 scripts/build_refs.py load_corpus_names(active 우선)와 _refs.json.
-- 운영요령 새 판(제2026-82호, ontology/수집/판대조.md)을 적재하면 active 가 바뀌므로 그때 참조를 다시 옮겨야 한다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-06_운영요령_참조정리.sql

DO $$
DECLARE
    v_active CONSTANT text := 'L1_중소기업창업지원사업운영요령_20241206';
    v_super  CONSTANT text := '중소기업창업 지원사업 운영요령(중소벤처기업부고시)(제2024-101호)(20241206)';
    v_지침   CONSTANT text := 'L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223';
    v_모두   CONSTANT text := '모두의 창업 프로젝트 세부관리기준(개정본)';
    v_대상 int;
    v_지침행 int;
    v_모두행 int;
    v_없는조 text;
    v_바뀐행 int;
BEGIN
    SELECT count(*), count(*) FILTER (WHERE src_doc_id = v_지침), count(*) FILTER (WHERE src_doc_id = v_모두)
      INTO v_대상, v_지침행, v_모두행
      FROM corpus.refs
     WHERE dst_doc_id = v_super AND src_doc_id IN (v_지침, v_모두);
    RAISE NOTICE '적용 전: 고칠 행 % (지침 %, 모두의 창업 %)', v_대상, v_지침행, v_모두행;

    IF v_대상 = 0 THEN
        RAISE NOTICE '고칠 행이 없다. 이미 적용했거나 corpus.refs 가 비어 있다';
    ELSIF v_지침행 <> 38 OR v_모두행 <> 3 THEN
        RAISE EXCEPTION '고칠 행이 예상과 다르다: 지침 %(38 이어야 한다), 모두의 창업 %(3 이어야 한다)', v_지침행, v_모두행;
    ELSE
        IF NOT EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_active AND status = 'active') THEN
            RAISE EXCEPTION '% 가 active 가 아니다. 새 판을 적재했으면 도착 문서를 다시 정한다', v_active;
        END IF;

        SELECT string_agg(DISTINCT r."dst_조번호", ', ') INTO v_없는조
          FROM corpus.refs r
         WHERE r.dst_doc_id = v_super AND r.src_doc_id IN (v_지침, v_모두) AND r."dst_조번호" IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM corpus.doc_articles a
                            WHERE a.doc_id = v_active AND a."조번호" = r."dst_조번호");
        IF v_없는조 IS NOT NULL THEN
            RAISE EXCEPTION 'active 문서에 없는 조: %', v_없는조;
        END IF;

        UPDATE corpus.refs SET dst_doc_id = v_active
         WHERE dst_doc_id = v_super AND src_doc_id IN (v_지침, v_모두);
        GET DIAGNOSTICS v_바뀐행 = ROW_COUNT;
        IF v_바뀐행 <> 41 THEN
            RAISE EXCEPTION '옮긴 행이 41행이 아니다: %', v_바뀐행;
        END IF;
    END IF;

    SELECT count(*) INTO v_대상 FROM corpus.refs WHERE dst_doc_id = v_super AND src_doc_id IN (v_지침, v_모두);
    SELECT count(*) INTO v_바뀐행 FROM corpus.refs WHERE dst_doc_id = v_super;
    RAISE NOTICE '적용 후: 모두의 창업 · 지침 → superseded %행, 전체 → superseded %행(src 가 superseded 인 행)', v_대상, v_바뀐행;
END
$$;
