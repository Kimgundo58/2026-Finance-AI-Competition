#!/usr/bin/env bash
# DB 복원 및 검증 스크립트
#   1) 빈 DB 에 앱 롤(suddoe_app) 생성
#   2) pg_dump(-Fc) 덤프 복원
#   3) db/init/10b_app_grants.sql 로 권한·search_path 설정
#   4) 권한 검증(1~5), suddoe_app 실제 쿼리(5-b), 테이블별 행 수 비교(선택), RLS 격리 테스트(6)
#
# 사용법 (deploy/ncp 디렉터리에서):
#   ./restore.sh <suddoe.dump> [source_rowcounts.txt]
#
# 검증 항목 번호는 db/init/10b_app_grants.sql 하단 주석과 같다. 하나라도 실패하면 exit 1 로 종료한다.
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
[ "$n" = "0" ] || fail "corpus/tenant schemas already exist. Preserve the existing volume and use a separate empty restore target"

# ── 1. 앱 롤 생성 ────────────────────────────────────────────────────
# RLS 정책은 PUBLIC 대상이라 롤 생성 순서와 무관하지만, 3단계 GRANT 전에는 있어야 한다.
psql_admin -v pw="$SUDDOE_APP_DB_PASSWORD" <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_app') THEN
        CREATE ROLE suddoe_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'suddoe_ingest') THEN
        CREATE ROLE suddoe_ingest NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END
$$;
ALTER ROLE suddoe_app WITH LOGIN PASSWORD :'pw' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
SQL
ok "role suddoe_app"

# ── 2. 덤프 복원 ─────────────────────────────────────────────────────
# 완전 백업은 그대로 보존한다. AGE 1.6 논리 복원에서 그래프 OID가 바뀌므로
# 파생 그래프·AGE catalog 데이터·뷰만 목록에서 빼고 원천을 복원해 다시 생성한다.
AGE_LIST=$(mktemp)
AGE_FILTERED=$(mktemp)
trap 'rm -f "$AGE_LIST" "$AGE_FILTERED"' EXIT
docker compose exec -T db pg_restore --list < "$DUMP" > "$AGE_LIST"
AGE_REBUILD=0
if grep -Eq ' EXTENSION - age([[:space:]]|$)' "$AGE_LIST"; then
    AGE_REBUILD=1
    [ -n "${SUDDOE_INGEST_DSN:-}" ] || fail "AGE 복원에는 설정된 SUDDOE_INGEST_DSN이 필요합니다"
    login=$(psql_admin -tA -c "SELECT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication FROM pg_roles WHERE rolname='suddoe_ingest'")
    [ "$login" = "t" ] || fail "suddoe_ingest 비특권 로그인 계정을 별도로 설정한 뒤 다시 실행하세요"
    awk -f age_restore_filter.awk "$AGE_LIST" > "$AGE_FILTERED" || fail "AGE 덤프 목록을 안전하게 해석하지 못했습니다"
    # 복원 시작 전에 로컬 소스와 동일한 재구축 코드가 API 이미지에 있는지 확인한다.
    expected=$(sha256sum ../../scripts/ontology_age_rebuild.py ../../scripts/ontology_age.py ../../scripts/ontology_age_validate.py | awk '{print $1}' | paste -sd ':' -)
    actual=$(docker compose run -T --no-deps --pull never --entrypoint python api -I -X utf8 -c \
        "import hashlib; print(':'.join(hashlib.sha256(open('/app/scripts/'+n,'rb').read()).hexdigest() for n in ('ontology_age_rebuild.py','ontology_age.py','ontology_age_validate.py')))") \
        || fail "AGE 재구축 API 이미지를 사용할 수 없습니다"
    [ "$expected" = "$actual" ] || fail "API 이미지의 AGE 재구축 코드가 현재 소스와 다릅니다"
elif grep -Eq ' SCHEMA - suddoe_ontology([[:space:]]|$)' "$AGE_LIST"; then
    fail "AGE 확장 없는 그래프 덤프는 복원할 수 없습니다"
fi

echo "restoring dump..."
if [ "$AGE_REBUILD" = "1" ]; then
    container_list=$(docker compose exec -T db mktemp /tmp/suddoe-age-restore.XXXXXX)
    docker compose cp "$AGE_FILTERED" "db:$container_list"
    docker compose exec -T db pg_restore -U postgres -d suddoe --no-owner --no-privileges --exit-on-error \
        -L "$container_list" < "$DUMP"
else
    docker compose exec -T db pg_restore -U postgres -d suddoe --no-owner --no-privileges --exit-on-error < "$DUMP"
fi
psql_admin <<'SQL'
ANALYZE;
SQL
ok "pg_restore + ANALYZE"

# ── 3. 권한·search_path ──────────────────────────────────────────────
psql_admin < ../../db/init/10b_app_grants.sql
ok "10b_app_grants.sql"
if [ "$AGE_REBUILD" = "1" ]; then
    psql_admin < age_restore_grants.sql
    docker compose exec -T db psql -U postgres -d suddoe -X --single-transaction -v ON_ERROR_STOP=1 \
        < ../../db/migrations/2026-11-09_온톨로지_AGE.sql
    restore_token=$(psql_admin -tA -c "UPDATE corpus.ontology_age_state SET dirty=true, counts=jsonb_build_object('restore_nonce', gen_random_uuid()::text) WHERE id=true RETURNING counts->>'restore_nonce'")
    [ -n "$restore_token" ] || fail "AGE 복원 확인 토큰을 만들지 못했습니다"
    docker compose run -T --no-deps --pull never --entrypoint python api -I -X utf8 /app/scripts/ontology_age_rebuild.py --restore-token "$restore_token" \
        || fail "AGE 재구축 실패: DB는 dirty 상태입니다. 수집 DSN을 바로잡은 뒤 --restore-token $restore_token 으로 재개하고 API를 켜기 전에 검증하세요"
    psql_app <<'SQL'
LOAD '$libdir/plugins/age';
SET search_path = ag_catalog, extensions, corpus, public;
SELECT * FROM cypher('suddoe_ontology', $$ MATCH (n:Entity) RETURN count(n) $$) AS (n agtype);
SQL
    ok "AGE 그래프 재구축·원천 대조·앱 Cypher"
fi

# ── 4. 권한 검증 1~5 ──────────────────────────────────────────────────
r=$(psql_admin -tA <<'SQL'
select rolsuper::text || '/' || rolbypassrls::text from pg_roles where rolname = 'suddoe_app';
SQL
)
[ "$r" = "false/false" ] && ok "1 suddoe_app: NOSUPERUSER, NOBYPASSRLS" || fail "1 suddoe_app rolsuper/rolbypassrls = $r"

# information_schema 에는 materialized view 가 나오지 않으므로 pg_class 기준으로 확인한다.
missing=$(psql_admin -tA <<'SQL'
select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where (n.nspname = 'corpus' and c.relkind in ('r','p','v','m')
        and not has_table_privilege('suddoe_app', c.oid, 'SELECT'))
    or (n.nspname = 'tenant' and c.relkind in ('r','p') and c.relname <> 'teams'  -- teams 는 함수로만 접근한다
        and not (has_table_privilege('suddoe_app', c.oid, 'SELECT')
             and has_table_privilege('suddoe_app', c.oid, 'INSERT')
             and has_table_privilege('suddoe_app', c.oid, 'UPDATE')
             and has_table_privilege('suddoe_app', c.oid, 'DELETE')));
SQL
)
[ "$missing" = "0" ] && ok "2 table privileges (corpus: SELECT, tenant: SELECT/INSERT/UPDATE/DELETE)" \
                     || fail "2 tables missing privileges: $missing"

acl=$(psql_admin -tA <<'SQL'
select string_agg(defaclobjtype::text, '' order by defaclobjtype::text) from pg_default_acl;
SQL
)
case "$acl" in
    *S*) ok "3 default privileges on sequences ($acl)" ;;
    *)   fail "3 no default privileges on sequences ($acl)" ;;
esac

e=$(psql_admin -tA <<'SQL'
select count(*) from pg_namespace where nspname = 'eval';
SQL
)
[ "$e" = "0" ] && ok "4 eval schema absent" || fail "4 eval (golden set) schema exists in the production DB"

# 5 스키마·함수 권한. 1~4 만으로는 extensions 스키마 USAGE 누락을 잡지 못한다(벡터 검색만 실패한다).
p=$(psql_admin -tA <<'SQL'
select has_schema_privilege('suddoe_app', 'extensions', 'USAGE')::text || '/' ||
       has_function_privilege('suddoe_app', 'tenant.계정찾기(text)', 'EXECUTE')::text || '/' ||
       (select count(*) from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
         where p.oid = 'tenant.계정찾기(text)'::regprocedure
           and a.grantee = 0 and a.privilege_type = 'EXECUTE')::text;
SQL
)
[ "$p" = "true/true/0" ] && ok "5 extensions USAGE, 계정찾기() EXECUTE (revoked from PUBLIC)" \
                         || fail "5 extensions USAGE / 계정찾기 EXECUTE / PUBLIC EXECUTE count = $p"

# 5-a 계정등록(). 2026-11-04 이전 백업에는 함수가 없다. 복원은 막지 않고 마이그레이션을 다시 적용하라고 알린다.
p=$(psql_admin -tA <<'SQL'
select case when to_regprocedure('tenant.계정등록(text,uuid)') is null then 'missing' else
       has_function_privilege('suddoe_app', 'tenant.계정등록(text,uuid)', 'EXECUTE')::text || '/' ||
       (select count(*) from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
         where p.proname = '계정등록' and a.grantee = 0 and a.privilege_type = 'EXECUTE')::text end;
SQL
)
case "$p" in
  true/0)  ok "5-a 계정등록() EXECUTE (revoked from PUBLIC)" ;;
  missing) echo "[WARN] 5-a 계정등록() is missing: apply db/migrations/2026-11-04_계정등록.sql or sign-ups get 503" ;;
  *)       fail "5-a 계정등록 EXECUTE / PUBLIC EXECUTE count = $p" ;;
esac

# 5-b suddoe_app 으로 실제 쿼리 실행: 벡터 거리 연산(검색 경로)과 로그인 함수
psql_app -tA >/dev/null <<'SQL' || fail "5-b suddoe_app: vector search query or 계정찾기() call failed"
SELECT 1 - (embedding <=> embedding) FROM corpus.chunks WHERE embedding IS NOT NULL LIMIT 1;
SELECT count(*) FROM tenant.계정찾기('restore-check@example.invalid');
SQL
ok "5-b suddoe_app: vector search query, 계정찾기() call"

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
        fail "row count mismatch (diff: '<' source, '>' restored)"
    fi
fi

# ── 6. RLS 격리 테스트 ───────────────────────────────────────────────
# suddoe_app 으로 직접 INSERT 해 본다. 모든 트랜잭션은 ROLLBACK 하므로 데이터는 남지 않는다.
orgs=$(psql_admin -tA <<'SQL'
select org_id from tenant.orgs order by org_id limit 2;
SQL
)
A=$(printf '%s\n' "$orgs" | sed -n 1p)
B=$(printf '%s\n' "$orgs" | sed -n 2p)
[ -n "$A" ] && [ -n "$B" ] || fail "6 fewer than 2 orgs to test"

psql_app -tA -v org="$A" >/dev/null <<'SQL' || fail "6-a INSERT into own org failed (or suddoe_app password login failed)"
BEGIN;
SELECT set_config('app.org_id', :'org', true);
INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (:'org', 'rls-test') RETURNING plan_id;
ROLLBACK;
SQL
ok "6-a INSERT into own org with app.org_id set: allowed"

out=$(psql_app -tA -v org="$A" 2>&1 <<'SQL' || true
BEGIN;
INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (:'org', 'rls-test');
ROLLBACK;
SQL
)
case "$out" in
    *42501*) ok "6-b INSERT without app.org_id: denied (42501)" ;;
    *)       fail "6-b INSERT without app.org_id was not denied with 42501: $out" ;;
esac

out=$(psql_app -tA -v org="$A" -v other="$B" 2>&1 <<'SQL' || true
BEGIN;
SELECT set_config('app.org_id', :'org', true);
INSERT INTO tenant.expense_plans (org_id, 질문원문) VALUES (:'other', 'rls-test');
ROLLBACK;
SQL
)
case "$out" in
    *42501*) ok "6-c INSERT into another org: denied (42501)" ;;
    *)       fail "6-c INSERT into another org was not denied with 42501: $out" ;;
esac

echo "DB restore and verification complete. Restore the separately backed up plan_attachments volume as needed before enabling the API."
echo "next: docker compose up -d --build"
