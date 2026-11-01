-- 용어 정리: 테이블·컬럼·인덱스·제약 이름, CHECK 허용 값, 저장값(JSONB 키와 문자열), COMMENT 를 표준 용어로 바꾼다.
-- 함께 하는 일: corpus.item_vocab PK 가 옛 db/init 의 ("비목") 이면 새 db/init 과 같은 ("비목", "비목체계") 로 바꾼다.
-- 실행 시점: 수동 마이그레이션(필수). 새 이름을 쓰는 이미지를 배포하기 직전에 운영 DB 에 한 번 실행한다.
-- 현재 db/init/ 으로 새로 만든 DB 는 이미 새 이름이라 적용할 필요가 없다(실행해도 0절에서 아무것도 바꾸지 않고 끝난다).
--
-- 전제
--   2026-11-01_decisions_강등코드_check.sql 을 적용한 DB. 2026-11-01_corpus_ingest.sql 은 이 파일 앞뒤 어느 쪽에
--   적용해도 된다(옛 파일로 이미 만든 corpus.ingest_jobs 는 9절이 새 컬럼명으로 바꾼다).
--   postgres 롤(슈퍼유저)로 실행한다. tenant 테이블은 RLS 가 걸려 있어 RLS 를 우회하는 롤이어야 모든 행이 바뀐다.
--
-- 실행 전
--   1. API 컨테이너를 멈춘다: docker compose stop api (옛 코드가 옛 이름으로 읽고 쓰지 않게)
--   2. ./backup.sh 로 pg_dump -Fc 백업을 만들고 파일 크기를 확인한다.
--
-- 실행 (NCP VM 의 deploy/ncp 디렉터리). psql 메타 명령(\gset, \if)을 쓰므로 psql 로만 실행한다.
--   docker compose exec -T -e PGCLIENTENCODING=UTF8 db psql -U postgres -d suddoe -X -v ON_ERROR_STOP=1 \
--       -f - < ../../db/migrations/2026-11-02_용어_정리.sql
--   한 트랜잭션이다. 중간에 실패하면 전부 롤백되고 옛 이미지를 다시 띄우면 된다.
--   다시 실행하면 0절에서 이미 새 이름인 것을 확인하고 아무것도 바꾸지 않고 끝난다(종료 코드 0).
--   옛 이름과 새 이름이 섞여 있는 등 예상과 다른 상태면 0절에서 예외를 내고 멈춘다.
--
-- 바꾸지 않는 것
--   - 규정 원문: chunks.text, doc_articles.본문, rules.원문발췌, precedence_rules.원문, golden_set.근거원문,
--     golden_set.정답근거[].원문
--   - 판정 라벨(가능·조건부·불가·판단불가)과 공식 규정 용어(비목·협약·주관기관·세부관리기준 등)
--   - 프롬프트에 들어가는 값: decisions.전제[].매핑, check_items.필요프로필필드 값, rules.사전승인_조건·금지예시·허용예시,
--     check_items.항목·설명, programs.우선규범. 프롬프트 변경과 같은 배포에서 따로 바꾼다.
--   - decisions.검색스냅샷 의 'dangling' 키(현재 코드가 같은 키로 쓴다)
--
-- 적용 후
--   10절 확인을 통과해야 커밋된다. 커밋 뒤 corpus.chunks·corpus.refs 를 VACUUM ANALYZE 한다.
--   새 이미지를 배포하고 /api/health, 로그인 계정의 /api/judge 1건, v_적재현황(로컬 DB) 을 확인한다.
--   변경 전 행은 backup_20261102 스키마에 남는다(앱 롤에는 권한이 없다). 확인 후 지운다:
--     DROP SCHEMA backup_20261102 CASCADE;
--   커밋 뒤 되돌려야 하면 실행 전 백업을 restore.sh 로 복원한다.

\set ON_ERROR_STOP on

-- 0. 사전 확인 ──────────────────────────────────────────────────────────
-- 이미 적용한 DB(또는 새 db/init 으로 만든 DB)면 아무것도 하지 않고 끝낸다.
SELECT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'tenant' AND table_name = 'decisions' AND column_name = '검증코드')
       AND to_regclass('tenant.profile_agreements') IS NOT NULL
       AND to_regclass('tenant.f_profile') IS NULL AS already_applied
\gset
\if :already_applied
\echo '용어 정리: 이미 새 이름이다(적용 완료 또는 db/init 으로 새로 만든 DB). 아무것도 바꾸지 않는다.'
\quit
\endif

BEGIN;
SET LOCAL lock_timeout = '10s';

DO $$
DECLARE
    필수 text[] := ARRAY[
        'corpus.documents', 'corpus.chunks', 'corpus.doc_articles', 'corpus.refs', 'corpus.rules',
        'corpus.precedence_rules', 'corpus.check_items', 'corpus.item_alias', 'corpus.item_vocab',
        'corpus.programs', 'corpus.evidence_sources', 'corpus.recheck_queue',
        'tenant.decisions', 'tenant.f_profile', 'tenant.f_exec', 'tenant.f_personnel', 'tenant.l3_documents',
        'tenant.l3_articles', 'tenant.judge_cache', 'tenant.incidents', 'tenant.org_programs',
        'tenant.unmapped_premise'];
    t text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'tenant' AND table_name = 'decisions' AND column_name = '강등코드') THEN
        RAISE EXCEPTION '용어 정리: tenant.decisions."강등코드" 가 없다. 옛 이름과 새 이름이 섞여 있거나 스키마가 예상과 다르다';
    END IF;
    IF to_regclass('tenant.profile_agreements') IS NOT NULL OR to_regnamespace('backup_20261102') IS NOT NULL THEN
        RAISE EXCEPTION '용어 정리: tenant.profile_agreements 또는 backup_20261102 스키마가 이미 있다';
    END IF;
    FOREACH t IN ARRAY 필수 LOOP
        IF to_regclass(t) IS NULL THEN
            RAISE EXCEPTION '용어 정리: 필요한 테이블 % 이 없다', t;
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION '용어 정리: % 롤은 RLS 를 우회하지 못한다. postgres 롤로 실행한다', current_user;
    END IF;
END $$;

-- eval 스키마와 적재 현황 뷰는 로컬 개발 DB 에만 있다. 운영 DB 에서는 관련 단계를 건너뛴다.
SELECT to_regnamespace('eval') IS NOT NULL AS has_eval,
       (to_regclass('corpus.v_적재현황') IS NOT NULL AND to_regnamespace('eval') IS NOT NULL) AS has_view
\gset

-- 0-1. 저장값을 바꾸는 테이블의 사본 ──────────────────────────────────────
-- chunks·documents 의 retrieval_scope 는 두 값을 1:1 로 바꾸기만 하므로 사본을 만들지 않는다.
CREATE SCHEMA backup_20261102;
COMMENT ON SCHEMA backup_20261102 IS
    '2026-11-02_용어_정리.sql 적용 전 사본. 확인 후 DROP SCHEMA backup_20261102 CASCADE 로 지운다.';
CREATE TABLE backup_20261102.decisions        AS TABLE tenant.decisions;
CREATE TABLE backup_20261102.org_programs     AS TABLE tenant.org_programs;
CREATE TABLE backup_20261102.refs             AS TABLE corpus.refs;
CREATE TABLE backup_20261102.recheck_queue    AS TABLE corpus.recheck_queue;
CREATE TABLE backup_20261102.rules            AS TABLE corpus.rules;
CREATE TABLE backup_20261102.precedence_rules AS TABLE corpus.precedence_rules;
CREATE TABLE backup_20261102.check_items      AS TABLE corpus.check_items;
CREATE TABLE backup_20261102.item_alias       AS TABLE corpus.item_alias;
CREATE TABLE backup_20261102.item_vocab       AS TABLE corpus.item_vocab;
CREATE TABLE backup_20261102.programs         AS TABLE corpus.programs;
CREATE TABLE backup_20261102.evidence_sources AS TABLE corpus.evidence_sources;
\if :has_eval
CREATE TABLE backup_20261102.golden_set       AS TABLE eval.golden_set;
CREATE TABLE backup_20261102.golden_chunks    AS TABLE eval.golden_chunks;
CREATE TABLE backup_20261102.runs             AS TABLE eval.runs;
CREATE TABLE backup_20261102.run_items        AS TABLE eval.run_items;
\endif

-- 0-2. 이 세션에서만 쓰는 함수(pg_temp, 세션이 끝나면 사라진다) ──────────────────

-- 모든 깊이의 JSONB 키를 m 에 따라 바꾼다. 이름이 고유한 키에만 쓴다.
CREATE FUNCTION pg_temp.키변경(j jsonb, m jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE r jsonb;
BEGIN
    IF j IS NULL THEN RETURN NULL; END IF;
    CASE jsonb_typeof(j)
        WHEN 'object' THEN
            SELECT coalesce(jsonb_object_agg(coalesce(m->>k, k), pg_temp.키변경(v, m)), '{}'::jsonb)
              INTO r FROM jsonb_each(j) e(k, v);
        WHEN 'array' THEN
            SELECT coalesce(jsonb_agg(pg_temp.키변경(v, m) ORDER BY i), '[]'::jsonb)
              INTO r FROM jsonb_array_elements(j) WITH ORDINALITY a(v, i);
        ELSE r := j;
    END CASE;
    RETURN r;
END $f$;

-- 최상위 키만 바꾼다. '조립'·'경로' 처럼 다른 위치에도 나올 수 있는 키에 쓴다.
CREATE FUNCTION pg_temp.상위키변경(j jsonb, m jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE AS $f$
    SELECT CASE WHEN jsonb_typeof(j) = 'object'
        THEN (SELECT coalesce(jsonb_object_agg(coalesce(m->>k, k), v), '{}'::jsonb) FROM jsonb_each(j) e(k, v))
        ELSE j END
$f$;

-- 처리 단계 문자열('1정규화+3검색+4조립+6검증')의 토큰을 바꾼다.
CREATE FUNCTION pg_temp.단계변경(s text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
    SELECT CASE WHEN s IS NULL OR s = '' THEN s ELSE (
        SELECT string_agg(coalesce(m.새값, t.v), '+' ORDER BY t.i)
          FROM unnest(string_to_array(s, '+')) WITH ORDINALITY t(v, i)
          LEFT JOIN (VALUES ('1정규화', '정규화'), ('1정규화(외부)', '정규화(재사용)'), ('3검색', '검색'),
                            ('4조립', '판정생성'), ('6검증', '검증'), ('A금지적중', '금지목록일치'),
                            ('B스코어미달', '검색유사도미달'), ('C비목갈림', '비목후보근접'), ('dry중단', 'dry종료')
                    ) m(옛값, 새값) ON m.옛값 = t.v) END
$f$;

-- 객체의 최상위 키를 처리 단계 문자열로 보고 바꾼다(runs.지표.처리단계빈도).
CREATE FUNCTION pg_temp.키에_단계변경(j jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE AS $f$
    SELECT CASE WHEN jsonb_typeof(j) = 'object'
        THEN (SELECT coalesce(jsonb_object_agg(pg_temp.단계변경(k), v), '{}'::jsonb) FROM jsonb_each(j) e(k, v))
        ELSE j END
$f$;

CREATE FUNCTION pg_temp.지연키() RETURNS jsonb LANGUAGE sql IMMUTABLE AS $f$
    SELECT '{"비목확정": "비목후보조회", "l3게이팅": "L3상위확인", "금지적중": "금지목록조회",
             "판정LLM": "판정호출", "조립": "프롬프트구성", "전제해소": "전제확인"}'::jsonb
$f$;

CREATE FUNCTION pg_temp.모델키() RETURNS jsonb LANGUAGE sql IMMUTABLE AS $f$
    SELECT '{"변형": "프롬프트버전", "추론content있음": "추론내용있음", "추론content길이": "추론내용길이",
             "추론content길이_합": "추론내용길이_합"}'::jsonb
$f$;

-- 자유 텍스트 표기: 겹화살괄호는 작은따옴표로, 빨간 원 이모지는 '주의: ' 로, 초록·노랑 원 이모지는 지운다.
CREATE FUNCTION pg_temp.표기정리(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
    SELECT regexp_replace(regexp_replace(regexp_replace(regexp_replace(t,
        '«([^«»]*)»', '''\1''', 'g'),
        '[«»]', '''', 'g'),
        '🔴 ?', '주의: ', 'g'),
        '[🟢🟡] ?', '', 'g')
$f$;

-- 검증 코드 값
CREATE FUNCTION pg_temp.코드변경(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
    SELECT CASE t WHEN 'CITE_HANG_MISMATCH' THEN 'CITE_PARAGRAPH_MISMATCH'
                  WHEN 'PREMISE_ENUM'       THEN 'PREMISE_INVALID_OUTCOME'
                  WHEN 'PRECEDENCE_FLIP'    THEN 'PRECEDENCE_OVERRIDE'
                  ELSE t END
$f$;

-- 골든셋 세트 값
CREATE FUNCTION pg_temp.세트변경(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
    SELECT CASE t WHEN '본세트' THEN '기본' WHEN '적대적' THEN '함정질문'
                  WHEN '보강' THEN '유형보충' WHEN '공식' THEN '공식질의응답' ELSE t END
$f$;

-- 검증 사유 문구(decisions.검증사유, run_items.원출력.검증사유)
CREATE FUNCTION pg_temp.사유문구(t text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE s text := t;
BEGIN
    IF s IS NULL THEN RETURN NULL; END IF;
    s := replace(s, 's맵', '문장맵');
    s := replace(s, 'F 스키마 밖', '프로필 스키마 밖');
    s := replace(s, '4-way enum 밖', '판정 enum(4종) 밖');
    s := replace(s, '판독본', 'VLM 추출본');
    s := replace(s, 'F축', '기업 프로필');
    s := regexp_replace(s, '^\(1\) 정규화 실패', '정규화 실패');
    s := regexp_replace(s, '^\(3\) L3 로드 실패', 'L3 로드 실패');
    s := regexp_replace(s, '^\(4\) LLM 실패', '판정 호출 실패');
    s := regexp_replace(s, '^\(7\) decisions 저장 실패', 'decisions 저장 실패');
    RETURN s;
END $f$;

-- 참조 해결 근거 고정 문구(refs.해결근거, decisions.참조관계[].해결근거)
CREATE FUNCTION pg_temp.참조근거문구(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
    SELECT replace(replace(replace(replace(replace(t,
        '로 해소', '로 해결'),
        '코퍼스에 없는 규범', '코퍼스에 없는 규정'),
        '상위 규범으로 위임', '상위 규정으로 위임'),
        '게이팅(파이프라인 §6.2)', 'L3 상위 규정 확인'),
        '규범명이 ', '규정명이 ')
$f$;

-- 검수 기록 문구(golden_set.검수메모, golden_set.문항메타 문자열). 작업 세션 식별자는 역할 이름으로 바꾼다.
CREATE FUNCTION pg_temp.검수문구(t text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE s text := pg_temp.표기정리(t);
BEGIN
    IF s IS NULL THEN RETURN NULL; END IF;
    s := regexp_replace(s, 'Claude ?\(오너 위임, ?([0-9-]+)\)', 'Claude 검수(사람 미검수), \1', 'g');
    s := regexp_replace(s, 'Claude ?\(오너 위임\)', 'Claude 검수(사람 미검수)', 'g');
    s := regexp_replace(s, '중앙 ai-[0-9a-f]{2} 검증', '통합 검증', 'g');
    s := regexp_replace(s, '중앙 ai-[0-9a-f]{2} 가 ', '통합 검증에서 ', 'g');
    s := regexp_replace(s, 'W[1-4](\(ai-[0-9a-f]{2}\))? 저작', '작성', 'g');
    s := regexp_replace(s, 'W[1-4] 교차검토', '교차검토', 'g');
    s := regexp_replace(s, 'ai-[0-9a-f]{2} ', '', 'g');
    s := regexp_replace(s, 'W[1-4]이 ', '작성자가 ', 'g');
    s := regexp_replace(s, 'W[1-4]의 ', '작성자의 ', 'g');
    s := regexp_replace(s, 'W[1-4] ', '작성자 ', 'g');
    s := replace(s, '워커', '작성자');
    s := replace(s, '저작', '작성');
    s := replace(s, '오너 스팟체크', '책임자 표본 검수');
    s := replace(s, '스팟체크', '표본 검수');
    s := regexp_replace(s, '(자기|자체) 기계검증|기계검증', '자동 검증', 'g');
    s := replace(s, '독립감사', '독립 검토');
    s := replace(s, '의심유형=L3고유_변형', '문항유형=L3변형');
    s := replace(s, '의심유형=L3고유', '문항유형=L3원형');
    s := replace(s, '금지적중() HIT 한다', '금지예시와 일치한다');
    s := replace(s, '금지적중() HIT', '금지예시 일치');
    s := replace(s, '금지적중() 미적중', '금지예시 미일치');
    s := regexp_replace(s, '금지적중\(\) ?이 ', '금지예시 대조가 ', 'g');
    s := replace(s, '금지적중()', '금지예시 대조');
    s := replace(s, '레인 L2 · ', '');
    s := replace(s, '오너 승인', '책임자 승인');
    s := replace(s, '오너', '책임자');
    s := replace(s, '가능 트랩', '가능정답 함정');
    s := replace(s, '트랩', '함정');
    s := replace(s, '핵이', '핵심어가');
    s := replace(s, '정본', '기준');
    s := replace(s, '결손', '누락');
    s := replace(s, '검사축', '검사항목');
    s := replace(s, 'verbatim', '원문일치');
    RETURN s;
END $f$;

-- 재검수 큐 문구(recheck_queue.상세 문자열)
CREATE FUNCTION pg_temp.재검수문구(t text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE s text := pg_temp.표기정리(t);
BEGIN
    IF s IS NULL THEN RETURN NULL; END IF;
    s := regexp_replace(s, '중앙 ai-[0-9a-f]{2}( · 레인 [A-Z0-9]+ 판정)?', '통합 검증', 'g');
    s := replace(s, '갈래', '유형');
    s := replace(s, '밀림', '번호이동');
    s := replace(s, '좌표', '위치');
    s := replace(s, '포인터 오류', '조번호 오매칭');
    s := replace(s, '포인터 교정', '조번호 교정');
    s := replace(s, '포인터를 고친', '조번호를 고친');
    s := replace(s, '포인터', '조번호');
    s := replace(s, '정본', '기준');
    s := replace(s, '오너', '책임자');
    RETURN s;
END $f$;

-- 평가 실행 메모(runs.설정.L3.메모, runs.설정.채점)
CREATE FUNCTION pg_temp.실행메모문구(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
    SELECT replace(replace(replace(replace(pg_temp.표기정리(t),
        '정본', '기준'), '분해', '분할'), '4-way', '4종'), '치명오답', '가능오판')
$f$;

CREATE FUNCTION pg_temp.문구정리(t text, 방식 text) RETURNS text LANGUAGE sql IMMUTABLE AS $f$
    SELECT CASE 방식 WHEN '표기'   THEN pg_temp.표기정리(t)
                     WHEN '검수'   THEN pg_temp.검수문구(t)
                     WHEN '재검수' THEN pg_temp.재검수문구(t)
                     WHEN '코드'   THEN pg_temp.코드변경(t)
                     WHEN '세트'   THEN pg_temp.세트변경(t)
                     ELSE t END
$f$;

-- JSONB 안의 문자열 값을 모두 바꾼다. 제외키 아래는 건드리지 않는다(규정 원문이 들어 있는 키).
CREATE FUNCTION pg_temp.값정리(j jsonb, 방식 text, 제외키 text[]) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE r jsonb;
BEGIN
    IF j IS NULL THEN RETURN NULL; END IF;
    CASE jsonb_typeof(j)
        WHEN 'object' THEN
            SELECT coalesce(jsonb_object_agg(k, CASE WHEN k = ANY (제외키) THEN v
                                                     ELSE pg_temp.값정리(v, 방식, 제외키) END), '{}'::jsonb)
              INTO r FROM jsonb_each(j) e(k, v);
        WHEN 'array' THEN
            SELECT coalesce(jsonb_agg(pg_temp.값정리(v, 방식, 제외키) ORDER BY i), '[]'::jsonb)
              INTO r FROM jsonb_array_elements(j) WITH ORDINALITY a(v, i);
        WHEN 'string' THEN
            r := to_jsonb(pg_temp.문구정리(j #>> '{}', 방식));
        ELSE r := j;
    END CASE;
    RETURN r;
END $f$;

-- corpus.recheck_queue.상세
CREATE FUNCTION pg_temp.재검수상세(j jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE r jsonb := j;
BEGIN
    IF r IS NULL OR jsonb_typeof(r) <> 'object' THEN RETURN r; END IF;
    -- 경유(문자열 1개)와 경유들(배열, 첫 원소가 경유와 같다)을 참조경로 배열 하나로 합친다
    IF r ? '경유들' THEN
        r := (r - '경유' - '경유들') || jsonb_build_object('참조경로', r->'경유들');
    ELSIF r ? '경유' THEN
        r := (r - '경유') || jsonb_build_object('참조경로',
               CASE WHEN jsonb_typeof(r->'경유') = 'array' THEN r->'경유' ELSE jsonb_build_array(r->'경유') END);
    END IF;
    r := pg_temp.상위키변경(r, '{"국가룰": "상위룰", "엄격지점": "엄격항목", "판들": "판목록",
                                "포인터오류": "조번호오매칭", "재지정": "rule_id재매핑",
                                "열위계층": "후순위계층", "해소": "해결"}');
    IF jsonb_typeof(r->'우선순위') = 'object' THEN
        r := jsonb_set(r, '{우선순위}', pg_temp.키변경(r->'우선순위', '{"L3승리": "L3우선"}'));
    END IF;
    IF jsonb_typeof(r->'엄격항목') = 'array' THEN
        r := jsonb_set(r, '{엄격항목}', pg_temp.키변경(r->'엄격항목', '{"국가": "상위"}'));
    END IF;
    RETURN pg_temp.값정리(r, '재검수',
        ARRAY['L3본문', '답변', '질문', '조제목', '룰후보', '상위룰', '기존룰', '참조경로', '조번호들']);
END $f$;

-- eval.run_items.원출력
CREATE FUNCTION pg_temp.실행출력(j jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE r jsonb := j;
BEGIN
    IF r IS NULL OR jsonb_typeof(r) <> 'object' THEN RETURN r; END IF;
    IF r->'갈래' = '[]'::jsonb THEN
        r := r - '갈래';                                   -- 현재 코드가 만들지 않는 키, 전부 빈 배열
    END IF;
    r := pg_temp.상위키변경(r, '{"치명": "가능오판", "강등코드": "검증코드", "강등사유": "검증사유", "경로": "처리단계",
                                "s맵": "문장맵", "게이트값": "최고유사도", "dangling": "미해결참조"}');
    IF jsonb_typeof(r->'처리단계') = 'string' THEN
        r := jsonb_set(r, '{처리단계}', to_jsonb(pg_temp.단계변경(r->>'처리단계')));
    END IF;
    IF jsonb_typeof(r->'지연ms') = 'object' THEN
        r := jsonb_set(r, '{지연ms}', pg_temp.상위키변경(r->'지연ms', pg_temp.지연키()));
    END IF;
    IF jsonb_typeof(r->'모델') = 'object' THEN
        r := jsonb_set(r, '{모델}', pg_temp.키변경(r->'모델', pg_temp.모델키()));
    END IF;
    IF jsonb_typeof(r->'인용목록') = 'array' THEN
        r := jsonb_set(r, '{인용목록}', pg_temp.키변경(r->'인용목록', '{"s번호": "문장ID"}'));
    END IF;
    IF r->>'실패단계' IN ('조립', '판정LLM') THEN
        r := jsonb_set(r, '{실패단계}',
                       to_jsonb(CASE r->>'실패단계' WHEN '조립' THEN '프롬프트구성' ELSE '판정호출' END));
    END IF;
    IF jsonb_typeof(r->'세트') = 'string' THEN
        r := jsonb_set(r, '{세트}', to_jsonb(pg_temp.세트변경(r->>'세트')));
    END IF;
    IF r ? '골든_해야할일' THEN
        r := jsonb_set(r, '{골든_해야할일}', pg_temp.값정리(r->'골든_해야할일', '표기', '{}'));
    END IF;
    IF jsonb_typeof(r->'검증사유') = 'array' THEN
        r := jsonb_set(r, '{검증사유}', (
            SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(e) = 'string'
                                           THEN to_jsonb(pg_temp.사유문구(e #>> '{}')) ELSE e END ORDER BY i), '[]'::jsonb)
              FROM jsonb_array_elements(r->'검증사유') WITH ORDINALITY a(e, i)));
    END IF;
    IF jsonb_typeof(r->'검증코드') = 'array' THEN
        r := jsonb_set(r, '{검증코드}', pg_temp.값정리(r->'검증코드', '코드', '{}'));
    END IF;
    RETURN r;
END $f$;

-- eval.runs.설정
CREATE FUNCTION pg_temp.실행설정(j jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE r jsonb := j;
BEGIN
    IF r IS NULL OR jsonb_typeof(r) <> 'object' THEN RETURN r; END IF;
    r := pg_temp.키변경(r, '{"폐포사용": "참조확장사용", "폐포깊이": "참조확장깊이", "게이트B임계": "검색최소유사도",
                            "변형": "프롬프트버전", "꽂기": "주입", "원본포획": "원본응답저장",
                            "프롬프트전문": "프롬프트전체저장", "부분집합_유보": "부분집합_보류",
                            "SUDDOE_플래그": "SUDDOE_환경변수", "관측_최대토큰_①": "관측_최대토큰_정규화",
                            "관측_최대토큰_④": "관측_최대토큰_판정", "정답고정": "정답매핑", "지문": "매핑해시",
                            "묶음": "분할", "held-out41": "검증41", "미매핑적재_발화": "미매핑적재_발생",
                            "B1블록_문항수": "기관규정블록_문항수"}');
    IF jsonb_typeof(r->'세트') = 'array' THEN
        r := jsonb_set(r, '{세트}', pg_temp.값정리(r->'세트', '세트', '{}'));
    END IF;
    IF jsonb_typeof(r->'분할') = 'object' THEN
        r := jsonb_set(r, '{분할}', pg_temp.값정리(r->'분할', '세트', '{}'));
    END IF;
    IF jsonb_typeof(r->'L3'->'메모') = 'string' THEN
        r := jsonb_set(r, '{L3,메모}', to_jsonb(pg_temp.실행메모문구(r->'L3'->>'메모')));
    END IF;
    IF jsonb_typeof(r->'채점') = 'string' THEN
        r := jsonb_set(r, '{채점}', to_jsonb(pg_temp.실행메모문구(r->>'채점')));
    END IF;
    RETURN r;
END $f$;

-- eval.runs.지표
CREATE FUNCTION pg_temp.실행지표(j jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE r jsonb := j;
BEGIN
    IF r IS NULL OR jsonb_typeof(r) <> 'object' THEN RETURN r; END IF;
    IF r ? '치명' AND r ? '치명오답' THEN
        RAISE EXCEPTION '용어 정리: runs.지표 에 치명 과 치명오답 이 함께 있어 가능오판 으로 합칠 수 없다';
    END IF;
    r := pg_temp.키변경(r, '{"강등코드빈도": "검증코드빈도", "강등코드표": "검증코드표",
                            "강등코드_목록밖": "검증코드_목록밖", "강등코드_목록출처": "검증코드_목록출처",
                            "강등코드_닻대조": "검증코드_기준목록대조", "경로빈도": "처리단계빈도",
                            "치명오답률": "가능오판율", "치명": "가능오판", "치명오답": "가능오판",
                            "치명오답_넓음": "가능오판_넓음", "치명오답_좁음": "가능오판_좁음",
                            "치명_불가조건부": "가능오판_불가조건부", "분해": "분할", "혼동": "혼동행렬",
                            "추론content길이_합": "추론내용길이_합", "겹침평균_20": "겹침평균_top20",
                            "겹침률_%": "겹침률_top20",
                            "CITE_HANG_MISMATCH": "CITE_PARAGRAPH_MISMATCH", "PREMISE_ENUM": "PREMISE_INVALID_OUTCOME",
                            "PRECEDENCE_FLIP": "PRECEDENCE_OVERRIDE",
                            "묶음:held-out41": "분할:검증41", "묶음:튜닝52": "분할:튜닝52",
                            "세트:본세트": "세트:기본", "세트:적대적": "세트:함정질문", "세트:보강": "세트:유형보충",
                            "세트:공식": "세트:공식질의응답"}');
    IF jsonb_typeof(r->'처리단계빈도') = 'object' THEN
        r := jsonb_set(r, '{처리단계빈도}', pg_temp.키에_단계변경(r->'처리단계빈도'));
    END IF;
    RETURN pg_temp.값정리(r, '코드', '{}');               -- 검증코드_목록밖 등 배열 안의 코드 값
END $f$;

-- 1. 허용 값이 바뀌는 CHECK 제거 ─────────────────────────────────────────
ALTER TABLE corpus.documents     DROP CONSTRAINT IF EXISTS documents_retrieval_scope_check;
ALTER TABLE corpus.chunks        DROP CONSTRAINT IF EXISTS chunks_retrieval_scope_check;
ALTER TABLE corpus.refs          DROP CONSTRAINT IF EXISTS "refs_해소상태_check";
ALTER TABLE tenant.decisions     DROP CONSTRAINT IF EXISTS "decisions_강등코드_check";
ALTER TABLE corpus.recheck_queue DROP CONSTRAINT IF EXISTS "recheck_queue_종류_check";
ALTER TABLE tenant.org_programs  DROP CONSTRAINT IF EXISTS "org_programs_연도출처_check";
ALTER TABLE tenant.l3_documents  DROP CONSTRAINT IF EXISTS "l3_documents_파싱품질_check";
ALTER TABLE tenant.incidents     DROP CONSTRAINT IF EXISTS "incidents_종류_check";

-- 2. 테이블 이름. RLS 정책·FK·GRANT·뷰는 OID 로 연결돼 있어 따라온다 ──────────────
ALTER TABLE tenant.f_profile   RENAME TO profile_agreements;
ALTER TABLE tenant.f_exec      RENAME TO profile_expenditures;
ALTER TABLE tenant.f_personnel RENAME TO profile_personnel;
ALTER TABLE tenant.profile_agreements   RENAME CONSTRAINT f_profile_pkey                TO profile_agreements_pkey;
ALTER TABLE tenant.profile_agreements   RENAME CONSTRAINT f_profile_org_id_fkey         TO profile_agreements_org_id_fkey;
ALTER TABLE tenant.profile_agreements   RENAME CONSTRAINT "f_profile_org_id_사업명_key" TO "profile_agreements_org_id_사업명_key";
ALTER TABLE tenant.profile_expenditures RENAME CONSTRAINT f_exec_pkey                   TO profile_expenditures_pkey;
ALTER TABLE tenant.profile_expenditures RENAME CONSTRAINT f_exec_profile_id_fkey        TO profile_expenditures_profile_id_fkey;
ALTER TABLE tenant.profile_expenditures RENAME CONSTRAINT "f_exec_재원_check"           TO "profile_expenditures_재원_check";
ALTER TABLE tenant.profile_personnel    RENAME CONSTRAINT f_personnel_pkey              TO profile_personnel_pkey;
ALTER TABLE tenant.profile_personnel    RENAME CONSTRAINT f_personnel_profile_id_fkey   TO profile_personnel_profile_id_fkey;
ALTER TABLE tenant.profile_personnel    RENAME CONSTRAINT "f_personnel_역할_check"      TO "profile_personnel_역할_check";
ALTER SEQUENCE IF EXISTS tenant.f_exec_exec_id_seq        RENAME TO profile_expenditures_exec_id_seq;
ALTER SEQUENCE IF EXISTS tenant.f_personnel_person_id_seq RENAME TO profile_personnel_person_id_seq;
-- ix_exec_agg 는 이름에 테이블명이 없어 그대로 둔다.

-- 3. 컬럼·인덱스·제약 이름 ─────────────────────────────────────────────
ALTER TABLE corpus.refs             RENAME COLUMN "해소상태"      TO "해결상태";
ALTER TABLE corpus.refs             RENAME COLUMN "보정근거"      TO "해결근거";
ALTER TABLE tenant.decisions        RENAME COLUMN "강등코드"      TO "검증코드";
ALTER TABLE tenant.decisions        RENAME COLUMN "강등사유"      TO "검증사유";
ALTER TABLE tenant.decisions        RENAME COLUMN "경로"          TO "처리단계";
ALTER TABLE tenant.decisions        RENAME COLUMN "버전스탬프"    TO "규정버전";
ALTER TABLE tenant.decisions        RENAME COLUMN "참조사슬"      TO "참조관계";
ALTER TABLE corpus.check_items      RENAME COLUMN "필요f필드"     TO "필요프로필필드";
ALTER TABLE corpus.programs         RENAME COLUMN "비목계통"      TO "비목체계";
ALTER TABLE corpus.item_vocab       RENAME COLUMN "계통"          TO "비목체계";
ALTER TABLE corpus.precedence_rules RENAME COLUMN "열위계층"      TO "후순위계층";
ALTER TABLE corpus.recheck_queue    RENAME COLUMN "구doc_id"      TO "이전판_doc_id";
ALTER TABLE corpus.recheck_queue    RENAME COLUMN "구조번호"      TO "이전판_조번호";
ALTER TABLE tenant.l3_documents     RENAME COLUMN "dangling수"    TO "미해결참조수";
ALTER TABLE tenant.judge_cache      RENAME COLUMN "설정_해시"     TO "데이터버전_해시";
ALTER TABLE corpus.item_alias       RENAME COLUMN "상품명"        TO "품목";
ALTER TABLE corpus.evidence_sources RENAME COLUMN "패키지"        TO "사업명";
ALTER TABLE corpus.evidence_sources RENAME COLUMN "해당비목_정본" TO "해당비목_표준";
ALTER TABLE corpus.doc_articles     RENAME COLUMN "조번호_int"    TO "조순번";
ALTER TABLE tenant.l3_articles      RENAME COLUMN "조번호_int"    TO "조순번";
ALTER TABLE tenant.unmapped_premise RENAME COLUMN "최초"          TO "최초발생";
ALTER TABLE tenant.unmapped_premise RENAME COLUMN "최근"          TO "최근발생";
ALTER TABLE tenant.incidents        RENAME COLUMN "해소"          TO "해결";
\if :has_eval
ALTER TABLE eval.golden_set         RENAME COLUMN "입력필드"      TO "문항메타";
ALTER TABLE eval.golden_chunks      RENAME COLUMN "고정일"        TO "매칭일";
\endif
ALTER INDEX tenant."ix_decisions_경로" RENAME TO "ix_decisions_처리단계";
ALTER INDEX corpus."ix_chunks_사업"    RENAME TO "ix_chunks_사업명";
ALTER TABLE corpus.precedence_rules RENAME CONSTRAINT "precedence_rules_열위계층_check"
                                                   TO "precedence_rules_후순위계층_check";
ALTER TABLE corpus.precedence_rules RENAME CONSTRAINT "precedence_rules_사업명_우선계층_열위계층_key"
                                                   TO "precedence_rules_사업명_우선계층_후순위계층_key";
ALTER TABLE corpus.programs         RENAME CONSTRAINT "programs_비목계통_check" TO "programs_비목체계_check";
ALTER TABLE corpus.item_vocab       RENAME CONSTRAINT "item_vocab_계통_check"   TO "item_vocab_비목체계_check";
-- ix_refs_dangling(WHERE 해결상태 = 'dangling')·uq_recheck_key·uq_item_alias_key·ix_alias_name·ix_articles_doc·
-- ix_l3_org 는 컬럼 이름 변경을 자동으로 따라간다.

-- item_vocab PK: 옛 db/init 으로 만든 DB 는 ("비목") 이고, 개발 DB 는 TIPS 비목을 넣으려고 (비목, 계통) 으로 넓혀 썼다.
-- 새 db/init 과 같게 ("비목", "비목체계") 로 맞춘다. 이미 두 컬럼이면 이름 변경만으로 끝난다. 이 PK 를 참조하는 FK 는 없다.
DO $$
BEGIN
    IF (SELECT array_agg(a.attname::text ORDER BY k.ord)
          FROM pg_constraint c
         CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
         WHERE c.conrelid = 'corpus.item_vocab'::regclass AND c.contype = 'p') = ARRAY['비목'] THEN
        ALTER TABLE corpus.item_vocab DROP CONSTRAINT item_vocab_pkey;
        ALTER TABLE corpus.item_vocab ADD CONSTRAINT item_vocab_pkey PRIMARY KEY ("비목", "비목체계");
    END IF;
END $$;

-- 4. 저장값 ─────────────────────────────────────────────────────────────

-- 4-1. 검색 범위
UPDATE corpus.documents SET retrieval_scope = CASE retrieval_scope WHEN '진입점' THEN '검색대상' ELSE '참조확장전용' END
 WHERE retrieval_scope IN ('진입점', '폐포전용');
UPDATE corpus.chunks    SET retrieval_scope = CASE retrieval_scope WHEN '진입점' THEN '검색대상' ELSE '참조확장전용' END
 WHERE retrieval_scope IN ('진입점', '폐포전용');
ALTER TABLE corpus.documents ALTER COLUMN retrieval_scope SET DEFAULT '검색대상';
ALTER TABLE corpus.chunks    ALTER COLUMN retrieval_scope SET DEFAULT '검색대상';

-- 4-2. 참조 그래프. build_refs.py 를 새 코드로 다시 실행해 만든 결과와 같아야 한다.
UPDATE corpus.refs SET "해결상태" = 'remapped' WHERE "해결상태" = 'shifted';
UPDATE corpus.refs SET "해결근거" = pg_temp.참조근거문구("해결근거")
 WHERE "해결근거" ~ '로 해소|규범|게이팅';

-- 4-3. 판정 기록과 캐시. 전제(프롬프트 값)는 바꾸지 않는다.
UPDATE tenant.decisions SET
    "검증코드"   = array_replace(array_replace(array_replace("검증코드",
                     'CITE_HANG_MISMATCH', 'CITE_PARAGRAPH_MISMATCH'),
                     'PREMISE_ENUM', 'PREMISE_INVALID_OUTCOME'),
                     'PRECEDENCE_FLIP', 'PRECEDENCE_OVERRIDE'),
    "검증사유"   = coalesce((SELECT array_agg(pg_temp.사유문구(s) ORDER BY i)
                               FROM unnest("검증사유") WITH ORDINALITY u(s, i)), "검증사유"),
    "처리단계"   = pg_temp.단계변경("처리단계"),
    "실패단계"   = CASE "실패단계" WHEN '조립' THEN '프롬프트구성' WHEN '판정LLM' THEN '판정호출' ELSE "실패단계" END,
    "지연ms"     = pg_temp.상위키변경("지연ms", pg_temp.지연키()),
    "검색스냅샷" = pg_temp.상위키변경("검색스냅샷", '{"s맵": "문장맵", "게이트값": "최고유사도"}'),
    "모델"       = pg_temp.키변경("모델", pg_temp.모델키()),
    "인용"       = pg_temp.키변경("인용", '{"s번호": "문장ID"}'),
    "참조관계"   = CASE WHEN jsonb_typeof("참조관계") = 'array' THEN (
                     SELECT coalesce(jsonb_agg(
                              CASE WHEN jsonb_typeof(e) = 'object' THEN
                                (SELECT coalesce(jsonb_object_agg(
                                          CASE k WHEN '표기' THEN '참조문자열' WHEN '보정' THEN '해결근거' ELSE k END,
                                          CASE WHEN k = '보정' AND jsonb_typeof(v) = 'string'
                                               THEN to_jsonb(pg_temp.참조근거문구(v #>> '{}')) ELSE v END), '{}'::jsonb)
                                   FROM jsonb_each(e) x(k, v))
                              ELSE e END ORDER BY i), '[]'::jsonb)
                       FROM jsonb_array_elements("참조관계") WITH ORDINALITY a(e, i))
                   ELSE "참조관계" END;
TRUNCATE tenant.judge_cache;          -- 캐시 값에 옛 키(버전스탬프, 인용.s번호)가 들어 있다. 다음 요청에서 다시 채워진다

-- 4-4. 재검수 큐
UPDATE corpus.recheck_queue SET "종류" = CASE "종류" WHEN 'A4개정' THEN '개정감지' WHEN 'A2엄격조항' THEN '엄격조항'
                                                  WHEN 'A5기관답변' THEN '기관답변' WHEN 'H4정산리허설' THEN '정산점검' END
 WHERE "종류" IN ('A4개정', 'A2엄격조항', 'A5기관답변', 'H4정산리허설');
UPDATE corpus.recheck_queue SET "상세" = pg_temp.재검수상세("상세")
 WHERE "상세" IS NOT NULL AND "상세" <> '{}'::jsonb;
-- 요약은 개정감지 행에 구·신 조문 차이(규정 원문)가 들어 있어 엄격조항 행의 표기만 고친다.
UPDATE corpus.recheck_queue SET "요약" = pg_temp.표기정리("요약")
 WHERE "종류" = '엄격조항' AND "요약" ~ '[«»🔴🟢🟡]';
UPDATE corpus.recheck_queue SET "처리자" = CASE "처리자"
        WHEN 'H-조제목대조'  THEN '자동-조제목대조'
        WHEN 'H-재적재정리'  THEN '자동-재적재정리'
        WHEN 'H-A4자동조정'  THEN '자동-개정감지조정'
        ELSE regexp_replace("처리자", '중앙 ai-[0-9a-f]{2}', '통합 검증') END
 WHERE "처리자" ~ '^H-|ai-[0-9a-f]{2}';

-- 4-5. 기관 목록·L3·인시던트·증빙
UPDATE tenant.org_programs SET "연도출처" = '대상연도' WHERE "연도출처" = '대상년도';
UPDATE tenant.l3_documents SET "파싱품질" = 'pending'  WHERE "파싱품질" = '대기';
UPDATE tenant.incidents    SET "종류"     = 'OTHER'    WHERE "종류" = '기타';
UPDATE corpus.evidence_sources
   SET "해당비목_분류" = array_replace(array_replace("해당비목_분류", 'R&D계통', 'RND'), '정본', '표준')
 WHERE "해당비목_분류" && ARRAY['R&D계통', '정본'];

-- 4-6. 우선순위 조항. 근거[].doc(로컬 파일 경로)는 documents.src_path 로 찾은 doc_id 로 바꾼다.
--      찾지 못한 경로는 값을 그대로 두고 키 이름만 바꾼다.
UPDATE corpus.precedence_rules SET "해석" = replace("해석", '이긴다', '우선 적용된다') WHERE "해석" LIKE '%이긴다%';
UPDATE corpus.precedence_rules p SET "근거" = (
    SELECT coalesce(jsonb_agg(
             CASE WHEN jsonb_typeof(e) = 'object' AND e ? 'doc' THEN
               (e - 'doc') || jsonb_build_object('doc_id', coalesce(
                   (SELECT d.doc_id FROM corpus.documents d WHERE d.src_path = e->>'doc'
                     ORDER BY (d.status = 'active') DESC, d.doc_id LIMIT 1),
                   e->>'doc'))
             ELSE e END ORDER BY i), '[]'::jsonb)
      FROM jsonb_array_elements(p."근거") WITH ORDINALITY a(e, i))
 WHERE jsonb_typeof("근거") = 'array' AND "근거" @? '$[*] ? (exists (@.doc))';

-- 4-7. 검수자. 'Claude (오너 위임, …)' 는 사람이 검수하지 않은 AI 검수이고, '오너 승인' 만 사람 검수다.
CREATE FUNCTION pg_temp.검수자문구(t text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE s text := t;
BEGIN
    IF s IS NULL THEN RETURN NULL; END IF;
    s := regexp_replace(s, 'Claude ?\(오너 위임, ?([0-9-]+)\)', 'Claude 검수(사람 미검수), \1', 'g');
    s := replace(s, 'G세션-4단검증', '4단계 검증');
    s := replace(s, '· S6-재도전', '· 재도전성공패키지 룰 추가');
    s := replace(s, '· S7-L1공통', '· L1 공통 룰 추가');
    s := replace(s, '· D1-중앙정정', '· 통합 정정');
    s := replace(s, '· D2-모두의창업축', '· 모두의 창업 기준');
    s := replace(s, '· D2-L1공통승인', '· L1 공통 승인');
    s := replace(s, '· D2b-제33조②직접인용', '· 제33조② 직접 인용');
    s := replace(s, 'ai-4e 내용검증 + ai-33 중앙확인', 'Claude 내용 검증 + 통합 확인');
    s := replace(s, '오너 승인', '책임자 승인');
    RETURN s;
END $f$;
UPDATE corpus.rules            SET "검수자" = pg_temp.검수자문구("검수자") WHERE "검수자" ~ '오너|ai-[0-9a-f]{2}|G세션|· [SD][0-9]';
UPDATE corpus.precedence_rules SET "검수자" = pg_temp.검수자문구("검수자") WHERE "검수자" ~ '오너|ai-[0-9a-f]{2}|G세션|· [SD][0-9]';
UPDATE corpus.check_items      SET "검수자" = pg_temp.검수자문구("검수자") WHERE "검수자" ~ '오너|ai-[0-9a-f]{2}|G세션|· [SD][0-9]';

-- 4-8. 체크항목 기한 근거 설명(코드가 읽지 않는 설명 텍스트)
UPDATE corpus.check_items SET "기한근거_상세" = replace(replace(pg_temp.표기정리("기한근거_상세"), '오너', '책임자'), '정본', '기준')
 WHERE "기한근거_상세" ~ '[«»🔴🟢🟡]|오너|정본';

-- 4-9. 품목 별칭 출처
UPDATE corpus.item_alias SET "출처" =
    regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace("출처",
        '^규정0906_어([A-C])$', '규정발췌_20260906_\1'),
        '^규정0906_각론$', '규정발췌_20260906_각론'),
        '^제거후보상식_', '삭제후보_일반상식_'),
        '^제거후보오염_격리_골든셋', '삭제후보_평가누수_골든셋'),
        '^되살림후보_격리_골든셋', '복원후보_평가누수검토_골든셋'),
        '^보류_격리_골든셋', '보류_평가누수검토_골든셋')
 WHERE "출처" ~ '^(규정0906_|제거후보|되살림후보_격리_|보류_격리_)';

-- 4-10. 사업·비목 어휘집 비고. 창업 비목체계 행은 db/init/04_validation_eval.sql 의 값과 같게 한다.
UPDATE corpus.programs SET "비고" = '2026-08-31 현재 corpus.rules 0행. 이후 적재. 구판(2025)이 active 상태였음'
 WHERE "사업명" = '재도전성공패키지' AND "비고" LIKE '%G1 이 적재%';
UPDATE corpus.programs SET "비고" =
    'RND 비목체계(R&D 비목표 26종)라 창업 비목체계 어휘집 10종과 다르다. 우선순위 조항 없음(precedence_rules 0행). 설계 결정 대기'
 WHERE "사업명" = 'TIPS' AND "비고" LIKE '%G2 설계 결정 대기%';
UPDATE corpus.item_vocab SET "비고" = '창업 비목표 6~22 가 이 한 비목으로 묶인다. 하위항목별 한도가 달라 rules 는 하위까지 봐야 한다'
 WHERE "비목" = '지급수수료' AND "비목체계" = '창업' AND "비고" LIKE '표A %';
UPDATE corpus.item_vocab SET "비고" = '통합관리지침 제44조②(본인부담금)·③(4대보험)이 전 사업에 적용된다'
 WHERE "비목" = '교육훈련비' AND "비목체계" = '창업' AND "비고" LIKE '%— G4';
UPDATE corpus.item_vocab SET "비고" =
    replace(replace(replace(replace(pg_temp.표기정리("비고"),
        '정본은 ''11.(산정기준) 분리'' 기준 (2026-09-06 오너 확정)', '''11.(산정기준) 분리'' 를 기준으로 한다 (2026-09-06 책임자 확정)'),
        '정본은 11.(분리) 기준(오너 결정)', '11.(분리) 를 기준으로 한다(책임자 결정)'),
        '2026-09-06 해소 — PK 를 (비목, 계통) 으로 바꾸고', '2026-09-06 해결 — PK 를 (비목, 비목체계) 로 바꾸고'),
        '오너', '책임자')
 WHERE "비목체계" = 'RND' AND "비고" ~ '[«»🔴🟢🟡]|정본|오너|해소';

-- 4-11. 평가 데이터(로컬 개발 DB 에만 있다)
\if :has_eval
UPDATE eval.golden_set SET "세트" = pg_temp.세트변경("세트") WHERE "세트" IN ('본세트', '적대적', '보강', '공식');
UPDATE eval.golden_set SET "채점모드" = '전체'       WHERE "채점모드" IN ('full', '공식');
UPDATE eval.golden_set SET "채점모드" = '가능오판만' WHERE "채점모드" = '치명오답만';
UPDATE eval.golden_set SET "평가범위" = '핵심'       WHERE "평가범위" = '주력';
UPDATE eval.golden_set SET "채점대상" = array_replace("채점대상", '치명오답률', '가능오판율')
 WHERE '치명오답률' = ANY ("채점대상");
-- 정답근거: 원소의 키 doc → doc_id, _대조방식 → 대조방식. 원문 값은 그대로 둔다.
UPDATE eval.golden_set SET "정답근거" = (
    SELECT coalesce(jsonb_agg(
             CASE WHEN jsonb_typeof(e) = 'object' THEN
               (SELECT coalesce(jsonb_object_agg(
                         CASE k WHEN 'doc' THEN 'doc_id' WHEN '_대조방식' THEN '대조방식' ELSE k END,
                         CASE WHEN k = '_대조방식' AND v = '"verbatim"'::jsonb THEN '"원문일치"'::jsonb ELSE v END),
                       '{}'::jsonb)
                  FROM jsonb_each(e) x(k, v))
             ELSE e END ORDER BY i), '[]'::jsonb)
      FROM jsonb_array_elements("정답근거") WITH ORDINALITY a(e, i))
 WHERE jsonb_typeof("정답근거") = 'array' AND "정답근거" <> '[]'::jsonb;
-- 문항메타: 키 이름, 값 표기(비목매핑·출처), 작성 메모 문구
UPDATE eval.golden_set SET "문항메타" = pg_temp.값정리(pg_temp.키변경("문항메타",
    '{"트랩": "함정", "가능트랩": "가능정답_함정", "저작메모": "작성메모", "층": "계층", "층별": "계층별",
      "층간충돌": "계층간충돌", "이유역할": "사유유형", "인용신뢰": "인용신뢰등급", "근거실재": "근거존재",
      "세부위치": "근거위치", "원비목": "원문비목", "추출": "추출방식"}'), '검수', ARRAY['doc_id', '조번호'])
 WHERE "문항메타" IS NOT NULL;
UPDATE eval.golden_set SET "문항메타" = jsonb_set("문항메타", '{비목매핑}',
    to_jsonb(CASE "문항메타"->>'비목매핑' WHEN '근사' THEN '유사' WHEN '직접' THEN '일치' ELSE '대응없음' END))
 WHERE "문항메타"->>'비목매핑' IN ('근사', '직접', '불가');
UPDATE eval.golden_set SET "문항메타" = jsonb_set("문항메타", '{출처}', '"직접작성"')
 WHERE "문항메타"->>'출처' = '직접작성(적대적)';
UPDATE eval.golden_set SET "검수메모" = pg_temp.검수문구("검수메모") WHERE "검수메모" IS NOT NULL;
UPDATE eval.golden_set SET "해야할일" = pg_temp.값정리("해야할일", '표기', '{}')
 WHERE "해야할일"::text ~ '[«»🔴🟢🟡]';
UPDATE eval.golden_chunks SET "실패사유" =
    replace(replace(replace(pg_temp.표기정리("실패사유"), '통째로', '전부'), '결손', '누락'), '좌표', '위치')
 WHERE "실패사유" ~ '[«»🔴🟢🟡]|통째로|결손|좌표';

UPDATE eval.run_items SET "원출력" = pg_temp.실행출력("원출력");
UPDATE eval.run_items SET "예측" = '비목선택필요' WHERE "예측" = '선택필요';
UPDATE eval.runs SET
    "설정" = pg_temp.실행설정("설정"),
    "지표" = pg_temp.실행지표("지표"),
    "비고" = replace(replace(pg_temp.표기정리("비고"), 'D7 스윕', '검색 가중치 스윕'), '원문역추적', '원문부분일치'),
    "라벨" = replace(replace(replace(pg_temp.표기정리("라벨"), 'GPU팟', 'GPU 파드'), '폐포', '참조확장'),
                     '역추적', '원문부분일치');
\endif

-- 5. CHECK 재생성. 정의는 db/init 과 같다 ────────────────────────────────────
ALTER TABLE corpus.documents ADD CONSTRAINT documents_retrieval_scope_check
    CHECK (retrieval_scope IN ('검색대상', '참조확장전용'));
ALTER TABLE corpus.chunks ADD CONSTRAINT chunks_retrieval_scope_check
    CHECK (retrieval_scope IN ('검색대상', '참조확장전용'));
ALTER TABLE corpus.refs ADD CONSTRAINT "refs_해결상태_check"
    CHECK ("해결상태" IN ('resolved', 'remapped', 'dangling'));
-- 기준 목록은 scripts/llm_validate.py 의 검증코드_전체 이고, 같은 순서로 맞춘다(db/init/04_validation_eval.sql 과 같은 목록).
ALTER TABLE tenant.decisions ADD CONSTRAINT "decisions_검증코드_check" CHECK ("검증코드" <@ ARRAY[
    'INVALID_JUDGMENT','CITE_NOT_IN_MAP','CITE_DB_MISSING','CITE_PARAGRAPH_MISMATCH',
    'PREMISE_NO_BASIS','PREMISE_BASIS_NOT_IN_MAP','PREMISE_INVALID_OUTCOME','PREMISE_UNMAPPED',
    'PREMISE_DB_MISSING',
    'NO_CITATION','VLM_DOWNGRADE','B_GRADE_DOWNGRADE','UNVERIFIED_RULE',
    'TASK_CODE_INVALID','TASK_DB_MISSING',
    'L3_ONLY_DOWNGRADE','TENANT_LEAK','DANGLING_WARN',
    'DOMAIN_WARN','PRECEDENCE_OVERRIDE',
    'TASK_STATE_UNSOURCED','TASK_STATE_MISMATCH',
    'TASK_NUMBER_UNSOURCED','TASK_BASIS_NOT_IN_MAP'
]::text[]);
ALTER TABLE corpus.recheck_queue ADD CONSTRAINT "recheck_queue_종류_check"
    CHECK ("종류" IN ('개정감지', '엄격조항', '기관답변', '정산점검'));
ALTER TABLE tenant.org_programs ADD CONSTRAINT "org_programs_연도출처_check"
    CHECK ("연도출처" IN ('대상연도', '파일명', '수집기록'));
ALTER TABLE tenant.l3_documents ADD CONSTRAINT "l3_documents_파싱품질_check"
    CHECK ("파싱품질" IN ('pending', 'pass', 'warn', 'fail'));
ALTER TABLE tenant.incidents ADD CONSTRAINT "incidents_종류_check"
    CHECK ("종류" IN ('TENANT_LEAK', 'INDEX_GUARD', 'SCHEMA_VIOLATION', 'ROUTING_BLOCK', 'OTHER'));

-- 6. corpus.rules 보호 트리거. 함수 본문은 db/init/16_rules_guard.sql 과 같다 ──────────────
DROP TRIGGER IF EXISTS "trg_rules_truncate_금지" ON corpus.rules;
DROP FUNCTION IF EXISTS corpus."f_rules_truncate_금지"();

CREATE OR REPLACE FUNCTION corpus.rules_block_truncate() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION
    'corpus.rules TRUNCATE 는 금지되어 있다. 룰 테이블에는 검수 이력이 누적되어 있어 재적재하면 되돌릴 수 없다. '
    '꼭 필요하면 백업 후 DROP TRIGGER trg_rules_block_truncate ON corpus.rules; 를 실행한다.';
END $fn$;

CREATE OR REPLACE FUNCTION corpus.rules_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    g jsonb;
    doc text;
    조번호값 text;
    found boolean;
    한도유형목록 text[] := ARRAY['금액','비율','개수'];
    변경됨 boolean;
BEGIN
    -- 근거: 비어 있으면 안 되고, 배열의 각 {doc_id,조번호} 가 실재하는 조여야 한다
    변경됨 := (TG_OP = 'INSERT') OR (NEW.근거 IS DISTINCT FROM OLD.근거);
    IF 변경됨 THEN
        IF NEW.근거 IS NULL OR jsonb_array_length(NEW.근거) = 0 THEN
            RAISE EXCEPTION 'rules_guard: rule_id=% 근거가 비어 있다. [{doc_id,조번호}] 가 1건 이상 필요하다', NEW.rule_id;
        END IF;
        FOR g IN SELECT jsonb_array_elements(NEW.근거) LOOP
            doc      := g->>'doc_id';
            조번호값 := g->>'조번호';
            IF doc IS NULL OR 조번호값 IS NULL THEN
                RAISE EXCEPTION 'rules_guard: rule_id=% 근거 원소에 doc_id/조번호 가 없다: %', NEW.rule_id, g;
            END IF;
            SELECT EXISTS(
                SELECT 1 FROM corpus.doc_articles a
                WHERE a.doc_id = doc AND a.조번호 = 조번호값 AND NOT a.삭제
            ) INTO found;
            IF NOT found THEN
                RAISE EXCEPTION 'rules_guard: rule_id=% 근거 조가 doc_articles 에 없거나 삭제되었다: % %',
                    NEW.rule_id, doc, 조번호값;
            END IF;
        END LOOP;
    END IF;

    -- 비목: corpus.item_vocab 에 없으면 거부한다
    변경됨 := (TG_OP = 'INSERT') OR (NEW.비목 IS DISTINCT FROM OLD.비목);
    IF 변경됨 AND NOT EXISTS (SELECT 1 FROM corpus.item_vocab v WHERE v.비목 = NEW.비목) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 비목 ''%'' 이 corpus.item_vocab 에 없다',
            NEW.rule_id, NEW.비목;
    END IF;

    -- 한도_유형/한도_값: 같이 있거나 같이 없어야 한다
    변경됨 := (TG_OP = 'INSERT')
              OR (NEW.한도_유형 IS DISTINCT FROM OLD.한도_유형)
              OR (NEW.한도_값 IS DISTINCT FROM OLD.한도_값);
    IF 변경됨 AND ((NEW.한도_유형 IS NULL) <> (NEW.한도_값 IS NULL)) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_유형과 한도_값은 같이 넣거나 같이 비워야 한다', NEW.rule_id;
    END IF;

    IF 변경됨 AND NEW.한도_유형 IS NOT NULL AND NOT (NEW.한도_유형 = ANY(한도유형목록)) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_유형 ''%'' 은 허용되지 않는다(금액/비율/개수 만 허용)', NEW.rule_id, NEW.한도_유형;
    END IF;

    -- 한도_단위: 이번에 변경된 경우만 12자 제한. 기존 위반 행은 이 컬럼을 건드리지 않으면 통과한다
    변경됨 := (TG_OP = 'INSERT') OR (NEW.한도_단위 IS DISTINCT FROM OLD.한도_단위);
    IF 변경됨 AND NEW.한도_단위 IS NOT NULL AND length(NEW.한도_단위) > 12 THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_단위가 %자다. 상한은 12자이고 문장은 사전승인_조건 에 넣는다: %',
            NEW.rule_id, length(NEW.한도_단위), left(NEW.한도_단위, 40);
    END IF;

    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_rules_block_truncate ON corpus.rules;
CREATE TRIGGER trg_rules_block_truncate BEFORE TRUNCATE ON corpus.rules
    FOR EACH STATEMENT EXECUTE FUNCTION corpus.rules_block_truncate();

-- 7. 적재 현황 뷰(로컬 개발 DB). db/init/17_check_items_questions.sql 과 같은 정의 ─────────────
\if :has_view
CREATE OR REPLACE VIEW corpus.v_적재현황 AS
SELECT '01. corpus.documents'      AS 테이블, count(*) AS 건수 FROM corpus.documents
UNION ALL SELECT '02. corpus.doc_articles',     count(*) FROM corpus.doc_articles
UNION ALL SELECT '03. corpus.chunks',           count(*) FROM corpus.chunks
UNION ALL SELECT '04. corpus.chunk_terms',      count(*) FROM corpus.chunk_terms
UNION ALL SELECT '05. corpus.chunk_len',        count(*) FROM corpus.chunk_len
UNION ALL SELECT '06. corpus.rules',            count(*) FROM corpus.rules
UNION ALL SELECT '07. corpus.precedence_rules', count(*) FROM corpus.precedence_rules
UNION ALL SELECT '08. corpus.refs',             count(*) FROM corpus.refs
UNION ALL SELECT '09. corpus.item_alias',       count(*) FROM corpus.item_alias
UNION ALL SELECT '10. corpus.item_vocab',       count(*) FROM corpus.item_vocab
UNION ALL SELECT '11. corpus.programs',         count(*) FROM corpus.programs
UNION ALL SELECT '12. corpus.check_items',      count(*) FROM corpus.check_items
UNION ALL SELECT '13. corpus.evidence_sources', count(*) FROM corpus.evidence_sources
UNION ALL SELECT '14. corpus.xref_mismatch',    count(*) FROM corpus.xref_mismatch
UNION ALL SELECT '15. corpus.recheck_queue',    count(*) FROM corpus.recheck_queue
UNION ALL SELECT '16. tenant.orgs',             count(*) FROM tenant.orgs
UNION ALL SELECT '17. tenant.org_programs',     count(*) FROM tenant.org_programs
UNION ALL SELECT '18. tenant.accounts',         count(*) FROM tenant.accounts
UNION ALL SELECT '19. tenant.l3_documents',     count(*) FROM tenant.l3_documents
UNION ALL SELECT '20. tenant.l3_articles',      count(*) FROM tenant.l3_articles
UNION ALL SELECT '21. tenant.profile_agreements',   count(*) FROM tenant.profile_agreements
UNION ALL SELECT '22. tenant.profile_expenditures', count(*) FROM tenant.profile_expenditures
UNION ALL SELECT '23. tenant.profile_personnel',    count(*) FROM tenant.profile_personnel
UNION ALL SELECT '24. tenant.expense_plans',    count(*) FROM tenant.expense_plans
UNION ALL SELECT '25. tenant.plan_tasks',       count(*) FROM tenant.plan_tasks
UNION ALL SELECT '26. tenant.decisions',        count(*) FROM tenant.decisions
UNION ALL SELECT '27. tenant.judge_cache',      count(*) FROM tenant.judge_cache
UNION ALL SELECT '28. tenant.incidents',        count(*) FROM tenant.incidents
UNION ALL SELECT '29. tenant.unmapped_premise', count(*) FROM tenant.unmapped_premise
UNION ALL SELECT '30. eval.golden_set',         count(*) FROM eval.golden_set
UNION ALL SELECT '31. eval.golden_chunks',      count(*) FROM eval.golden_chunks
UNION ALL SELECT '32. eval.runs',               count(*) FROM eval.runs
UNION ALL SELECT '33. eval.run_items',          count(*) FROM eval.run_items
ORDER BY 1;
\endif

-- 8. COMMENT. db/init 의 COMMENT ON 문과 같은 문자열이다(새 이름 기준) ──────────────────
COMMENT ON TABLE corpus.documents IS '원본 문서 대장. 인덱서는 status=active 만 읽는다.';
COMMENT ON COLUMN corpus.documents.retrieval_scope IS
  '검색대상 = 검색 후보 / 참조확장전용 = refs 로만 도달. 범위를 줄일 때는 측정(Recall@5) 후 UPDATE 로 확정한다.';
COMMENT ON COLUMN corpus.chunks.text IS '원문 문자열 그대로. 인용 검증이 이 값과 대조한다.';
COMMENT ON COLUMN corpus.rules.verified IS 'false 인 룰만으로 "가능" 판정 금지. 조문 인용 동반 필수.';
COMMENT ON TABLE corpus.precedence_rules IS
  '우선순위 조항. 없으면 폴백(상위 규정 우선 + 엄격한 값 우선). 사업별로 문구가 달라 사람 검수 대상.';
COMMENT ON COLUMN corpus.precedence_rules.범위 IS
  'all = 항상 우선계층을 적용(재도전성공패키지 제3조) / unspecified_only = 상위에 없는 사항만 하위 적용(초격차 제3조)';
COMMENT ON TABLE  corpus.refs IS
  '참조 그래프. 검색된 조에서 깊이 3까지 참조 확장 대상을 모아 LLM 컨텍스트에 넣는다. LLM 은 이 표를 보지 않는다 — 코드가 조회해 텍스트로 넣는다.';
COMMENT ON COLUMN corpus.refs.해결상태 IS
  'resolved=정상 / remapped=조번호가 구판이라 조제목으로 재매칭함 / dangling=코퍼스에 대상 없음(판단불가 예고)';
COMMENT ON COLUMN corpus.refs.관계 IS
  '미규정위임 = "이 지침에서 정하지 아니한 사항은 ~에 따름". 건국대 지침에서 확인한 문장 형식. L3 상위 규정 확인(docs/5_룰/5-2_효력_결정.md 3절)의 근거가 된다.';
COMMENT ON TABLE corpus.check_items IS
    '결제 전/후 확인 항목의 고정 목록. code 가 재판정 간 진행상황을 잇는 키다.';
COMMENT ON TABLE corpus.evidence_sources IS
    '증빙서류별 발급처·설명. 출처 CSV 124행. 판정에 쓰이지 않고 안내에만 쓴다.';
COMMENT ON TABLE tenant.expense_plans IS
    '지출 계획. 상태는 진행(draft/judged)이지 판정이 아니다.';
COMMENT ON TABLE tenant.plan_tasks IS
    '집행 준비 체크리스트와 캘린더가 공유하는 한 테이블. due_date 유무로 갈린다.';
COMMENT ON COLUMN tenant.expense_plans.사업명 IS
    '룰 조회 키(사업 x 비목)의 절반. 게스트는 profile_agreements 행이 없어 여기가 유일한 저장처다.';
COMMENT ON COLUMN tenant.profile_personnel.역할 IS
    '대표자 | 신규채용 | 기존직원. CHECK 로 값을 제한한다 — 인원수 집계가 한도의 분모라서.';
COMMENT ON COLUMN tenant.l3_documents."출처" IS
  '테스트픽스처 = 합성 L3(L3 상위 규정 확인·RLS 검증용). 판정 코퍼스 통계·실기관 규정과 분리하는 라벨.';
COMMENT ON COLUMN corpus.check_items."기본_오프셋일" IS
  '집행예정일 기준 부호 있는 일수. due_date = 집행예정일 + 이 값. '
  '결제전은 음수(집행 전), 결제후는 양수(집행 후), NULL 은 기한 없음. '
  '구분으로 분기하지 않는다 — 부호가 방향을 나타낸다 (2026-08-31 결정).';
COMMENT ON COLUMN tenant.plan_tasks."유형" IS
    '캘린더 배지용 표시 값. 판정(4종)과도 plan_tasks.상태 와도 무관하다.';
COMMENT ON COLUMN tenant.expense_plans."추가설명" IS
    '폼의 자유 텍스트 원문. 정규화 용도에 합류하되 원문은 여기 남긴다.';
COMMENT ON COLUMN corpus.check_items."유형" IS
    '캘린더 배지용 표시 값. plan_tasks."유형" 이 이 값을 따른다 — 항목이 추가돼도 분류가 어긋나지 않도록 '
    '코드 분류표 대신 여기서 관리한다.';
COMMENT ON COLUMN tenant.l3_documents."파싱품질" IS
    'pending = 업로드만 접수됐고 파싱 평가 전. pass/warn/fail 은 평가를 마친 뒤에만 쓴다 — '
    '접수 시점에 warn 을 넣으면 "평가 결과 의심" 과 "아직 평가 안 함" 이 같은 값이 되어 구분할 수 없다.';
COMMENT ON TABLE  tenant.org_programs IS
    '사업×연도별 기관 목록. 창진원 공개자료 원본에서 추출. L3 규정 본문이 아니라 ''누가 그 사업의 기관인가'' 만 담는다';
COMMENT ON COLUMN tenant.org_programs.역할 IS
    '원문이 쓴 말 그대로. 사업마다 다르다 — 주관기관/운영기관/멘토기관/운영사/협력기관';
COMMENT ON COLUMN tenant.org_programs.연도출처 IS
    '기준연도를 무엇에서 읽었나. 대상연도(본문) > 파일명 순으로 신뢰한다';
COMMENT ON COLUMN tenant.orgs."사업명" IS
    '파생값. 기준 데이터는 tenant.org_programs 다 (2026-09-02). 연도를 못 담아서 분리했다';
COMMENT ON COLUMN corpus.programs."L3발행역할" IS
    '이 사업에서 L3(기관 규정)를 발행하는 역할. tenant.org_programs.역할 과 같은 어휘';
DO $$
BEGIN
    IF to_regclass('tenant."v_사업_기준연도"') IS NOT NULL THEN
        COMMENT ON VIEW tenant."v_사업_기준연도" IS
            '사업별 기관 목록의 최신 연도. 프론트 「2025 기준」 표시의 출처. 사업마다 다르다';
    END IF;
    IF to_regclass('tenant."v_기관명부_최신"') IS NOT NULL THEN
        COMMENT ON VIEW tenant."v_기관명부_최신" IS
            '사업별 최신 연도의 기관 목록. 기관 선택 화면용. 주의: org_id 를 그대로 내보내지 않는다 — 기관 사칭에 쓰일 수 있다';
    END IF;
    IF to_regclass('tenant."v_L3발행기관_최신"') IS NOT NULL THEN
        COMMENT ON VIEW tenant."v_L3발행기관_최신" IS
            'L3(기관 규정)를 올리는 기관만, 사업별 최신 연도로. 주의: org_id 를 그대로 내보내지 않는다 — 기관 사칭에 쓰일 수 있다';
    END IF;
    IF to_regprocedure('tenant.계정찾기(text)') IS NOT NULL THEN
        COMMENT ON FUNCTION tenant.계정찾기(text) IS
            '로그인 전용. 이메일 1건 → (org_id, account_id) 1행. RLS 를 우회하는 유일한 통로이므로 '
            '인자·반환을 절대 늘리지 마라 — 늘리는 순간 accounts 전건 열람이 된다.';
    END IF;
END $$;
\if :has_eval
COMMENT ON TABLE eval.golden_set IS
  '절대 corpus.chunks 에 넣지 말 것 (정답 유출). Supabase 덤프 대상도 아니다.';
COMMENT ON COLUMN eval.golden_set.비목 IS
    '폼 입력의 기준값이자 비목분류 정확도 채점 기준. 비목 어휘집의 JSON 스키마 enum 10종.';
COMMENT ON COLUMN eval.golden_set.문항메타 IS
    '문항 작성 메타데이터 {출처, 함정, 작성메모, 계층별 근거 ...}. 평가 코드는 읽지 않는다. NULL = 미작성.';
COMMENT ON COLUMN eval.golden_chunks."조번호" IS
  '골든셋 정답근거의 원표기 그대로(제20조① · [붙임2] …). 청크의 조번호가 아니다 — '
  '비교할 때는 chunk_id 로 corpus.chunks 를 조인해 그쪽 조번호를 쓴다 (2026-09-01 결정).';
COMMENT ON COLUMN eval.golden_set."채점모드" IS
    '전체 | 가능오판만 | 판정만. 문항을 만들 때 기록한 채점 범위 표시다. 평가 코드는 이 값으로 분기하지 않는다.';
COMMENT ON COLUMN eval.golden_set."평가범위" IS
    '핵심 | 유효 | 범위밖(사유). 주관기관 대상 문항은 검색 필터(적용대상 IN (창업기업, 공통)) 때문에 '
    '구조적으로 검색되지 않으므로 범위밖으로 표시한다.';
\endif

-- 9. 옛 2026-11-01_corpus_ingest.sql 로 이미 만든 corpus.ingest_jobs ─────────────────────
DO $$
BEGIN
    IF to_regclass('corpus.ingest_jobs') IS NOT NULL AND EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'corpus' AND table_name = 'ingest_jobs' AND column_name = '분해전략') THEN
        ALTER TABLE corpus.ingest_jobs RENAME COLUMN "분해전략" TO "분할전략";
        ALTER TABLE corpus.ingest_jobs RENAME COLUMN "대체한_doc_id" TO "이전판_doc_id";
        UPDATE corpus.ingest_jobs SET "분할전략" = CASE "분할전략"
                WHEN 'jo_titled' THEN 'article_titled'
                WHEN 'jo_bare'   THEN 'article_untitled'
                WHEN 'jang'      THEN 'chapter'
                ELSE "분할전략" END
         WHERE "분할전략" IN ('jo_titled', 'jo_bare', 'jang');
    END IF;
END $$;

-- 10. 확인. 옛 이름·값이 하나라도 남아 있으면 예외를 내고 전부 롤백한다 ──────────────────────
DO $$
DECLARE r record; 남은것 text := '';
BEGIN
    FOR r IN
        SELECT 'chunks.retrieval_scope' AS 항목, count(*) AS n FROM corpus.chunks WHERE retrieval_scope IN ('진입점', '폐포전용')
        UNION ALL SELECT 'documents.retrieval_scope', count(*) FROM corpus.documents WHERE retrieval_scope IN ('진입점', '폐포전용')
        UNION ALL SELECT 'refs.해결상태', count(*) FROM corpus.refs WHERE "해결상태" = 'shifted'
        UNION ALL SELECT 'refs.해결근거', count(*) FROM corpus.refs
                   WHERE "해결근거" ~ '로 해소|코퍼스에 없는 규범|상위 규범으로|게이팅|규범명이 '
        UNION ALL SELECT 'decisions.검증코드', count(*) FROM tenant.decisions
                   WHERE "검증코드" && ARRAY['CITE_HANG_MISMATCH', 'PREMISE_ENUM', 'PRECEDENCE_FLIP']
        UNION ALL SELECT 'decisions.검증사유', count(*) FROM tenant.decisions, unnest("검증사유") s
                   WHERE s ~ 's맵|F 스키마|4-way|판독본|F축|^\([0-9]\) '
        UNION ALL SELECT 'decisions.처리단계', count(*) FROM tenant.decisions
                   WHERE "처리단계" ~ '1정규화|3검색|4조립|6검증|A금지적중|B스코어미달|C비목갈림|dry중단'
                      OR "실패단계" IN ('조립', '판정LLM')
        UNION ALL SELECT 'decisions.JSONB 키', count(*) FROM tenant.decisions
                   WHERE "지연ms" ?| ARRAY['비목확정', 'l3게이팅', '금지적중', '판정LLM', '조립', '전제해소']
                      OR "검색스냅샷" ?| ARRAY['s맵', '게이트값']
                      OR "모델"::text ~ '"(변형|추론content[^"]*)":'
                      OR "인용"::text ~ '"s번호":'
                      OR "참조관계"::text ~ '"(표기|보정)":'
        UNION ALL SELECT 'judge_cache', count(*) FROM tenant.judge_cache
        UNION ALL SELECT 'recheck_queue.종류', count(*) FROM corpus.recheck_queue WHERE "종류" ~ '^(A[245]|H4)'
        UNION ALL SELECT 'recheck_queue.상세', count(*) FROM corpus.recheck_queue
                   WHERE "상세" ?| ARRAY['국가룰', '엄격지점', '판들', '포인터오류', '재지정', '열위계층', '해소', '경유', '경유들']
                      OR "상세"::text ~ '[«»🔴🟢🟡]|ai-[0-9a-f]{2}|레인 [A-Z]|"L3승리"|"국가":'
        UNION ALL SELECT 'recheck_queue.처리자·요약', count(*) FROM corpus.recheck_queue
                   WHERE "처리자" ~ '^H-|ai-[0-9a-f]{2}' OR ("종류" = '엄격조항' AND "요약" ~ '[«»🔴🟢🟡]')
        UNION ALL SELECT 'org_programs.연도출처', count(*) FROM tenant.org_programs WHERE "연도출처" = '대상년도'
        UNION ALL SELECT 'l3_documents.파싱품질', count(*) FROM tenant.l3_documents WHERE "파싱품질" = '대기'
        UNION ALL SELECT 'incidents.종류', count(*) FROM tenant.incidents WHERE "종류" = '기타'
        UNION ALL SELECT 'evidence_sources.해당비목_분류', count(*) FROM corpus.evidence_sources
                   WHERE "해당비목_분류" && ARRAY['R&D계통', '정본']
        UNION ALL SELECT 'precedence_rules', count(*) FROM corpus.precedence_rules
                   WHERE "해석" LIKE '%이긴다%' OR "근거"::text ~ '"doc":' OR "검수자" ~ '오너'
        UNION ALL SELECT 'rules.검수자', count(*) FROM corpus.rules WHERE "검수자" ~ '오너|ai-[0-9a-f]{2}|G세션|· [SD][0-9]'
        UNION ALL SELECT 'check_items', count(*) FROM corpus.check_items
                   WHERE "검수자" ~ '오너|ai-[0-9a-f]{2}|G세션|· [SD][0-9]' OR "기한근거_상세" ~ '[«»🔴🟢🟡]|오너|정본'
        UNION ALL SELECT 'item_alias.출처', count(*) FROM corpus.item_alias
                   WHERE "출처" ~ '^(규정0906_|제거후보|되살림후보|보류_격리)'
        UNION ALL SELECT 'programs.비고', count(*) FROM corpus.programs WHERE "비고" ~ '계통|G[0-9] |정본|[«»🔴🟢🟡]'
        UNION ALL SELECT 'item_vocab.비고', count(*) FROM corpus.item_vocab
                   WHERE "비고" ~ '정본|오너|계통|표A|— G[0-9]|[«»🔴🟢🟡]'
    LOOP
        IF r.n > 0 THEN 남은것 := 남은것 || format(' %s=%s', r.항목, r.n); END IF;
    END LOOP;
    IF 남은것 <> '' THEN
        RAISE EXCEPTION '용어 정리: 옛 이름·값이 남았다:%', 남은것;
    END IF;
    RAISE NOTICE '용어 정리: corpus·tenant 확인 통과';
END $$;

\if :has_eval
DO $$
DECLARE r record; 남은것 text := '';
BEGIN
    FOR r IN
        SELECT 'golden_set.세트·채점' AS 항목, count(*) AS n FROM eval.golden_set
         WHERE "세트" IN ('본세트', '적대적', '보강', '공식') OR "채점모드" IN ('full', '공식', '치명오답만')
            OR "평가범위" = '주력' OR '치명오답률' = ANY ("채점대상")
        UNION ALL SELECT 'golden_set.정답근거', count(*) FROM eval.golden_set
                   WHERE "정답근거"::text ~ '"(doc|_대조방식)":|"verbatim"'
        UNION ALL SELECT 'golden_set.문항메타 키', count(*) FROM eval.golden_set
                   WHERE "문항메타"::text ~ '"(트랩|가능트랩|저작메모|층|층별|층간충돌|이유역할|인용신뢰|근거실재|세부위치|원비목|추출)":'
        UNION ALL SELECT 'golden_set.문항메타 값', count(*) FROM eval.golden_set
                   WHERE "문항메타"::text ~ 'verbatim|[«»🔴🟢🟡]|ai-[0-9a-f]{2}|W[1-4] |오너|트랩|검사축|저작|직접작성\(적대적\)'
                      OR "문항메타"->>'비목매핑' IN ('근사', '직접', '불가')
        UNION ALL SELECT 'golden_set.검수메모', count(*) FROM eval.golden_set
                   WHERE "검수메모" ~ '오너|ai-[0-9a-f]{2}|W[1-4]|레인 L|저작|스팟체크|기계검증|독립감사|의심유형|금지적중|워커|[«»🔴🟢🟡]|트랩|핵이|정본|결손|검사축'
        UNION ALL SELECT 'golden_set.해야할일', count(*) FROM eval.golden_set WHERE "해야할일"::text ~ '[«»🔴🟢🟡]'
        UNION ALL SELECT 'golden_chunks.실패사유', count(*) FROM eval.golden_chunks
                   WHERE "실패사유" ~ '[«»🔴🟢🟡]|통째로|결손|좌표'
        UNION ALL SELECT 'run_items.원출력 키', count(*) FROM eval.run_items
                   WHERE "원출력" ?| ARRAY['치명', '강등코드', '강등사유', '경로', 's맵', '게이트값', 'dangling', '갈래']
                      OR "원출력"->'지연ms' ?| ARRAY['비목확정', 'l3게이팅', '금지적중', '판정LLM', '조립', '전제해소']
                      OR "원출력"->'모델' ?| ARRAY['변형', '추론content있음', '추론content길이', '추론content길이_합']
                      OR ("원출력"->'인용목록')::text ~ '"s번호":'
        UNION ALL SELECT 'run_items.원출력 값', count(*) FROM eval.run_items
                   WHERE "원출력"->>'처리단계' ~ '1정규화|3검색|4조립|6검증|A금지적중|B스코어미달|C비목갈림|dry중단'
                      OR "원출력"->>'실패단계' IN ('조립', '판정LLM')
                      OR "원출력"->>'세트' IN ('본세트', '적대적', '보강', '공식')
                      OR ("원출력"->'골든_해야할일')::text ~ '[«»]'
                      OR ("원출력"->'검증사유')::text ~ 's맵|F 스키마|4-way|판독본|"\([0-9]\) '
                      OR ("원출력"->'검증코드')::text ~ 'CITE_HANG_MISMATCH|PREMISE_ENUM|PRECEDENCE_FLIP'
                      OR "예측" = '선택필요'
        UNION ALL SELECT 'runs.설정', count(*) FROM eval.runs
                   WHERE "설정"::text ~ '"(폐포사용|폐포깊이|게이트B임계|변형|꽂기|원본포획|프롬프트전문|부분집합_유보|SUDDOE_플래그|관측_최대토큰_①|관측_최대토큰_④|정답고정|지문|묶음|held-out41|미매핑적재_발화|B1블록_문항수)":'
                      OR "설정"::text ~ '"(본세트|적대적|보강|공식)"|[«»]|정본|4-way|치명오답'
        UNION ALL SELECT 'runs.지표', count(*) FROM eval.runs
                   WHERE "지표"::text ~ '"(강등코드[^"]*|경로빈도|치명[^"]*|분해|혼동|추론content길이_합|겹침평균_20|겹침률_%|묶음:[^"]*|세트:(본세트|적대적|보강|공식))":'
                      OR "지표"::text ~ 'CITE_HANG_MISMATCH|PREMISE_ENUM|PRECEDENCE_FLIP|1정규화|4조립|6검증|A금지적중|C비목갈림|dry중단'
        UNION ALL SELECT 'runs.비고·라벨', count(*) FROM eval.runs
                   WHERE "비고" ~ '[«»🔴🟢🟡]|D7 스윕|원문역추적' OR "라벨" ~ '[«»🔴🟢🟡]|GPU팟|폐포|역추적'
    LOOP
        IF r.n > 0 THEN 남은것 := 남은것 || format(' %s=%s', r.항목, r.n); END IF;
    END LOOP;
    IF 남은것 <> '' THEN
        RAISE EXCEPTION '용어 정리: eval 에 옛 이름·값이 남았다:%', 남은것;
    END IF;
    RAISE NOTICE '용어 정리: eval 확인 통과';
END $$;
\endif

COMMIT;

-- 트랜잭션 밖: 값을 많이 바꾼 테이블의 통계와 공간을 정리한다
VACUUM (ANALYZE) corpus.chunks;
VACUUM (ANALYZE) corpus.refs;
VACUUM (ANALYZE) corpus.documents;
VACUUM (ANALYZE) tenant.decisions;
\if :has_eval
VACUUM (ANALYZE) eval.run_items;
\endif
