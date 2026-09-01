# -*- coding: utf-8 -*-
"""DB 스키마 실측 덤프 — 살아있는 DB 를 pg_catalog/information_schema 로 직접 조회한다.

🔴 왜 db/init/*.sql 을 안 읽나: 파일과 실 DB 가 갈라지는 게 이 프로젝트 상시 상태다
   (check_items."유형"·l3_documents 파싱품질 CHECK 가 SQL 파일보다 먼저 DB 에 들어간
   사례가 실제로 있었다 — `db/init/05_frontend_alter.sql` §④⑤ 참고).
   그래서 이 스크립트는 오직 pg_catalog 질의로만 만든다 — SQL 파일은 한 줄도 안 읽는다.

재생성:
    PYTHONIOENCODING=utf-8 python db/tools/dump_db_schema.py > db_schema_dump.md

다른 DB 와 대조하려면 `SUDDOE_DSN` 을 바꿔서 두 번 돌리고 diff 하면 된다:
    SUDDOE_DSN=postgresql://postgres:devpw@localhost:5432/<대조DB> \
        python db/tools/dump_db_schema.py > b.md
    diff a.md b.md

🔴 이 대조가 증명하는 것과 증명하지 못하는 것은 다르다 — **빈 DB 에 db/init/*.sql 을
   적용해서 대조하면 "새로 만들면 같다" 만 증명한다.** "이미 있는 DB 에 적용하면
   최신이 된다" 는 별개 보장이고, `CREATE TABLE IF NOT EXISTS` 안에 인라인으로만 넣은
   변경은 기존 DB에서는 절대 안 생긴다(2026-09-01 사례). 기존 DB 업그레이드 경로를
   검증하려면 그 DB 를 흉내 낸 상태에서 대조해야 한다 — 빈 DB 대조 하나로 안심하지 마라.

대상: tenant.* 전부 + corpus.check_items 하나만 (corpus 나머지는 Agent 세션 소유라 안 건드림).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server._common import _질의

TABLES = [
    ("tenant", "accounts"), ("tenant", "decisions"), ("tenant", "expense_plans"),
    ("tenant", "f_exec"), ("tenant", "f_personnel"), ("tenant", "f_profile"),
    ("tenant", "incidents"), ("tenant", "l3_articles"), ("tenant", "l3_documents"),
    ("tenant", "orgs"), ("tenant", "plan_tasks"), ("tenant", "unmapped_premise"),
    ("corpus", "check_items"),
]

CONTYPE = {"p": "PRIMARY KEY", "f": "FOREIGN KEY", "u": "UNIQUE", "c": "CHECK", "x": "EXCLUDE"}


def 행수(스키마: str, 테이블: str) -> int:
    r = _질의(f'SELECT count(*) FROM "{스키마}"."{테이블}"')
    return r[0][0] if r else -1


def 컬럼(스키마: str, 테이블: str):
    return _질의(
        """
        SELECT column_name, data_type, udt_name, is_nullable, column_default, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (스키마, 테이블),
    )


def 제약(스키마: str, 테이블: str):
    return _질의(
        """
        SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = %s AND t.relname = %s
        ORDER BY c.contype, c.conname
        """,
        (스키마, 테이블),
    )


def 인덱스(스키마: str, 테이블: str):
    return _질의(
        'SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s '
        "ORDER BY indexname",
        (스키마, 테이블),
    )


def 타입표시(data_type: str, udt_name: str) -> str:
    if data_type == "ARRAY":
        return f"{udt_name.lstrip('_')}[]"
    if data_type == "USER-DEFINED":
        return udt_name
    return data_type


def 표(rows, headers) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c).replace("\n", " ") if c is not None else "" for c in r) + " |")
    return "\n".join(lines)


def main():
    out = []
    out.append("<!-- 실측 덤프 — pg_catalog 직접 조회, db/init/*.sql 안 읽음. 재생성: db/tools/dump_db_schema.py -->\n")
    out.append("## 테이블 목록\n")
    out.append(표([(s, t, 행수(s, t)) for s, t in TABLES], ["schema", "table", "행수"]))
    out.append("")

    for 스키마, 테이블 in TABLES:
        out.append(f"\n### `{스키마}.{테이블}`\n")

        cols = 컬럼(스키마, 테이블)
        col_rows = [
            (c[5], c[0], 타입표시(c[1], c[2]), "NULL 허용" if c[3] == "YES" else "NOT NULL", c[4] or "")
            for c in cols
        ]
        out.append("**컬럼**\n")
        out.append(표(col_rows, ["순서", "이름", "타입", "NULL", "DEFAULT"]))

        cons = 제약(스키마, 테이블)
        out.append("\n**제약**\n")
        if cons:
            con_rows = [(CONTYPE.get(ct, ct), name, defn) for name, ct, defn in cons]
            out.append(표(con_rows, ["종류", "이름", "정의"]))
        else:
            out.append("(없음)")

        idx = 인덱스(스키마, 테이블)
        out.append("\n**인덱스**\n")
        if idx:
            out.append(표(idx, ["이름", "정의"]))
        else:
            out.append("(없음)")

    print("\n".join(out))


if __name__ == "__main__":
    main()
