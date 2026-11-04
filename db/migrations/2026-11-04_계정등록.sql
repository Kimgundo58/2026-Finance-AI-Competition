-- 필수. 가입한 계정을 주관기관에 등록하는 함수 tenant.계정등록(email, org_id) 를 만든다(POST /api/accounts).
-- 함수를 하나 더할 뿐이고 기존 행·정책은 바꾸지 않는다. api 를 멈추지 않아도 된다. 멱등이다.
-- 새 이미지보다 먼저 적용한다. 함수가 없으면 등록 요청이 503 이 된다.
--
-- cd /opt/suddoe/deploy/ncp
-- docker compose exec -T db psql -U postgres -d suddoe --single-transaction -v ON_ERROR_STOP=1 -f - \
--   < ../../db/migrations/2026-11-04_계정등록.sql
--
-- db/init/11_accounts_login.sql 과 같은 내용이다.


-- 사전 검사. FORCE RLS 가 켜져 있으면 소유자도 정책에 걸려 함수가 accounts 에 쓰지 못한다.
DO $$
BEGIN
    IF (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'tenant.accounts'::regclass)
       OR (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'tenant.orgs'::regclass) THEN
        RAISE EXCEPTION 'tenant.accounts 또는 tenant.orgs 에 FORCE ROW LEVEL SECURITY 가 켜져 있다';
    END IF;
END
$$;


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

COMMENT ON FUNCTION tenant.계정찾기(text) IS
    '로그인 전용. 이메일 1건 → (org_id, account_id) 1행. RLS 를 우회하는 조회 통로이므로 '
    '인자·반환을 절대 늘리지 마라 — 늘리는 순간 accounts 전건 열람이 된다.';

REVOKE ALL ON FUNCTION tenant.계정등록(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenant.계정등록(text, uuid) TO suddoe_app;


-- 사후 검사. 소유자가 테이블 소유자와 다르면 함수가 accounts 에 쓰지 못한다.
DO $$
BEGIN
    IF (SELECT proowner FROM pg_proc WHERE oid = 'tenant.계정등록(text,uuid)'::regprocedure)
       IS DISTINCT FROM (SELECT relowner FROM pg_class WHERE oid = 'tenant.accounts'::regclass) THEN
        RAISE EXCEPTION '함수 소유자가 tenant.accounts 소유자와 다르다';
    END IF;
END
$$;
