# -*- coding: utf-8 -*-
"""HWPX(OWPML) 본문 텍스트 추출기와 파일 형식 판별(sniff) (zipfile + xml.etree 만 사용).

text_extract 와 docx_extract 가 쓴다. 공개 함수: extract(path) -> str, sniff(path) -> 판별 결과 문자열,
예외 NotHwpxError·HwpProtectedError. hwpx 는 zip 이고 본문은 `Contents/section*.xml`, 문단은 `hp:p`,
글자는 `hp:run/hp:t`, 표는 `hp:tbl/hp:tr/hp:tc/hp:subList/hp:p` 로 중첩된다.
불변 조건: 후속 조문 분할 단계가 줄 단위라 문단 하나·표 셀 하나를 각각 한 줄로 낸다. hwpx 가 아니면
빈 문자열 대신 NotHwpxError 로, 배포용/암호화 HWP 는 HwpProtectedError 로 실패한다.
"""
from __future__ import annotations

import io
import re
import struct
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
    """파일이 hwpx 가 아니다. 메시지에 판별 결과를 담는다."""


class HwpProtectedError(ValueError):
    """hwp/hwpx 형식은 맞지만 배포용(DRM)·암호화라 본문을 못 읽는다.

    `hwp_extract.extract()` 는 이런 파일에서도 예외 없이 성공 반환하는데,
    실제로 추출되는 건 뷰어 안내문 몇 글자뿐이다. 겉보기엔 성공한 조 0개짜리
    규정이라 위험하다.
    """


# ── 형식 판별 ──────────────────────────────────────────────────────
def _hwp_ole_kind(path: Path) -> str | None:
    """OLE2 파일이 진짜 HWP v5 인지, 배포용/암호화라 본문이 막혀 있는지 확인한다.

    FileHeader 스트림이 없거나 서명이 다르면(구형 .doc 등 HWP 아닌 OLE2) None.
    오프셋 36:40 의 32비트 속성 플래그는 `hwp_extract.is_compressed()` 와 같은
    자리 — HWP 5.0 스펙: bit0 압축, bit1 암호화, bit2 배포용 문서(DRM).
    """
    import olefile

    try:
        ole = olefile.OleFileIO(str(path))
    except Exception:
        return None
    try:
        names = {"/".join(s) for s in ole.listdir()}
        if "FileHeader" not in names:
            return None
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            return None
        if len(header) < 40:
            return "HWP-PROTECTED: 손상된 FileHeader (속성 플래그 없음) — 본문 추출 불가"
        (flags,) = struct.unpack("<I", header[36:40])
        if flags & (1 << 1):
            return "HWP-PROTECTED: 암호화 문서(비밀번호 보호) — 본문 추출 불가"
        if flags & (1 << 2):
            return "HWP-PROTECTED: 배포용 문서(뷰어 전용) — 본문 추출 불가"
        return "OLE2 (HWP v5)"
    finally:
        ole.close()


def sniff(path: str | Path) -> str:
    """파일의 실제 형식을 판별해 문자열로 돌려준다. 'hwpx' 면 진짜다.

    zip 이면 내용물로, zip 이 아니면 매직 바이트로 판정한다.
    OLE2 이면서 진짜 HWP 인 경우 배포용/암호화 여부까지 연다(`_hwp_ole_kind`) —
    '본문이 없다'는 파싱 실패가 아니라 파일 형식의 속성이라 여기서 판별한다.
    """
    p = Path(path)
    if not zipfile.is_zipfile(p):
        head = p.open("rb").read(8)
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            return _hwp_ole_kind(p) or "OLE2 (HWP v5 또는 MS Office 97-2003)"
        if head.startswith(b"%PDF"):
            return "PDF"
        if head.lstrip().startswith(b"<?xml"):
            return "XML (HWPML 등)"
        return "zip 아님 (알 수 없는 형식)"
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


# ── 본문 순회 ──────────────────────────────────────────────────────
def _t_text(t: ET.Element) -> str:
    """hp:t — 텍스트와 그 안의 인라인 제어(lineBreak·tab·fwSpace) 를 순서대로."""
    out = [t.text or ""]
    for c in t:
        out.append(_INLINE.get(c.tag, ""))
        out.append(c.tail or "")
    return "".join(out)


def _walk_para(p: ET.Element, lines: list[str]) -> None:
    """hp:p 하나 → 줄 목록에 추가. 문단 안에 표가 끼면 표 앞뒤 텍스트를 나눠 낸다."""
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
        # 도형·글상자·각주 등 — 안에 subList 가 있으면 그 문단들을 추출한다
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
    for p in root.findall(_P):               # 최상위 문단만. 표 안 문단은 _walk_table 이 처리
        _walk_para(p, lines)
    return lines


# ── 공개 API ───────────────────────────────────────────────────────
def extract(path: str | Path) -> str:
    """hwpx → 평문. 문단·표 셀 경계는 줄바꿈. hwpx 가 아니면 NotHwpxError."""
    p = Path(path)
    kind = sniff(p)
    if kind != "hwpx":
        raise NotHwpxError(f"hwpx 아님 — 판별 결과: {kind} ({p.name})")
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
