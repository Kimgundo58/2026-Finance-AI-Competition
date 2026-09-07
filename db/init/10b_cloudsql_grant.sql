-- Cloud SQL 전용. GRANT 만 친다 — 인스턴스를 새로 만들 때 이 파일을 쓴다. 전부 멱등이라 다시 쳐도 안전하다.
-- 10_rls_guc.sql 을 Cloud SQL 에 그대로 치지 마라 — CREATE ROLE ... NOSUPERUSER NOBYPASSRLS 가 안 돈다
-- (운영 postgres 는 superuser 가 아니다). 롤은 `gcloud sql users create` 로 밖에서 만든다.
-- Cloud SQL 이 만드는 롤은 결과적으로 NOSUPERUSER·NOBYPASSRLS 지만 SQL 로 명시할 수단이 없다 —
-- 아래 검증질의로만 확인한다. 롤을 다시 만들면 반드시 다시 확인해라.
-- 치는 법: gcloud sql connect 또는 인가 IP 에서 postgres 로 붙어 이 파일을 흘린다. psql 메타명령은 쓰지 않는다 — psycopg 로 흘리는 경로가 실제 경로다.


-- 스키마 접근
GRANT USAGE ON SCHEMA tenant, corpus TO suddoe_app;

-- eval 은 운영에 없다 — 정답셋이라 가면 안 된다. 없는 스키마에 REVOKE 를 치면 그 줄에서 죽으므로 REVOKE 도 쓰지 않는다.


-- tenant : 읽고 쓴다. 격리는 «권한» 이 아니라 RLS 정책이 문다.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA tenant TO suddoe_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA tenant TO suddoe_app;

-- corpus : 읽기만. 규정·청크는 앱이 못 고친다 (파이프라인만 쓴다).
GRANT SELECT ON ALL TABLES IN SCHEMA corpus TO suddoe_app;


-- 앞으로 생길 객체.
-- 시퀀스(S) 기본권한이 빠지면 새 테이블은 INSERT 권한을 상속받아도 그 serial 시퀀스만 「권한 없음」 이 된다 —
-- 증상이 RLS 위반과 똑같은 42501 이라 원인 찾기에 시간이 든다.
-- ALTER DEFAULT PRIVILEGES 는 부여자(grantor)별이다 — 운영 객체를 만드는 롤이 바뀌면 이 줄들은 조용히 무효가 된다.
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT USAGE, SELECT ON SEQUENCES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA corpus
    GRANT SELECT ON TABLES TO suddoe_app;


-- search_path — pg_dump 가 안 실어 오는 자리. 복원 직후에는 비어 있어 안 걸면 벡터 질의가 전부
-- `operator does not exist: extensions.vector <=> extensions.vector` 로 죽는다. eval 은 의도적으로 뺀다.
ALTER DATABASE suddoe SET search_path = corpus, tenant, extensions, public;
ALTER ROLE suddoe_app SET search_path = corpus, tenant, extensions, public;


-- 검증 — 「SQL 을 쳤다」로 끝내지 마라. 아래가 통과해야 개통이다.
--
--   ① 롤 성질            select rolsuper, rolbypassrls from pg_roles
--                          where rolname='suddoe_app';        → f · f
--   ② 권한 행수          select table_schema, count(*) from information_schema.role_table_grants
--                          where grantee='suddoe_app' group by 1;
--                                                             → corpus 16 · tenant 64 (=16×4)
--   ③ 시퀀스 기본권한    select defaclobjtype from pg_default_acl;
--                                                             → r 둘 «과 S 하나» 가 있어야 한다
--   ④ eval 부재          select 1 from pg_namespace where nspname='eval';  → 0행
--
-- ①~④ 가 다 통과해도 «쓰기가 된다» 는 증거가 아니다. 마지막 닻은 반드시 «비특권 롤로 직접 넣어 보기» 다:
--
--   ⑤ 실제 INSERT (suddoe_app 으로 붙어서)
--        select set_config('app.org_id', '<실재 org_id>', true);
--        insert into tenant.expense_plans (org_id, 질문원문) values ('<같은 org>','개통확인')
--          returning plan_id;                                 → plan_id 가 나와야 한다
--        delete from tenant.expense_plans where plan_id = <위 값>;   -- 반드시 지운다
--
--      GUC 를 «안» 세우고 같은 INSERT 를 치면 42501 이 나야 정상이다. 남의 org_id 로
--      쳐도 42501 이 나야 정상이다. 셋 중 하나라도 어긋나면 격리가 깨진 것이다.
