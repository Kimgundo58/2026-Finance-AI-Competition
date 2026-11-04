-- 로그인 조회 함수 tenant.계정찾기(email) 와 가입 등록 함수 tenant.계정등록(email, org_id) 를 만든다.
-- tenant.accounts 를 RLS 밖에서 다루는 경로는 이 둘뿐이다.
-- 실행 시점: init. 운영 DB 에 다시 적용할 때는 단일 트랜잭션으로 실행한다:
--     psql "$DSN" --single-transaction -v ON_ERROR_STOP=1 -f db/init/11_accounts_login.sql
--   자동커밋으로 한 문장씩 실행하다 중간에 실패하면 PUBLIC 에 EXECUTE 가 남은 SECURITY DEFINER 함수만
--   남는 위험한 상태가 될 수 있다.
--
-- tenant.accounts 정책은 org_id = current_org() 하나뿐인데, 로그인 시점에는 org 를 아직 모른다.
-- 비특권 롤에서는 조회가 0행이 되고 로그인이 403 이 된다. 로컬 postgres 는 superuser 라 이 문제가 보이지 않는다.
-- 정책을 SELECT true 로 넓히면 이메일과 계정 목록 전체가 읽힌다. 대신 SECURITY DEFINER 함수 하나로
-- 노출 범위를 「이메일 1건 → 그 1건의 org」로 제한한다. RLS 정책은 바꾸지 않는다.


-- 사전 검사 1 — DDL 보다 먼저 실행한다. 파일 끝에 두면 자동커밋으로 실행했을 때 CREATE FUNCTION·GRANT 가
-- 먼저 커밋된 뒤 검사가 실패해, 함수만 운영에 남는 결과가 된다.
--
-- 확인하는 것: SECURITY DEFINER 는 소유자 권한으로 실행되는데, FORCE ROW LEVEL SECURITY 가 켜져 있으면
-- 소유자도 정책에 걸려 0행이 나온다. 증상이 「계정 목록이 비었다」(로그인 403)와 같다.
-- 로컬 소유자 postgres 는 superuser 라 FORCE 와 무관하게 우회하므로 로컬에서는 차이가 보이지 않는다.
--
-- 이 검사는 적용 시점에만 실행된다. 나중에 FORCE 를 켜면 여기서 잡지 못한다.
DO $$
DECLARE
    _force  boolean;
    _테이블소유자  name;
    _함수소유자  name;
BEGIN
    SELECT relforcerowsecurity, pg_get_userbyid(relowner)
      INTO _force, _테이블소유자
      FROM pg_class WHERE oid = 'tenant.accounts'::regclass;

    IF _force THEN
        RAISE EXCEPTION
            'tenant.accounts 에 FORCE ROW LEVEL SECURITY 가 켜져 있다. SECURITY DEFINER 함수도 '
            '정책에 걸려 로그인이 403 이 된다. FORCE 를 끄거나 로그인 조회 방식을 다시 설계해야 한다.';
    END IF;

    -- 재적용일 때만 확인한다. 첫 적용에는 함수가 없어 NULL 이다.
    -- to_regprocedure 를 쓴다. ::regprocedure 는 함수가 없으면 예외를 던진다.
    SELECT pg_get_userbyid(proowner) INTO _함수소유자
      FROM pg_proc WHERE oid = to_regprocedure('tenant.계정찾기(text)');

    IF _함수소유자 IS NOT NULL AND _함수소유자 IS DISTINCT FROM _테이블소유자 THEN
        RAISE EXCEPTION
            '함수 소유자(%)와 테이블 소유자(%)가 다르다. 정의자가 accounts 를 읽지 못하면 '
            '함수는 0행을 반환하고 증상은 「계정 목록이 비었다」와 같다.', _함수소유자, _테이블소유자;
    END IF;
END
$$;


-- 로그인 조회 — 이메일 1건 → 1행.
-- 설계 조건 (하나라도 빠지면 SELECT true 정책과 같아진다):
--   1. 인자는 이메일 완전일치 하나다. LIKE·패턴·목록 인자를 두지 않는다
--   2. LIMIT 1. email 은 UNIQUE 지만 함수 계약으로도 1행을 보장한다. 반환 개수로 전체가 노출되는 경로를 막는다
--   3. NULL·빈 문자열이면 0행이다. 여기에 COALESCE 를 넣으면 전체 행이 반환될 수 있다
--   4. SET search_path = pg_catalog, tenant. SECURITY DEFINER 에 이게 없으면 search_path 를 통한 객체 바꿔치기가 가능하다
--   5. 소유자는 테이블 소유자여야 한다. 소유자가 accounts 를 읽지 못하면 함수도 읽지 못한다
CREATE OR REPLACE FUNCTION tenant.계정찾기(_email text)
RETURNS TABLE (org_id uuid, account_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
    SELECT a.org_id, a.account_id
    FROM tenant.accounts AS a
    WHERE _email IS NOT NULL
      AND _email <> ''
      AND a.email = _email
    LIMIT 1
$$;

COMMENT ON FUNCTION tenant.계정찾기(text) IS
    '로그인 전용. 이메일 1건 → (org_id, account_id) 1행. RLS 를 우회하는 조회 통로이므로 '
    '인자·반환을 절대 늘리지 마라 — 늘리는 순간 accounts 전건 열람이 된다.';


-- 계정 등록 — 가입자 1명의 계정 1행을 만든다(POST /api/accounts). 2026-11-04_계정등록.sql 과 같은 내용이다.
-- 이 시점에는 소속 org 가 없어 앱 롤이 accounts 에 직접 쓸 수 없다. 정책을 넓히지 않고 이 함수로 제한한다.
-- 설계 조건:
--   1. email 은 서버가 서명을 검증한 토큰에서 꺼낸 값만 넘긴다. 소문자가 아니거나 비어 있으면 0행이다
--   2. 이미 있는 email 은 바꾸지 않는다. 이 함수로는 소속 기관을 옮길 수 없다
--   3. 데모 기관에는 등록하지 않는다. 없는 기관이면 0행이다
--   4. 반환은 그 email 의 1행뿐이다
CREATE OR REPLACE FUNCTION tenant.계정등록(_email text, _org_id uuid)
RETURNS TABLE (org_id uuid, account_id uuid)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
BEGIN
    IF _email IS NULL OR _email = '' OR _email <> lower(_email) THEN
        RETURN;
    END IF;

    INSERT INTO tenant.accounts (org_id, email)
    SELECT o.org_id, _email
      FROM tenant.orgs AS o
     WHERE o.org_id = _org_id
       AND o.기관명 NOT LIKE '[데모] %'
    ON CONFLICT (email) DO NOTHING;

    RETURN QUERY
    SELECT a.org_id, a.account_id
      FROM tenant.accounts AS a
     WHERE a.email = _email
     LIMIT 1;
END
$$;

COMMENT ON FUNCTION tenant.계정등록(text, uuid) IS
    '가입 전용. 이메일 1건을 기관 1곳에 등록하고 그 1행을 돌려준다. 이미 있는 이메일은 바꾸지 않는다. '
    'email 은 서명을 검증한 토큰에서 꺼낸 값만 넘긴다.';


-- 실행 권한. CREATE FUNCTION 은 기본으로 PUBLIC 에 EXECUTE 를 준다. SECURITY DEFINER 함수에 이 권한이
-- 남으면 누구나 계정 목록을 조회할 수 있으므로 PUBLIC 에서 먼저 회수한다. REVOKE 를 GRANT 뒤에 두면 방금 준 권한까지 회수된다.
REVOKE ALL ON FUNCTION tenant.계정찾기(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.계정등록(text, uuid) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        GRANT EXECUTE ON FUNCTION tenant.계정찾기(text) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.계정등록(text, uuid) TO suddoe_app;
    END IF;
END
$$;


-- 사전 검사 2 — 생성 후 확인. 첫 적용에서는 검사 1 이 함수 소유자를 확인하지 못한다(아직 없다).
-- 소유자가 다른 함수는 데이터를 노출하지 않고 0행만 반환하므로 여기서 실패시킨다.
DO $$
DECLARE
    _테이블소유자 name;
    _함수소유자 name;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO _테이블소유자
      FROM pg_class WHERE oid = 'tenant.accounts'::regclass;
    SELECT pg_get_userbyid(proowner) INTO _함수소유자
      FROM pg_proc WHERE oid = 'tenant.계정찾기(text)'::regprocedure;

    IF _함수소유자 IS DISTINCT FROM _테이블소유자 THEN
        RAISE EXCEPTION
            '함수 소유자(%)와 테이블 소유자(%)가 다르다. 이 함수는 0행만 반환한다. '
            'ALTER FUNCTION tenant.계정찾기(text) OWNER TO % 로 맞춘다.', _함수소유자, _테이블소유자, _테이블소유자;
    END IF;

    SELECT pg_get_userbyid(proowner) INTO _함수소유자
      FROM pg_proc WHERE oid = 'tenant.계정등록(text,uuid)'::regprocedure;
    IF _함수소유자 IS DISTINCT FROM _테이블소유자 THEN
        RAISE EXCEPTION '계정등록 함수 소유자(%)와 테이블 소유자(%)가 다르다.', _함수소유자, _테이블소유자;
    END IF;

    RAISE NOTICE '사전 검사 통과: 소유자=%, FORCE RLS=off', _테이블소유자;
END
$$;


-- 이 파일이 하지 않는 것.
-- accounts 정책은 그대로 둔다. 함수를 거치지 않는 직접 SELECT 는 비특권 롤에서 계속 0행이어야 한다.
--   적용한 뒤 직접 SELECT 도 따로 확인한다. 함수만 열린 것과 테이블 전체가 열린 것은 로그인 증상이 같다.
-- 데모 세션의 tenant.orgs INSERT 는 정책 변경 없이 처리한다. 앱이 uuid 를 먼저 만들어
--   GUC 에 설정하고 같은 값으로 INSERT 한다(server/routes_orgs.py).
