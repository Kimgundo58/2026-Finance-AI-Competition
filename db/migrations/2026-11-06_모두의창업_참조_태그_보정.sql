-- 필수(데이터). 모두의 창업 세부관리기준의 참조 · 적용대상을 원문대로 고친다(docs/5_룰/5-7_원문_추출_절차.md 결함 표, 대조 기록은 ontology/수집/P0-2_P0-4_P0-5_기록.md).
--   1. 쉼표로 이어진 지침 조가 빠졌다: 제25조③ → 제30~34조, 제33조② → 제38 · 40 · 42 · 43 · 45조, 제45조① → 제60 · 61조, 제48조① → 제69조 (13행 추가)
--   2. 제30조 「지침 제33조」가 12차 조번호로 오인돼 제36조로 옮겨졌다 → 14차 제33조(회의비)
--   3. 제41 · 43조 「지침의 붙임 제1 · 2호」가 이 문서의 붙임으로 해결됐다(이 문서에 붙임이 없다) → 지침 붙임1 · 붙임2
--   4. 제46 · 50 · 52조 청크가 주관기관 태그라 검색 FILTER 에서 빠진다 → 공통
-- 고치기 전에 원문 문구와 도착 조제목을 확인한다. 다르면 멈춘다. 멱등이다. 문서가 없는 DB 에서는 아무것도 하지 않는다.
-- 데이터 수정이라 db/init 은 고치지 않는다. 다시 만들 때 사라지지 않도록 scripts/build_refs.py(scan), scripts/corpus_ingest.py(_적용대상_고정),
-- 2026_Finance_DATA_FOR_RAG/_refs.json 을 같이 고쳤다. refs 행 수가 바뀌어 판정 캐시 데이터 버전 해시가 바뀐다(server/response_cache.py).
-- 2026-11-06_운영요령_참조정리.sql 과 순서는 상관없다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-06_모두의창업_참조_태그_보정.sql

DO $$
DECLARE
    v_모두 CONSTANT text := '모두의 창업 프로젝트 세부관리기준(개정본)';
    v_지침 CONSTANT text := 'L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223';
    v_근거 CONSTANT text := '쉼표로 이어진 조 표기';
    v_행 record;
    v_n int;
    v_합 int := 0;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_모두 AND status = 'active')
       OR NOT EXISTS (SELECT 1 FROM corpus.documents WHERE doc_id = v_지침 AND status = 'active') THEN
        RAISE NOTICE '모두의 창업 또는 지침 제14차가 active 가 아니다. 아무것도 하지 않는다';
        RETURN;
    END IF;

    -- 원문 확인 (줄바꿈이 끼어 있어 공백을 지우고 비교한다)
    FOR v_행 IN SELECT * FROM (VALUES
        ('제25조', '지침제29조,제30조,제31조,제32조,제33조에따르되,제34조에따라지침에서정하지않은'),
        ('제30조', '제30조(회의비)①회의비는지침제33조에따라'),
        ('제33조', '지침제37조,제38조,제40조,제42조,제43조,제45조에따르되'),
        ('제41조', '지침의붙임제1호의제재'),
        ('제43조', '지침의붙임제2호의제재'),
        ('제45조', '지침제59조,제60조,제61조에따르되'),
        ('제48조', '지침제68조,제69조를따른다'),
        ('제46조', '주관기관및후속지원자는'),
        ('제50조', '주관기관및진출자가'),
        ('제52조', '본관리기준에명시되지않은사항')) AS t(조, 문구)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM corpus.doc_articles a
                        WHERE a.doc_id = v_모두 AND a."조번호" = v_행.조
                          AND position(v_행.문구 IN regexp_replace(a."본문", '\s', '', 'g')) > 0) THEN
            RAISE EXCEPTION '모두의 창업 % 원문에 「%」가 없다', v_행.조, v_행.문구;
        END IF;
    END LOOP;

    -- 도착 조 확인
    FOR v_행 IN SELECT * FROM (VALUES
        ('제30조', '인건비'), ('제31조', '일반수용비'), ('제32조', '여비'), ('제33조', '회의비'), ('제34조', '기타'),
        ('제38조', '외주용역비'), ('제40조', '특허권 등 무형자산 취득비'), ('제42조', '지급수수료'), ('제43조', '여비'),
        ('제45조', '광고선전비'), ('제60조', '창업기업등의 사업비 정산'), ('제61조', '정산잔액 처리원칙'),
        ('제69조', '창업기업등의 사후관리'), ('붙임1', '【붙임 제1호】'), ('붙임2', '【붙임 제2호】')) AS t(조, 제목)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM corpus.doc_articles a
                        WHERE a.doc_id = v_지침 AND a."조번호" = v_행.조 AND a."조제목" = v_행.제목) THEN
            RAISE EXCEPTION '지침 %의 조제목이 %가 아니다', v_행.조, v_행.제목;
        END IF;
    END LOOP;

    -- 1. 빠진 참조 추가. build_refs.scan 이 만드는 값과 같게 쓴다
    INSERT INTO corpus.refs (src_doc_id, "src_조번호", src_layer, "참조문자열", "관계", dst_doc_id, "dst_조번호", "해결상태", "해결근거")
    SELECT v_모두, t.src, 'L2', '지침 ' || t.dst, '준용', v_지침, t.dst, 'resolved', v_근거
      FROM (VALUES
        ('제25조', '제30조'), ('제25조', '제31조'), ('제25조', '제32조'), ('제25조', '제33조'), ('제25조', '제34조'),
        ('제33조', '제38조'), ('제33조', '제40조'), ('제33조', '제42조'), ('제33조', '제43조'), ('제33조', '제45조'),
        ('제45조', '제60조'), ('제45조', '제61조'), ('제48조', '제69조')) AS t(src, dst)
    ON CONFLICT (src_doc_id, "src_조번호", "참조문자열") DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RAISE NOTICE '1. 추가한 참조 %행', v_n;

    -- 2. 제30조 재매핑 오류
    UPDATE corpus.refs SET "dst_조번호" = '제33조', "해결상태" = 'resolved', "해결근거" = NULL
     WHERE src_doc_id = v_모두 AND "src_조번호" = '제30조' AND "참조문자열" = '지침 제33조'
       AND dst_doc_id = v_지침 AND "dst_조번호" = '제36조' AND "해결상태" = 'remapped';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RAISE NOTICE '2. 제30조 재매핑 정정 %행', v_n;

    -- 3. 지침의 붙임
    UPDATE corpus.refs SET dst_doc_id = v_지침, "해결근거" = '본문 표기 「지침의 붙임」'
     WHERE src_doc_id = v_모두 AND dst_doc_id = v_모두
       AND ("src_조번호", "참조문자열", "dst_조번호") IN (('제41조', '붙임 제1', '붙임1'), ('제43조', '붙임 제2', '붙임2'));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RAISE NOTICE '3. 지침 붙임 정정 %행', v_n;

    -- 4. 적용대상. 제46 · 50조는 주관기관과 후속지원자 · 진출자 양쪽의 의무, 제52조는 문서 전체의 해석 조항이다
    UPDATE corpus.chunks SET "적용대상" = '공통'
     WHERE doc_id = v_모두 AND "조번호" IN ('제46조', '제50조', '제52조') AND "적용대상" = '주관기관';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RAISE NOTICE '4. 적용대상 정정 %청크', v_n;

    -- 결과 확인
    SELECT count(*) INTO v_합 FROM corpus.refs r
      JOIN corpus.doc_articles a ON a.doc_id = r.dst_doc_id AND a."조번호" = r."dst_조번호"
     WHERE r.src_doc_id = v_모두 AND r.dst_doc_id = v_지침 AND (
           (r."src_조번호" = '제25조' AND r."dst_조번호" IN ('제29조','제30조','제31조','제32조','제33조','제34조'))
        OR (r."src_조번호" = '제30조' AND r."dst_조번호" = '제33조')
        OR (r."src_조번호" = '제33조' AND r."dst_조번호" IN ('제37조','제38조','제40조','제42조','제43조','제45조'))
        OR (r."src_조번호" = '제41조' AND r."dst_조번호" = '붙임1')
        OR (r."src_조번호" = '제43조' AND r."dst_조번호" = '붙임2')
        OR (r."src_조번호" = '제45조' AND r."dst_조번호" IN ('제59조','제60조','제61조'))
        OR (r."src_조번호" = '제48조' AND r."dst_조번호" IN ('제68조','제69조')));
    IF v_합 <> 20 THEN   -- 제25조 6 · 제30조 1 · 제33조 6 · 제41조 1 · 제43조 1 · 제45조 3 · 제48조 2
        RAISE EXCEPTION '보정 뒤 도착 조가 있는 참조가 20행이 아니다: %', v_합;
    END IF;
    IF EXISTS (SELECT 1 FROM corpus.chunks WHERE doc_id = v_모두 AND "조번호" IN ('제46조', '제50조', '제52조') AND "적용대상" <> '공통') THEN
        RAISE EXCEPTION '제46 · 50 · 52조에 공통이 아닌 청크가 남았다';
    END IF;
    RAISE NOTICE '확인: 보정 대상 참조 %행의 도착 조가 모두 있다', v_합;
END
$$;
