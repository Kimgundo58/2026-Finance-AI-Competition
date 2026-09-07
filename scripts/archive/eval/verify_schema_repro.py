# -*- coding: utf-8 -*-
"""`db/init/*.sql` 을 빈 DB 에 돌려 운영 DB 스키마(컬럼·제약·인덱스·정책·트리거·뷰)와 대조한다.

실행:  PYTHONIOENCODING=utf-8 python scripts/archive/eval/verify_schema_repro.py
"""
from __future__ import annotations

# `scripts/_lib` 이 보일 때까지 위로 올라가 scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# archive 하위 폴더끼리 import 하므로 scripts/archive/ 의 모든 하위 폴더도 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)

import os, subprocess, sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())
컨테이너 = os.environ.get("SUDDOE_CONTAINER", "suddoe-db")
운영DB = os.environ.get("SUDDOE_DB", "suddoe")
검증DB = "suddoe_verify"


def psql(db: str, *args: str, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-i", 컨테이너, "psql", "-U", "postgres",
                           "-d", db, *args],
                          capture_output=True, text=True, encoding="utf-8", stdin=stdin)


def 스냅(db: str) -> dict[str, set[str]]:
    import psycopg
    with psycopg.connect(f"postgresql://postgres:devpw@localhost:5432/{db}") as c:
        q = lambda s: {r[0] for r in c.execute(s).fetchall()}
        return dict(
            컬럼=q("""select table_schema||'.'||table_name||'.'||column_name||' '||data_type
                          ||' null='||is_nullable||' def='||coalesce(column_default,'-')
                       from information_schema.columns
                      where table_schema in ('corpus','tenant','eval','extensions')"""),
            제약=q("""select con.conrelid::regclass::text||' '||con.conname||' '
                          ||con.contype::text||' '||pg_get_constraintdef(con.oid)
                       from pg_constraint con join pg_namespace n on n.oid=con.connamespace
                      where n.nspname in ('corpus','tenant','eval')"""),
            인덱스=q("""select schemaname||'.'||indexname||' '||indexdef from pg_indexes
                        where schemaname in ('corpus','tenant','eval')"""),
            정책=q("""select polrelid::regclass::text||' '||polname||' '
                          ||coalesce(pg_get_expr(polqual,polrelid),'-') from pg_policy"""),
            트리거=q("""select tgrelid::regclass::text||' '||tgname from pg_trigger
                        where not tgisinternal"""),
            뷰=q("""select table_schema||'.'||table_name from information_schema.views
                     where table_schema in ('corpus','tenant','eval')"""))


def main() -> None:
    psql("postgres", "-q", "-c", f"DROP DATABASE IF EXISTS {검증DB};")
    psql("postgres", "-q", "-c", f"CREATE DATABASE {검증DB};")
    for f in sorted((ROOT / "db" / "init").glob("*.sql")):
        with f.open(encoding="utf-8") as fh:
            r = psql(검증DB, "-q", "-v", "ON_ERROR_STOP=1", stdin=fh)
        if r.returncode != 0:
            print(f"🔴 {f.name} 실행 실패:\n{r.stderr[-900:]}")
            sys.exit(1)
        print(f"  {f.name} 실행 OK")

    운영, 재현 = 스냅(운영DB), 스냅(검증DB)
    차이 = 0
    print()
    for k in 운영:
        a, b = 운영[k] - 재현[k], 재현[k] - 운영[k]
        if not (a or b):
            print(f"  ✅ {k:5} {len(운영[k]):4}개 일치"); continue
        차이 += len(a) + len(b)
        print(f"  🔴 {k}: 운영에만 {len(a)} · init 에만 {len(b)}")
        for x in sorted(a)[:8]: print(f"       🔴 운영에만 있다(= init 에 빠졌다): {x[:96]}")
        for x in sorted(b)[:8]: print(f"       ⚠️ init 에만 있다(= 운영에 미적용):  {x[:96]}")

    psql("postgres", "-q", "-c", f"DROP DATABASE IF EXISTS {검증DB};")
    print()
    if 차이:
        print(f"🔴 차이 {차이}건 — init SQL 이 운영 DB 를 재현하지 못한다.")
        print("   '운영에만' 은 psql 로 직접 친 변경이 파일에 안 들어간 것이다. 파일에 반영하라.")
        sys.exit(1)
    print("✅ db/init/*.sql 이 운영 DB 를 완전히 재현한다")


if __name__ == "__main__":
    main()
