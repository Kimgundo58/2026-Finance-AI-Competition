# -*- coding: utf-8 -*-
"""스캔 PDF 판독 — GCP Document AI 버전. `vlm_extract.py` 와 **같은 계약**을 지킨다.

🔴 왜 필요한가 (2026-09-06 오너 지시로 착수, 레인 D):
    `vlm_extract.py`(Anthropic 비전 API)는 이미 있고 게이트도 있지만, 기관 문서를
    «외부»(Anthropic)로 내보낸다. Document AI 는 같은 GCP 프로젝트(project-35d896d7-
    67d7-4b2a-a8f) 안에서 돈다 — 이미 결제·Cloud Run·Cloud SQL 이 거기 있다. 판독기를
    이걸로 바꿔 끼울 수 있게, «같은 계약» 으로 새 모듈을 만든다.

━━ 계약 — `vlm_extract.extract()` 와 글자 하나까지 같다 ━━━━━━━━━━━━━━━━━━━━━
    `extract(path)` -> (본문: str, 페이지오프셋: dict[int,int])
    호출부(`l3_parse.py`)는 이 계약만 보고 두 모듈 중 하나를 고른다 — 코드 두 벌을
    만들지 않는다. 페이지 판정(어느 페이지가 "글자가 없다"인지)·PNG 렌더링은
    `vlm_extract` 것을 그대로 재사용한다(새로 안 만든다 — 임계값이 둘로 갈리면
    다음 사람이 "50 이 맞나 60 이 맞나" 를 또 실측해야 한다).

━━ 표를 살리는 방법 — `table_splice.py` 와 «같은 직렬화 함수» 를 쓴다 ━━━━━━━━━
    `table_splice._마크다운_표()` 를 그대로 import 해서 쓴다. 파이프 마크다운
    형식이 한 글자라도 갈리면 `stage2_chunk.py::RE_박스표` 감지망이나 하류
    소비자가 깨진다 — 형식의 원본(source of truth)은 하나여야 한다.
    Document AI 응답의 `Table`(header_rows/body_rows, 셀마다 text_anchor) 을
    `list[list[str]]` 로 풀어서 그 함수에 그대로 넘긴다.

━━ 판독 불가 — 지어내지 않는다 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Document AI 는 토큰·문단마다 `layout.confidence`(0~1)를 준다. `vlm_extract`
    의 "[판독불가]" 프롬프트 지시와 같은 효과를 내려고, 문단 신뢰도가
    `PARAGRAPH_CONFIDENCE_MIN` 미만이면 그 문단 구간을 `ILLEGIBLE_MARKER` 로
    바꿔 넣는다(원문을 지어내지 않는다). `vlm_extract.ILLEGIBLE_MARKER` 를 그대로
    가져와 쓴다 — 마커 문자열이 두 판독기에서 다르면 하류(경고 카운트 등)가
    판독기별로 분기해야 한다.

━━ 게이트 둘 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    1) `SUDDOE_ALLOW_DOCAI=1` — `SUDDOE_ALLOW_EXTERNAL` 과 «같은 모양» 의 명시적
       스위치다. Document AI 는 같은 GCP 프로젝트 안에서 돌아 엄밀히는 "외부"가
       아니지만(기관 문서가 제3자 API로 안 나간다), 그래도 실비용이 드는 API 호출을
       사람이 켜고 끌 수 있어야 한다 — 조용히 나가면 안 된다는 원칙은 "외부냐 아니냐"
       와 별개다. 그래서 이름을 `SUDDOE_ALLOW_EXTERNAL` 과 다르게 뗐다(의미가 다르므로
       한 스위치로 묶으면 "외부로 나가는 것"과 "GCP 안에서 도는 것"을 못 구분한다).
    2) 설정 누락 — `SUDDOE_DOCAI_PROJECT`·`SUDDOE_DOCAI_LOCATION`·
       `SUDDOE_DOCAI_PROCESSOR_ID` 중 하나라도 없으면 `DocAIConfigMissing` 을
       명시적으로 던진다(조용히 빈 값으로 넘어가지 않는다 — 오늘 이 프로젝트가
       네 번 밟은 사고).

━━ 🔴 정확도 — 샘플이 없어 «못 잰다» ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    scratchpad/D_DocumentAI_설계.md 에 그대로 적었다. 이 파일의 self-test 는
    "API 를 안 불러도 되는 부분"(게이트·계약·마크다운 직렬화·저신뢰 마커 치환
    로직)만 검사한다. `documentai.googleapis.com` 이 꺼져 있고 프로세서도 없어서
    **실제 판독 정확도는 이 세션에서 측정할 방법이 없다** — 추정치를 적지 않는다.

실행 (자가검사는 API·SDK 설치 없이 돈다):
    PYTHONIOENCODING=utf-8 python scripts/docai_extract.py --selftest
    SUDDOE_ALLOW_DOCAI=1 SUDDOE_DOCAI_PROJECT=... SUDDOE_DOCAI_LOCATION=us \
        SUDDOE_DOCAI_PROCESSOR_ID=... \
        PYTHONIOENCODING=utf-8 python scripts/docai_extract.py --file <pdf> [--pages 1-3]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 🔴 페이지 판정·렌더링·판독불가 마커는 vlm_extract 것을 그대로 쓴다 — 새로 안 만든다.
#    (임계값 50자·DPI 200 이 vlm_extract 실측으로 정해진 값이라 여기서 다시 정하면 갈린다)
from vlm_extract import (  # noqa: E402
    MIN_CHARS_PER_PAGE,
    ILLEGIBLE_MARKER,
    페이지별_글자수,
    _렌더,
)
# 🔴 표 직렬화는 table_splice 것을 그대로 쓴다 — 파이프 마크다운 형식의 source of truth
from table_splice import _마크다운_표  # noqa: E402

NL = "\n"

# ── 상수 ─────────────────────────────────────────────────────────────────
PARAGRAPH_CONFIDENCE_MIN = 0.5   # 이 미만이면 원문 대신 ILLEGIBLE_MARKER — 🔴 실측 아님, 초안값
                                  # (오너/central 조정 가능. self-test 는 이 값이 쓰이는지만 검사)


class DocAINotAllowed(RuntimeError):
    """`SUDDOE_ALLOW_DOCAI=1` 이 없다. `vlm_extract.ExternalNotAllowed` 와 같은 모양 —
    다만 별개 스위치다(Document AI 는 같은 GCP 프로젝트 안, Anthropic 호출은 외부)."""


class DocAIConfigMissing(RuntimeError):
    """프로젝트·리전·프로세서ID 중 하나라도 없다. 조용히 빈 문자열로 넘어가지 않는다."""


class DocAICallFailed(RuntimeError):
    """API 호출 자체가 실패했다. 실패를 빈 문자열로 흘리지 않는다(vlm_extract.VLMCallFailed 짝)."""


# ══════════════════════════════════════════════════════════════════════════
# 1. 게이트 + 설정
# ══════════════════════════════════════════════════════════════════════════

def _허용됐나() -> None:
    if os.environ.get("SUDDOE_ALLOW_DOCAI") != "1":
        raise DocAINotAllowed(
            "SUDDOE_ALLOW_DOCAI=1 이 없다 — Document AI 호출(실비용 발생)은 이 스위치를 "
            "통과해야 한다. SUDDOE_ALLOW_EXTERNAL 과는 다른 스위치다(의미가 다르다).")


def _설정() -> tuple[str, str, str]:
    project = os.environ.get("SUDDOE_DOCAI_PROJECT")
    location = os.environ.get("SUDDOE_DOCAI_LOCATION")
    processor_id = os.environ.get("SUDDOE_DOCAI_PROCESSOR_ID")
    빠진것 = [n for n, v in [("SUDDOE_DOCAI_PROJECT", project),
                           ("SUDDOE_DOCAI_LOCATION", location),
                           ("SUDDOE_DOCAI_PROCESSOR_ID", processor_id)] if not v]
    if 빠진것:
        raise DocAIConfigMissing(
            f"환경변수 없음: {', '.join(빠진것)} — 활성화 절차는 "
            "scratchpad/D_DocumentAI_설계.md 참고.")
    return project, location, processor_id


# ══════════════════════════════════════════════════════════════════════════
# 2. Document 객체 -> (본문, 판독불가문단수) — API 응답 가공. «순수 함수» 로 뗀다
#    (real Document 객체 없이도 self-test 에서 검증할 수 있게 — 아래 3.의 duck-type 참고)
# ══════════════════════════════════════════════════════════════════════════

def _앵커_텍스트(전체텍스트: str, text_anchor) -> str:
    """`Layout.text_anchor` -> 그 구간의 텍스트. 세그먼트가 여러 개면 이어붙인다."""
    segs = getattr(text_anchor, "text_segments", None) or []
    if not segs:
        return ""
    조각 = []
    for s in segs:
        start = int(getattr(s, "start_index", 0) or 0)
        end = int(getattr(s, "end_index", 0) or 0)
        조각.append(전체텍스트[start:end])
    return "".join(조각)


def _테이블_행렬(전체텍스트: str, table) -> list[list[str]]:
    """Document AI `Table`(header_rows + body_rows) -> 셀 문자열 배열."""
    행렬: list[list[str]] = []
    for row in list(getattr(table, "header_rows", []) or []) + list(getattr(table, "body_rows", []) or []):
        행 = [_앵커_텍스트(전체텍스트, cell.layout.text_anchor).strip()
             for cell in getattr(row, "cells", []) or []]
        행렬.append(행)
    return 행렬


def document_to_text(document, *, 신뢰도임계: float = PARAGRAPH_CONFIDENCE_MIN) -> str:
    """Document AI `Document` 객체 -> 표는 파이프 마크다운, 저신뢰 문단은 마커로 바꾼 최종 본문.

    🔴 순서: 표 구간과 저신뢰 문단 구간을 모두 모아 **역순(뒤에서부터)** 치환한다 —
    앞에서부터 치환하면 뒤쪽 구간의 char offset 이 밀려 어긋난다.
    표 구간과 겹치는 저신뢰 문단은 건너뛴다(표 쪽 치환이 이미 그 구간을 덮는다).
    """
    전체 = document.text or ""
    치환목록: list[tuple[int, int, str]] = []   # (start, end, 대체문자열)

    표구간: list[tuple[int, int]] = []
    for page in getattr(document, "pages", []) or []:
        for table in getattr(page, "tables", []) or []:
            segs = getattr(table.layout.text_anchor, "text_segments", None) or []
            if not segs:
                continue
            start = int(getattr(segs[0], "start_index", 0) or 0)
            end = int(getattr(segs[-1], "end_index", 0) or 0)
            표구간.append((start, end))
            행렬 = _테이블_행렬(전체, table)
            치환목록.append((start, end, _마크다운_표(행렬)))

    def _표안(a: int, b: int) -> bool:
        return any(s <= a and b <= e for s, e in 표구간)

    for page in getattr(document, "pages", []) or []:
        for para in getattr(page, "paragraphs", []) or []:
            layout = para.layout
            신뢰도 = float(getattr(layout, "confidence", 1.0) or 0.0)
            if 신뢰도 >= 신뢰도임계:
                continue
            segs = getattr(layout.text_anchor, "text_segments", None) or []
            if not segs:
                continue
            start = int(getattr(segs[0], "start_index", 0) or 0)
            end = int(getattr(segs[-1], "end_index", 0) or 0)
            if _표안(start, end):
                continue
            치환목록.append((start, end, ILLEGIBLE_MARKER))

    치환목록.sort(key=lambda t: t[0], reverse=True)
    본문 = 전체
    for start, end, 대체 in 치환목록:
        본문 = 본문[:start] + 대체 + 본문[end:]
    return 본문


# ══════════════════════════════════════════════════════════════════════════
# 3. 실제 API 호출 — SDK 는 함수 안에서 지연 import (미설치여도 self-test 는 돈다)
# ══════════════════════════════════════════════════════════════════════════

def _판독_한이미지(png_bytes: bytes) -> tuple[str, dict]:
    """PNG 한 장 -> (본문, 메타). 실패하면 `DocAICallFailed` — 빈 문자열로 안 삼킨다."""
    _허용됐나()
    project, location, processor_id = _설정()
    try:
        from google.cloud import documentai_v1 as documentai  # noqa: E402
    except ImportError as e:
        raise DocAICallFailed(
            f"google-cloud-documentai 가 설치돼 있지 않다 — {e}. "
            "requirements-api.txt 에 추가했는지 확인해라.") from e

    try:
        client = documentai.DocumentProcessorServiceClient()
        name = client.processor_path(project, location, processor_id)
        raw_document = documentai.RawDocument(content=png_bytes, mime_type="image/png")
        요청 = documentai.ProcessRequest(name=name, raw_document=raw_document)
        결과 = client.process_document(request=요청)
    except Exception as e:  # noqa: BLE001 — 원인 다양(권한·네트워크·프로세서없음). 빈 값으로 안 삼킨다
        raise DocAICallFailed(f"Document AI 호출 실패 — {type(e).__name__}: {e}") from e

    본문 = document_to_text(결과.document)
    메타 = {"판독불가마커수": 본문.count(ILLEGIBLE_MARKER)}
    return 본문, 메타


# ══════════════════════════════════════════════════════════════════════════
# 4. 메인 진입점 — `vlm_extract.extract_meta()` 와 같은 모양(페이지 선별 → 필요한 것만 호출)
# ══════════════════════════════════════════════════════════════════════════

def extract(path: str | Path, *, page_range: tuple[int, int] | None = None,
            임계: int = MIN_CHARS_PER_PAGE) -> tuple[str, dict[int, int]]:
    """(본문, {문자오프셋: 페이지번호}) — `vlm_extract.extract()` 와 완전히 같은 모양."""
    본문, 메타 = extract_meta(path, page_range=page_range, 임계=임계)
    return 본문, 메타["페이지오프셋"]


def extract_meta(path: str | Path, *, page_range: tuple[int, int] | None = None,
                  임계: int = MIN_CHARS_PER_PAGE) -> tuple[str, dict]:
    import pdftext  # noqa: E402 — vlm_extract 와 같은 관용구(먼저 텍스트 레이어 시도)
    import pdfplumber

    path = Path(path)
    글자수 = 페이지별_글자수(path)
    n_pages = len(글자수)
    대상 = {i for i, n in enumerate(글자수, 1) if n < 임계}
    if page_range:
        lo, hi = page_range
        대상 = {i for i in 대상 if lo <= i <= hi}

    with pdfplumber.open(path) as pdf:
        probe = pdf.pages[min(4, len(pdf.pages) - 1)].extract_text() or "" if pdf.pages else ""
        deduped = pdftext.dup_ratio(probe) > pdftext.DUP_THRESHOLD
        src_pages = [p.dedupe_chars() for p in pdf.pages] if deduped else pdf.pages
        기본텍스트 = [p.extract_text() or "" for p in src_pages]

    조각: list[str] = []
    오프셋: dict[int, int] = {}
    pos = 0
    docai_페이지: list[int] = []
    판독불가_페이지: list[int] = []
    실패_페이지: list[tuple[int, str]] = []

    for i in range(1, n_pages + 1):
        if i in 대상:
            try:
                png = _렌더(path, i)
                텍스트, 호출메타 = _판독_한이미지(png)
                docai_페이지.append(i)
                if 호출메타.get("판독불가마커수"):
                    판독불가_페이지.append(i)
            except (DocAINotAllowed, DocAIConfigMissing):
                raise  # 🔴 관문이 막았다 — 조용히 대체하지 않는다(vlm_extract 와 같은 원칙)
            except DocAICallFailed as e:
                실패_페이지.append((i, str(e)))
                텍스트 = 기본텍스트[i - 1]
        else:
            텍스트 = 기본텍스트[i - 1]
        오프셋[pos] = i
        조각.append(텍스트)
        pos += len(텍스트) + 1

    return NL.join(조각), {
        "페이지오프셋": 오프셋,
        "총페이지": n_pages,
        "docai_페이지": docai_페이지,
        "판독불가_페이지": 판독불가_페이지,
        "실패_페이지": 실패_페이지,
        "pdftext_dedupe": deduped,
    }


# ══════════════════════════════════════════════════════════════════════════
# 5. 자가검사 — API·SDK 없이 돈다 (게이트·계약·마크다운 직렬화·저신뢰 치환만 검사)
# ══════════════════════════════════════════════════════════════════════════

class _FakeSeg:
    def __init__(self, s, e):
        self.start_index, self.end_index = s, e


class _FakeAnchor:
    def __init__(self, *segs):
        self.text_segments = [_FakeSeg(s, e) for s, e in segs]


class _FakeLayout:
    def __init__(self, text_anchor, confidence=1.0):
        self.text_anchor, self.confidence = text_anchor, confidence


class _FakeCell:
    def __init__(self, layout):
        self.layout = layout


class _FakeRow:
    def __init__(self, cells):
        self.cells = cells


class _FakeTable:
    def __init__(self, layout, header_rows, body_rows):
        self.layout, self.header_rows, self.body_rows = layout, header_rows, body_rows


class _FakeParagraph:
    def __init__(self, layout):
        self.layout = layout


class _FakePage:
    def __init__(self, tables=None, paragraphs=None):
        self.tables = tables or []
        self.paragraphs = paragraphs or []


class _FakeDocument:
    def __init__(self, text, pages):
        self.text, self.pages = text, pages


def _self_test() -> int:
    실패: list[str] = []

    def eq(이름, got, want):
        if got != want:
            실패.append(f"{이름}: {got!r} != {want!r}")

    # 1. 관문 — SUDDOE_ALLOW_DOCAI 없으면 즉시 예외
    복원 = os.environ.pop("SUDDOE_ALLOW_DOCAI", None)
    try:
        threw = False
        try:
            _허용됐나()
        except DocAINotAllowed:
            threw = True
        eq("관문_기본값_차단", threw, True)
    finally:
        if 복원 is not None:
            os.environ["SUDDOE_ALLOW_DOCAI"] = 복원

    # 2. 관문 통과해도 설정 없으면 명시적 예외
    os.environ["SUDDOE_ALLOW_DOCAI"] = "1"
    복원값 = {k: os.environ.pop(k, None) for k in
             ("SUDDOE_DOCAI_PROJECT", "SUDDOE_DOCAI_LOCATION", "SUDDOE_DOCAI_PROCESSOR_ID")}
    try:
        threw = False
        try:
            _설정()
        except DocAIConfigMissing:
            threw = True
        eq("설정없음_명시적예외", threw, True)
    finally:
        os.environ.pop("SUDDOE_ALLOW_DOCAI", None)
        for k, v in 복원값.items():
            if v is not None:
                os.environ[k] = v

    # 3. 표 → 파이프 마크다운 (table_splice 형식과 동일한지)
    본문 = "머리1머리2값A값B"    # 0:머리1(0-3) 3:머리2(3-6) 6:값A(6-8) 8:값B(8-10)
    tbl = _FakeTable(
        layout=_FakeLayout(_FakeAnchor((0, len(본문)))),
        header_rows=[_FakeRow([_FakeCell(_FakeLayout(_FakeAnchor((0, 3)))),
                               _FakeCell(_FakeLayout(_FakeAnchor((3, 6))))])],
        body_rows=[_FakeRow([_FakeCell(_FakeLayout(_FakeAnchor((6, 8)))),
                             _FakeCell(_FakeLayout(_FakeAnchor((8, 10))))])],
    )
    doc = _FakeDocument(text=본문, pages=[_FakePage(tables=[tbl])])
    결과 = document_to_text(doc)
    eq("표_마크다운_변환", 결과, "| 머리1 | 머리2 |\n| --- | --- |\n| 값A | 값B |")

    # 4. 저신뢰 문단 -> ILLEGIBLE_MARKER (표 구간과 안 겹칠 때만)
    본문2 = "정상문단 흐린문단입니다"   # 0:정상문단(0-4) 4:' '(4-5) 5:흐린문단입니다(5-12)
    p1 = _FakeParagraph(_FakeLayout(_FakeAnchor((0, 5)), confidence=0.95))
    p2 = _FakeParagraph(_FakeLayout(_FakeAnchor((5, 12)), confidence=0.2))
    doc2 = _FakeDocument(text=본문2, pages=[_FakePage(paragraphs=[p1, p2])])
    결과2 = document_to_text(doc2)
    eq("저신뢰문단_마커치환", 결과2, f"정상문단 {ILLEGIBLE_MARKER}")

    # 5. 표 구간과 겹치는 저신뢰 문단은 표 치환이 우선(마커로 덮어쓰지 않는다)
    본문3 = "AB"
    tbl3 = _FakeTable(
        layout=_FakeLayout(_FakeAnchor((0, 2))),
        header_rows=[_FakeRow([_FakeCell(_FakeLayout(_FakeAnchor((0, 1)))),
                               _FakeCell(_FakeLayout(_FakeAnchor((1, 2))))])],
        body_rows=[],
    )
    p3 = _FakeParagraph(_FakeLayout(_FakeAnchor((0, 2)), confidence=0.1))
    doc3 = _FakeDocument(text=본문3, pages=[_FakePage(tables=[tbl3], paragraphs=[p3])])
    결과3 = document_to_text(doc3)
    eq("표우선_저신뢰안덮음", 결과3, "| A | B |\n| --- | --- |")

    # 6. 임계값 산술이 vlm_extract 와 같은 상수를 쓰는지(회귀 방지 — 둘이 갈리면 안 된다)
    eq("임계값_공유", MIN_CHARS_PER_PAGE, 50)

    for 이름 in 실패:
        print(f"  🔴 {이름}")
    if 실패:
        print(f"🔴 self-test 실패 {len(실패)}건")
    else:
        print("✅ self-test 전건 통과 (API·SDK 호출 없음)")
    return 1 if 실패 else 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--pages", help="예: 1-3 (1-base, 양끝 포함)")
    a = ap.parse_args()

    if a.selftest:
        return _self_test()

    if not a.file:
        print(__doc__)
        return 2

    page_range = None
    if a.pages:
        lo, hi = a.pages.split("-")
        page_range = (int(lo), int(hi))

    try:
        본문, 메타 = extract_meta(a.file, page_range=page_range)
    except (DocAINotAllowed, DocAIConfigMissing) as e:
        print(f"🔴 판독 못 함 — {type(e).__name__}: {e}")
        return 1
    print(f"총페이지={메타['총페이지']} · pdftext_dedupe={메타['pdftext_dedupe']}")
    print(f"Document AI 호출 페이지: {메타['docai_페이지']}")
    print(f"판독불가 마커 있는 페이지: {메타['판독불가_페이지']}")
    print(f"실패 페이지: {메타['실패_페이지']}")
    print(f"최종 본문 길이: {len(본문)}자")
    print("--- 앞 500자 ---")
    print(본문[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
