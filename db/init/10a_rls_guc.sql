-- 로컬 DB 에 RLS 정책이 적용되는 앱 롤 suddoe_app 을 만든다.
-- 실행 시점: init. 권한 부여는 바로 다음 파일 10b_app_grants.sql 이 한다(운영 복원과 같은 파일).
-- 파일 이름을 10a 로 둔 이유: init 은 파일명을 로케일 순서로 정렬하므로 10_ 는 10b 뒤에 실행될 수 있다.
--
-- 로컬 postgres 는 superuser(rolbypassrls=true)라 RLS 를 우회한다. 정책은 비특권 롤에서만 적용되므로
-- 운영과 같은 성질의 롤로 재현해야 한다: GUC 없이 INSERT 하면 거부, SELECT 는 오류 없이 0행,
-- GUC 를 설정하면 자기 org 쓰기만 통과하고 다른 org 는 거부된다.
-- 행 수가 같아도 어느 롤로 읽느냐에 따라 결과가 다르므로 행 수 비교로는 이 차이를 잡지 못한다.
-- 이 파일은 롤만 만든다. 정책(policy)은 건드리지 않는다.


-- 앱 롤. 비밀번호는 로컬 전용이다. 운영 비밀번호는 deploy/ncp/.env 의 SUDDOE_APP_DB_PASSWORD 다.
-- NOSUPERUSER, NOBYPASSRLS 를 명시한다. 기본값에 기대면 나중에 다른 속성으로 재생성돼도 드러나지 않는다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        CREATE ROLE suddoe_app LOGIN PASSWORD 'devpw' NOSUPERUSER NOBYPASSRLS
                                     NOCREATEDB NOCREATEROLE;
    ELSE
        ALTER ROLE suddoe_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

-- eval 은 아무 권한도 주지 않는다. 골든셋이라 앱이 읽을 수 있으면 그 자체가 정답 유출이다.
REVOKE ALL ON SCHEMA eval FROM suddoe_app;


-- GUC 규약. 앱은 트랜잭션마다 app.org_id 를 설정하고, tenant.current_org() 는
-- request.jwt.claims -> app.org_id 순으로 읽는다.
-- `SET LOCAL app.org_id = %s` 는 쓸 수 없다. SET 문은 바인딩 파라미터를 받지 않고, 문자열을 이어 붙이면 SQL 인젝션이 된다.
-- `SELECT set_config('app.org_id', %s, true)` 를 쓴다. 세 번째 인자 true 가 트랜잭션 범위를 뜻한다.
-- false(세션 범위)로 두면 커넥션을 재사용할 때 다음 요청이 앞 요청의 org 를 그대로 본다(TENANT_LEAK).
-- 값은 검증된 주체(토큰, 데모 세션)에서만 가져온다. 클라이언트가 보낸 ?org_id= 를 쓰면 RLS 가 무의미해진다.


-- 정책으로 해결되지 않은 항목. 바꾸려면 정책 DDL 을 db/migrations/ 에 별도 파일로 낸다.
-- 1. 게스트(org_id IS NULL) 행은 GUC 와 무관하게 RLS 를 통과하지 못한다. NULL = NULL 은 참이 아니라 NULL 이다.
--    그래서 suddoe_app 으로는 게스트 판정을 tenant.decisions 에 남길 수 없다.
-- 2. tenant.unmapped_premise 는 RLS 가 켜져 있고 정책이 없다. suddoe_app 의 쓰기는 거부되고 읽기는 0행이다.
--    앱은 이 테이블에 쓰지 않는다(PREMISE_UNMAPPED 코드는 decisions.검증코드 에만 남는다).
-- 데모 세션의 tenant.orgs INSERT 는 앱이 uuid 를 먼저 만들어 GUC 에 설정한 뒤 같은 값으로 INSERT 해 통과한다.
