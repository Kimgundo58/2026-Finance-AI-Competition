#!/usr/bin/env bash
# DB 복원 및 검증 스크립트
#   1) 빈 DB 에 앱 롤(suddoe_app) 생성
#   2) pg_dump(-Fc) 덤프 복원
#   3) db/init/10b_cloudsql_grant.sql 로 권한·search_path 설정
#   4) 권한 검증(①~④, ⑥), suddoe_app 실제 쿼리(⑦), 테이블별 행 수 비교(선택), RLS 격리 테스트(⑤)
#
# 사용법 (deploy/ncp 디렉터리에서):
#   ./restore.sh <suddoe.dump> [source_rowcounts.txt]
#
# 검증 항목은 db/init/10b_cloudsql_grant.sql 하단 주석과 같다. 하나라도 실패하면 exit 1 로 종료한다.
set -euo pipefail
cd "$(dirname "$0")"

DUMP=${1:?usage: ./restore.sh <suddoe.dump> [source_rowcounts.txt]}
COUNTS=${2:-}
[ -f "$DUMP" ] || { echo "dump file not found: $DUMP" >&2; exit 1; }

set -a; . ./.env; set +a
: "${SUDDOE_APP_DB_PASSWORD:?SUDDOE_APP_DB_PASSWORD is not set in .env}"

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok()   { echo "[ OK ] $*"; }

# postgres(superuser)로 컨테이너 내부 소켓 접속
psql_admin() {
    docker compose exec -T -e PGCLIENTENCODING=UTF8 db \
        psql -U postgres -d suddoe -X -q -v ON_ERROR_STOP=1 "$@"
}
# suddoe_app 으로 TCP + 비밀번호 접속 (API 와 같은 인증 경로). 에러에 SQLSTATE 가 나오도록 VERBOSITY=verbose
psql_app() {
    docker compose exec -T -e PGCLIENTENCODING=UTF8 -e PGPASSWORD="$SUDDOE_APP_DB_PASSWORD" db \
        psql -h 127.0.0.1 -U suddoe_app -d suddoe -X -q -v ON_ERROR_STOP=1 -v VERBOSITY=verbose "$@"
}

# ── 0. DB 컨테이너 기동 ──────────────────────────────────────────────
docker compose up -d db
for _ in $(seq 60); do
    docker compose exec -T db pg_isready -U postgres -d suddoe >/dev/null 2>&1 && break
    sleep 2
done
docker compose exec -T db pg_isready -U postgres -d suddoe >/dev/null || fail "DB is not ready"

# 빈 DB 에서만 실행한다. 기존 데이터를 덮어쓰지 않는다.
n=$(psql_admin -tA <<'SQL'
select count(*) from pg_namespace where nspname in ('corpus', 'tenant');
SQL
)
[ "$n" = "0" ] || fail "corpus/tenant 스키마가 이미 있다. 다시 복원하려면 'docker compose down -v' 로 볼륨을 먼저 삭제한다"

# ── 1. 앱 롤 생성 ────────────────────────────────────────────────────
# RLS 정책은 PUBLIC 대상이라 롤 생성 순서와 무관하지만, 3단계 GRANT 전에는 있어야 한다.
psql_admin -v pw="$SUDDOE_APP_DB_PASSWORD" <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        CREATE ROLE suddoe_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;
ALTER ROLE suddoe_app WITH LOGIN PASSWORD :'pw' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
SQL
ok "role suddoe_app"

# ── 2. 덤프 복원 ─────────────────────────────────────────────────────
echo "restoring dump..."
docker compose exec -T db pg_restore -U postgres -d suddoe --no-owner --no-privileges --exit-on-error < "$DUMP"
psql_admin <<'SQL'
ANALYZE;
SQL
ok "pg_restore + ANALYZE"

# ── 3. 권한·search_path ──────────────────────────────────────────────
# Cloud SQL 용으로 작성된 파일이지만 GRANT/ALTER 문만 있어 self-hosted PostgreSQL 에도 그대로 적용된다.
psql_admin < ../../db/init/10b_cloudsql_grant.sql
ok "10b_cloudsql_grant.sql"

# ── 4. 권한 검증 ①~④ ─────────────────────────────────────────────────
r=$(psql_admin -tA <<'SQL'
select rolsuper::text || '/' || rolbypassrls::text from pg_roles where rolname = 'suddoe_app';
SQL
)
[ "$r" = "false/false" ] && ok "① suddoe_app: NOSUPERUSER, NOBYPASSRLS" || fail "① suddoe_app rolsuper/rolbypassrls = $r"

# information_schema 에는 materialized view 가 나오지 않으므로 pg_class 기준으로 확인한다.
missing=$(psql_admin -tA <<'SQL'
select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where (n.nspname = 'corpus' and c.relkind in ('r','p','v','m')
        and not has_table_privilege('suddoe_app', c.oid, 'SELECT'))
    or (n.nspname = 'tenant' and c.relkind in ('r','p')
        and not (has_table_privilege('suddoe_app', c.oid, 'SELECT')
             and has_table_privilege('suddoe_app', c.oid, 'INSERT')
             and has_table_privilege('suddoe_app', c.oid, 'UPDATE')
             and has_table_privilege('suddoe_app', c.oid, 'DELETE')));
SQL
)
[ "$missing" = "0" ] && ok "② table privileges (corpus: SELECT, tenant: SELECT/INSERT/UPDATE/DELETE)" \
                     || fail "② 권한이 누락된 테이블 $missing 개"

acl=$(psql_admin -tA <<'SQL'
select string_agg(defaclobjtype::text, '' order by defaclobjtype::text) from pg_default_acl;
SQL
)
case "$acl" in
    *S*) ok "③ default privileges on sequences ($acl)" ;;
    *)   fail "③ 시퀀스 default privileges 가 없다 ($acl)" ;;
esac

e=$(psql_admin -tA <<'SQL'
select count(*) from pg_namespace where nspname = 'eval';
SQL
)
[ "$e" = "0" ] && ok "④ eval schema absent" || fail "④ 운영 DB 에 eval(골든셋) 스키마가 있다"

# ⑥ 스키마·함수 권한. ①~④ 만으로는 extensions 스키마 USAGE 누락을 잡지 못한다(벡터 검색만 실패한다).
p=$(psql_admin -tA <<'SQL'
select has_schema_privilege('suddoe_app', 'extensions', 'USAGE')::text || '/' ||
       has_function_privilege('suddoe_app', 'tenant.계정찾기(text)', 'EXECUTE')::text || '/' ||
       (select count(*) from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
         where p.oid = 'tenant.계정찾기(text)'::regprocedure
           and a.grantee = 0 and a.privilege_type = 'EXECUTE')::text;
SQL
)
[ "$p" = "true/true/0" ] && ok "⑥ extensions USAGE, 계정찾기() EXECUTE (PUBLIC 회수됨)" \
                         || fail "⑥ extensions USAGE / 계정찾기 EXECUTE / PUBLIC EXECUTE 수 = $p"

# ⑦ suddoe_app 으로 실제 쿼리 실행: 벡터 거리 연산(검색 경로)과 로그인 함수
psql_app -tA >/dev/null <<'SQL' || fail "⑦ suddoe_app 으로 벡터 검색 쿼리 또는 계정찾기() 호출 실패"
SELECT 1 - (embedding <=> embedding) FROM corpus.chunks WHERE embedding IS NOT NULL LIMIT 1;
SELECT count(*) FROM tenant.계정찾기('restore-check@example.invalid');
SQL
ok "⑦ suddoe_app: 벡터 검색 쿼리, 계정찾기() 호출"

# ── 행 수 비교 (선택) ─────────────────────────────────────────────────
if [ -n "$COUNTS" ]; then
    actual=$(psql_admin -tA <<'SQL'
select n.nspname || ' ' || c.relname || ' ' ||
       (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', n.nspname, c.relname), false, true, '')))[1]::text
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where n.nspname in ('corpus', 'tenant') and c.relkind in ('r','p','m')
 order by 1;
SQL
)
    if diff <(grep -E '^(corpus|tenant) ' "$COUNTS" | tr -d '\r' | sort) <(printf '%s\n' "$actual" | sort); then
        ok "row counts match source ($(printf '%s\n' "$actual" | wc -l) tables)"
    else
        fail "행 수 불일치 (diff: '<' 원본, '>' 복원본)"
    fi
fi

# ── 5. RLS 격리 테스트 ───────────────────────────────────────────────
# suddoe_app 으로 직접 INSERT 해 본다. 모든 트랜잭션은 ROLLBACK 하므로 데이터는 남지 않는다.
orgs=$(psql_admin -tA <<'SQL'
select org_id from tenant.orgs order by org_id limit 2;
SQL
)
A=$(printf '%s\n' "$orgs" | sed -n 1p)
B=$(printf '%s\n' "$orgs" | sed -n 2p)
[ -n "$A" ] && [ -n "$B" ] || fail "⑤ 테스트할 org 가 2개 미만"

psql_app -tA -v org="$A" >/dev/null <<'SQL' || fail "⑤-a 자기 org INSERT 실패 (또는 suddoe_app 비밀번호 접속 실패)"
BEGIN;
SELECT set_config('app.org_id', :'org', true);
INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (:'org', 'rls-test') RETURNING plan_id;
ROLLBACK;
SQL
ok "⑤-a app.org_id 설정 후 자기 org INSERT: 허용"

out=$(psql_app -tA -v org="$A" 2>&1 <<'SQL' || true
BEGIN;
INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (:'org', 'rls-test');
ROLLBACK;
SQL
)
case "$out" in
    *42501*) ok "⑤-b app.org_id 미설정 INSERT: 거부 (42501)" ;;
    *)       fail "⑤-b app.org_id 미설정 INSERT 가 42501 로 거부되지 않았다: $out" ;;
esac

out=$(psql_app -tA -v org="$A" -v other="$B" 2>&1 <<'SQL' || true
BEGIN;
SELECT set_config('app.org_id', :'org', true);
INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (:'other', 'rls-test');
ROLLBACK;
SQL
)
case "$out" in
    *42501*) ok "⑤-c 다른 org 로 INSERT: 거부 (42501)" ;;
    *)       fail "⑤-c 다른 org INSERT 가 42501 로 거부되지 않았다: $out" ;;
esac

echo "restore and verification complete. next: docker compose up -d --build"
