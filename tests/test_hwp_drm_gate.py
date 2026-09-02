# -*- coding: utf-8 -*-
"""HWP 배포용(DRM) 게이트 + 빈 추출 게이트 회귀 테스트.

    pytest tests/test_hwp_drm_gate.py -q

■ 무엇을 지키나
  ① 실물 DRM 문서(TIPS 총괄 운영지침 「본문」)가 `stage0_extract.extract()` 에서
     조용히 성공하지 않고 `HwpProtectedError` 로 정체를 담아 실패한다
  ② 정상 hwp(비압축·배포용 아님)는 그대로 파싱된다 — 게이트가 정상 문서를 안 막는다
  ③ `추출_품질_점검()` 은 던지지 않고 값을 돌려준다. 글자수 임계치(200자)만 하드
     게이트로 쓰고, 조수==0 은 단독으로 판단불가를 트리거하지 않는다(목록·현황표류
     정상 문서가 실측상 다수 있다 — 아래 합성 테스트가 그 실측 수치를 재현한다)

DB 를 쓰지 않는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hwpx_extract as hx    # noqa: E402
import stage0_extract as s0  # noqa: E402

_TIPS_DIR = ROOT / "2026_Finance_DATA_FOR_RAG/창진원/민관공동창업자발굴육성(TIPS)"
실물_DRM = [
    _TIPS_DIR / "2023/첨부 1. 2023년 팁스TIPS 총괄 운영지침 일부개정 본문.hwp",
    _TIPS_DIR / "2024/첨부 1. 2024년 팁스TIPS 총괄 운영지침 3차 개정_본문.hwp",
    _TIPS_DIR / "2025/첨부 1. 2025년 팁스TIPS 총괄 운영지침 4차 개정_본문.hwp",
]
실물_정상 = ROOT / "2026_Finance_DATA_FOR_RAG/창진원/초기창업패키지/초기창업패키지 세부관리기준(2023년).hwp"


# ── ① 실물 DRM 문서 ──────────────────────────────────────────────────
@pytest.mark.parametrize("f", 실물_DRM, ids=lambda f: f.name)
def test_실물_DRM_hwp는_조용히_성공하지_않는다(f):
    if not f.exists():
        pytest.skip(f"실물 픽스처 없음: {f}")
    assert hx.sniff(f).startswith("HWP-DRM: 배포용")
    with pytest.raises(hx.HwpProtectedError, match="배포용"):
        s0.extract(f)


# ── ② 정상 문서는 그대로 파싱된다 ────────────────────────────────────
@pytest.mark.skipif(not 실물_정상.exists(), reason="실물 정상 hwp 픽스처 없음")
def test_실물_정상_hwp는_그대로_파싱된다():
    kind, (text, _) = s0.extract(실물_정상)
    assert kind == "text"
    q = s0.추출_품질_점검(text)
    assert q["판단불가"] is False
    assert q["조수"] >= 20                        # 실측 107개(2026-09-02)


# 참고: `olefile` 은 쓰기 API 가 없어 FileHeader 플래그만 다른 합성 OLE 픽스처를
# 만들 수 없다. 플래그 분기(`_hwp_ole_kind`)는 실물 전수(DRM 8개·정상 35개, 위 ①②)로
# 대신 검증한다 — 실측(2026-09-02) 로 100% 정확히 갈렸다(105페이지 근거는 보고 참고).


# ── ③ 빈 추출 게이트 — 값으로 돌아온다, 조수==0 단독으로는 안 걸린다 ────
@pytest.mark.parametrize("글자수, 기대_판단불가", [
    (0, True),
    (106, True),                    # 실측 DRM 최소값
    (107, True),                    # 실측 DRM 최대값
    (199, True),
    (200, False),                   # 임계치 경계 — 200은 통과
    (430, False),                   # 실측 최단 정상 문서("재도전성공패키지 주관기관 현황")
])
def test_임계치_경계는_실측값_그대로다(글자수, 기대_판단불가):
    text = "가" * 글자수
    q = s0.추출_품질_점검(text)
    assert q["글자수"] == 글자수
    assert q["판단불가"] is 기대_판단불가
    assert bool(q["사유"]) is 기대_판단불가


def test_조0개_목록문서는_길이만_넘으면_판단불가가_아니다():
    """실측: TIPS 운영사 현황(2023) 11,413자·조 0개 — 정상 문서다."""
    text = "창업기업명, 대표자, 소재지\n" * 400          # 조가 없는 표 형태 목록을 흉내
    q = s0.추출_품질_점검(text)
    assert q["조수"] == 0
    assert q["글자수"] > s0.빈_추출_글자수_임계치
    assert q["판단불가"] is False


def test_짧고_조도_없으면_판단불가다():
    q = s0.추출_품질_점검("이 문서는 상위 버전의 배포용 문서입니다.")
    assert q["조수"] == 0
    assert q["판단불가"] is True
    assert "임계치" in q["사유"]


def test_품질점검은_예외를_던지지_않는다():
    for text in ("", "가", "가" * 100000):
        q = s0.추출_품질_점검(text)                      # 예외 없이 항상 dict 를 돌려줘야 한다
        assert isinstance(q, dict) and {"글자수", "조수", "판단불가", "사유"} <= q.keys()
