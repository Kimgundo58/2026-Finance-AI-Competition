-- 필수(데이터). P0-6 · P0-7 문서를 적재한 뒤 한 번 실행한다. 절차: ontology/수집/P0-6_P0-7_기록.md
-- 1) active 문서에서 옛 판으로 가는 참조를 새 판으로 옮긴다. 참조 확장은 도착 문서가 active 일 때만 따라가므로(scripts/retrieve.py 참조확장SQL)
--    옮기지 않으면 판이 바뀐 문서의 조문이 판정 문맥에서 빠진다. 옛 판에서 나가는 참조와 옛 판 문서 · 조문은 그대로 둔다.
-- 2) 제3자 부당개입 지침을 가리키던 미해결 참조(모두의 창업 제2조26 → 지침 제5 · 6조)를 푼다.
-- 3) 운영요령 제19조① → 집행지침은 문서 단위 참조로 둔다. 어느 장인지 원문에 없어 참조 확장은 하지 않는다(묶음 E · H 추출 때 정한다).
-- 4) 복원본 경로가 src_path 에 남긴 admin_upload/ 를 저장소 경로로 바꾼다.
-- 5) 판정 캐시를 비운다. 판정 캐시 키는 코퍼스 상태를 담지 않는다.
-- 멱등이다. 새 판이 active 가 아니면 멈춘다. 옛 판에 있던 조가 새 판에 없으면 멈춘다.
-- 옛 판에도 없던 조로 가는 참조(사례집 → 요령 제45조 3행, 산업안전보건법 제408조 1행)는 지금도 확장되지 않으므로 그대로 옮긴다.
-- 데이터 수정이라 db/init 은 고치지 않는다. 원천은 2026_Finance_DATA_FOR_RAG/_refs.json · scripts/build_refs.py.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-07_현행판_참조이동.sql

DO $$
DECLARE
    v_판 CONSTANT text[][] := ARRAY[
        ['L1_중소기업창업지원사업운영요령_20241206', 'L1_중소기업창업지원사업운영요령_20261008'],
        ['중소기업창업 지원사업 운영요령(중소벤처기업부고시)(제2024-101호)(20241206)', 'L1_중소기업창업지원사업운영요령_20261008'],
        ['L1_보조금관리에관한법률시행령_20260102', 'L1_보조금관리에관한법률시행령_20260825'],
        ['L1_공공재정부정청구금지및부정이익환수등에관한법률_20240927', 'L1_공공재정부정청구금지및부정이익환수등에관한법률_20261002'],
        ['공공재정부정청구금지및부정이익환수등에관한법률_제8조', 'L1_공공재정부정청구금지및부정이익환수등에관한법률_20261002'],
        ['L1_공공재정부정청구금지및부정이익환수등에관한법률시행령_20240927', 'L1_공공재정부정청구금지및부정이익환수등에관한법률시행령_20260929'],
        ['L1_중소기업창업지원법시행령_20260324', 'L1_중소기업창업지원법시행령_20260901'],
        ['L1_산업안전보건법_20260801', 'L1_산업안전보건법_20261027'],
        ['L1_소득세법시행령_20260701', 'L1_소득세법시행령_20261001']];
    v_제3자 CONSTANT text := '창업지원사업 제3자 부당개입 근절을 위한 업무처리 지침_20260630';
    v_집행 CONSTANT text := 'L1_예산및기금운용계획집행지침_20260105';
    v_운영요령 CONSTANT text := 'L1_중소기업창업지원사업운영요령_20261008';
    v_모두 CONSTANT text := '모두의 창업 프로젝트 세부관리기준(개정본)';
    v_제3자참조 CONSTANT text := '「창업지원사업 제3자 부당개입 근절을 위한 업무처리 지침(이하 ‘제3자 부당개입 처리지침)」';
    v_옛 text; v_새 text; v_없는조 text; v_행 int; v_합 int := 0; v_캐시 int;
BEGIN
    FOR i IN 1 .. array_length(v_판, 1) LOOP
        v_옛 := v_판[i][1]; v_새 := v_판[i][2];
        IF NOT EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_새 AND status = 'active') THEN
            RAISE EXCEPTION '% 가 active 가 아니다. 문서를 먼저 적재한다', v_새;
        END IF;
        SELECT string_agg(DISTINCT r."dst_조번호", ', ') INTO v_없는조
          FROM corpus.refs r JOIN corpus.documents s ON s.doc_id = r.src_doc_id AND s.status = 'active'
         WHERE r.dst_doc_id = v_옛 AND r."dst_조번호" IS NOT NULL
           AND EXISTS (SELECT 1 FROM corpus.doc_articles a WHERE a.doc_id = v_옛 AND a."조번호" = r."dst_조번호")
           AND NOT EXISTS (SELECT 1 FROM corpus.doc_articles a WHERE a.doc_id = v_새 AND a."조번호" = r."dst_조번호");
        IF v_없는조 IS NOT NULL THEN
            RAISE EXCEPTION '% 에 없는 조로 가는 참조: %', v_새, v_없는조;
        END IF;
        UPDATE corpus.refs r SET dst_doc_id = v_새
          FROM corpus.documents s
         WHERE r.dst_doc_id = v_옛 AND s.doc_id = r.src_doc_id AND s.status = 'active';
        GET DIAGNOSTICS v_행 = ROW_COUNT;
        v_합 := v_합 + v_행;
        RAISE NOTICE '% -> %: %행', v_옛, v_새, v_행;
    END LOOP;
    RAISE NOTICE '옛 판 참조 이동 합계 %행', v_합;

    -- 모두의 창업 제2조26: 「…」제5조 및 제6조에 따른다
    IF NOT EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_제3자 AND status = 'active') THEN
        RAISE EXCEPTION '% 가 active 가 아니다', v_제3자;
    END IF;
    UPDATE corpus.refs SET dst_doc_id = v_제3자, "dst_조번호" = '제5조', "해결상태" = 'resolved', "해결근거" = '원문 대조: 제2조26 「…」제5조 및 제6조'
     WHERE src_doc_id = v_모두 AND "src_조번호" = '제2조' AND "참조문자열" = v_제3자참조;
    GET DIAGNOSTICS v_행 = ROW_COUNT;
    RAISE NOTICE '제3자 지침 제5조 참조: %행', v_행;
    INSERT INTO corpus.refs (src_doc_id, "src_조번호", src_layer, "참조문자열", "관계", dst_doc_id, "dst_조번호", "해결상태", "해결근거")
    VALUES (v_모두, '제2조', 'L2', v_제3자참조 || '제6조', '인용', v_제3자, '제6조', 'resolved', '원문 대조: 제2조26 「…」제5조 및 제6조')
    ON CONFLICT (src_doc_id, "src_조번호", "참조문자열") DO NOTHING;

    -- 운영요령 제19조①: 계상기준 … "예산 및 기금운용계획 집행지침"에 따른다. 따옴표 인용이라 패턴이 잡지 못한다
    IF NOT EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_집행 AND status = 'active') THEN
        RAISE EXCEPTION '% 가 active 가 아니다', v_집행;
    END IF;
    INSERT INTO corpus.refs (src_doc_id, "src_조번호", src_layer, "참조문자열", "관계", dst_doc_id, "dst_조번호", "해결상태", "해결근거")
    VALUES (v_운영요령, '제19조', 'L1', '"예산 및 기금운용계획 집행지침"', '인용', v_집행, NULL, 'resolved', '원문 대조: 제19조① 따옴표 인용, 장 미특정')
    ON CONFLICT (src_doc_id, "src_조번호", "참조문자열") DO NOTHING;

    UPDATE corpus.documents SET src_path = v.경로
      FROM (VALUES
        (v_운영요령, '법령 PDF\L1_법령\L1_중소기업창업지원사업운영요령_20261008.html'),
        ('L1_보조금관리에관한법률시행령_20260825', '법령 PDF\L1_법령\L1_보조금관리에관한법률시행령_20260825.html'),
        ('L1_공공재정부정청구금지및부정이익환수등에관한법률_20261002', '법령 PDF\L1_법령\L1_공공재정부정청구금지및부정이익환수등에관한법률_20261002.html'),
        ('L1_공공재정부정청구금지및부정이익환수등에관한법률시행령_20260929', '법령 PDF\L1_법령\L1_공공재정부정청구금지및부정이익환수등에관한법률시행령_20260929.html'),
        ('L1_중소기업창업지원법시행령_20260901', '법령 PDF\L1_법령\L1_중소기업창업지원법시행령_20260901.html'),
        ('L1_산업안전보건법_20261027', '법령 PDF\L1_법령\L1_산업안전보건법_20261027.html'),
        ('L1_소득세법시행령_20261001', '법령 PDF\L1_법령\L1_소득세법시행령_20261001.html'),
        (v_집행, '법령 PDF\L1_법령\L1_예산및기금운용계획집행지침_20260105.hwpx'),
        (v_제3자, '2026_Finance_DATA_FOR_RAG\창진원\창업지원사업 제3자 부당개입 근절을 위한 업무처리 지침 전문.hwpx'),
        ('제2026-511호 모두의 창업 프로젝트 통합 모집공고(2차)_20260820',
         '2026_Finance_DATA_FOR_RAG\창진원\모두의 창업 (일반-기술)\(제2026-511호)「모두의_창업_프로젝트」_통합_모집_공고_(2차)_중기부게시본.pdf')
      ) AS v(doc_id, 경로)
     WHERE corpus.documents.doc_id = v.doc_id;

    DELETE FROM tenant.judge_cache;
    GET DIAGNOSTICS v_캐시 = ROW_COUNT;
    RAISE NOTICE '판정 캐시 삭제 %행', v_캐시;
END
$$;
