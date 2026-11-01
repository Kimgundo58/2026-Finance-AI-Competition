# -*- coding: utf-8 -*-
"""DOCX(OOXML WordprocessingML) 본문 텍스트 추출기 (python-docx).

stage0_extract 가 zip 형식 중 docx 로 판정된 파일(LibreOffice 로 변환한 HWP 포함)에 쓴다.
공개 함수: extract(path) -> str. 문단 하나 = 한 줄, 표 셀 하나 = 한 줄이다. 본문 문단과 표는
`Document.iter_inner_content()` 로 등장 순서대로 순회한다 — 따로 읽으면 조문 순서가 깨진다.
정체 판정은 `hwpx_extract.sniff()` 를 쓰고, docx 가 아니면 NotDocxError 로 실제 정체를 담아 실패한다.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from hwpx_extract import sniff


class NotDocxError(ValueError):
    """파일이 docx 가 아니다. 메시지에 실제 정체를 담는다."""


def _walk_blocks(blocks, lines: list[str]) -> None:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for block in blocks:
        if isinstance(block, Paragraph):
            t = block.text
            if t.strip():
                lines.extend(t.split("\n"))
        elif isinstance(block, Table):
            _walk_table(block, lines)


def _walk_table(tbl, lines: list[str]) -> None:
    """표 → 셀마다 한 줄. 가로 병합은 같은 tc 가 행 안에서 반복되므로 중복 제거한다."""
    for row in tbl.rows:
        seen: set[int] = set()
        for cell in row.cells:
            key = id(cell._tc)
            if key in seen:
                continue
            seen.add(key)
            _walk_blocks(cell.iter_inner_content(), lines)


def extract(path: str | Path) -> str:
    """docx → 평문. 문단·표 셀 경계는 줄바꿈. docx 가 아니면 NotDocxError."""
    p = Path(path)
    kind = sniff(p)
    if not kind.startswith("DOCX"):
        raise NotDocxError(f"docx 아님 — 실제 정체: {kind} ({p.name})")

    from docx import Document

    doc = Document(str(p))
    lines: list[str] = []
    _walk_blocks(doc.iter_inner_content(), lines)
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    text = extract(sys.argv[1])
    if len(sys.argv) > 2:
        open(sys.argv[2], "w", encoding="utf-8").write(text)
        print(f"wrote {len(text)} chars -> {sys.argv[2]}")
    else:
        print(text)
