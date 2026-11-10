# -*- coding: utf-8 -*-
"""규정 원본 파일에서 텍스트(또는 법령 XML 의 조문 구조)를 추출한다.

l3_parse(L3 업로드)와 코퍼스 적재가 부른다. 공개 함수: extract(path) ->
('articles', [조문 dict]) 또는 ('text', (본문, {문자오프셋: 페이지번호})), 빈_추출_글자수_임계치.
형식은 확장자가 아니라 파일 앞 바이트로 판정한다: %PDF → pdftext, OLE → HWP v5,
ZIP → HWPX/DOCX(내용물로 구분), <?xml → 법령 XML 또는 HWPML, 그 밖의 .txt → 평문.
불변 조건: 모든 형식의 출력은 extract() 한 곳에서 서로게이트 정리를 거친다.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

_log = logging.getLogger(__name__)


# ── XML (L1 법령·행정규칙) ────────────────────────────────────────
# 행정규칙(AdmRulService)은 <조문단위> 구조가 없고 <조문내용> 평문이 조 단위로
# 나열된다. 법령 파서를 그대로 돌리면 빈 리스트가 나오므로 루트 태그를 보고 나눠 처리한다.
_RE_행정규칙_조 = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*(?:\(([^)]*)\))?")
_RE_ADM_HEAD = re.compile(r"^제\s*\d+\s*[장절관편]\b")


def _extract_admrul(tree) -> list[dict]:
    """행정규칙 XML → 조 단위. <조문내용> 하나가 이미 조 하나다."""
    out = []
    for n in tree.findall(".//조문내용"):
        본문 = (n.text or "").strip()
        if not 본문 or _RE_ADM_HEAD.match(본문):   # '제1장 총칙' 등 편제 헤더
            continue
        m = _RE_행정규칙_조.match(본문)
        if not m:
            continue
        번호, 가지, 제목 = m.group(1), m.group(2), (m.group(3) or "").strip()
        조번호 = f"제{번호}조" + (f"의{가지}" if 가지 else "")
        out.append({"조번호": 조번호, "조제목": 제목, "본문": 본문, "페이지": None})
    return out


def extract_xml(path: Path) -> list[dict]:
    """국가법령정보 DRF XML → 조 단위 리스트. 이미 조 단위로 구조화되어 있어 조 경계를 다시 찾지 않는다."""
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

    # 별표는 조문단위 바깥에 있다. 인용 대상이므로 조문 형식의 항목으로 만들어 같이 넘긴다.
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


def _xml_루트(path: Path) -> str:
    """XML 루트 태그 이름. 법령 XML 과 HWPML 을 나누는 데만 쓴다."""
    from lxml import etree

    # 첫 start 이벤트에서 멈춘다. 파서가 연 파일은 close() 로 바로 닫는다(Windows 에서 파일 잠금이 남지 않게).
    it = etree.iterparse(str(path), events=("start",))
    try:
        for _, el in it:
            return el.tag
        return ""
    finally:
        close = getattr(it, "close", None)
        if close:
            close()


# ── PDF ──────────────────────────────────────────────────────────
def _pdf_pypdf(path: Path) -> tuple[str, dict[int, int]]:
    """pypdf 경로. pdftext 가 예외를 낼 때만 쓰는 폴백이다."""
    import warnings
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


def _pdf_pdftext(path: Path) -> tuple[str, dict[int, int]]:
    """pdftext 경로. 문자중복 레이어와 2단·4분면 조판을 처리한 페이지별 본문을 쓴다."""
    import pdftext

    parts, offsets, pos = [], {}, 0
    for i, t in enumerate(pdftext.extract_pages(str(path)), 1):
        offsets[pos] = i
        parts.append(t)
        pos += len(t) + 1
    return "\n".join(parts), offsets


def extract_pdf(path: Path) -> tuple[str, dict[int, int]]:
    """PDF → 평문 + {문자오프셋: 페이지번호}.

    pdftext(pdfplumber 기반)를 기본으로 쓴다. pypdf 는 pdftext 가 예외를 낼 때만
    쓰는 폴백이다 — 한국어 PDF 에서 쉼표·괄호·숫자를 문장 끝으로 밀어내 조문 헤더를 깨뜨린다.
    """
    try:
        return _pdf_pdftext(path)
    except Exception:
        return _pdf_pypdf(path)


# ── HWP (OLE) ────────────────────────────────────────────────────
def _find_soffice() -> str | None:
    """LibreOffice `soffice` 실행 파일 경로. 없으면 None(호출부가 폴백한다)."""
    import shutil as _sh

    후보 = [
        os.environ.get("SOFFICE_BIN"),
        _sh.which("soffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
    ]
    for c in 후보:
        if c and Path(c).exists():
            return c
    return None


def _libreoffice_docx(path: Path) -> str | None:
    """구형 `.hwp`(OLE) 를 LibreOffice+H2Orestart 로 `.docx` 변환해 본문을 돌려준다. 실패하면 `None`.

    `hwp_extract.py` 는 PARA_TEXT 레코드만 읽어 표 구조가 없다. LibreOffice 로
    docx 를 거치면 `docx_extract._walk_table()` 의 표 처리를 그대로 쓸 수 있다.
    실패는 예외로 올리지 않고 `None` 을 돌려준다 — 호출부(`_extract_hwp_ole`)가
    `hwp_extract` 경로로 폴백한다. 변환본은 임시 디렉터리 안에서 읽고 함께 지운다.

    ASCII 임시 파일명으로 복사한 뒤 변환한다 — 한글 파일명은 H2Orestart(Java)
    인코딩 문제로 깨진다.
    """
    import shutil, subprocess, tempfile, uuid

    soffice = _find_soffice()
    if not soffice:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="hwp2docx_") as tmp_s:
            tmp = Path(tmp_s)
            src = tmp / f"{uuid.uuid4().hex}.hwp"
            shutil.copy(path, src)
            proc = subprocess.run(
                [soffice, "--headless", "--norestore", "--convert-to", "docx",
                 "--outdir", str(tmp), str(src)],
                capture_output=True, timeout=180,
            )
            out = src.with_suffix(".docx")
            if proc.returncode != 0 or not out.exists():
                _log.warning(
                    "LibreOffice 변환 실패(%s): rc=%s stderr=%s",
                    path.name, proc.returncode, (proc.stderr or b"")[:300],
                )
                return None
            try:
                return _route_zip_office(out)
            except Exception as e:
                _log.warning("LibreOffice 변환본 파싱 실패(%s) — 기존 경로로 폴백: %s", path.name, e)
                return None
    except Exception as e:
        _log.warning("LibreOffice 변환 예외(%s): %s", path.name, e)
        return None


def _extract_hwp_ole(path: Path) -> str:
    """OLE 복합문서 → 본문. 배포용/암호화 HWP 는 HwpProtectedError.

    배포용/암호화 문서는 `hwp_extract.extract()` 가 예외 없이 뷰어 안내문뿐인 빈 본문을
    내므로, 파싱 후 글자수로 거르지 않고 형식 판별 단계에서 먼저 막는다.
    """
    from hwpx_extract import sniff, HwpProtectedError

    kind = sniff(path)
    if kind.startswith("HWP-PROTECTED"):
        raise HwpProtectedError(f"{kind} ({path.name})")

    # hwp_extract 는 표 구조가 없으므로 LibreOffice 변환을 먼저 시도하고,
    # 실패하면(미설치·변환 오류·재파싱 실패) hwp_extract 로 폴백한다.
    변환본문 = _libreoffice_docx(path)
    if 변환본문 is not None:
        return 변환본문

    from hwp_extract import extract

    return extract(str(path))


def _extract_hwpml(path: Path) -> str:
    """내용이 HWPML(XML) 인 파일(예: 국가법령정보센터에서 내려받은 .hwp 규정)."""
    from lxml import etree

    tree = etree.parse(str(path))
    lines = []
    for p in tree.getroot().iter("P"):
        t = "".join(x for x in p.itertext())
        t = t.strip()
        if t:
            lines.append(t)
    return "\n".join(lines)


# ── ZIP (HWPX / DOCX) ────────────────────────────────────────────
def _route_zip_office(path: Path) -> str:
    """zip 파일을 `sniff()` 로 확인해 hwpx 또는 docx 파서로 보낸다.

    둘 다 zip 이라 매직 바이트만으로는 구분하지 못한다. 그 밖(XLSX·PPTX·ODF·알 수 없는 형식)은
    `hwpx_extract` 의 NotHwpxError 에 판별 결과를 담아 실패한다.
    """
    from hwpx_extract import sniff

    kind = sniff(path)
    if kind.startswith("DOCX"):
        from docx_extract import extract as docx_추출

        return docx_추출(path)

    from hwpx_extract import extract as hwpx_추출

    return hwpx_추출(path)


# ── TXT ──────────────────────────────────────────────────────────
def extract_txt(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp949", "euc-kr", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ── 최소 추출 글자수 검사 ─────────────────────────────────────────────
# 배포용(DRM) 문서는 추출해도 뷰어 안내문 100자 안팎만 나온다. 정상 문서와
# 겹치지 않도록 기준을 200자로 둔다.
빈_추출_글자수_임계치 = 200


# ── 서로게이트 정리 ──────────────────────────────────────────────────
_서로게이트_시작, _서로게이트_끝 = 0xD800, 0xDFFF
_상위서로게이트_끝, _하위서로게이트_시작 = 0xDBFF, 0xDC00
_치환문자 = chr(0xFFFD)


def 서로게이트_정리(text: str, 라벨: str = "") -> str:
    """UTF-16 서로게이트(surrogate) 쌍을 합치고, 못 합친 짝 없는 서로게이트는 U+FFFD 로 치환한다.

    결합되지 않은 서로게이트 쌍은 파이썬 문자열에 서로게이트 코드 2개로 남아 UTF-8
    인코딩이 불가능하다 — 그대로 DB 로 가면 psycopg 가 UnicodeEncodeError 를 낸다.

    짝 없는 서로게이트는 지우지 않고 U+FFFD 로 치환해 자리를 남기며, 몇 개를 처리했는지 로그에 남긴다.

    정규식을 쓰지 않는다 — 패턴에 서로게이트 문자를 적으려면 이 소스 파일이 그 문자를
    포함해야 하는데, 그러면 UTF-8 로 저장할 수 없다.
    """
    if not text:
        return text
    if not any(_서로게이트_시작 <= ord(c) <= _서로게이트_끝 for c in text):
        return text
    결과, 쌍수, 짝없음수 = [], 0, 0
    i, n = 0, len(text)
    while i < n:
        o = ord(text[i])
        if (_서로게이트_시작 <= o <= _상위서로게이트_끝 and i + 1 < n
                and _하위서로게이트_시작 <= ord(text[i + 1]) <= _서로게이트_끝):
            결과.append(chr(0x10000 + ((o - _서로게이트_시작) << 10)
                           + (ord(text[i + 1]) - _하위서로게이트_시작)))
            쌍수 += 1
            i += 2
            continue
        if _서로게이트_시작 <= o <= _서로게이트_끝:
            결과.append(_치환문자)          # 버리지 않는다 — 자리를 남긴다
            짝없음수 += 1
        else:
            결과.append(text[i])
        i += 1
    _log.warning("서로게이트 정리%s — 쌍 결합 %d개 · 짝 없는 서로게이트 U+FFFD 치환 %d개",
                  f"({라벨})" if 라벨 else "", 쌍수, 짝없음수)
    return "".join(결과)


# ── 디스패처 ─────────────────────────────────────────────────────
def _형식(path: Path) -> str:
    """파일 앞 바이트로 형식을 판정한다. 판정할 수 없고 .txt 도 아니면 ValueError."""
    with path.open("rb") as f:
        head = f.read(16)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "hwp"
    if head.startswith(b"PK"):
        return "zip"
    if head.lstrip(b"\xef\xbb\xbf \t\r\n")[:5] == b"<?xml":
        return "hwpml" if _xml_루트(path) == "HWPML" else "xml"
    if path.suffix.lower() == ".txt":
        return "txt"
    raise ValueError(f"지원하지 않는 형식: {path.suffix.lower()}")


# 평문을 내는 형식별 추출기. 페이지 오프셋은 PDF 만 있다.
_텍스트_추출기 = {
    "hwp": _extract_hwp_ole,
    "zip": _route_zip_office,
    "hwpml": _extract_hwpml,
    "txt": extract_txt,
}


def extract(path: Path):
    """반환: ('articles', list) 또는 ('text', (str, page_offsets)).

    모든 형식이 이 한 곳으로 나간다. 서로게이트 정리도 여기 한 번만 둔다 —
    형식별 추출기마다 흩어 두면 새 형식이 하나 늘 때 빠뜨리기 쉽다.
    """
    라벨 = path.name
    형식 = _형식(path)
    if 형식 == "xml":
        arts = extract_xml(path)
        for a in arts:                       # 조문 구조 경로도 같은 위험이 있다
            a["본문"] = 서로게이트_정리(a["본문"], 라벨)
            if a.get("조제목"):
                a["조제목"] = 서로게이트_정리(a["조제목"], 라벨)
        return "articles", arts
    if 형식 == "pdf":
        본문, offs = extract_pdf(path)
        return "text", (서로게이트_정리(본문, 라벨), offs)
    return "text", (서로게이트_정리(_텍스트_추출기[형식](path), 라벨), {})
