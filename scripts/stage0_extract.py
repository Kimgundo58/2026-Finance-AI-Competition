# -*- coding: utf-8 -*-
"""Stage 0-a~d : 포맷별 원문 추출.

XML  → 조문 구조 그대로 (품질 최상)
PDF  → pdfplumber
HWP  → hwp_extract.py 재사용 (OLE v5) · HWPML(XML) 은 lxml
HWPX → hwpx_extract.py (zip). 확장자가 아니라 **내용물**로 갈라 보낸다
DOCX → docx_extract.py (zip, python-docx). hwpx 와 같은 sniff() 로 내용물 재검증
TXT  → 그대로
"""
from __future__ import annotations
import logging, os, re, sys, zlib, struct
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

    ⚠️ pdfplumber 를 기본으로 쓴다. pypdf 는 실패 시 대체 경로일 뿐이다.

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


def _libreoffice_docx(path: Path) -> Path | None:
    """구형 `.hwp`(OLE) 를 LibreOffice+H2Orestart 로 `.docx` 변환한다. 실패하면 `None`.

    `hwp_extract.py` 는 PARA_TEXT 레코드만 훑어 표 구조가 없다. LibreOffice 로
    docx 를 거치면 `docx_extract._walk_table()` 의 표 걷기를 그대로 태울 수 있다
    (L3_시연적재_안내.md §5-3, 검증됨). 실패는 예외로 올리지 않고 `None` 을 돌려준다 —
    호출부(`extract_hwp`)가 기존 `hwp_extract` 경로로 폴백한다. 지금 동작을 잃지 않는다.

    🔴 **ASCII 임시 파일명으로 복사한 뒤 변환한다.** 한글 파일명은 H2Orestart(Java)
       인코딩 문제로 깨진다(§5-4①: `FileNotFoundException: /tmp/L3_???…hwp`).
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
                _로그.warning(
                    "LibreOffice 변환 실패(%s): rc=%s stderr=%s",
                    path.name, proc.returncode, (proc.stderr or b"")[:300],
                )
                return None
            # tmp 디렉터리가 with 종료 시 지워지므로, 그 전에 영속 위치로 옮겨 돌려준다.
            keep_dir = Path(tempfile.mkdtemp(prefix="hwp2docx_out_"))
            keep = keep_dir / out.name
            shutil.copy(out, keep)
            return keep
    except Exception as e:
        _로그.warning("LibreOffice 변환 예외(%s): %s", path.name, e)
        return None


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


def _route_zip_office(path: Path) -> str:
    """PK(zip) 로 시작하는 hwpx/docx 후보 — 확장자가 아니라 `sniff()` 로 실제 파서를 고른다.

    실측(2026-09-02): hwpx 확장자에 docx 가, docx 확장자에 hwpx 가 들어와도
    내용물대로 가야 한다(둘 다 zip 이라 PK 매직만으로는 못 가른다). 그 밖(XLSX·PPTX·
    ODF·정체불명)은 `hwpx_extract` 의 NotHwpxError 로 정체를 담아 실패한다 —
    `docx_extract` 도 같은 `sniff()` 를 쓰므로 어느 쪽에서 걸려도 정체 메시지는 같다.
    """
    from hwpx_extract import sniff

    kind = sniff(path)
    if kind.startswith("DOCX"):
        from docx_extract import extract as extract_docx

        return extract_docx(path)

    from hwpx_extract import extract as extract_hwpx

    return extract_hwpx(path)


def extract_hwp(path: Path) -> str:
    """`.hwp` / `.hwpx` 공용. 🔴 확장자를 믿지 않고 앞 바이트로 갈라 보낸다.

    실측(2026-09-02): TIPS 운영사 현황(2026년).hwpx 는 확장자만 hwpx 고 내부는
    XLSX 였다. 확장자로 보내면 OLE 파서가 NotOleFileError 로 죽고 정체를 못 밝힌다.
      PK     → zip → 내용물로 hwpx/docx 를 가른다(`_route_zip_office`)
      D0 CF 11 E0     → OLE  → 배포용/암호화면 HwpProtectedError, 아니면 hwp_extract
      <?xml           → HWPML
      그 밖           → 그대로 hwp_extract 로 보내 그쪽 예외를 살린다

    🔴 실측(2026-09-02): TIPS 총괄 운영지침 「본문」 hwp 3개년치는 `hwp_extract.extract()`
    가 예외 없이 성공 반환하는데 실제로 뽑히는 건 뷰어 안내문 106~107자뿐이다
    (배포용 문서, 조 0개). 조용히 성공한 빈 규정이 판정에 들어가는 걸 막으려면
    `hwp_extract.extract()` 를 부르기 **전에** 배포용/암호화 여부를 확인해야 한다 —
    파싱이 끝난 뒤 글자수로 되짚는 게 아니라, 정체 판정 단계에서 막는다.
    """
    head = path.open("rb").read(16)
    if head.startswith(b"PK"):
        return _route_zip_office(path)
    if head.lstrip()[:5] == b"<?xml":
        return _extract_hwpml(path)

    from hwpx_extract import sniff, HwpProtectedError

    kind = sniff(path)
    if kind.startswith("HWP-DRM"):
        raise HwpProtectedError(f"{kind} ({path.name})")

    # 🔴 구형 .hwp(OLE) 는 표 구조가 없다(hwp_extract 는 PARA_TEXT 만 훑는다).
    #    LibreOffice+H2Orestart 로 .docx 를 거쳐 docx_extract 의 표 걷기를 태운다.
    #    실패하면(LibreOffice 미설치·변환 오류·재파싱 실패) 기존 경로로 폴백한다 —
    #    지금 동작을 절대 잃지 않는다. 검증: scratchpad/인H_hwp갈래.md
    변환됨 = _libreoffice_docx(path)
    if 변환됨:
        try:
            return extract_docx(변환됨)
        except Exception as e:
            _로그.warning("LibreOffice 변환본 파싱 실패(%s) — 기존 경로로 폴백: %s", path.name, e)

    from hwp_extract import extract

    return extract(str(path))


def extract_docx(path: Path) -> str:
    """`.docx` 진입점. 🔴 확장자를 믿지 않고 내용으로 재검증한다.

    zip 이 아니면(오래된 `.doc` OLE2 등) 파서가 없다 — `docx_extract` 자신의
    `sniff()` 가 정체를 담아 NotDocxError 로 실패시킨다. `.doc` 전용 파서는
    만들지 않는다(제품 결정: 프론트 업로드 허용 목록에서 뺀다).
    """
    head = path.open("rb").read(4)
    if head.startswith(b"PK"):
        return _route_zip_office(path)

    from docx_extract import extract as extract_docx_impl

    return extract_docx_impl(path)


# ── 빈 추출 게이트 ───────────────────────────────────────────────────
# 실측(2026-09-02, `2026_Finance_DATA_FOR_RAG/` 전수 43개 hwp 원문 — hwp_extract 직접 호출,
# 배포용 게이트 적용 전 원시값):
#   배포용 문서(DRM) 8건 전부 정확히 106~107자 · 조 0개
#   정상 문서 35건 중 가장 짧은 것도 430자("재도전성공패키지 주관기관 현황") — 겹치지 않는다
# 106~429자 구간 어디든 안전해 200자를 컷오프로 둔다(양쪽에 100자 이상 여유).
빈_추출_글자수_임계치 = 200


def 추출_품질_점검(text: str) -> dict:
    """추출된 본문이 「성공했지만 사실상 비었다」인지 **값으로** 돌려준다 — 던지지 않는다.

    🔴 정체가 이미 밝혀진 실패(hwpx/docx 위장, hwp 배포용/암호화)는 `extract_*()` 단계에서
    이미 예외로 막는다. 이 함수는 그다음 층 — 정체 판정을 통과했는데도 원인 불명으로 짧게
    나온 추출(예: hwpx 의 동일 계열 문제, 스캔 판독 실패, 아직 모르는 새 실패 유형)을
    호출부가 「판단불가」로 접을 수 있게 값을 준다.

    🔴 조수(=0)만으로 판단불가를 트리거하지 않는다 — 실측에 「TIPS 운영사 현황」·
    「~ 주관기관 현황」처럼 조가 원래 없는 정상 문서(목록·현황표류)가 다수 있다
    (430~25,359자, 조 0개 전부 정상). 글자수 임계치만 하드 게이트로 쓰고 조수는
    참고 값으로 같이 돌려준다 — 규정류만 받아야 하는 호출부(예: L3 업로드)는
    이 값을 보고 그쪽 기준(조 0개 거부 등)을 따로 적용해라.
    """
    글자수 = len(text)
    조수 = len(re.findall(r"제\s*\d+\s*조", text))
    판단불가 = 글자수 < 빈_추출_글자수_임계치
    return {
        "글자수": 글자수,
        "조수": 조수,
        "판단불가": 판단불가,
        "사유": f"본문 {글자수}자 — 임계치 {빈_추출_글자수_임계치}자 미만" if 판단불가 else "",
    }


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
_로그 = logging.getLogger(__name__)

_대리_시작, _대리_끝 = 0xD800, 0xDFFF
_상위_끝, _하위_시작 = 0xDBFF, 0xDC00
_치환문자 = chr(0xFFFD)


def 서러게이트_정리(text: str, 라벨: str = "") -> str:
    """🔴 **UTF-16 대리(surrogate) 쌍을 «합치고», 못 합친 고아는 U+FFFD 로 치환한다.**

    실측(2026-09-03 · 경상국립대 사업비사용안내문.hwp): 추출문에 U+DB80 U+DC7E 쌍이
    **결합되지 않은 채** 22개(11쌍) 남는다. 실제 글자는 PUA U+F007E 인데 파이썬
    문자열에는 «대리 코드 2개» 로 들어와 있어 **UTF-8 인코딩이 불가능하다.**
    그대로 DB 로 가면 psycopg 가 UnicodeEncodeError 로 죽는다 — 실제로 죽었다.

    🔴 그때 안 죽은 건 «우연» 이다. 그 단락들이 100자 미만이라 `split_articles` 의
       단락 필터에 걸려 버려졌을 뿐이고, **그 필터를 고치면 바로 발현한다.**
       그래서 이 정리가 «먼저» 있어야 한다.

    🔴 **조용히 지우지 않는다.** 고아는 삭제가 아니라 U+FFFD 치환이고, 몇 개를
       어떻게 했는지 로그에 남긴다. 글자가 사라진 걸 아무도 모르는 게 이 프로젝트가
       반복해 데인 모양이다.

    ⚠️ 정규식을 안 쓴다 — 패턴에 대리 문자를 «적어» 두려면 소스 파일 자체가 그 문자를
       품어야 하는데, 그러면 이 파일이 UTF-8 로 저장이 안 된다(한 번 밟았다).
    """
    if not text:
        return text
    if not any(_대리_시작 <= ord(c) <= _대리_끝 for c in text):
        return text
    나온, 쌍수, 고아수 = [], 0, 0
    i, n = 0, len(text)
    while i < n:
        o = ord(text[i])
        if (_대리_시작 <= o <= _상위_끝 and i + 1 < n
                and _하위_시작 <= ord(text[i + 1]) <= _대리_끝):
            나온.append(chr(0x10000 + ((o - _대리_시작) << 10)
                           + (ord(text[i + 1]) - _하위_시작)))
            쌍수 += 1
            i += 2
            continue
        if _대리_시작 <= o <= _대리_끝:
            나온.append(_치환문자)          # 🔴 버리지 않는다 — 자리를 남긴다
            고아수 += 1
        else:
            나온.append(text[i])
        i += 1
    _로그.warning("서러게이트 정리%s — 쌍 결합 %d개 · 고아 U+FFFD 치환 %d개",
                  f"({라벨})" if 라벨 else "", 쌍수, 고아수)
    return "".join(나온)


def extract(path: Path):
    """반환: ('articles', list) 또는 ('text', (str, page_offsets))

    🔴 **모든 포맷이 이 한 곳으로 나간다.** 그래서 서러게이트 정리도 여기 «한 번만»
       둔다 — 포맷별 추출기마다 흩어 두면 새 포맷이 하나 늘 때 조용히 빠진다.
    """
    ext = path.suffix.lower()
    라벨 = path.name
    if ext == ".xml":
        arts = extract_xml(path)
        for a in arts:                       # 조문 구조 경로도 같은 위험을 진다
            a["본문"] = 서러게이트_정리(a["본문"], 라벨)
            if a.get("조제목"):
                a["조제목"] = 서러게이트_정리(a["조제목"], 라벨)
        return "articles", arts
    if ext == ".pdf":
        본문, offs = extract_pdf(path)
        return "text", (서러게이트_정리(본문, 라벨), offs)
    if ext in (".hwp", ".hwpx"):
        return "text", (서러게이트_정리(extract_hwp(path), 라벨), {})
    if ext == ".docx":
        return "text", (서러게이트_정리(extract_docx(path), 라벨), {})
    if ext == ".txt":
        return "text", (서러게이트_정리(extract_txt(path), 라벨), {})
    raise ValueError(f"지원하지 않는 형식: {ext}")
