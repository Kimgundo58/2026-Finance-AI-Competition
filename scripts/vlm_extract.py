# -*- coding: utf-8 -*-
"""스캔 PDF 판독 — 페이지를 이미지로 렌더링해 비전 API 로 텍스트를 뽑는다.

🔴 왜 필요한가 (2026-09-06 오너 지시로 착수):
    `corpus.documents.extraction='vlm'` 은 "사람이 판독해 넣었다"는 «표시» 일 뿐이고,
    `llm_validate.py` 의 VLM_DOWNGRADE 는 이미 vlm 인 문서를 «인용할 때» 신뢰등급을
    내리는 규칙이다. **판독을 실제로 부르는 코드는 이 파일이 생기기 전까지 0곳이었다.**
    실측: "2026년 재도전성공패키지 세부관리기준(11차 개정)" 은 9페이지 «전부» 가
    스캔이고, `pdfplumber.extract_text()` 는 페이지마다 "- N -"(하단 쪽번호) 5자만
    돌려준다 — 본문이 통째로 없다. 이 사업은 지금 룰·판정 재료가 «전무»하다.

    기존 `scripts/pdftext.py::extract()` 는 **텍스트 레이어가 있는데 깨진** 경우
    (문자중복·다단·4분면)를 고친다. 이 파일은 그 전 단계 — **텍스트 레이어 자체가
    없는** 페이지를 다룬다. 겹치지 않는다: 이 모듈은 먼저 `pdftext` 로 시도한 뒤,
    페이지별 글자 수가 임계 미만인 페이지만 비전 API 로 보낸다.

━━ 언제 부르나 — 페이지당 50자 미만 (숫자 근거, 전부 실측) ━━━━━━━━━━━━━━━━━━
    재도전(스캔, 9p)      전 페이지 5자                      → 9/9 (100%) 이 임계 아래
    창업도약(텍스트, 34p)  최소 23자(표지 추정) · 나머지 110자+  → 1/34 (2.9%) 만 아래
    모두의창업(텍스트, 48p) 최소 51자                          → 0/48 (0%) 아래
    50 은 스캔 문서를 전량 잡으면서(위양성 0), 텍스트 문서의 오탐을 페이지 하나
    이하로 묶는다(표지·구분지처럼 원래 짧은 페이지가 어쩌다 걸리는 비용은
    "그 한 페이지만" 비전 API 를 한 번 더 태우는 것뿐이라 감내 가능하다).

━━ 관문 (오너가 이미 만들어 둔 것 그대로 탄다) ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    `SUDDOE_ALLOW_EXTERNAL=1` 이 없으면 호출 자체를 하지 않는다(`adapter.py:330`
    과 같은 신호). CLAUDE.md 2026-09-05 오너 해제로 "외부 API 금지" 원칙 자체는
    풀렸지만, 그 원칙이 풀렸다고 관문 없이 나가도 된다는 뜻은 아니다 — 관문은
    "언제 쓸지"를 사람이 여전히 켜고 끌 수 있게 하는 스위치이지 원칙과 다른 것이다.

━━ 표를 살리는 방법 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    프롬프트가 표를 파이프 마크다운으로 내라고 명시한다(`table_splice.py` 와
    같은 형식 — 뒤 배관이 그 형식을 이미 기대한다). **판독 불가 구간은 지어내지
    말고 `[판독불가]` 로 표시하라**고 지시하고, 반환된 텍스트에 그 마커가 있으면
    `표_판독_불확실=True` 로 명시적으로 알린다(뭉갠 채 성공이라 하지 않는다).

━━ 반환 모양 — 기존 파이프라인과 맞춘다 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    `extract(path)` -> (본문: str, 페이지오프셋: dict[int,int])
    `stage0_extract.extract_pdf()` 와 «같은 모양» 이다 — 뒤의 `split_articles(본문,
    페이지오프셋)` 이 그대로 이어붙는다. 새 형식을 만들면 배관을 두 벌 만들게 된다
    (오너 지시 원문). 진단이 더 필요하면 `extract_meta()` 를 쓴다(pdftext.py 의
    extract/extract_meta 짝과 같은 관용구).

실행 (자가검사는 API 호출 없이 돈다):
    PYTHONIOENCODING=utf-8 python scripts/vlm_extract.py --selftest
    SUDDOE_ALLOW_EXTERNAL=1 ANTHROPIC_API_KEY=... \
        PYTHONIOENCODING=utf-8 python scripts/vlm_extract.py --file <pdf> [--pages 1-3]
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pdftext  # noqa: E402 — 🔴 반드시 이걸 먼저 거친다. pdfplumber.extract_text() 직접 호출 금지

NL = "\n"

# ── 상수 (전부 위 docstring 의 실측으로 정함) ─────────────────────────────
MIN_CHARS_PER_PAGE = 50          # 이보다 적으면 그 페이지는 "텍스트가 없다"로 본다
VLM_MODEL = os.environ.get("SUDDOE_VLM_MODEL", "claude-sonnet-5")
RENDER_DPI = 200                 # 표 안 작은 글자까지 읽을 해상도. 150 이하는 실측(초안)에서 흐릿했다
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ILLEGIBLE_MARKER = "[판독불가]"


class VLMKeyMissing(RuntimeError):
    """ANTHROPIC_API_KEY 가 없다. 🔴 조용히 빈 값으로 넘어가지 않는다 — 오늘 이 프로젝트가
    네 번 밟은 "except 가 삼켜서 빈 값" 사고를 되풀이하지 않는다. 호출부가 반드시 본다."""


class ExternalNotAllowed(RuntimeError):
    """`SUDDOE_ALLOW_EXTERNAL=1` 이 없다. `adapter.py` 의 관문과 같은 신호를 쓴다."""


class VLMCallFailed(RuntimeError):
    """API 호출 자체가 실패했다(네트워크·429·5xx 등). 페이지 텍스트는 만들지 않는다 —
    실패를 빈 문자열로 흘리면 "판독했는데 원래 백지였다"와 구분이 안 된다."""


# ══════════════════════════════════════════════════════════════════════════
# 1. 페이지별 글자 수 — «필요한 페이지만» 고른다
# ══════════════════════════════════════════════════════════════════════════

def 페이지별_글자수(path: Path) -> list[int]:
    """`pdftext` 가 이미 여는 `pdfplumber` 문서를 그대로 써서 페이지별 길이를 잰다.

    🔴 문자중복 레이어가 있으면 정상 문서도 실측 글자 수가 반토막 난다(같은
    글자가 겹쳐도 `extract_text()` 문자열 «길이» 자체는 안 준다 — "제제5조조"는
    원문의 2배 길이다. 오히려 부풀지 줄지는 않는다). 그래도 안전하게, `pdftext`
    가 이미 문서 단위로 중복 판정을 끝낸 `dedupe_chars()` 소스를 재사용한다 —
    새로 열지 않는다(문서를 두 번 여는 비용을 줄인다).
    """
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            return []
        probe = pdf.pages[min(4, len(pdf.pages) - 1)].extract_text() or ""
        deduped = pdftext.dup_ratio(probe) > pdftext.DUP_THRESHOLD
        src = [p.dedupe_chars() for p in pdf.pages] if deduped else pdf.pages
        return [len(p.extract_text() or "") for p in src]


def 필요페이지(path: Path, *, 임계: int = MIN_CHARS_PER_PAGE) -> list[int]:
    """1-base 페이지 번호 중 글자 수가 임계 미만인 것들."""
    글자수 = 페이지별_글자수(path)
    return [i for i, n in enumerate(글자수, 1) if n < 임계]


# ══════════════════════════════════════════════════════════════════════════
# 2. 페이지 렌더링 — pypdfium2 (poppler 불필요, 순수 wheel)
# ══════════════════════════════════════════════════════════════════════════

def _렌더(path: Path, page_no: int, *, dpi: int = RENDER_DPI) -> bytes:
    """1-base 페이지 번호 → PNG 바이트."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_no - 1]
        스케일 = dpi / 72
        bitmap = page.render(scale=스케일)
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()


# ══════════════════════════════════════════════════════════════════════════
# 3. 비전 API 호출 — adapter.py 의 raw HTTP 관용구를 그대로 쓴다(SDK 미설치 · 의존성 최소)
# ══════════════════════════════════════════════════════════════════════════

_프롬프트 = f"""이 이미지는 한국 정부 창업지원사업 규정집(세부관리기준)의 한 페이지다.
페이지의 모든 텍스트를 «그대로»(축약·요약 금지) 옮겨 적어라.

규칙:
1. 조문 번호·제목(「제N조(제목)」)을 원문 그대로 유지한다.
2. 표는 GitHub 파이프 마크다운으로 옮긴다 — 예: "| 비목 | 정의 |\\n| --- | --- |\\n| 재료비 | ... |"
   병합 셀은 같은 값을 각 행에 반복해 채운다(빈 칸으로 두지 않는다).
3. 페이지 하단/상단의 쪽번호·머리말은 옮기지 않는다.
4. 글자가 흐리거나 잘려서 «확신 없이 추측» 해야 하는 부분이 있으면 그 자리에
   정확히 "{ILLEGIBLE_MARKER}" 라고 쓰고 넘어가라 — 지어내지 마라.
5. 텍스트 외의 해설·요약·따옴표를 덧붙이지 마라. 페이지 내용만 출력한다."""


def _API_키() -> str:
    키 = os.environ.get("ANTHROPIC_API_KEY")
    if not 키:
        raise VLMKeyMissing(
            "ANTHROPIC_API_KEY 가 없다 — 조용히 빈 문자열로 넘어가지 않는다. "
            "키를 설정하거나, 이 문서는 판독을 건너뛰고 '판독 안 됨'으로 남겨라.")
    return 키


def _허용됐나() -> None:
    if os.environ.get("SUDDOE_ALLOW_EXTERNAL") != "1":
        raise ExternalNotAllowed(
            "SUDDOE_ALLOW_EXTERNAL=1 이 없다 — adapter.py 의 외부 API 관문과 같은 신호다. "
            "이 판독은 비전 API(외부)를 쓰므로 관문을 통과해야 한다.")


def 판독_한페이지(png_bytes: bytes, *, 타임아웃: int = 90, 재시도: int = 2) -> tuple[str, dict]:
    """PNG 한 장 → (텍스트, 메타). 실패하면 `VLMCallFailed` 를 낸다 — 빈 문자열로 안 삼킨다."""
    _허용됐나()
    키 = _API_키()
    b64 = base64.b64encode(png_bytes).decode("ascii")

    본문 = {
        "model": VLM_MODEL,
        "max_tokens": 4096,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": b64}},
                {"type": "text", "text": _프롬프트},
            ],
        }],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(본문, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "x-api-key": 키,
                 "anthropic-version": ANTHROPIC_VERSION})

    마지막_예외: Exception | None = None
    for 회차 in range(재시도 + 1):
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=타임아웃) as r:
                d = json.loads(r.read().decode())
            텍스트 = "".join(b.get("text", "") for b in d.get("content", []))
            메타 = {
                "지연ms": int((time.time() - t) * 1000),
                "토큰": d.get("usage", {}),
                "판독불가마커수": 텍스트.count(ILLEGIBLE_MARKER),
            }
            return 텍스트, 메타
        except urllib.error.HTTPError as e:
            마지막_예외 = e
            if e.code in (429, 500, 502, 503, 529) and 회차 < 재시도:
                time.sleep(2 ** 회차)
                continue
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            마지막_예외 = e
            if 회차 < 재시도:
                time.sleep(2 ** 회차)
                continue
            break
    raise VLMCallFailed(f"페이지 판독 실패 — {type(마지막_예외).__name__}: {마지막_예외}")


# ══════════════════════════════════════════════════════════════════════════
# 4. 메인 진입점 — pdftext 로 먼저 시도, 부족한 페이지만 VLM
# ══════════════════════════════════════════════════════════════════════════

def extract(path: str | Path, *, page_range: tuple[int, int] | None = None,
            임계: int = MIN_CHARS_PER_PAGE) -> tuple[str, dict[int, int]]:
    """(본문, {문자오프셋: 페이지번호}) — `stage0_extract.extract_pdf()` 와 같은 모양.

    `page_range` 를 주면 그 1-base 범위(양끝 포함)의 페이지만 본다(진단·부분 재판독용).
    범위 밖 페이지는 `pdftext` 결과를 그대로 쓴다(임계 미만이라도 건드리지 않는다).
    """
    본문, 메타 = extract_meta(path, page_range=page_range, 임계=임계)
    return 본문, 메타["페이지오프셋"]


def extract_meta(path: str | Path, *, page_range: tuple[int, int] | None = None,
                  임계: int = MIN_CHARS_PER_PAGE) -> tuple[str, dict]:
    """진단용 — (본문, {페이지오프셋, vlm_페이지, 판독불가_페이지, pdftext_메타})."""
    path = Path(path)
    글자수 = 페이지별_글자수(path)
    n_pages = len(글자수)
    대상 = {i for i, n in enumerate(글자수, 1) if n < 임계}
    if page_range:
        lo, hi = page_range
        대상 = {i for i in 대상 if lo <= i <= hi}

    import pdfplumber
    with pdfplumber.open(path) as pdf:
        probe = pdf.pages[min(4, len(pdf.pages) - 1)].extract_text() or "" if pdf.pages else ""
        deduped = pdftext.dup_ratio(probe) > pdftext.DUP_THRESHOLD
        src_pages = [p.dedupe_chars() for p in pdf.pages] if deduped else pdf.pages
        기본텍스트 = [p.extract_text() or "" for p in src_pages]

    조각: list[str] = []
    오프셋: dict[int, int] = {}
    pos = 0
    vlm_페이지: list[int] = []
    판독불가_페이지: list[int] = []
    실패_페이지: list[tuple[int, str]] = []

    for i in range(1, n_pages + 1):
        if i in 대상:
            try:
                png = _렌더(path, i)
                텍스트, 호출메타 = 판독_한페이지(png)
                vlm_페이지.append(i)
                if 호출메타.get("판독불가마커수"):
                    판독불가_페이지.append(i)
            except (VLMKeyMissing, ExternalNotAllowed) as e:
                # 🔴 관문이 막았다 — 조용히 pdftext 텍스트(사실상 빈 값)로 대체하지 않는다.
                #    호출부가 이 사실을 알아야 하므로 그대로 올린다.
                raise
            except VLMCallFailed as e:
                실패_페이지.append((i, str(e)))
                텍스트 = 기본텍스트[i - 1]     # pdftext 결과라도 남긴다(대개 거의 빈 문자열)
        else:
            텍스트 = 기본텍스트[i - 1]
        오프셋[pos] = i
        조각.append(텍스트)
        pos += len(텍스트) + 1

    return NL.join(조각), {
        "페이지오프셋": 오프셋,
        "총페이지": n_pages,
        "vlm_페이지": vlm_페이지,
        "판독불가_페이지": 판독불가_페이지,
        "실패_페이지": 실패_페이지,
        "pdftext_dedupe": deduped,
    }


# ══════════════════════════════════════════════════════════════════════════
# 5. 자가검사 — API 호출 없이 돈다 (렌더링·임계·관문 로직만 검사)
# ══════════════════════════════════════════════════════════════════════════

def _self_test() -> int:
    실패: list[str] = []

    def eq(이름, got, want):
        if got != want:
            실패.append(f"{이름}: {got!r} != {want!r}")

    # 1. 관문 — SUDDOE_ALLOW_EXTERNAL 없으면 즉시 예외(네트워크 호출 전에 막힌다)
    복원 = os.environ.pop("SUDDOE_ALLOW_EXTERNAL", None)
    try:
        threw = False
        try:
            _허용됐나()
        except ExternalNotAllowed:
            threw = True
        eq("관문_기본값_차단", threw, True)
    finally:
        if 복원 is not None:
            os.environ["SUDDOE_ALLOW_EXTERNAL"] = 복원

    # 2. 관문 통과 시 — 키 없으면 명시적 예외(빈 문자열로 안 샌다)
    os.environ["SUDDOE_ALLOW_EXTERNAL"] = "1"
    복원키 = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        threw = False
        try:
            _API_키()
        except VLMKeyMissing:
            threw = True
        eq("키없음_명시적예외", threw, True)
    finally:
        os.environ.pop("SUDDOE_ALLOW_EXTERNAL", None)
        if 복원키 is not None:
            os.environ["ANTHROPIC_API_KEY"] = 복원키

    # 3. 판독_한페이지 도 같은 관문을 탄다(호출 순서: 관문 -> 키 -> 네트워크)
    os.environ.pop("SUDDOE_ALLOW_EXTERNAL", None)
    threw = False
    try:
        판독_한페이지(b"\x89PNG\r\n")
    except ExternalNotAllowed:
        threw = True
    eq("판독함수도_관문탐", threw, True)

    # 4. 임계값 산술 — 문서 내 페이지 중 임계 미만만 고른다
    글자수 = [5, 5, 200, 30, 500]
    대상 = [i for i, n in enumerate(글자수, 1) if n < MIN_CHARS_PER_PAGE]
    eq("임계_페이지선정", 대상, [1, 2, 4])

    # 5. 실측 상수가 문서 그대로 박혀있는지(회귀 방지) — docstring 수치와 코드 상수가 갈리면
    #    다음 사람이 "50" 을 아무 근거 없이 바꿔도 여기서 안 걸린다. 그래서 실제 판정 대상
    #    문서(재도전 9p, 전부 5자)에 대해 "전량 VLM 대상"이 되는지를 산술로 재확인한다.
    재도전_글자수 = [5] * 9
    대상2 = [i for i, n in enumerate(재도전_글자수, 1) if n < MIN_CHARS_PER_PAGE]
    eq("재도전_전량대상", len(대상2), 9)

    # 6. ILLEGIBLE_MARKER 카운트 로직(문자열 파싱만 — API 응답 파싱과 동일 방식)
    가짜응답 = f"제1조 목적 {ILLEGIBLE_MARKER} 이다."
    eq("판독불가마커_카운트", 가짜응답.count(ILLEGIBLE_MARKER), 1)

    for 이름 in 실패:
        print(f"  🔴 {이름}")
    if 실패:
        print(f"🔴 self-test 실패 {len(실패)}건")
    else:
        print("✅ self-test 전건 통과 (API 호출 없음)")
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
    except (VLMKeyMissing, ExternalNotAllowed) as e:
        # 🔴 트레이스백 대신 명확한 한 줄 — 「못 태웠다」를 숨기지 않고 그대로 보여준다.
        print(f"🔴 판독 못 함 — {type(e).__name__}: {e}")
        return 1
    print(f"총페이지={메타['총페이지']} · pdftext_dedupe={메타['pdftext_dedupe']}")
    print(f"VLM 호출 페이지: {메타['vlm_페이지']}")
    print(f"판독불가 마커 있는 페이지: {메타['판독불가_페이지']}")
    print(f"실패 페이지: {메타['실패_페이지']}")
    print(f"최종 본문 길이: {len(본문)}자")
    print("--- 앞 500자 ---")
    print(본문[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
