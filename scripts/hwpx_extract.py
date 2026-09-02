# -*- coding: utf-8 -*-
"""HWPX(OWPML) 본문 텍스트 추출기 (zipfile + xml.etree 만 사용).

hwpx = zip. 본문은 `Contents/section*.xml`, 문단은 `hp:p`, 글자는 `hp:run/hp:t`,
표는 `hp:tbl/hp:tr/hp:tc/hp:subList/hp:p` 로 중첩된다.

시그니처·반환형은 `hwp_extract.extract(path) -> str` 과 같다.

🔴 경계 규칙 — 뒷단 조문 재조립(`stage0_run`)이 줄 단위로 돌기 때문에
   문단 하나 = 한 줄, 표 셀 하나 = 한 줄(셀 안에 문단이 여럿이면 각각 한 줄).
   `hp:lineBreak` 는 줄바꿈, `hp:tab` 은 탭, `hp:fwSpace` 는 공백으로 살린다.

🔴 확장자 위장 방어 — 확장자가 .hwpx 여도 zip 을 열어 **내용물로 판정**한다.
   실물 사례: TIPS 운영사 현황(2026년).hwpx 는 내부가 xl/workbook.xml 인 XLSX 였다.
   hwpx 가 아니면 조용히 빈 문자열을 돌려주지 않고 `NotHwpxError` 로 실패한다
   (프로젝트 원칙: 모든 실패의 기본값은 판단불가 — 조용한 빈 값이 제일 나쁘다).
"""
from __future__ import annotations

import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS_P = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HWPX_MIMETYPE = "application/hwp+zip"

_T = f"{{{NS_P}}}t"
_P = f"{{{NS_P}}}p"
_RUN = f"{{{NS_P}}}run"
_TBL = f"{{{NS_P}}}tbl"
_TR = f"{{{NS_P}}}tr"
_TC = f"{{{NS_P}}}tc"
_SUBLIST = f"{{{NS_P}}}subList"
_CTRL = f"{{{NS_P}}}ctrl"
_LINESEG = f"{{{NS_P}}}linesegarray"
_INLINE = {                     # hp:t 안에 자식으로 섞여 들어오는 제어 문자
    f"{{{NS_P}}}lineBreak": "\n",
    f"{{{NS_P}}}tab": "\t",
    f"{{{NS_P}}}fwSpace": " ",
    f"{{{NS_P}}}nbSpace": " ",
    f"{{{NS_P}}}hyphen": "-",
}

_RE_SECTION = re.compile(r"^Contents/section(\d+)\.xml$", re.I)


class NotHwpxError(ValueError):
    """파일이 hwpx 가 아니다. 메시지에 실제 정체를 담는다."""


# ── 정체 판정 ──────────────────────────────────────────────────────
def sniff(path: str | Path) -> str:
    """파일의 실제 정체를 문자열로 돌려준다. 'hwpx' 면 진짜다.

    zip 이면 내용물로, zip 이 아니면 매직 바이트로 판정한다.
    """
    p = Path(path)
    if not zipfile.is_zipfile(p):
        head = p.open("rb").read(8)
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            return "OLE2 (HWP v5 또는 MS Office 97-2003)"
        if head.startswith(b"%PDF"):
            return "PDF"
        if head.lstrip().startswith(b"<?xml"):
            return "XML (HWPML 등)"
        return "zip 아님 (정체 불명)"
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        mimetype = z.read("mimetype").decode("ascii", "replace").strip() if "mimetype" in names else ""
        if mimetype == HWPX_MIMETYPE or any(_RE_SECTION.match(n) for n in names):
            return "hwpx"
        if "xl/workbook.xml" in names:
            return "XLSX (xl/workbook.xml)"
        if "word/document.xml" in names:
            return "DOCX (word/document.xml)"
        if "ppt/presentation.xml" in names:
            return "PPTX (ppt/presentation.xml)"
        if mimetype.startswith("application/vnd.oasis.opendocument"):
            return f"ODF ({mimetype})"
        if mimetype:
            return f"zip (mimetype={mimetype})"
        sample = ", ".join(sorted(names)[:5])
        return f"zip (hwpx 아님; 항목 예: {sample})"


def _section_names(z: zipfile.ZipFile) -> list[str]:
    hits = [(int(m.group(1)), n) for n in z.namelist() if (m := _RE_SECTION.match(n))]
    return [n for _, n in sorted(hits)]


# ── 본문 걷기 ──────────────────────────────────────────────────────
def _t_text(t: ET.Element) -> str:
    """hp:t — 텍스트와 그 안의 인라인 제어(lineBreak·tab·fwSpace) 를 순서대로."""
    out = [t.text or ""]
    for c in t:
        out.append(_INLINE.get(c.tag, ""))
        out.append(c.tail or "")
    return "".join(out)


def _walk_para(p: ET.Element, lines: list[str]) -> None:
    """hp:p 하나 → 줄 목록에 추가. 문단 안에 표가 끼면 표 앞뒤 텍스트를 갈라 낸다."""
    buf: list[str] = []

    def flush():
        s = "".join(buf)
        buf.clear()
        if s.strip():
            lines.extend(seg for seg in s.split("\n"))

    for run in p:
        if run.tag == _LINESEG:
            continue
        if run.tag != _RUN:
            _walk_any(run, lines, buf, flush)
            continue
        for node in run:
            _walk_any(node, lines, buf, flush)
    flush()


def _walk_any(node: ET.Element, lines: list[str], buf: list[str], flush) -> None:
    if node.tag == _T:
        buf.append(_t_text(node))
    elif node.tag == _TBL:
        flush()
        _walk_table(node, lines)
    elif node.tag == _CTRL:
        return                                  # 단 설정·쪽번호 등, 본문 아님
    elif node.tag in _INLINE:
        buf.append(_INLINE[node.tag])
    else:
        # 도형·글상자·각주 등 — 안에 subList 가 있으면 그 문단들을 살린다
        subs = node.findall(f".//{_SUBLIST}")
        if subs:
            flush()
            for sl in subs:
                for p in sl.findall(_P):
                    _walk_para(p, lines)


def _walk_table(tbl: ET.Element, lines: list[str]) -> None:
    """표 → 셀마다 한 줄(셀 안 문단 여럿이면 각각 한 줄). 행 사이엔 아무것도 안 넣는다."""
    for tr in tbl.findall(_TR):
        for tc in tr.findall(_TC):
            cell: list[str] = []
            for sl in tc.findall(_SUBLIST):
                for p in sl.findall(_P):
                    _walk_para(p, cell)
            lines.extend(cell)


def _section_text(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    lines: list[str] = []
    for p in root.findall(_P):               # 최상위 문단만. 표 안 문단은 표 걷기가 처리
        _walk_para(p, lines)
    return lines


# ── 공개 API ───────────────────────────────────────────────────────
def extract(path: str | Path) -> str:
    """hwpx → 평문. 문단·표 셀 경계는 줄바꿈. hwpx 가 아니면 NotHwpxError."""
    p = Path(path)
    kind = sniff(p)
    if kind != "hwpx":
        raise NotHwpxError(f"hwpx 아님 — 실제 정체: {kind} ({p.name})")
    lines: list[str] = []
    with zipfile.ZipFile(p) as z:
        sections = _section_names(z)
        if not sections:
            raise NotHwpxError(f"hwpx 이지만 Contents/section*.xml 이 없다 ({p.name})")
        for name in sections:
            lines.extend(_section_text(z.read(name)))
    return "\n".join(line.rstrip() for line in lines)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    text = extract(sys.argv[1])
    if len(sys.argv) > 2:
        open(sys.argv[2], "w", encoding="utf-8").write(text)
        print(f"wrote {len(text)} chars -> {sys.argv[2]}")
    else:
        print(text)
