# -*- coding: utf-8 -*-
"""`pdftext.py::extract()` 를 우회하는 `.extract_text(` 직접호출을 저장소 전체에서 찾는다.

**왜.** `.claude/hooks/check_pdf_extract.py` 는 PostToolUse(Write|Edit) 라 **신규 저장 때만**
걸린다 — 이미 존재하는 파일(예: `scripts/stage0_extract.py`)은 훅을 안 태우고 지나갔다.
게다가 그 훅의 판정식(`"pdftext" 문자열이 없고 ".extract_text(" 가 있으면 경고`)은
**`pdftext.py` 자기 자신도 걸린다** — 전수 검사에는 그대로 못 쓴다. 2026-09-05 ai-35·ai-66
교차확인에서 나온 결함(stage0_extract.py 가 L3 파싱 경로에서 이 훅을 우회)의 재발 방지용.

이 스크립트는 훅과 같은 판정식을 쓰되:
  - `scripts/pdftext.py` 자기 자신은 제외한다 (훅은 못 하는 것)
  - 경로에 `archive/` 가 들어간 파일은 **낮은 우선순위**로 따로 표시한다 (조사용 진단
    스크립트가 많아 오탐이 잦다 — `scripts/archive/eval/scan_tables.py` 등)
  - `.venv`·`node_modules`·`.git` 은 애초에 안 본다

🔴 **이게 못 잡는 것.** `pdftext` 문자열이 파일 어디에든(주석·docstring 포함) 있으면 통과로
본다 — 훅과 동일한 약점이다. 즉 "pdftext 얘기는 하지만 실제로는 안 부르는" 파일은
오탐(PASS인데 사실 위반)일 수 있다. 여기서 나온 목록은 **사람이 각 파일을 열어 확인**하는
출발점이지, 그 자체로 "이 파일들이 전부 결함"이라는 뜻은 아니다.

사용법:
    python scripts/pdftext_bypass_check.py            # 저장소 전체
    python scripts/pdftext_bypass_check.py --root scripts
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
