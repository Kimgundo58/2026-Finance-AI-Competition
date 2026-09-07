# -*- coding: utf-8 -*-
"""문서 안의 「기계로 확인 가능한 주장」을 코드·DB 와 대조한다.

문서에 `` `스키마.테이블` `` 이 나오면 그 뒤 짧은 구간의 숫자나 「없다/0행/0건」을 실제
`SELECT COUNT(*)` 와 대조한다. 경로 주장(`scripts/*.py`·`db/**`)은 파일 존재 여부만 본다.

못 잡는 것: 닫힌 결정이 미결 문서에 남는 것, DB·파일시스템 밖의 사실(외부 인프라 상태),
pytest 통과 수처럼 실행해야 아는 수치. MISMATCH 는 재확인 대상이지 자동으로 옳다는 뜻이 아니다.

    python scripts/tools/doc_facts_check.py                 # docs/ + CLAUDE.md + README.md
    python scripts/tools/doc_facts_check.py --no-db          # 경로 주장만 (DB 연결 없이)
    python scripts/tools/doc_facts_check.py --root docs/9_미결.md   # 파일 하나만
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/
from _lib import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # scripts/tools/ → 루트

# docs/ 안에서도 이 아래는 "그 시점의 관측" — 소급 수정 금지 대상이라 낡아도 정상이다.
EXCLUDE_DIR_NAMES = {"기록", "archive"}

DEFAULT_TARGETS = ["docs", "CLAUDE.md", "README.md"]

KNOWN_SCHEMAS = {"corpus", "tenant", "eval", "ops", "public"}

# `스키마.테이블` — 백틱 인라인 코드 안에서만 잡는다 (자유문 중 우연한 "a.b" 오탐 방지).
TABLE_RE = re.compile(
    r"`(?P<schema>" + "|".join(KNOWN_SCHEMAS) + r")\.(?P<table>[a-zA-Z_][a-zA-Z0-9_]*)`"
)

# 테이블 언급 뒤 짧은 구간에서 주장을 찾는다. 숫자(콤마 허용) 또는 "없다"/"0행"/"0건".
ABSENT_RE = re.compile(r"없다|0\s*(?:행|건)")
# 앞에 한글/영문/숫자가 바로 붙어 있으면 해시·식별자 조각("chunks20525")일 가능성이 높아 제외
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_가-힣])(\d[\d,]{0,14})\s*(?:행|건|개)?")
WINDOW = 40  # 테이블명 뒤 이 글자 수 안에서만 주장을 찾는다 — 너무 넓히면 다음 문장 숫자를 줍는다

# `scripts/...` · `db/...` · `server/...` 등 프로젝트 상대경로만 대상. 글롭(*)·자리표시자는 제외.
PATH_RE = re.compile(
    r"`((?:scripts|db|server)/[A-Za-z0-9_./가-힣-]+)`"
)


@dataclass
class Finding:
    kind: str  # "path" | "table"
    file: str
    line: int
    detail: str


def find_target_files(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for t in targets:
        p = REPO_ROOT / t
        if p.is_file() and p.suffix == ".md":
            files.append(p)
            continue
        if not p.is_dir():
            continue
        for md in p.rglob("*.md"):
            if any(part in EXCLUDE_DIR_NAMES for part in md.relative_to(REPO_ROOT).parts):
                continue
            files.append(md)
    return sorted(set(files))


def check_paths(files: list[Path]) -> list[Finding]:
    findings = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in PATH_RE.finditer(line):
                path = m.group(1)
                if "*" in path or "<" in path or path.endswith("/"):
                    continue  # 글롭·자리표시자 — 구체 경로가 아니다
                if not (REPO_ROOT / path).exists():
                    findings.append(Finding(
                        "path", rel, lineno,
                        f"`{path}` — 파일이 없다"))
    return findings


def extract_table_claims(files: list[Path]):
    """(파일, 줄, 스키마, 테이블, 주장종류, 주장값, 원문조각) 리스트."""
    claims = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in TABLE_RE.finditer(line):
                schema, table = m.group("schema"), m.group("table")
                window = line[m.end():m.end() + WINDOW]
                # 마크다운 표 셀 경계(|)를 넘어가면 다른 열 숫자를 줍는다 — 셀 안으로 자른다
                window = window.split("|", 1)[0]
                am = ABSENT_RE.search(window)
                if am:
                    claims.append((rel, lineno, schema, table, "absent", 0, window.strip()))
                    continue
                nm = NUMBER_RE.search(window)
                if nm:
                    try:
                        value = int(nm.group(1).replace(",", ""))
                    except ValueError:
                        continue
                    claims.append((rel, lineno, schema, table, "count", value, window.strip()))
    return claims


def check_tables(claims) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    unresolved: list[str] = []
    try:
        conn = db.connect(autocommit=True, connect_timeout=5)
    except Exception as e:  # noqa: BLE001 — DB 없이도 경로 검사는 살려야 한다
        unresolved.append(f"DB 연결 실패 — 수치·부재 주장 검사 전체를 건너뜀 ({e})")
        return findings, unresolved

    cache: dict[tuple[str, str], int | None] = {}
    with conn, conn.cursor() as cur:
        for rel, lineno, schema, table, kind, value, snippet in claims:
            key = (schema, table)
            if key not in cache:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
                    cache[key] = cur.fetchone()[0]
                except Exception:
                    cache[key] = None
                    conn.rollback()
            actual = cache[key]
            if actual is None:
                unresolved.append(f"{rel}:{lineno} `{schema}.{table}` — 쿼리 실패(테이블 없음?)")
                continue
            expect = 0 if kind == "absent" else value
            if actual != expect:
                findings.append(Finding(
                    "table", rel, lineno,
                    f"`{schema}.{table}` 문서 주장 {expect}({snippet!r}) ↔ 실제 {actual}"))
    return findings, unresolved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", nargs="*", default=DEFAULT_TARGETS,
                     help="검사할 파일·디렉터리 (기본: docs/ CLAUDE.md README.md)")
    ap.add_argument("--no-db", action="store_true", help="DB 없이 경로 주장만 검사")
    args = ap.parse_args()

    files = find_target_files(args.root)
    print(f"대상 문서 {len(files)}개 (기록/·archive/ 제외)")

    path_findings = check_paths(files)

    table_findings: list[Finding] = []
    unresolved: list[str] = []
    if not args.no_db:
        claims = extract_table_claims(files)
        print(f"수치·부재 주장 {len(claims)}건 추출")
        table_findings, unresolved = check_tables(claims)

    all_findings = path_findings + table_findings
    if not all_findings:
        print("\n걸린 것 없음 — 그러나 아래 '못 잡는 것'은 여전히 사람이 봐야 한다.")
    else:
        print(f"\n=== 어긋난 주장 {len(all_findings)}건 ===")
        for f in sorted(all_findings, key=lambda x: (x.file, x.line)):
            print(f"[{f.kind}] {f.file}:{f.line}  {f.detail}")

    if unresolved:
        print(f"\n=== 확인 못 함 {len(unresolved)}건 (테이블명 오타 가능성 포함) ===")
        for u in unresolved:
            print(f"- {u}")

    print("\n=== 이 검사기가 못 잡는 것 (docstring 참고) ===")
    print("- ③ 닫힌 결정이 미결 문서에 남는 것 — 사람 판단이라 규칙(중앙 통지)으로 막는다")
    print("- DB·파일시스템 밖 사실 — RunPod 팟 수, GCP 환경변수 등 외부 인프라 상태")
    print("- pytest 통과 수 등 '실행해야 아는' 수치 — 정적 분석이라 코드를 실행하지 않는다")
    print("- 오탐 방향 — MISMATCH 는 재확인 대상이지 자동으로 옳다는 뜻이 아니다")
    print("- 경로 이관이 진행 중인 파일(archive/) — 이관 완료 후 기준선 재설정 필요")

    return 1 if all_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
