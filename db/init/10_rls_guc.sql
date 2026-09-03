-- ════════════════════════════════════════════════════════════════════════════
-- 10_rls_guc.sql — 로컬에도 «RLS 를 무는» 롤을 둔다.   2026-09-03
--
-- 🔴 **왜 이 파일이 있나.**
--    `tenant.*` 는 전부 RLS 가 켜져 있고 정책은 `USING (org_id = tenant.current_org())`
--    다. 그런데 로컬 개발용 `postgres` 는 superuser 라 `rolbypassrls=True` —
--    **RLS 를 통째로 우회한다.** 그래서 로컬에서는 정책이 한 번도 물린 적이 없고,
--    Cloud SQL 의 앱 계정(`suddoe_app`, rolsuper=False rolbypassrls=False)으로
--    바꾸는 순간 처음으로 물렸다. 그 결과가 이것이다 (비특권 롤로 재현한 값):
--
--        GUC 없음 · INSERT tenant.expense_plans   🔴 InsufficientPrivilege
--        GUC 없음 · SELECT tenant.expense_plans   ✅ 통과하는데 «0행»
--        GUC 세움 · INSERT 내 org                 ✅ 통과
--        GUC 세움 · INSERT 남의 org               🔴 InsufficientPrivilege
--
--    즉 **실서버로 바꾸면 쓰기가 전부 죽고 읽기는 조용히 0행이 된다.**
--    `POST /api/plans` · 판정 저장 · L3 업로드 · `PATCH tasks` 가 전부 여기 걸린다.
--
-- 🔴 **개수 검산으로는 절대 안 잡히는 자리다.** 12개 테이블 행수가 전부 일치해도
--    「누가 읽느냐」가 다르면 결과가 다르다. 닻이 달라야 잡힌다 — 그 닻이 «롤» 이다.
--
-- 🔴 이 파일은 **롤과 권한만** 만든다. 정책(policy)은 건드리지 않는다.
--    정책을 고쳐야 푸는 문제가 둘 남아 있는데(아래 「미결」) 그건 오너 결정이다.
-- ════════════════════════════════════════════════════════════════════════════


-- ── 앱 롤 — 운영의 `suddoe_app` 과 «같은 성질» 을 로컬에 둔다 ────────────────
--
-- 비밀번호는 로컬 전용이다. 운영 계정은 Secret Manager 가 든다 (여기서 안 만든다).
-- 🔴 NOSUPERUSER · NOBYPASSRLS 를 «명시» 한다. 기본값에 기대면 나중에 누가
--    이 롤을 다시 만들 때 슈퍼유저로 만들어도 아무도 모른다.
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

-- corpus : 읽기만. 🔴 규정·청크는 앱이 못 고친다 (파이프라인만 쓴다).
GRANT SELECT ON ALL TABLES IN SCHEMA corpus TO suddoe_app;

-- 🔴 `eval` 은 «아무 권한도 안 준다.** 정답셋이다 — 앱이 닿으면 그 자체가 정답 유출이다
--    (CLAUDE.md 「정답셋은 인덱스 투입 금지」와 같은 뿌리, 다른 방어선).
REVOKE ALL ON SCHEMA eval FROM suddoe_app;

-- 앞으로 생길 테이블에도 같은 권한이 붙게 한다. 🔴 이게 없으면 새 테이블 하나가
-- 조용히 «권한 없음» 으로 남아, RLS 가 아니라 GRANT 누락으로 죽는다 — 증상이 같아서
-- 원인을 찾는 데 시간을 버린다.
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA tenant
    GRANT USAGE, SELECT ON SEQUENCES TO suddoe_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA corpus
    GRANT SELECT ON TABLES TO suddoe_app;


-- ── GUC 계약 — 앱이 트랜잭션마다 세운다 ──────────────────────────────────────
--
-- `tenant.current_org()` 는 `request.jwt.claims` → `app.org_id` 순으로 읽는다.
-- 로컬·Cloud Run 은 후자다. 앱 쪽 배관은 `server/_common.py::_org_세우기()`.
--
-- 🔴 **`SET LOCAL app.org_id = %s` 는 못 쓴다.** SET 문법에 바인딩 파라미터가 없어서
--    SyntaxError 가 난다(실측). 문자열로 이어붙이면 SQL 인젝션이다.
--    → `SELECT set_config('app.org_id', %s, true)` 를 쓴다. 세 번째 인자 `true` 가
--      «트랜잭션 한정» 이다. `false`(세션 GUC)로 하면 커넥션 재사용 시 **다음 요청이
--      앞 요청의 org 를 그대로 본다** — 측정으로 확인했다. 그날이 TENANT_LEAK 이다.
--
-- 🔴 값의 «출처» 는 검증된 주체(`auth.주체.검증됨`, 출처 token·demo)뿐이다.
--    자기신고 `?org_id=` 를 넣으면 RLS 가 장식이 된다 — 감사에는 「RLS 켜져 있음」으로
--    통과하는데 클라이언트가 말한 값을 그대로 도장 찍는 꼴이다.


-- ── 🔴 미결 — 정책을 고쳐야 풀린다. 여기서 «안 고친다» (오너 결정) ───────────
--
-- ① `POST /api/demo/session` 이 `tenant.orgs` 를 «만드는» 요청인데, 정책이
--    `org_id = current_org()` 라 org 가 이미 있어야 통과한다. 닭-달걀이다.
--    지금 상태: INSERT tenant.orgs → InsufficientPrivilege (실측).
--
-- ② 게스트(`org_id IS NULL`)는 GUC 를 안 세워도 막힌다. `NULL = NULL` 은 참이 아니라
--    NULL 이고, RLS 는 참이 아닌 것을 통과시키지 않는다. 즉 「게스트 행」이라는 개념이
--    이 정책에서는 성립하지 않는다.
--
-- 두 건 모두 «안» 을 보고서에 적었다. 정책 DDL 은 결정 후 11번 파일로 따로 낸다.
-- 🔴 여기에 미리 써 두고 주석 처리하지 않는다 — 주석 처리된 DDL 은 누가 지나가다
--    풀어 버린다. 결정 안 난 것은 파일에 «없는» 게 맞다.
