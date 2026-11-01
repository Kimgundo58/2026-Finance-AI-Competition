-- 앱 롤 suddoe_app 의 권한과 DB 기본 search_path 를 설정한다. GRANT/ALTER 만 있고 전부 멱등이다.
-- 실행 시점: init (10a_rls_guc.sql 이 롤을 만든 직후) 과 운영 복원(deploy/ncp/restore.sh 3단계).
--   pg_dump --no-privileges 덤프를 복원하면 권한과 search_path 가 빠지므로 복원 후 이 파일을 다시 실행한다.
-- 롤 생성은 이 파일 밖이다(로컬은 10a_rls_guc.sql, 운영은 restore.sh 1단계).
-- psql 메타명령을 쓰지 않는다 — psycopg 로 실행해도 같은 결과가 나와야 한다.


-- 스키마 접근
-- extensions 는 pgvector 타입과 연산자(extensions.vector, <=>)가 있는 스키마다.
-- USAGE 가 없으면 벡터 검색이 "permission denied for schema extensions" 로 실패한다.
GRANT USAGE ON SCHEMA tenant, corpus, extensions TO suddoe_app;

-- 로그인용 SECURITY DEFINER 함수 실행 권한. 11_accounts_login.sql 과 같은 내용이다.
-- --no-privileges 덤프를 복원하면 함수 권한이 기본값(PUBLIC EXECUTE)으로 돌아가므로 여기서 다시 설정한다.
-- 로컬 init 에서는 이 파일이 11 보다 먼저 실행되어 함수가 아직 없으므로, 함수가 있을 때만 적용한다.
DO $$
BEGIN
    IF to_regprocedure('tenant.계정찾기(text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION tenant.계정찾기(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION tenant.계정찾기(text) TO suddoe_app;
    END IF;
END
$$;

-- eval(정답셋)은 운영에 없다. 없는 스키마에 REVOKE 를 실행하면 오류가 나므로 여기서는 eval 을 다루지 않는다.
-- 로컬의 eval 권한 회수는 10a_rls_guc.sql 이 한다.


-- tenant: 읽고 쓴다. 기관 간 격리는 권한이 아니라 RLS 정책이 담당한다.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA tenant TO suddoe_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA tenant TO suddoe_app;

-- corpus: 읽기만. 규정·청크·룰은 수집 파이프라인만 쓴다.
GRANT SELECT ON ALL TABLES IN SCHEMA corpus TO suddoe_app;


-- 앞으로 생길 객체.
-- 시퀀스 기본 권한이 빠지면 새 테이블에 INSERT 권한이 있어도 serial 시퀀스에서 권한 오류가 난다.
-- 이 오류는 RLS 위반과 같은 SQLSTATE 42501 이라 원인을 구분하기 어렵다.
-- ALTER DEFAULT PRIVILEGES 는 객체를 만드는 롤(grantor) 기준이다. 객체를 만드는 롤이 바뀌면 다시 실행한다.
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT USAGE, SELECT ON SEQUENCES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA corpus
    GRANT SELECT ON TABLES TO suddoe_app;


-- search_path. pg_dump 는 DB 수준 설정을 덤프하지 않으므로 복원 직후에는 기본값이다.
-- 설정하지 않으면 벡터 질의가 "operator does not exist: extensions.vector <=> extensions.vector" 로 실패한다.
-- DB 이름을 하드코딩하지 않고 current_database() 를 쓴다. eval 은 평가 스크립트(postgres 롤)가 쓰므로
-- DB 수준 경로에는 남기고, 앱 롤 경로에서는 뺀다(운영에는 eval 스키마가 없고 앱은 정답셋을 읽지 않는다).
DO $do$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET search_path = corpus, tenant, eval, extensions, public',
        current_database());
END
$do$;
ALTER ROLE suddoe_app SET search_path = corpus, tenant, extensions, public;


-- 검증. SQL 실행만으로 끝내지 않고 아래를 확인한다. deploy/ncp/restore.sh 가 같은 항목을 자동으로 검사한다.
--
--   1. 롤 속성       select rolsuper, rolbypassrls from pg_roles where rolname='suddoe_app';  -> f, f
--   2. 테이블 권한   corpus 의 모든 테이블에 SELECT, tenant 의 모든 테이블에 SELECT/INSERT/UPDATE/DELETE
--   3. 기본 권한     select defaclobjtype from pg_default_acl;  -> r 두 개와 S 한 개
--   4. eval 부재     select 1 from pg_namespace where nspname='eval';  -> 운영에서는 0행
--   5. 함수 권한     extensions USAGE, tenant.계정찾기(text) EXECUTE, PUBLIC EXECUTE 없음
--
-- 권한 조회가 모두 통과해도 쓰기가 된다는 증거는 아니다. 최종 확인은 suddoe_app 으로 직접 INSERT 하는 것이다:
--
--   6. 실제 INSERT (suddoe_app 으로 접속, 트랜잭션 안에서 실행하고 ROLLBACK)
--        begin;
--        select set_config('app.org_id', '<실재 org_id>', true);
--        insert into tenant.expense_plans (org_id, 질문원문) values ('<같은 org>', '권한 확인') returning plan_id;
--        rollback;
--
--      GUC 를 설정하지 않고 같은 INSERT 를 실행하면 42501 로 거부되어야 한다.
--      다른 org_id 로 INSERT 해도 42501 로 거부되어야 한다. 하나라도 다르면 격리가 깨진 것이다.
