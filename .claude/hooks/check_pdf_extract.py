# -*- coding: utf-8 -*-
"""PostToolUse(Write|Edit) — pdfplumber 직접 호출 경고.

일부 PDF 는 같은 글자가 두 겹으로 겹쳐 있어 extract_text() 가 모든 한글을 2배로
뱉는다("제제5조조"). 그러면 `제\d+조` 정규식이 하나도 안 걸리고 **조용히** 파싱에
실패한다. scripts/pdftext.py::extract() 가 중복을 감지해 해소한다.
(구 CLAUDE.md L144-145)
"""
import json
import sys
from pathlib import Path

NEEDLE = ".extract_text("
ALLOW = ("scripts/pdftext.py",)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    path = (data.get("tool_input") or {}).get("file_path", "") or ""
    norm = path.replace("\\", "/")

    if not norm.endswith(".py") or any(a in norm for a in ALLOW):
        return 0
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0

    if NEEDLE in text and "pdftext" not in text:
        sys.stderr.write(
            f"[hook] {Path(path).name} 이 extract_text() 를 직접 부른다.\n"
            "문자중복 레이어 PDF 에서 조 0개로 조용히 실패한다.\n"
            "scripts/pdftext.py::extract() 를 경유할 것 — (본문, 중복여부) 를 돌려준다.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
