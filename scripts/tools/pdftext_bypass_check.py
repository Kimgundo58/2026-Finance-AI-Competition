# -*- coding: utf-8 -*-
"""`pdftext.py::extract()` 를 우회하는 `.extract_text(` 직접호출을 저장소 전체에서 찾는다.

판정식은 `.claude/hooks/check_pdf_extract.py` 와 같다 — `pdftext` 언급이 없고 `.extract_text(` 가
있으면 위반. `scripts/pdftext.py` 자신은 제외하고, `archive/` 아래는 낮은 우선순위로 따로 표시한다.
한계: `pdftext` 문자열이 주석에라도 있으면 통과로 본다. 결과는 사람이 확인하는 출발점이다.

    python scripts/tools/pdftext_bypass_check.py            # 저장소 전체
    python scripts/tools/pdftext_bypass_check.py --root scripts
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # scripts/tools/ → 저장소 루트
NEEDLE = ".extract_text("
SELF = "scripts/pdftext.py"
SKIP_DIR_NAMES = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def _iter_py_files(root: Path):
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def check(root: Path) -> tuple[list[dict], list[dict]]:
    """반환: (진짜 우선순위 위반, archive/ 낮은 우선순위 위반)."""
    hits, archive_hits = [], []
    for path in _iter_py_files(root):
        rel = path.relative_to(ROOT).as_posix()
        if rel == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if NEEDLE not in text or "pdftext" in text:
            continue
        lines = [i + 1 for i, ln in enumerate(text.splitlines()) if NEEDLE in ln]
        entry = {"path": rel, "lines": lines}
        (archive_hits if "archive/" in rel else hits).append(entry)
    return hits, archive_hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="검사 시작 경로 (기본: 저장소 루트)")
    args = ap.parse_args()
    root = (ROOT / args.root).resolve()

    hits, archive_hits = check(root)

    print(f"[pdftext 우회 검사] {NEEDLE!r} 있고 'pdftext' 언급 없는 파일 — {SELF} 제외\n")
    print(f"── 우선순위 (archive/ 밖) : {len(hits)}건 ──")
    for h in hits:
        print(f"  {h['path']}:{','.join(map(str, h['lines']))}")
    print(f"\n── 낮은 우선순위 (archive/ 안, 진단용 스크립트 다수) : {len(archive_hits)}건 ──")
    for h in archive_hits:
        print(f"  {h['path']}:{','.join(map(str, h['lines']))}")

    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
