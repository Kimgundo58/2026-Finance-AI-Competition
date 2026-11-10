# -*- coding: utf-8 -*-
"""PDF 텍스트 추출 공용 모듈 — 문자중복 레이어와 2단·4분면 조판을 자동으로 감지해 처리한다.

text_extract.extract_pdf 와 corpus_ingest 가 쓴다(PDF 텍스트 추출은 반드시 이 모듈을 거친다). 공개 함수:
extract_meta(path) -> (본문, 진단 dict), extract_pages(path) -> 페이지별 본문 목록. 문자중복 레이어는 pdfplumber 의 `page.dedupe_chars()` 로
풀고, 2단 조판은 단 사이 여백(gutter, 세로 빈 띠)을 찾아 좌우로 crop 한다. 2단 여부는 문서 단위로 판정하되
자르는 위치는 페이지별로 쓴다 — 표지·목차는 본문과 단 위치가 다르다.
"""
from __future__ import annotations

import statistics

import pdfplumber

DUP_THRESHOLD = 0.35
NL = chr(10)           # 개행

# 단 사이 여백 판정 파라미터
GUTTER_MIN_W = 18      # 이보다 좁은 빈 띠는 자간·표 여백일 수 있다
GUTTER_RANGE = (0.25, 0.75)  # 페이지 폭의 이 구간 안에 있어야 단 경계로 본다
GUTTER_SAMPLE = 8      # 검사할 페이지 수
GUTTER_AGREE = 0.5     # 표본 중 이 비율 이상에서 나와야 채택
GUTTER_SPREAD = 20     # 표본 간 x 편차가 이보다 크면 단 경계가 아니다

# 4분면(4-up) 판정 — A5 4쪽을 A4 한 장에 배치한 인쇄 형식
BAND_MIN_H = 40        # 가로 빈 띠 최소 높이
BAND_RANGE = (0.30, 0.70)
BAND_AGREE = 0.5


def dup_ratio(txt: str) -> float:
    """연속 동일 한글 쌍의 비율. 정상 문서는 0.05 안팎, 중복 레이어는 0.5 안팎."""
    han = [c for c in txt if "가" <= c <= "힣"]
    if len(han) < 200:
        return 0.0
    return sum(1 for a, b in zip(han, han[1:]) if a == b) / len(han)


def page_gutter(page) -> tuple[float, float] | None:
    """이 페이지의 가장 넓은 세로 빈 띠. (중심 x, 폭) 또는 None.

    x0 만 보면 표에서 오탐이 난다 — x0~x1 구간을 합쳐 글자가 차지한 영역으로 판단한다.
    """
    words = page.extract_words()
    if len(words) < 30:
        return None
    W = page.width
    lo, hi = GUTTER_RANGE[0] * W, GUTTER_RANGE[1] * W

    spans = sorted((w["x0"], w["x1"]) for w in words)
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    best = None
    for i in range(len(merged) - 1):
        g0, g1 = merged[i][1], merged[i + 1][0]
        w = g1 - g0
        mid = (g0 + g1) / 2
        if w < GUTTER_MIN_W or not (lo < mid < hi):
            continue
        if best is None or w > best[1]:
            best = (mid, w)
    return best


def page_band(page) -> tuple[float, float] | None:
    """이 페이지의 가장 넓은 가로 빈 띠. (중심 y, 높이) 또는 None."""
    words = page.extract_words()
    if len(words) < 40:
        return None
    H = page.height
    lo, hi = BAND_RANGE[0] * H, BAND_RANGE[1] * H
    spans = sorted((w["top"], w["bottom"]) for w in words)
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 2:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    best = None
    for i in range(len(merged) - 1):
        g0, g1 = merged[i][1], merged[i + 1][0]
        h = g1 - g0
        mid = (g0 + g1) / 2
        if h < BAND_MIN_H or not (lo < mid < hi):
            continue
        if best is None or h > best[1]:
            best = (mid, h)
    return best


def is_quadrant(pages, gutter: float | None) -> float | None:
    """4분면(4-up) 배치인가. 맞으면 대표 가로 띠 y, 아니면 None.

    좌상 -> 우상 -> 좌하 -> 우하 순으로 읽어야 조 순서가 맞다.
    세로 단 사이 여백이 없으면 4분면일 수 없고, 과반 페이지에서 나와야 채택한다.
    """
    if gutter is None or len(pages) < 2:
        return None
    step = max(1, len(pages) // GUTTER_SAMPLE)
    sample = pages[::step][:GUTTER_SAMPLE]
    ys = [b[0] for b in (page_band(p) for p in sample) if b]
    need = max(2, int(len(sample) * BAND_AGREE))
    if len(ys) < need:
        return None
    med = statistics.median(ys)
    if sum(1 for y in ys if abs(y - med) <= GUTTER_SPREAD * 2) < need:
        return None
    return med


def is_two_column(pages) -> float | None:
    """이 문서가 2단인가. 2단이면 대표 단 사이 여백 x, 아니면 None.

    이상치를 버리고 판정한다. 표지·목차는 본문과 단 위치가 달라 이상치가 되므로,
    median 근처에 몰린 페이지가 과반이면 2단으로 본다.
    """
    if len(pages) < 2:
        return None
    step = max(1, len(pages) // GUTTER_SAMPLE)
    sample = pages[::step][:GUTTER_SAMPLE]
    xs = [g[0] for g in (page_gutter(p) for p in sample) if g]
    need = max(2, int(len(sample) * GUTTER_AGREE))
    if len(xs) < need:
        return None
    med = statistics.median(xs)
    if sum(1 for x in xs if abs(x - med) <= GUTTER_SPREAD) < need:
        return None          # 위치가 페이지마다 흩어진다 = 표. 단 경계가 아니다
    return med


def _page_text(page, two_col: float | None, quad: float | None = None) -> str:
    """2단이면 그 페이지 자신의 단 사이 여백으로 자른다. 4분면이면 좌상->우상->좌하->우하.

    문서 대표값으로 일괄 자르면 표지처럼 단 위치가 다른 페이지에서 글자를 가른다.
    페이지에 단 사이 여백이 없으면(전면 표 등) 자르지 않는다 — 표를 둘로 자르는 것보다 낫다.
    """
    if two_col is None:
        return page.extract_text() or ""
    g = page_gutter(page)
    if g is None:
        return page.extract_text() or ""
    x, W, H = g[0], page.width, page.height

    if quad is not None:
        b = page_band(page)
        y = b[0] if b else quad
        boxes = [(0, 0, x, y), (x, 0, W, y), (0, y, x, H), (x, y, W, H)]
    else:
        boxes = [(0, 0, x, H), (x, 0, W, H)]

    return NL.join((page.crop(bx).extract_text() or "") for bx in boxes)


def extract_meta(path, max_pages: int | None = None) -> tuple[str, dict]:
    """(본문, {dedupe, gutter, pages}) — 진단용."""
    texts, meta = _pages_meta(path, max_pages)
    return NL.join(texts), meta


def extract_pages(path, max_pages: int | None = None) -> list[str]:
    """페이지별 본문 목록. NL 로 이으면 `extract_meta()` 의 본문과 같다."""
    return _pages_meta(path, max_pages)[0]


def _pages_meta(path, max_pages: int | None) -> tuple[list[str], dict]:
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        if not pages:
            return [], {"dedupe": False, "gutter": None, "quad": None, "pages": 0}

        probe = pages[min(4, len(pages) - 1)].extract_text() or ""
        deduped = dup_ratio(probe) > DUP_THRESHOLD
        src = [p.dedupe_chars() for p in pages] if deduped else pages

        gutter = is_two_column(src)
        quad = is_quadrant(src, gutter)
        texts = [_page_text(p, gutter, quad) for p in src]
        return texts, {"dedupe": deduped, "gutter": gutter, "quad": quad,
                       "pages": len(pages)}
