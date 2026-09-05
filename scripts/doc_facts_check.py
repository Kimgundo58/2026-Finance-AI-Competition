# -*- coding: utf-8 -*-
"""문서 안의 「기계로 확인 가능한 주장」을 코드·DB 와 대조한다 (재발 방지용 신설, 2026-09-05).

**왜.** `docs/낡은_문서_목록_0905.md` 실측으로 하루 만에 낡은 서술 15건이 나왔다. 유형을
가르면 넷이고 그중 셋(경로 이동 제외)이 기계로 잡힌다:

  ① 「없다 / 0행 / 0건」이 나중에 생김   — A1 키 · A3 계정 · A4 팟
  ② 수치가 갱신됨                        — B1~B6
  ③ 결정이 닫혔는데 미결에 남음           — A2 챗봇 · A8 TIPS (**기계로 못 잡는다** — 아래 참고)

이 스크립트는 ①②를 **하나의 메커니즘**으로 묶는다. 문서에 `` `스키마.테이블` `` 이 나오면
그 뒤 짧은 구간에서 숫자나 「없다/0행/0건」을 찾아 실제 `SELECT COUNT(*)` 와 대조한다.
같은 정규식이 ①(기대값 0)과 ②(기대값 N)를 함께 잡는다. 경로 주장(`scripts/*.py`·`db/**`)은
별도로 파일 존재 여부만 본다.

🔴 **이 검사기가 «못» 잡는 것 — 반드시 같이 읽는다.**

  - **③ (닫힌 결정이 미결 문서에 남음)** — A2 가 정확한 사례다. "화면 12 AI CHAT 이 닫혔다"는
    사실은 DB 에도 파일 경로에도 없다, 사람 판단이다. 여기는 규칙으로 막는다:
    결정을 닫은 세션이 `9_미결` 을 직접 못 고치면 어디에 적었는지 중앙에 알린다.
  - **DB·파일시스템 밖의 사실** — RunPod 팟 개수(A4), GCP Cloud Run 환경변수(A1) 같은 외부
    인프라 상태는 이 스크립트가 못 본다. `gcloud`/`runpodctl` 실측은 사람이 계속 손으로 한다.
  - **pytest 통과 수(B2) 같은 "실행해야 아는" 수치** — 정적 분석이라 코드를 실행하지 않는다.
    별도로 `python -m pytest -q` 를 돌려 대조해야 한다.
  - **오탐(정밀도) 쪽 한계** — 자유문 안의 숫자를 정규식으로 줍기 때문에, 표 안의 다른 열 숫자를
    그 스키마.테이블 것으로 잘못 묶을 수 있다. 여기서 나온 MISMATCH 는 **재확인 대상**이지
    자동으로 옳다는 뜻이 아니다 (`docs/0-3_초록이_가린다.md` — 초록이 결함을 가리는 반대쪽도
    조심한다: 이 검사기의 PASS 도 "확인했다"의 증거일 뿐 "판정이 맞다"의 증거가 아니다).
  - **경로 이동 중인 파일** — 2026-09-05 시점 `scripts/archive/` 이관(ai-69)이 진행 중이면
    그 파일들은 실행할 때마다 결과가 바뀐다. 이관이 끝난 뒤 기준선을 다시 잡는다.

사용법:
    python scripts/doc_facts_check.py                 # docs/ + CLAUDE.md + README.md
    python scripts/doc_facts_check.py --no-db          # 경로 주장만 (DB 연결 없이)
    python scripts/doc_facts_check.py --root docs/9_미결.md   # 파일 하나만
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

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
