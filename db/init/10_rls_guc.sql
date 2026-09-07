-- 로컬에도 «RLS 를 무는» 롤을 둔다.
--
-- 로컬 postgres 는 superuser(rolbypassrls=True) 라 RLS 를 우회한다 — 정책이 로컬에서 한 번도 물린 적이 없다.
-- 비특권 롤(Cloud SQL 앱 계정과 같은 성질)로 재현하면: GUC 없이 INSERT 는 거부, SELECT 는 에러 없이 0행,
-- GUC 를 세워야 내 org 쓰기가 통과하고 남의 org 는 거부된다. 실서버로 바꾸면 쓰기가 전부 죽고 읽기는 조용히 0행이 된다.
--
-- 개수 검산으로는 안 잡히는 자리다 — 행수가 일치해도 「누가 읽느냐」가 다르면 결과가 다르다.
-- 이 파일은 롤과 권한만 만든다. 정책(policy)은 건드리지 않는다 — 아래 「미결」은 정책을 고쳐야 풀린다.


-- 앱 롤 — 운영 suddoe_app 과 같은 성질을 로컬에 둔다. 비밀번호는 로컬 전용, 운영 계정은 Secret Manager 가 든다.
-- NOSUPERUSER · NOBYPASSRLS 를 명시한다 — 기본값에 기대면 나중에 슈퍼유저로 재생성돼도 아무도 모른다.
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

GRANT USAGE ON SCHEMA tenant, corpus TO suddoe_app;

-- tenant : 읽고 쓴다. 격리는 «권한» 이 아니라 RLS 정책이 문다.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA tenant TO suddoe_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA tenant TO suddoe_app;

-- corpus : 읽기만. 규정·청크는 앱이 못 고친다 (파이프라인만 쓴다).
GRANT SELECT ON ALL TABLES IN SCHEMA corpus TO suddoe_app;

-- eval 은 아무 권한도 주지 않는다. 정답셋이다 — 앱이 닿으면 그 자체가 정답 유출이다.
REVOKE ALL ON SCHEMA eval FROM suddoe_app;

-- 앞으로 생길 테이블에도 같은 권한이 붙게 한다 — 없으면 새 테이블이 조용히 「권한 없음」 으로 남아
-- RLS 가 아니라 GRANT 누락으로 죽는다. 증상이 같아서 원인 찾기에 시간이 든다.
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT USAGE, SELECT ON SEQUENCES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA corpus
    GRANT SELECT ON TABLES TO suddoe_app;


-- GUC 계약 — 앱이 트랜잭션마다 세운다. tenant.current_org() 는 request.jwt.claims → app.org_id 순으로 읽는다.
-- `SET LOCAL app.org_id = %s` 는 못 쓴다 — SET 문법에 바인딩 파라미터가 없다. 문자열을 이어붙이면 SQL 인젝션이다.
-- `SELECT set_config('app.org_id', %s, true)` 를 쓴다 — 세 번째 인자 true 가 트랜잭션 한정이다.
-- false(세션 GUC)로 하면 커넥션 재사용 시 다음 요청이 앞 요청의 org 를 그대로 본다 — 그게 TENANT_LEAK 이다.
-- 값의 출처는 검증된 주체(token·demo)뿐이어야 한다 — 자기신고 ?org_id= 를 쓰면 RLS 가 장식이 된다.


-- 미결 — 정책을 고쳐야 풀린다. 여기서는 고치지 않는다.
-- ① POST /api/demo/session 이 tenant.orgs 를 만드는 요청인데 정책이 org_id = current_org() 라 org 가 이미 있어야 통과한다. 닭-달걀이다.
-- ② 게스트(org_id IS NULL)는 GUC 를 안 세워도 막힌다 — NULL = NULL 은 참이 아니라 NULL 이라 RLS 를 통과 못 한다.
-- ③ tenant.unmapped_premise 는 RLS 가 켜져 있는데 정책이 하나도 없다 — 쓰기는 거부되고 읽기는 에러 없이 0행이 된다.
-- 세 건 모두 여기서는 고치지 않는다 — 정책 DDL 은 결정 후 별도 파일로 낸다. 주석 처리로 미리 넣지 않는다 — 지나가다 풀릴 수 있다.
