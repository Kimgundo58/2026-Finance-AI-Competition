# -*- coding: utf-8 -*-
"""PDF 텍스트 추출 공용 유틸 — 문자중복 레이어 + 다단 조판 자동 처리.

🔴 함정 1. 문자중복 레이어 (2026-08-27 실측)
   일부 PDF 는 같은 글자가 **두 겹으로 겹쳐** 있어 `extract_text()` 가
   "제제5조조(전전문문기기관관)" 처럼 모든 한글을 2배로 뱉는다.
   그러면 `제\\d+조` 정규식이 **하나도 안 걸린다** — 조 0개로 조용히 파싱 실패한다.

   실측 3건 (전부 핵심 문서):
     · L1_중소기업창업_지원사업_통합관리지침_제14차개정  ← 판정 최상위 근거
     · L3_2025초기창업패키지_주요질의응답집_별첨4        ← 정답셋 정답지
     · 창업도약패키지 지원사업 세부관리기준(2022년)

   pdfplumber 의 `page.dedupe_chars()` 가 정확히 이걸 푼다.
   비용이 있으므로 **중복이 감지된 문서에만** 적용한다.

🔴 함정 2. 2단 조판 (2026-08-28 실측)
   창진원 세부관리기준 다수가 2단이다. `extract_text()` 는 두 단을 **줄 단위로
   교대로** 읽어서 조문 본문이 섞인다:

     제4조(멘토) 멘토의 역할은 다음과 → 같다. 제9조(협약의 변경) ① 주관 →
     1. 창업기업이 필요한 자문 서비스 → 기관은 협약 변경 시 지침 제21조를

   조 **목록**은 온전하고 **본문**만 섞이므로 조 개수 검사로는 안 잡힌다.
   문법적으로도 멀쩡해 보인다. 유일한 신호는 **조 번호가 비단조**가 되는 것이다
   (제4 → 제9 → 제5 → 제10 → 제6 …).

   실측: 현행 세부관리기준 8건 중 7건이 2단. TIPS 만 1단.

   대응 — 단어 x 좌표에서 **거터(gutter, 세로 빈 띠)** 를 찾아 좌/우로 crop 한 뒤
   각 단을 pdfplumber 에 다시 맡긴다. 직접 단어를 재조립하지 않는다
   (줄바꿈·들여쓰기 복원을 pdfplumber 가 이미 한다).

   ⚠️ "2단인가" 는 **문서 단위**로 판정하고, 실제 자르는 위치는 **페이지별**로 쓴다.
      문서 단위로 판정하는 이유: 표가 있는 한 페이지 때문에 그 페이지만 잘리면 안 된다.
      페이지별로 자르는 이유: 표지·목차는 본문과 단 위치가 다르다
      (실측 예비창업 2025 — 0쪽 x372 / 1~5쪽 x421 / 6~8쪽 x418~423).
"""
from __future__ import annotations

import statistics

import pdfplumber

DUP_THRESHOLD = 0.35
NL = chr(10)           # 개행. 리터럴로 쓰면 파일 생성 도구가 치환해 버린다

# 거터 판정 파라미터 — 전부 실측 기반
GUTTER_MIN_W = 18      # 이보다 좁은 빈 띠는 자간·표 여백일 수 있다
GUTTER_BAND = (0.25, 0.75)   # 페이지 폭의 이 구간 안에 있어야 단 경계로 본다
GUTTER_SAMPLE = 8      # 검사할 페이지 수
GUTTER_AGREE = 0.5     # 표본 중 이 비율 이상에서 나와야 채택
GUTTER_SPREAD = 20     # 표본 간 x 편차가 이보다 크면 단 경계가 아니다

# 4분면(4-up) 판정 — A5 4쪽을 A4 한 장에 앉힌 제본
BAND_MIN_H = 40        # 가로 빈 띠 최소 높이
BAND_BAND = (0.30, 0.70)
BAND_AGREE = 0.5


def dup_ratio(txt: str) -> float:
    """연속 동일 한글 쌍의 비율. 정상 문서는 0.05 안팎, 중복 레이어는 0.5 안팎."""
    han = [c for c in txt if "가" <= c <= "힣"]
    if len(han) < 200:
        return 0.0
    return sum(1 for a, b in zip(han, han[1:]) if a == b) / len(han)


def page_gutter(page) -> tuple[float, float] | None:
    """이 페이지의 가장 넓은 세로 빈 띠. (중심 x, 폭) 또는 None.

    단어의 x0~x1 구간을 합쳐 덮인 영역을 만들고, 그 사이 빈 구간을 본다.
    x0 만 보면 표에서 오탐이 난다 — 열 시작점이 몰려 있어도 실제로는 덮여 있다.
    """
    words = page.extract_words()
    if len(words) < 30:
        return None
    W = page.width
    lo, hi = GUTTER_BAND[0] * W, GUTTER_BAND[1] * W

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
    lo, hi = BAND_BAND[0] * H, BAND_BAND[1] * H
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

    실측(초격차 제10차): 거터 x297 + 가로 띠 y428(높이 841의 50%)이 10/10 쪽에서 나온다.
    좌단만 위->아래로 읽으면 제1,2,6,7,8,9 순이 되어 조 순서가 깨진다.
    좌상 -> 우상 -> 좌하 -> 우하 로 읽어야 제1~제13이 맞다.

    ⚠️ 세로 거터가 없으면 4분면일 수 없다. 그리고 **과반 페이지**에서 나와야 한다 —
       실측 8건 중 초기창업이 1/10 쪽에서 우연히 걸렸다(단락 여백). 초격차만 10/10.
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
    """이 문서가 2단인가. 2단이면 대표 거터 x, 아니면 None.

    ⚠️ 이상치를 버리고 판정한다. 표지·목차는 본문과 단 위치가 다르다 —
       실측(예비창업 2025): 0쪽 x372 / 1~5쪽 x421 / 6~8쪽 x418~423.
       "전 표본이 median ±20pt" 로 걸면 0쪽 하나 때문에 문서 전체가 1단으로 오판된다.
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
    """2단이면 그 페이지 **자신의** 거터로 자른다. 4분면이면 좌상->우상->좌하->우하.

    문서 대표값으로 일괄 자르면 표지처럼 단 위치가 다른 페이지에서 글자를 가른다.
    페이지에 거터가 없으면(전면 표 등) 자르지 않는다 — 표를 반토막 내는 것보다 낫다.
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


def extract(path, max_pages: int | None = None) -> tuple[str, bool]:
    """(본문, 중복레이어였나) 를 돌려준다.

    다단 조판은 감지되면 조용히 해소한다. 어느 문서가 다단이었는지 알아야 하면
    `extract_meta()` 를 쓴다.
    """
    text, meta = extract_meta(path, max_pages)
    return text, meta["dedupe"]


def extract_meta(path, max_pages: int | None = None) -> tuple[str, dict]:
    """(본문, {dedupe, gutter, pages}) — 진단용."""
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        if not pages:
            return "", {"dedupe": False, "gutter": None, "quad": None, "pages": 0}

        probe = pages[min(4, len(pages) - 1)].extract_text() or ""
        deduped = dup_ratio(probe) > DUP_THRESHOLD
        src = [p.dedupe_chars() for p in pages] if deduped else pages

        gutter = is_two_column(src)
        quad = is_quadrant(src, gutter)
        text = NL.join(_page_text(p, gutter, quad) for p in src)
        return text, {"dedupe": deduped, "gutter": gutter, "quad": quad,
                      "pages": len(pages)}
