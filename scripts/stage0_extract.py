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


# ── XML (L1 법령·행정규칙) ────────────────────────────────────────
# 행정규칙(AdmRulService)은 <조문단위> 구조가 없다. <조문내용> 평문이 조 단위로
# 나열될 뿐이다. 법령 파서를 그대로 돌리면 빈 리스트가 나오고 문서가 통째로
# 유실되므로 루트 태그를 보고 갈라야 한다.
_RE_ADM_JO = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*(?:\(([^)]*)\))?")
_RE_ADM_HEAD = re.compile(r"^제\s*\d+\s*[장절관편]\b")


def _extract_admrul(tree) -> list[dict]:
    """행정규칙 XML → 조 단위. <조문내용> 하나가 이미 조 하나다."""
    out = []
    for n in tree.findall(".//조문내용"):
        본문 = (n.text or "").strip()
        if not 본문 or _RE_ADM_HEAD.match(본문):   # '제1장 총칙' 등 편제 헤더
            continue
        m = _RE_ADM_JO.match(본문)
        if not m:
            continue
        번호, 가지, 제목 = m.group(1), m.group(2), (m.group(3) or "").strip()
        조번호 = f"제{번호}조" + (f"의{가지}" if 가지 else "")
        out.append({"조번호": 조번호, "조제목": 제목, "본문": 본문, "페이지": None})
    return out


def extract_xml(path: Path) -> list[dict]:
    """국가법령정보 DRF XML → 조 단위 리스트. 이미 구조화되어 있어 재조립 불필요."""
    from lxml import etree

    tree = etree.parse(str(path))
    out = []
    if tree.getroot().tag == "AdmRulService" or not tree.findall(".//조문단위"):
        out.extend(_extract_admrul(tree))
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

    # 별표는 조문단위 바깥에 있다. 인용 대상이므로(공무원 여비 규정 별표1 제2호,
    # 청탁금지법 시행령 별표1 가액 범위) 의사 조문으로 만들어 같이 넘긴다.
    for b in tree.findall(".//별표단위"):
        내용 = (b.findtext("별표내용") or "").strip()
        if not 내용:
            continue
        번호 = (b.findtext("별표번호") or "").strip().lstrip("0") or "1"
        가지 = (b.findtext("별표가지번호") or "").strip().lstrip("0")
        구분 = (b.findtext("별표구분") or "별표").strip()
        조번호 = f"{구분}{번호}" + (f"의{가지}" if 가지 else "")
        out.append({
            "조번호": 조번호,
            "조제목": (b.findtext("별표제목") or "").strip(),
            "본문": 내용,
            "페이지": None,
        })
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

    ⚠️ pdfplumber 를 기본으로 쓴다. pypdf 는 실패 시 폴백일 뿐이다.

    실측 (L2 통합관리지침 제14차, 55p):
        pypdf        2.7초  →  조 12개, 제목 0개   ← 쓸 수 없음
        pdfplumber  12.8초  →  조 83개, 제목 83개  ← 정상

    pypdf 는 한국어 PDF 에서 쉼표·괄호·숫자를 문장 끝으로 밀어내고
    조문 헤더를 깨뜨린다. 인용 원문이 곧 제품 품질(§원칙 4)이므로
    문서당 10초를 아끼려고 정확도를 포기할 이유가 없다.
    """
    try:
        return _pdf_plumber(path)
    except Exception:
        return _pdf_pypdf(path)


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
