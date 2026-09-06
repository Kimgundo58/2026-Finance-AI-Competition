# -*- coding: utf-8 -*-
"""L3 근거 기반 `due_date` — «근거가 있을 때만 날짜를 만든다».

🔴 이 테스트가 지키는 것은 정확도가 아니라 **안 띄우는 쪽으로 떨어지는가** 다.
   `check_items.기본_오프셋일` 은 52행 중 45행이 규정 근거가 없다(`기한근거='운영기본값'`).
   근거 없는 값이 캘린더에 날짜로 뜨면 사용자는 그것을 규정상 기한으로 읽는다.
"""
from __future__ import annotations

from datetime import date

import pytest

from server import l3_deadline
from server.routes_tasks import _due계산


# ── 조문 파싱 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("본문, 기대", [
    ("취득가액 500만원을 초과하는 기계장치는 취득일부터 1개월 이내에 등록하여야 한다", 30),
    ("집행일로부터 15일 이내에 증빙을 제출한다", 15),
    ("지출일 기준 2주 이내에 보고한다", 14),
    ("구입일부터 7일 이내", 7),
])
def test_환산가능한_기준점은_일수로_나온다(본문, 기대):
    got = l3_deadline._조문_기한(본문)
    assert got is not None, "환산 가능한 기한을 놓쳤다"
    assert got[0] == 기대


@pytest.mark.parametrize("본문", [
    # 🔴 우리가 가진 기준점은 집행예정일 하나뿐이다. 아래는 «계산할 수 없다» —
    #    추측해서 띄우면 운영기본값을 띄우던 것과 같은 잘못이 된다.
    "사업 종료 후 30일 이내에 정산보고서와 증빙서류를 제출하여야 한다",
    "협약종료일 30일 이전까지 창업을 완료하여야 한다",
    "이전 달 카드 사용 건을 다음달 3일까지 등록 완료",
    "멘토링비는 1인 1일 20만원 이내로 한다",          # 금액이다. 기한이 아니다
    "협약기간 : 8~10개월 이내",                      # 기간이다. 기한이 아니다
])
def test_환산_불가하거나_기한이_아니면_None(본문):
    assert l3_deadline._조문_기한(본문) is None


# ── due_date 판단 ────────────────────────────────────────────────────────
_집행일 = date(2026, 9, 10)


def test_규정근거면_오프셋을_그대로_쓴다():
    got = _due계산(_집행일, "org1", "자산관리대장에 등록하세요", None,
                  오프셋=30, 기한근거="규정근거")
    assert got == "2026-10-10"


def test_운영기본값이고_L3에_없으면_날짜를_만들지_않는다(monkeypatch):
    monkeypatch.setattr(l3_deadline, "_질의", lambda *a, **k: [])
    got = _due계산(_집행일, "org1", "비교견적 3곳 이상 확보하세요", None,
                  오프셋=-7, 기한근거="운영기본값")
    assert got is None, "근거 없는 오프셋이 날짜로 새어나갔다"


@pytest.mark.parametrize("기한근거", ["운영기본값", "미확정", None])
def test_근거가_규정근거가_아니면_오프셋을_안_쓴다(monkeypatch, 기한근거):
    monkeypatch.setattr(l3_deadline, "_질의", lambda *a, **k: [])
    assert _due계산(_집행일, "org1", "아무 항목", None,
                   오프셋=30, 기한근거=기한근거) is None


def test_L3에_근거가_있으면_그_날짜를_쓴다(monkeypatch):
    monkeypatch.setattr(l3_deadline, "_질의", lambda *a, **k: [
        (1, "제21조", "자산의 등록 및 관리",
         "취득가액 500만원을 초과하는 기계장치는 취득일부터 1개월 이내에 "
         "자산관리대장에 등록하여야 한다"),
    ])
    got = _due계산(_집행일, "org1", "취득가액 500만원 초과면 자산관리대장에 등록하세요",
                  None, 오프셋=None, 기한근거="운영기본값")
    assert got == "2026-10-10"


def test_L3가_있어도_무관한_조문이면_안_붙는다(monkeypatch):
    monkeypatch.setattr(l3_deadline, "_질의", lambda *a, **k: [
        (1, "제21조", "자산의 등록 및 관리",
         "취득가액 500만원을 초과하는 기계장치는 취득일부터 1개월 이내에 "
         "자산관리대장에 등록하여야 한다"),
    ])
    got = _due계산(_집행일, "org1", "국외 출장이면 출국 전에 주관기관에 보고하세요",
                  None, 오프셋=-14, 기한근거="미확정")
    assert got is None


def test_집행예정일이_없으면_언제나_None():
    assert _due계산(None, "org1", "무엇이든", None, 오프셋=30, 기한근거="규정근거") is None


def test_org_id가_없으면_L3를_안_본다():
    assert l3_deadline.기한_해석(None, "자산관리대장에 등록하세요") is None


def test_DB가_죽어도_안전한_쪽으로_떨어진다(monkeypatch):
    """🔴 `_질의` 는 실패해도 빈 리스트를 준다. 여기선 그게 «맞는» 동작이다 —
       장애가 「근거 없음」과 같이 «안 띄움» 으로 떨어져야 한다."""
    monkeypatch.setattr(l3_deadline, "_질의", lambda *a, **k: [])
    assert l3_deadline.기한_해석("org1", "자산관리대장에 등록하세요") is None
