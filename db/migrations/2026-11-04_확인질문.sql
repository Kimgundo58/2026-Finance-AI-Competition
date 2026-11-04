-- 필수. 판정 전 확인 질문(docs/LLM_판정아키텍쳐/07_DB_변경.md)에 필요한 스키마를 더하고 corpus.refs 5행을 고친다.
--   1. tenant.judge_cache 종류 CHECK 에 'questions' 를 더한다
--   2. tenant.decisions 에 "사용자답변" 컬럼을 더한다
--   3. decisions_검증코드_check 에 ANSWER_UNCONFIRMED 를 더한다
--   4. corpus.refs 의 「지침 제40조」 5행이 14차 제37조(재료비)를 잘못 가리키던 것을 고친다
-- 새 이미지보다 먼저 적용한다. 새 코드가 "사용자답변" 을 쓴다. 옛 코드는 영향이 없어 api 를 멈추지 않는다. 멱등이다.
-- 스키마 변경은 db/init/04_validation_eval.sql, 13_judge_cache.sql 과 같다. 4번은 데이터 수정이라 db/init 에 없다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-04_확인질문.sql


-- 1. 질문세트 캐시 ──────────────────────────────────────────────────────────
ALTER TABLE tenant.judge_cache DROP CONSTRAINT IF EXISTS "judge_cache_종류_check";
ALTER TABLE tenant.judge_cache ADD CONSTRAINT "judge_cache_종류_check"
    CHECK (종류 IN ('normalize', 'judge', 'questions'));


-- 2. 판정 기록의 사용자 답변 ────────────────────────────────────────────────
ALTER TABLE tenant.decisions ADD COLUMN IF NOT EXISTS "사용자답변" JSONB NOT NULL DEFAULT '[]';


-- 3. 검증코드. 기준 목록은 scripts/llm_validate.py 의 검증코드_전체 이고, 같은 순서로 맞춘다 ─
ALTER TABLE tenant.decisions DROP CONSTRAINT IF EXISTS "decisions_검증코드_check";
ALTER TABLE tenant.decisions
  ADD CONSTRAINT "decisions_검증코드_check"
  CHECK ("검증코드" <@ ARRAY[
    'INVALID_JUDGMENT','CITE_NOT_IN_MAP','CITE_DB_MISSING','CITE_PARAGRAPH_MISMATCH',
    'PREMISE_NO_BASIS','PREMISE_BASIS_NOT_IN_MAP','PREMISE_INVALID_OUTCOME','PREMISE_UNMAPPED',
    'PREMISE_DB_MISSING',
    'NO_CITATION','VLM_DOWNGRADE','B_GRADE_DOWNGRADE','UNVERIFIED_RULE',
    'TASK_CODE_INVALID','TASK_DB_MISSING',
    'L3_ONLY_DOWNGRADE','TENANT_LEAK','DANGLING_WARN',
    'DOMAIN_WARN','PRECEDENCE_OVERRIDE',
    'TASK_STATE_UNSOURCED','TASK_STATE_MISMATCH',
    'TASK_NUMBER_UNSOURCED','TASK_BASIS_NOT_IN_MAP',
    'ANSWER_UNCONFIRMED'
  ]::text[]);


-- 4. corpus.refs 5행 ────────────────────────────────────────────────────────
-- build_refs.py 의 조문 대응표가 13차 「지침 제40조」를 재료비로 적어 14차 제37조로 옮겼다. 13차 제40조는 여비다.
--   4행(13차 조번호로 쓴 문서): 원문이 여비를 말한다 → 14차 제43조(여비)
--   1행(초격차 제10차 참고2): 14차 조번호로 쓴 표이고 원문이 특허권·무형자산을 말한다 → 14차 제40조.
--     같은 표가 여비를 「지침 제43조」로 적으므로 제43조로 옮기면 또 틀린다.
-- 같은 대응표로 옮겨진 나머지 행은 고치지 않는다(docs/9_백로그.md).
-- 5행이 아니면 멈춘다. 0행은 이미 적용했거나 데이터가 없는 DB 다.
DO $$
DECLARE
    v_지침 CONSTANT text := 'L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223';
    v_제10차 CONSTANT text := '초격차 스타트업 프로젝트 세부관리기준(제10차)';
    v_대상 int;
    v_제10차_대상 int;
    v_바뀐행 int;
BEGIN
    SELECT count(*), count(*) FILTER (WHERE src_doc_id = v_제10차 AND "src_조번호" = '참고2')
      INTO v_대상, v_제10차_대상
      FROM corpus.refs
     WHERE "참조문자열" = '지침 제40조' AND "해결상태" = 'remapped'
       AND dst_doc_id = v_지침 AND "dst_조번호" = '제37조';
    RAISE NOTICE '적용 전: 고칠 행 %, 그중 초격차 제10차 참고2 %', v_대상, v_제10차_대상;

    IF v_대상 = 0 THEN
        RAISE NOTICE '고칠 행이 없다. 이미 적용했거나 corpus.refs 가 비어 있다';
    ELSIF v_대상 <> 5 OR v_제10차_대상 <> 1 THEN
        RAISE EXCEPTION '고칠 행이 예상과 다르다: 전체 %(5 또는 0 이어야 한다), 초격차 제10차 참고2 %(1 이어야 한다)',
            v_대상, v_제10차_대상;
    ELSE
        IF NOT EXISTS (SELECT 1 FROM corpus.doc_articles
                        WHERE doc_id = v_지침 AND "조번호" = '제43조' AND "조제목" = '여비') THEN
            RAISE EXCEPTION '14차 제43조의 조제목이 여비가 아니다';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM corpus.doc_articles
                        WHERE doc_id = v_지침 AND "조번호" = '제40조' AND "조제목" = '특허권 등 무형자산 취득비') THEN
            RAISE EXCEPTION '14차 제40조의 조제목이 특허권 등 무형자산 취득비가 아니다';
        END IF;

        -- build_refs.py 를 다시 돌렸을 때 나오는 값과 같게 쓴다
        UPDATE corpus.refs
           SET "dst_조번호" = '제43조',
               "해결근거" = '제13차 조번호로 표기됨 → 조제목 ''여비'' 로 재매칭'
         WHERE "참조문자열" = '지침 제40조' AND "해결상태" = 'remapped'
           AND dst_doc_id = v_지침 AND "dst_조번호" = '제37조'
           AND NOT (src_doc_id = v_제10차 AND "src_조번호" = '참고2');
        GET DIAGNOSTICS v_바뀐행 = ROW_COUNT;
        IF v_바뀐행 <> 4 THEN
            RAISE EXCEPTION '제43조로 고친 행이 4행이 아니다: %', v_바뀐행;
        END IF;

        UPDATE corpus.refs
           SET "dst_조번호" = '제40조', "해결상태" = 'resolved', "해결근거" = NULL
         WHERE "참조문자열" = '지침 제40조' AND "해결상태" = 'remapped'
           AND dst_doc_id = v_지침 AND "dst_조번호" = '제37조'
           AND src_doc_id = v_제10차 AND "src_조번호" = '참고2';
        GET DIAGNOSTICS v_바뀐행 = ROW_COUNT;
        IF v_바뀐행 <> 1 THEN
            RAISE EXCEPTION '제40조로 고친 행이 1행이 아니다: %', v_바뀐행;
        END IF;
    END IF;

    SELECT count(*) INTO v_대상
      FROM corpus.refs
     WHERE "참조문자열" = '지침 제40조' AND dst_doc_id = v_지침 AND "dst_조번호" = '제37조';
    SELECT count(*) INTO v_바뀐행
      FROM corpus.refs
     WHERE "참조문자열" = '지침 제40조' AND dst_doc_id = v_지침 AND "dst_조번호" = '제43조';
    SELECT count(*) INTO v_제10차_대상
      FROM corpus.refs
     WHERE "참조문자열" = '지침 제40조' AND dst_doc_id = v_지침 AND "dst_조번호" = '제40조';
    RAISE NOTICE '적용 후: 제37조 %, 제43조 %, 제40조 %', v_대상, v_바뀐행, v_제10차_대상;
END
$$;
