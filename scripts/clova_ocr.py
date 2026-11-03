# -*- coding: utf-8 -*-
"""텍스트 레이어가 없는 PDF(스캔본)를 네이버클라우드 CLOVA OCR(General)로 읽어 평문을 만든다.

l3_parse 가 L3 업로드 중 스캔본으로 판정한 PDF 에만 부른다. 공개 함수: 설정됨(), 최대_쪽수(),
pdf_문자인식(경로) -> (본문, {문자오프셋: 페이지번호}) — text_extract.extract_pdf 와 같은 모양이다.
실패하면 문자인식오류(실패사유 코드 포함)를 올린다.

쪽마다 JPEG 로 렌더링해 한 쪽씩 호출한다. PDF 를 그대로 보내면 한 번에 10쪽까지만 읽고, 여러 쪽 응답에서
쪽을 어떻게 나누는지 API 문서에 나와 있지 않아서다. 요금은 PDF 도 쪽당 1회 호출이라 같다.
메모리를 일정하게 두려고 _동시_호출 쪽씩 렌더링하고 호출한다.

환경변수(없으면 설정되지 않은 것으로 보고, 스캔본은 지금처럼 실패로 기록한다):
  SUDDOE_OCR_URL       CLOVA OCR 도메인의 APIGW Invoke URL(/general 까지)
  SUDDOE_OCR_SECRET    도메인의 Secret Key(X-OCR-SECRET 헤더)
  SUDDOE_OCR_MAX_PAGES 문서당 쪽수 상한(기본 100). 넘으면 호출하지 않고 실패(쪽수초과)로 기록한다

직접 확인: PYTHONIOENCODING=utf-8 python scripts/clova_ocr.py <PDF 경로>   # 실제 API 를 호출한다(쪽당 과금)
API 문서: https://api.ncloud-docs.com/docs/ai-application-service-ocr-ocr
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_log = logging.getLogger(__name__)

_렌더_배율 = 200 / 72        # 200 DPI. A4 한 쪽이 약 1654×2339 픽셀
_JPEG_품질 = 85
_동시_호출 = 4
_호출_제한시간 = 60          # 초. 쪽 하나 기준
_재시도_대기 = (1.0, 3.0)    # 429·5xx·한도 초과(0025)일 때 재시도 전 대기(초)


class 문자인식오류(Exception):
    """OCR 을 끝내지 못했다. 실패사유 는 server/routes_l3.py `_실패_안내` 의 코드다."""

    def __init__(self, 실패사유: str, 사유: str):
        super().__init__(사유)
        self.실패사유 = 실패사유


def 설정됨() -> bool:
    return bool(os.environ.get("SUDDOE_OCR_URL") and os.environ.get("SUDDOE_OCR_SECRET"))


def 최대_쪽수() -> int:
    try:
        return max(1, int(os.environ.get("SUDDOE_OCR_MAX_PAGES", "100")))
    except ValueError:
        return 100


def 쪽_본문(결과: dict) -> str:
    """응답의 images[0] 하나를 평문으로 바꾼다. lineBreak 가 참인 단어 뒤에서 줄을 바꾼다."""
    줄들, 줄 = [], []
    for f in 결과.get("fields") or []:
        t = (f.get("inferText") or "").strip()
        if t:
            줄.append(t)
        if f.get("lineBreak") and 줄:
            줄들.append(" ".join(줄))
            줄 = []
    if 줄:
        줄들.append(" ".join(줄))
    return "\n".join(줄들)


def _쪽_인식(client, 이미지: bytes, 쪽: int) -> dict:
    """JPEG 한 쪽을 보내 응답의 images[0] 을 돌려준다. 일시 오류는 재시도한다."""
    본문 = {
        "version": "V2", "requestId": str(uuid.uuid4()), "timestamp": int(time.time() * 1000),
        "lang": "ko",
        "images": [{"format": "jpg", "name": f"p{쪽}", "data": base64.b64encode(이미지).decode()}],
    }
    헤더 = {"X-OCR-SECRET": os.environ["SUDDOE_OCR_SECRET"], "Content-Type": "application/json"}
    for 시도, 대기 in enumerate((*_재시도_대기, None)):
        r = client.post(os.environ["SUDDOE_OCR_URL"], headers=헤더, content=json.dumps(본문))
        일시오류 = r.status_code == 429 or r.status_code >= 500 or '"0025"' in r.text[:300]
        if r.status_code == 200:
            images = r.json().get("images") or []
            if images and images[0].get("inferResult") == "SUCCESS":
                return images[0]
            메시지 = images[0].get("message") if images else "images 없음"
            raise 문자인식오류("문자인식실패", f"{쪽}쪽 인식 결과 {메시지}")
        if not 일시오류 or 대기 is None:
            # 응답 본문에는 키가 없다. 시크릿은 로그·사유에 남기지 않는다
            raise 문자인식오류("문자인식실패", f"{쪽}쪽 HTTP {r.status_code}: {r.text[:200]}")
        _log.warning("CLOVA OCR %d쪽 HTTP %d — %.0f초 뒤 다시 시도(%d)", 쪽, r.status_code, 대기, 시도 + 1)
        time.sleep(대기)
    raise AssertionError("도달하지 않는다")


def pdf_문자인식(경로: Path) -> tuple[str, dict[int, int]]:
    """스캔본 PDF → (평문, {문자오프셋: 페이지번호}). 쪽수가 상한을 넘으면 호출 없이 실패한다."""
    import httpx
    import pypdfium2 as pdfium

    if not 설정됨():
        raise 문자인식오류("문자인식실패", "SUDDOE_OCR_URL·SUDDOE_OCR_SECRET 이 설정되지 않았다")
    pdf = pdfium.PdfDocument(str(경로))
    try:
        쪽수 = len(pdf)
        if 쪽수 > 최대_쪽수():
            raise 문자인식오류("쪽수초과", f"{쪽수}쪽 — OCR 은 문서당 {최대_쪽수()}쪽까지 한다")
        본문들: list[str] = []
        with httpx.Client(timeout=_호출_제한시간) as client, ThreadPoolExecutor(_동시_호출) as 실행기:
            for 시작 in range(0, 쪽수, _동시_호출):
                # pdfium 은 스레드 안전하지 않아 렌더링은 이 스레드에서 하고 호출만 나눠 보낸다
                묶음 = []
                for i in range(시작, min(시작 + _동시_호출, 쪽수)):
                    page = pdf[i]
                    buf = io.BytesIO()
                    page.render(scale=_렌더_배율).to_pil().convert("RGB").save(
                        buf, "JPEG", quality=_JPEG_품질)
                    page.close()
                    묶음.append((i + 1, buf.getvalue()))
                결과들 = 실행기.map(lambda x: _쪽_인식(client, x[1], x[0]), 묶음)
                본문들.extend(쪽_본문(r) for r in 결과들)
    finally:
        pdf.close()

    from text_extract import 서로게이트_정리
    parts, offsets, pos = [], {}, 0
    for i, t in enumerate(본문들, 1):
        t = 서로게이트_정리(t, f"{경로.name} {i}쪽")
        offsets[pos] = i
        parts.append(t)
        pos += len(t) + 1
    return "\n".join(parts), offsets


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if len(sys.argv) != 2:
        sys.exit("사용법: python scripts/clova_ocr.py <PDF 경로>")
    시작 = time.time()
    본문, 오프셋 = pdf_문자인식(Path(sys.argv[1]))
    print(f"{len(오프셋)}쪽 · {len(본문)}자 · {time.time() - 시작:.1f}초")
    print(본문[:800])
