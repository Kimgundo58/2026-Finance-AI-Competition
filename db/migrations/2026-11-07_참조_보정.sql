-- 필수(데이터). 2026-11-07_현행판_참조이동.sql 다음에 실행한다. 절차: ontology/수집/P0-6_P0-7_기록.md
-- 1) 가지조 참조(보조금법 제31조의2 등)를 가지조로 보낸다. 지금까지 본조(제31조)로 도착해 다른 조가 근거 문맥에 들어갔다.
--    표기의 첫 조에 가지 번호가 있는 행만 고친다(「제29조부터 제29조의2까지」의 시작 조 제29조는 맞다). 도착 문서에 그 가지조가 없으면 확장되지 않는다.
-- 2) 제3자 지침 제17 · 19조: 통합관리지침 붙임 제3호, 시행령 별표 4, 보조금법 시행령 별표 6, 지침 붙임 3-2호를 제 조로 보낸다.
-- 3) 다른 지침 · 자기 표기를 통합관리지침으로 푼 행을 미해결로 돌린다: 집행지침의 총사업비 관리지침 · 국고보조금 통합관리지침, 국고보조금 지침 사례집.
-- 4) 자기 문서에 없는 별표 · 참고로 resolved 된 행: 안내 책자는 실제 조로 잇고, 없으면 미해결로 둔다. 집행지침도 같다.
-- 5) 집행지침을 가리키던 미해결 참조(창업도약 3건, 경인교대 사례집 1건)를 문서 단위로 푼다. 어느 장인지 원문에 없어 확장하지 않는다.
-- 6) 안내 책자의 version 을 판 표기 없음으로, src_path 를 저장소 경로로 고친다(이 파일은 게시본 10-15 판이 아니라 10-14 저장본이다. ontology/수집/안내책자_판확인.md).
-- 7) 판정 캐시를 비운다. 행 수가 바뀌지 않는 수정이라 데이터 버전 해시로는 무효화되지 않는다.
-- 멱등이다. 해당 문서가 하나도 없는 DB(빈 DB 검증)는 그 단계를 건너뛰고, 제3자 지침 행을 못 찾으면 멈춘다.
-- 데이터 수정이라 db/init 은 고치지 않는다. 같은 규칙은 scripts/build_refs.py(scan, 자기_표기_잇기)에 있어 새 적재에는 처음부터 들어간다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-07_참조_보정.sql

DO $$
DECLARE
    v_제3자 CONSTANT text := '창업지원사업 제3자 부당개입 근절을 위한 업무처리 지침_20260630';
    v_집행 CONSTANT text := 'L1_예산및기금운용계획집행지침_20260105';
    v_책자 CONSTANT text := '2차 모두의 창업 1라운드 진출자 지원 안내_20261015';
    v_지침 CONSTANT text := 'L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223';
    v_행 int;
BEGIN
    -- 1) 가지조
    WITH c AS (
        SELECT r.ref_id, regexp_match(regexp_replace(r."참조문자열", '\s', '', 'g'), '제([0-9]+)조(의([0-9]+))?') AS m
          FROM corpus.refs r
         WHERE r."dst_조번호" IS NOT NULL AND r."dst_조번호" ~ '^제[0-9]+조$' AND r."해결상태" <> 'remapped')
    UPDATE corpus.refs r SET "dst_조번호" = '제' || c.m[1] || '조의' || c.m[3]
      FROM c WHERE c.ref_id = r.ref_id AND c.m[3] IS NOT NULL AND r."dst_조번호" = '제' || c.m[1] || '조';
    GET DIAGNOSTICS v_행 = ROW_COUNT;
    RAISE NOTICE '가지조로 옮긴 참조 %행', v_행;

    -- 2) 제3자 지침
    IF EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_제3자) THEN
        UPDATE corpus.refs SET dst_doc_id = v_지침, "dst_조번호" = '붙임3', "해결상태" = 'resolved', "해결근거" = '본문 표기 「통합관리지침 붙임 제3호」'
         WHERE src_doc_id = v_제3자 AND "src_조번호" = '제17조' AND "참조문자열" = '붙임 제3';
        GET DIAGNOSTICS v_행 = ROW_COUNT;
        IF v_행 = 0 THEN RAISE EXCEPTION '제3자 지침 제17조 붙임 제3 행을 찾지 못했다'; END IF;
        UPDATE corpus.refs SET dst_doc_id = 'L1_중소기업창업지원법시행령_20260901', "dst_조번호" = '별표4', "해결상태" = 'resolved', "해결근거" = '본문 표기: 앞에 적힌 규정의 별표'
         WHERE src_doc_id = v_제3자 AND "src_조번호" = '제17조' AND "참조문자열" = '별표 4';
        GET DIAGNOSTICS v_행 = ROW_COUNT;
        IF v_행 = 0 THEN RAISE EXCEPTION '제3자 지침 제17조 별표 4 행을 찾지 못했다'; END IF;
        UPDATE corpus.refs SET dst_doc_id = 'L1_보조금관리에관한법률시행령_20260825', "dst_조번호" = '별표6', "해결상태" = 'resolved', "해결근거" = '본문 표기: 앞에 적힌 규정의 별표'
         WHERE src_doc_id = v_제3자 AND "src_조번호" = '제19조' AND "참조문자열" = '별표 6';
        GET DIAGNOSTICS v_행 = ROW_COUNT;
        IF v_행 = 0 THEN RAISE EXCEPTION '제3자 지침 제19조 별표 6 행을 찾지 못했다'; END IF;
        -- 제19조②: ‘붙임 3-2호’. scan 은 3-1 · 3-2 를 한 표기로 읽는다
        INSERT INTO corpus.refs (src_doc_id, "src_조번호", src_layer, "참조문자열", "관계", dst_doc_id, "dst_조번호", "해결상태", "해결근거")
        VALUES (v_제3자, '제19조', 'L2', '붙임 3-2호', '별표참조', v_제3자, '붙임3[2]', 'resolved', '원문 대조: 제19조② ‘붙임 3-2호’')
        ON CONFLICT (src_doc_id, "src_조번호", "참조문자열") DO NOTHING;
    ELSE
        RAISE NOTICE '제3자 지침이 없다. 2단계를 건너뛴다';
    END IF;

    -- 3) 다른 지침
    UPDATE corpus.refs SET dst_doc_id = NULL, "dst_조번호" = NULL, "해결상태" = 'dangling', "해결근거" = '다른 지침의 조다. 통합관리지침이 아니다'
     WHERE (src_doc_id = v_집행 OR src_doc_id LIKE '사례집_기획재정부_국고보조금통합관리지침%')
       AND "참조문자열" LIKE '지침 제%' AND "해결상태" <> 'dangling';
    GET DIAGNOSTICS v_행 = ROW_COUNT;
    RAISE NOTICE '다른 지침 참조 %행', v_행;

    -- 4) 자기 문서 표기
    UPDATE corpus.refs r SET "dst_조번호" = v.조, "해결근거" = '표제로 찾은 자기 문서 조'
      FROM (VALUES ('참고1', '참고1-p25-1'), ('참고2', '참고2-p27-1')) AS v(표기, 조)
     WHERE r.src_doc_id = v_책자 AND r.dst_doc_id = v_책자 AND r."관계" = '별표참조' AND r."dst_조번호" = v.표기 AND r."src_조번호" <> v.조;
    UPDATE corpus.refs r SET dst_doc_id = NULL, "dst_조번호" = NULL, "해결상태" = 'dangling',
                             "해결근거" = CASE WHEN r."dst_조번호" LIKE '참고%' THEN '표제에 적힌 자기 표기' ELSE '자기 문서에 없는 표기' END
     WHERE r.src_doc_id IN (v_책자, v_집행) AND r.dst_doc_id = r.src_doc_id AND r."관계" = '별표참조'
       AND NOT EXISTS (SELECT 1 FROM corpus.doc_articles a WHERE a.doc_id = r.dst_doc_id AND a."조번호" = r."dst_조번호");
    GET DIAGNOSTICS v_행 = ROW_COUNT;
    RAISE NOTICE '자기 문서에 없는 표기 %행', v_행;
    -- 붙임 표제가 자기를 적은 행. 적재 경로(build_refs.자기_표기_잇기)와 같게 둔다
    UPDATE corpus.refs SET dst_doc_id = NULL, "dst_조번호" = NULL, "해결상태" = 'dangling', "해결근거" = '표제에 적힌 자기 표기'
     WHERE src_doc_id = v_제3자 AND dst_doc_id = v_제3자 AND "관계" = '별표참조' AND "dst_조번호" = "src_조번호";

    -- 5) 집행지침을 가리키던 미해결 참조
    IF EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_집행) THEN
        UPDATE corpus.refs SET dst_doc_id = v_집행, "해결상태" = 'resolved', "해결근거" = '문서 단위 참조. 장 미특정'
         WHERE "해결상태" = 'dangling' AND src_doc_id <> v_집행
           AND "참조문자열" IN ('「예산 및 기금운용계획 집행지침」', '「예산 및 기금운용계획 집행 지침」');
        GET DIAGNOSTICS v_행 = ROW_COUNT;
        RAISE NOTICE '집행지침 문서 단위 참조 %행', v_행;
    END IF;

    -- 6) 안내 책자 판 표기
    UPDATE corpus.documents SET version = '판 표기 없음(2026.10.14 저장본)',
           src_path = '2026_Finance_DATA_FOR_RAG\창진원\모두의 창업 (일반-기술)\2026년 모두의 창업 프로젝트 2차 1라운드 진출자 지원 안내 책자.pdf'
     WHERE doc_id = v_책자;
    UPDATE corpus.chunks SET version = '판 표기 없음(2026.10.14 저장본)' WHERE doc_id = v_책자;
    UPDATE corpus.ingest_jobs SET version = '판 표기 없음(2026.10.14 저장본)' WHERE doc_id = v_책자;

    DELETE FROM tenant.judge_cache;
    GET DIAGNOSTICS v_행 = ROW_COUNT;
    RAISE NOTICE '판정 캐시 삭제 %행', v_행;
END
$$;
