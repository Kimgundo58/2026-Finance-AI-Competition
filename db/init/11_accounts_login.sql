-- ════════════════════════════════════════════════════════════════════════════
-- 11_accounts_login.sql — 로그인 «한 건» 만 RLS 밖으로 낸다.   2026-09-03
--
-- 🔴 **왜 필요한가 — 명부를 채워도 로그인이 안 열린다.**
--    `tenant.accounts` 의 정책은 `org_id = current_org()` 하나뿐이다. 그런데
--    `auth._계정조회()` 는 «org 를 알아내려고» accounts 를 읽는다 — 읽는 시점에
--    `app.org_id` 가 아직 없다. 그래서 비특권 롤에서는 0행이고, 그건 곧 403 이다.
--    실측(심어 둔 계정 1건 기준):
--
--        postgres(superuser)  →  찾음
--        suddoe_app(비특권)   →  None  →  403
--
--    로컬 `postgres` 가 superuser 라 **이 자리는 로컬에서 절대 안 보인다.**
--    `10_rls_guc.sql` 의 GRANT 를 다 열어도 안 열린다 — GRANT 가 아니라 정책이다.
--    (`tenant.orgs` 엔 `orgs_read_all(SELECT true)` 가 있고 accounts 엔 없다.)
--
-- 🔴 **왜 정책을 안 늘리고 함수로 가는가.**
--    `accounts` 에 `FOR SELECT USING (true)` 를 붙이면 **이메일↔기관 명부가 통째로
--    읽힌다.** 격리 문서를 스스로 뒤집는 처방이다. 앱 롤에 `BYPASSRLS` 를 주는 건
--    이 프로젝트가 로컬에서 이미 세 번 데인 바로 그 함정이고.
--    → `SECURITY DEFINER` 함수 하나로 **노출면을 「이메일 1건 → 그 1건의 org」로**
--      닫는다. 전건 열람 통로를 안 만드는 게 이 파일의 전부다.
--
-- 🔴 이 파일은 **`10_rls_guc.sql` 을 건드리지 않는다.** (그쪽은 Cloud SQL 적용 중)
--
-- ── 적용 방법 ───────────────────────────────────────────────────────────────
--    🔴 **단일 트랜잭션으로 흘려라.** 자동커밋으로 한 문장씩 흘리면, 중간에서
--       터졌을 때 «앞부분만 적용된» 상태가 남는다. 특히 REVOKE/GRANT 가 갈리면
--       PUBLIC 에 EXECUTE 가 남은 SECURITY DEFINER 함수가 되는데, 그건 명부를
--       한 건씩 캐낼 수 있는 상태다. 전부 되거나 전부 안 되거나여야 한다.
--
--           psql "$DSN" --single-transaction -v ON_ERROR_STOP=1 -f db/init/11_accounts_login.sql
--
--    가장 위험한 검사(FORCE RLS)는 **DDL 보다 앞** 에 두어서, 자동커밋으로 흘려도
--    「거부했는데 함수는 남는」 모양이 안 나오게 했다. 그래도 위 방법이 맞다.
-- ════════════════════════════════════════════════════════════════════════════


-- ── 🔴 전제 검사 ①  «DDL 보다 먼저» 돈다 ───────────────────────────────────
--
-- 🔴 **왜 앞인가.** 이 검사가 파일 «끝» 에 있으면, 자동커밋으로 흘렸을 때
--    `CREATE FUNCTION` 과 `GRANT` 가 먼저 커밋되고 나서 검사가 터진다 —
--    「적용을 거부했다」인데 **함수는 이미 운영에 남는다.** 가장 나쁜 결과다.
--    앞에 두면 아직 아무것도 안 만든 상태라 실행 방식과 무관하게 안전하다.
--    (그래도 단일 트랜잭션으로 흘리는 게 맞다 — 아래 「적용 방법」 참조)
--
-- 무엇을 보나: `SECURITY DEFINER` 는 «소유자 권한» 으로 도는데, 소유자도 RLS 를 물게
-- 만드는 스위치가 있다 — `FORCE ROW LEVEL SECURITY`. 켜져 있으면 정의자가 정책에
-- 걸려 0행이 나오고, 증상은 **「명부가 비었다」와 완전히 같다** (로그인 403).
--
-- 🔴 실측이다. 소유자를 «비특권» 롤로 바꿔 놓고 비특권 롤로 함수를 부른 값:
--
--        FORCE rls = false  →  1행   ✅
--        FORCE rls = true   →  0행   🔴 무력화
--
-- 🔴 **로컬에서는 이 차이가 안 보인다.** 로컬 소유자 `postgres` 는 superuser 라
--    FORCE 와 무관하게 우회한다 — 소유자를 비특권으로 바꾸기 «전» 에 잰 값은
--    FORCE 를 켜도 1행이었다. 값을 한 번만 읽으면 못 가른다. 운영 Cloud SQL 의
--    `postgres` 는 superuser 가 아니다. `10_rls_guc.sql` 머리말과 같은 뿌리다.
--
-- ⚠️ 이 검사는 «적용 시점» 에만 돈다. 인스턴스를 다시 만들거나 누가 나중에 FORCE 를
--    켜면 여기선 못 잡는다 — 그때는 위 두 줄을 손으로 다시 재라.
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

    -- 재적용일 때만 본다. 첫 적용엔 함수가 없어서 NULL 이다
    -- (그래서 `::regprocedure` 가 아니라 `to_regprocedure` 다 — 앞자는 없으면 던진다)
    SELECT pg_get_userbyid(proowner) INTO _f소유
      FROM pg_proc WHERE oid = to_regprocedure('tenant.계정찾기(text)');

    IF _f소유 IS NOT NULL AND _f소유 IS DISTINCT FROM _t소유 THEN
        RAISE EXCEPTION
            '함수 소유자(%)와 테이블 소유자(%)가 다르다 — 정의자가 accounts 를 못 읽으면 '
            '함수는 0행을 돌려주고 증상은 「명부가 비었다」와 같아진다.', _f소유, _t소유;
    END IF;
END
$$;


-- ── 로그인 조회 — 이메일 1건 → 1행 ──────────────────────────────────────────
--
-- 🔴 설계 조건 (하나라도 빠지면 이게 「SELECT true 정책」과 같아진다)
--   ① 인자는 이메일 «완전일치» 하나다. LIKE·패턴·목록 인자를 두지 않는다
--   ② `LIMIT 1` — email 은 UNIQUE 지만 함수 계약으로도 1행을 못 박는다.
--      「전건이 나오는 통로」는 인자가 아니라 **반환 개수** 로도 열린다
--   ③ NULL·빈 문자열이면 0행. `email = NULL` 은 어차피 0행이지만 명시한다 —
--      나중에 누가 `COALESCE` 를 끼워 넣는 순간 전건이 열리는 자리다
--   ④ `SET search_path = pg_catalog, tenant` — 🔴 **SECURITY DEFINER 에 이게 없으면
--      search_path 탈취가 열린다.** 호출자가 자기 스키마에 가짜 `accounts` 를 만들어
--      두면 정의자 권한으로 그게 읽힌다
--   ⑤ 소유자는 «테이블 소유자» 여야 한다. SECURITY DEFINER 는 소유자 권한으로 도는데
--      소유자가 accounts 를 못 읽으면 아무 의미가 없다 (지금은 둘 다 `postgres`)
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


-- ── 실행 권한 — 🔴 PUBLIC 에서 «회수» 가 먼저다 ─────────────────────────────
--
-- `CREATE FUNCTION` 은 기본으로 PUBLIC 에 EXECUTE 를 준다. SECURITY DEFINER 함수에
-- 이게 남아 있으면 DB 에 붙을 수 있는 누구나 명부를 한 건씩 캐낼 수 있다.
-- 🔴 REVOKE 를 GRANT «앞» 에 둔다. 뒤에 두면 방금 준 권한을 도로 뺏는다.
REVOKE ALL ON FUNCTION tenant.계정찾기(text) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        GRANT EXECUTE ON FUNCTION tenant.계정찾기(text) TO suddoe_app;
    END IF;
END
$$;


-- ── 🔴 전제 검사 ②  «만든 뒤» 확인 ─────────────────────────────────────────
--
-- 첫 적용에서는 위 ①이 함수 소유자를 못 본다(아직 없다). 만든 «뒤» 한 번 더 본다.
-- ⚠️ 자동커밋으로 흘리면 여기서 터져도 함수는 이미 남는다 — 그래서 진짜 위험한
--    FORCE 검사는 ①에 두고, 여기엔 「덜 위험한 쪽」만 남긴다. 소유자가 어긋난 함수는
--    새는 게 아니라 0행을 돌려줄 뿐이다.
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


-- ── 🔴 이 파일이 «안» 하는 것 ───────────────────────────────────────────────
--
-- · `accounts` 정책은 그대로 둔다. 함수를 «통하지 않는» 직접 SELECT 는 비특권 롤에서
--   여전히 0행이어야 한다. 함수만 뚫린 것과 테이블이 통째로 뚫린 것은 증상이 같아서
--   («로그인이 된다») 눈으로는 못 가른다 — 적용한 뒤 반드시 직접 SELECT 를 따로 재라.
-- · 닭-달걀 ①(`POST /api/demo/session` 이 orgs 를 만든다)은 여기서 안 푼다.
--   앱이 uuid 를 먼저 뽑아 GUC 에 세우고 같은 값으로 INSERT 하면 정책 변경 없이
--   통과한다(실측) — `scripts/seed_demo.py::_org잡기` 가 그 길이다.
