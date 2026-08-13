# -*- coding: utf-8 -*-
"""Stage 0-a~d : 포맷별 원문 추출.

XML  → 조문 구조 그대로 (품질 최상)
PDF  → pdfplumber
HWP  → hwp_extract.py 재사용
TXT  → 그대로
"""
from __future__ import annotations
import os, re, sys, zlib, struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── XML (L1 법령) ────────────────────────────────────────────────
def extract_xml(path: Path) -> list[dict]:
    """국가법령정보 DRF XML → 조 단위 리스트. 이미 구조화되어 있어 재조립 불필요."""
    from lxml import etree

    tree = etree.parse(str(path))
    out = []
    for u in tree.findall(".//조문단위"):
        여부 = (u.findtext("조문여부") or "").strip()
        if 여부 != "조문":          # '전문' = 장 제목 등, 조가 아님
            continue
        번호 = (u.findtext("조문번호") or "").strip()
        가지 = (u.findtext("조문가지번호") or "").strip()
        제목 = (u.findtext("조문제목") or "").strip()
        조번호 = f"제{번호}조" + (f"의{가지}" if 가지 else "")

        parts = [(u.findtext("조문내용") or "").strip()]
        for 항 in u.findall("항"):
            t = (항.findtext("항내용") or "").strip()
            if t:
                parts.append(t)
            for 호 in 항.findall("호"):
                t = (호.findtext("호내용") or "").strip()
                if t:
                    parts.append(t)
                for 목 in 호.findall("목"):
                    t = (목.findtext("목내용") or "").strip()
                    if t:
                        parts.append(t)
        for 호 in u.findall("호"):        # 항 없이 호가 바로 붙는 경우
            t = (호.findtext("호내용") or "").strip()
            if t:
                parts.append(t)

        본문 = "\n".join(p for p in parts if p)
        if 본문:
            out.append({"조번호": 조번호, "조제목": 제목, "본문": 본문, "페이지": None})
    return out


# ── PDF ──────────────────────────────────────────────────────────
_RE_JO_COUNT = re.compile(r"제\s*\d+\s*조\s*\(")


def _pdf_pypdf(path: Path) -> tuple[str, dict[int, int]]:
    """빠른 경로. 단일 컬럼 문서면 이걸로 충분하다."""
    import logging, warnings
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore")
    parts, offsets, pos = [], {}, 0
    reader = PdfReader(str(path))
    for i, page in enumerate(reader.pages, 1):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        offsets[pos] = i
        parts.append(t)
        pos += len(t) + 1
    return "\n".join(parts), offsets


def _pdf_plumber(path: Path) -> tuple[str, dict[int, int]]:
    """느린 경로. 다단 레이아웃 등 pypdf 가 실패할 때만."""
    import pdfplumber

    parts, offsets, pos = [], {}, 0
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            t = page.extract_text() or ""
            offsets[pos] = i
            parts.append(t)
            pos += len(t) + 1
    return "\n".join(parts), offsets


def extract_pdf(path: Path) -> tuple[str, dict[int, int]]:
    """PDF → 평문 + {문자오프셋: 페이지번호}.

    pypdf 로 먼저 뽑고, 조문 구조가 안 잡히면 pdfplumber 로 재시도한다.
    (pdfplumber 는 10배 이상 느리므로 필요한 문서에만 쓴다)
    """
    text, offsets = _pdf_pypdf(path)
    if len(_RE_JO_COUNT.findall(text)) >= 5 or len(text) > 3000:
        return text, offsets
    try:
        return _pdf_plumber(path)
    except Exception:
        return text, offsets


# ── HWP ──────────────────────────────────────────────────────────
def _extract_hwpml(path: Path) -> str:
    """확장자는 .hwp 지만 실제 내용이 HWPML(XML) 인 파일.
    (예: 국가법령정보센터에서 내려받은 서울대 규정)"""
    from lxml import etree

    tree = etree.parse(str(path))
    lines = []
    for p in tree.getroot().iter("P"):
        t = "".join(x for x in p.itertext())
        t = t.strip()
        if t:
            lines.append(t)
    return "\n".join(lines)


def extract_hwp(path: Path) -> str:
    head = path.open("rb").read(16)
    if head.lstrip()[:5] == b"<?xml":
        return _extract_hwpml(path)

    from hwp_extract import extract

    return extract(str(path))


# ── TXT ──────────────────────────────────────────────────────────
def extract_txt(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp949", "euc-kr", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ── 디스패처 ─────────────────────────────────────────────────────
def extract(path: Path):
    """반환: ('articles', list) 또는 ('text', (str, page_offsets))"""
    ext = path.suffix.lower()
    if ext == ".xml":
        return "articles", extract_xml(path)
    if ext == ".pdf":
        return "text", extract_pdf(path)
    if ext in (".hwp", ".hwpx"):
        return "text", (extract_hwp(path), {})
    if ext == ".txt":
        return "text", (extract_txt(path), {})
    raise ValueError(f"지원하지 않는 형식: {ext}")
