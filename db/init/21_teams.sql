-- 팀: 같은 주관기관의 여러 계정이 기업 프로필·지출계획·판정·일정·첨부·판정 캐시를 함께 쓴다.
-- 데이터 표와 RLS 는 고치지 않는다. 팀 데이터는 「대표 계정」(teams.대표_account_id)의 account_id 로 저장되고,
-- 로그인할 때 tenant.로그인주체() 가 구성원의 데이터 계정을 대표 계정으로 돌려준다(server/auth.py).
-- 들어오기·나가기는 데이터를 옮기지 않는다. 개인 데이터는 그대로 남아 팀을 나가면 다시 보인다.
-- 이 파일은 2026-11-06_팀.sql 과 같다.

CREATE TABLE IF NOT EXISTS tenant.teams (
    team_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    팀이름            TEXT NOT NULL CHECK (char_length(팀이름) BETWEEN 1 AND 40),
    사업명            TEXT NOT NULL CHECK (char_length(사업명) BETWEEN 1 AND 100),
    대표_account_id   UUID NOT NULL UNIQUE REFERENCES tenant.accounts(account_id) ON DELETE CASCADE,
    초대코드_해시      TEXT UNIQUE,
    초대코드_만료      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE tenant.teams IS '팀. 앱 롤은 직접 읽지 못하고 아래 SECURITY DEFINER 함수로만 다룬다.';
COMMENT ON COLUMN tenant.teams.대표_account_id IS '대표 계정: 팀 데이터가 저장되는 계정.';

ALTER TABLE tenant.accounts ADD COLUMN IF NOT EXISTS team_id UUID REFERENCES tenant.teams(team_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_accounts_team ON tenant.accounts (team_id) WHERE team_id IS NOT NULL;

-- 직접 만든 일정을 누가 만들었는지. 팀에서 다른 구성원에게 이메일 앞부분으로 보인다.
ALTER TABLE tenant.plan_tasks ADD COLUMN IF NOT EXISTS 작성_account_id UUID
    REFERENCES tenant.accounts(account_id) ON DELETE SET NULL;

-- 정책 없이 RLS 를 켜면 앱 롤의 직접 접근은 0행이다. 소유자 함수만 다룬다.
ALTER TABLE tenant.teams ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON tenant.teams FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        REVOKE ALL ON tenant.teams FROM suddoe_app;
    END IF;
END
$$;


-- 로그인 조회. tenant.계정찾기 와 같은 조건(이메일 완전일치 1건)이고 데이터 계정을 함께 돌려준다.
CREATE OR REPLACE FUNCTION tenant.로그인주체(_email text)
RETURNS TABLE (org_id uuid, 본인_account_id uuid, account_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
    SELECT a.org_id, a.account_id, COALESCE(t.대표_account_id, a.account_id)
      FROM tenant.accounts AS a
      LEFT JOIN tenant.teams AS t ON t.team_id = a.team_id
     WHERE _email IS NOT NULL AND _email <> '' AND a.email = _email
     LIMIT 1
$$;

-- 이 계정의 팀. 구성원 1명당 1행이고 팀이 없으면 0행이다.
CREATE OR REPLACE FUNCTION tenant.팀조회(_본인 uuid)
RETURNS TABLE (팀이름 text, 사업명 text, 대표_account_id uuid, 초대코드_만료 timestamptz, account_id uuid, email text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
    SELECT t.팀이름, t.사업명, t.대표_account_id, t.초대코드_만료, m.account_id, m.email
      FROM tenant.accounts AS a
      JOIN tenant.teams AS t ON t.team_id = a.team_id
      JOIN tenant.accounts AS m ON m.team_id = t.team_id
     WHERE a.account_id = _본인
     ORDER BY m.account_id = t.대표_account_id DESC, m.created_at
$$;

-- 아래 함수는 모두 결과 문자열을 돌려주고 서버가 안내 문장으로 바꾼다. 잠금 순서는 팀 → 계정이다.
CREATE OR REPLACE FUNCTION tenant.팀만들기(_본인 uuid, _팀이름 text, _사업명 text, _해시 text, _만료 timestamptz)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
DECLARE
    _계정 tenant.accounts;
    _팀 uuid;
BEGIN
    SELECT * INTO _계정 FROM tenant.accounts WHERE account_id = _본인 FOR UPDATE;
    IF NOT FOUND THEN RETURN '계정없음'; END IF;
    IF _계정.team_id IS NOT NULL THEN RETURN '이미팀있음'; END IF;
    INSERT INTO tenant.teams (org_id, 팀이름, 사업명, 대표_account_id, 초대코드_해시, 초대코드_만료)
    VALUES (_계정.org_id, _팀이름, _사업명, _본인, _해시, _만료)
    RETURNING team_id INTO _팀;
    UPDATE tenant.accounts SET team_id = _팀 WHERE account_id = _본인;
    RETURN '성공';
END
$$;

CREATE OR REPLACE FUNCTION tenant.팀코드재발급(_본인 uuid, _해시 text, _만료 timestamptz)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
BEGIN
    UPDATE tenant.teams SET 초대코드_해시 = _해시, 초대코드_만료 = _만료 WHERE 대표_account_id = _본인;
    IF NOT FOUND THEN RETURN '권한없음'; END IF;
    RETURN '성공';
END
$$;

CREATE OR REPLACE FUNCTION tenant.팀가입(_본인 uuid, _해시 text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
DECLARE
    _팀 tenant.teams;
    _계정 tenant.accounts;
BEGIN
    SELECT * INTO _팀 FROM tenant.teams WHERE 초대코드_해시 = _해시 AND 초대코드_만료 > now() FOR UPDATE;
    IF NOT FOUND THEN RETURN '코드오류'; END IF;
    SELECT * INTO _계정 FROM tenant.accounts WHERE account_id = _본인 FOR UPDATE;
    IF NOT FOUND THEN RETURN '계정없음'; END IF;
    IF _계정.team_id IS NOT NULL THEN RETURN '이미팀있음'; END IF;
    IF _계정.org_id <> _팀.org_id THEN RETURN '다른기관'; END IF;
    UPDATE tenant.accounts SET team_id = _팀.team_id WHERE account_id = _본인;
    RETURN '성공';
END
$$;

-- 새 가입자가 초대 코드로 등록한다. 기관은 팀의 기관이다. 이미 있는 이메일은 건드리지 않는다.
CREATE OR REPLACE FUNCTION tenant.팀가입_신규(_email text, _해시 text)
RETURNS TABLE (결과 text, org_id uuid, 팀이름 text, 사업명 text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
DECLARE
    _팀 tenant.teams;
BEGIN
    IF _email IS NULL OR _email = '' OR _email <> lower(_email) THEN
        RETURN QUERY SELECT '코드오류'::text, NULL::uuid, NULL::text, NULL::text;
        RETURN;
    END IF;
    SELECT * INTO _팀 FROM tenant.teams AS t WHERE t.초대코드_해시 = _해시 AND t.초대코드_만료 > now() FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT '코드오류'::text, NULL::uuid, NULL::text, NULL::text;
        RETURN;
    END IF;
    INSERT INTO tenant.accounts (org_id, email, team_id) VALUES (_팀.org_id, _email, _팀.team_id)
    ON CONFLICT (email) DO NOTHING;
    IF NOT FOUND THEN
        RETURN QUERY SELECT '이미등록'::text, NULL::uuid, NULL::text, NULL::text;
        RETURN;
    END IF;
    RETURN QUERY SELECT '성공'::text, _팀.org_id, _팀.팀이름, _팀.사업명;
END
$$;

-- 구성원은 그냥 나간다. 대표는 다른 구성원이 있으면 나갈 수 없고, 혼자면 팀이 없어진다. 데이터는 대표 계정에 남는다.
CREATE OR REPLACE FUNCTION tenant.팀나가기(_본인 uuid)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
DECLARE
    _팀아이디 uuid;
    _팀 tenant.teams;
BEGIN
    SELECT team_id INTO _팀아이디 FROM tenant.accounts WHERE account_id = _본인;
    IF _팀아이디 IS NULL THEN RETURN '팀없음'; END IF;
    SELECT * INTO _팀 FROM tenant.teams WHERE team_id = _팀아이디 FOR UPDATE;
    IF NOT FOUND THEN RETURN '팀없음'; END IF;
    IF _팀.대표_account_id <> _본인 THEN
        UPDATE tenant.accounts SET team_id = NULL WHERE account_id = _본인 AND team_id = _팀아이디;
        RETURN '성공';
    END IF;
    IF EXISTS (SELECT 1 FROM tenant.accounts WHERE team_id = _팀아이디 AND account_id <> _본인) THEN
        RETURN '구성원있음';
    END IF;
    DELETE FROM tenant.teams WHERE team_id = _팀아이디;
    RETURN '성공';
END
$$;

CREATE OR REPLACE FUNCTION tenant.팀원내보내기(_본인 uuid, _대상 uuid)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
DECLARE
    _팀 tenant.teams;
BEGIN
    SELECT * INTO _팀 FROM tenant.teams WHERE 대표_account_id = _본인 FOR UPDATE;
    IF NOT FOUND THEN RETURN '권한없음'; END IF;
    IF _대상 = _본인 THEN RETURN '대표'; END IF;
    UPDATE tenant.accounts SET team_id = NULL WHERE account_id = _대상 AND team_id = _팀.team_id;
    IF NOT FOUND THEN RETURN '없는구성원'; END IF;
    -- 내보낸 사람이 아직 가진 코드로 다시 들어오지 못하게 지금 코드를 막는다
    UPDATE tenant.teams SET 초대코드_만료 = now() WHERE team_id = _팀.team_id;
    RETURN '성공';
END
$$;

-- CREATE FUNCTION 은 PUBLIC 에 EXECUTE 를 준다. SECURITY DEFINER 라 먼저 회수한다.
REVOKE ALL ON FUNCTION tenant.로그인주체(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.팀조회(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.팀만들기(uuid, text, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.팀코드재발급(uuid, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.팀가입(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.팀가입_신규(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.팀나가기(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant.팀원내보내기(uuid, uuid) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        GRANT EXECUTE ON FUNCTION tenant.로그인주체(text) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.팀조회(uuid) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.팀만들기(uuid, text, text, text, timestamptz) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.팀코드재발급(uuid, text, timestamptz) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.팀가입(uuid, text) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.팀가입_신규(text, text) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.팀나가기(uuid) TO suddoe_app;
        GRANT EXECUTE ON FUNCTION tenant.팀원내보내기(uuid, uuid) TO suddoe_app;
    END IF;
END
$$;
