-- 로그인 조회 한 건만 RLS 밖으로 낸다.
-- tenant.accounts 정책은 org_id = current_org() 하나뿐인데, 로그인 시점엔 org 를 아직 모른다 —
-- 비특권 롤에서는 조회가 0행이 되고 그게 403 이다. 로컬 postgres 는 superuser 라 이 문제가 안 보인다.
-- 정책을 SELECT true 로 늘리면 이메일↔기관 명부가 통째로 읽힌다 — 대신 SECURITY DEFINER 함수 하나로
-- 노출면을 「이메일 1건 → 그 1건의 org」로 닫는다. 10_rls_guc.sql 은 건드리지 않는다.
--
-- 적용 방법: 단일 트랜잭션으로 흘려라 — 자동커밋으로 한 문장씩 흘리면 중간에 터졌을 때
-- PUBLIC 에 EXECUTE 가 남은 SECURITY DEFINER 함수만 남는 위험한 상태가 될 수 있다.
--     psql "$DSN" --single-transaction -v ON_ERROR_STOP=1 -f db/init/11_accounts_login.sql


-- 전제 검사 ① — DDL 보다 먼저 돈다. 파일 끝에 두면 자동커밋으로 흘렸을 때 CREATE FUNCTION·GRANT 가
-- 먼저 커밋되고 나서 검사가 터져 "함수는 이미 운영에 남는" 최악의 결과가 나온다.
--
-- 무엇을 보나: SECURITY DEFINER 는 소유자 권한으로 도는데, FORCE ROW LEVEL SECURITY 가 켜져 있으면
-- 소유자도 정책에 걸려 0행이 나온다 — 증상이 「명부가 비었다」(로그인 403)와 완전히 같다.
-- 로컬에서는 이 차이가 안 보인다 — 로컬 소유자 postgres 는 superuser 라 FORCE 와 무관하게 우회한다.
--
-- 이 검사는 적용 시점에만 돈다. 인스턴스를 다시 만들거나 나중에 FORCE 를 켜면 여기선 못 잡는다.
DO $$
DECLARE
    _force  boolean;
    _t소유  name;
    _f소유  name;
BEGIN
    SELECT relforcerowsecurity, pg_get_userbyid(relowner)
      INTO _force, _t소유
      FROM pg_class WHERE oid = 'tenant.accounts'::regclass;

    IF _force THEN
        RAISE EXCEPTION
            'tenant.accounts 에 FORCE ROW LEVEL SECURITY 가 켜져 있다 — SECURITY DEFINER 가 '
            '무력화되어 로그인이 403 이 된다(실측). 끄든가, 이 처방을 다시 설계해라.';
    END IF;

    -- 재적용일 때만 본다 — 첫 적용엔 함수가 없어 NULL 이다. to_regprocedure 를 쓴다 — ::regprocedure 는 없으면 던진다.
    SELECT pg_get_userbyid(proowner) INTO _f소유
      FROM pg_proc WHERE oid = to_regprocedure('tenant.계정찾기(text)');

    IF _f소유 IS NOT NULL AND _f소유 IS DISTINCT FROM _t소유 THEN
        RAISE EXCEPTION
            '함수 소유자(%)와 테이블 소유자(%)가 다르다 — 정의자가 accounts 를 못 읽으면 '
            '함수는 0행을 돌려주고 증상은 「명부가 비었다」와 같아진다.', _f소유, _t소유;
    END IF;
END
$$;


-- 로그인 조회 — 이메일 1건 → 1행.
-- 설계 조건 (하나라도 빠지면 이게 「SELECT true 정책」과 같아진다):
--   ① 인자는 이메일 완전일치 하나다. LIKE·패턴·목록 인자를 두지 않는다
--   ② LIMIT 1 — email 은 UNIQUE 지만 함수 계약으로도 1행을 못 박는다. 반환 개수로도 전건 노출 통로가 열린다
--   ③ NULL·빈 문자열이면 0행을 명시한다 — 나중에 COALESCE 를 끼워 넣는 순간 전건이 열리는 자리다
--   ④ SET search_path = pg_catalog, tenant — SECURITY DEFINER 에 이게 없으면 search_path 탈취가 열린다
--   ⑤ 소유자는 테이블 소유자여야 한다 — 소유자가 accounts 를 못 읽으면 함수도 못 읽는다
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
    '로그인 전용. 이메일 1건 → (org_id, account_id) 1행. RLS 를 우회하는 유일한 통로이므로 '
    '인자·반환을 절대 늘리지 마라 — 늘리는 순간 accounts 전건 열람이 된다.';


-- 실행 권한 — PUBLIC 에서 회수가 먼저다. CREATE FUNCTION 은 기본으로 PUBLIC 에 EXECUTE 를 주는데
-- SECURITY DEFINER 함수에 남아 있으면 누구나 명부를 캐낼 수 있다. REVOKE 를 GRANT 앞에 둔다 — 뒤에 두면 방금 준 권한을 도로 뺏는다.
REVOKE ALL ON FUNCTION tenant.계정찾기(text) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        GRANT EXECUTE ON FUNCTION tenant.계정찾기(text) TO suddoe_app;
    END IF;
END
$$;


-- 전제 검사 ② — 만든 뒤 확인. 첫 적용에서는 ①이 함수 소유자를 못 본다(아직 없다).
-- 소유자가 어긋난 함수는 새는 게 아니라 0행을 돌려줄 뿐이라 여기엔 「덜 위험한 쪽」만 남긴다.
DO $$
DECLARE
    _t소유 name;
    _f소유 name;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO _t소유
      FROM pg_class WHERE oid = 'tenant.accounts'::regclass;
    SELECT pg_get_userbyid(proowner) INTO _f소유
      FROM pg_proc WHERE oid = 'tenant.계정찾기(text)'::regprocedure;

    IF _f소유 IS DISTINCT FROM _t소유 THEN
        RAISE EXCEPTION
            '함수 소유자(%)와 테이블 소유자(%)가 다르다 — 이 함수는 0행만 돌려준다. '
            'ALTER FUNCTION tenant.계정찾기(text) OWNER TO % 로 맞춰라.', _f소유, _t소유, _t소유;
    END IF;

    RAISE NOTICE '전제 검사 통과 — 소유자=% · FORCE rls=off', _t소유;
END
$$;


-- 이 파일이 안 하는 것.
-- accounts 정책은 그대로 둔다 — 함수를 통하지 않는 직접 SELECT 는 비특권 롤에서 여전히 0행이어야 한다.
--   적용한 뒤 반드시 직접 SELECT 를 따로 재라 — 함수만 뚫린 것과 테이블이 통째로 뚫린 것은 증상이 같다.
-- 닭-달걀 ①(POST /api/demo/session 이 orgs 를 만든다)은 여기서 안 푼다 — 앱이 uuid 를 먼저 뽑아
--   GUC 에 세우고 같은 값으로 INSERT 하면 정책 변경 없이 통과한다 (scripts/archive/seed/seed_demo.py::_org잡기).
