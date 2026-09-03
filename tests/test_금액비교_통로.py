# -*- coding: utf-8 -*-
"""금액 비교가 실판정 경로에서 도는가.   **[F1 회귀 잠금]**

🔴 **결함 (2026-09-03 검증 ai-a3 발견 · 중앙 ai-43 재확인 · ai-0b 실측).**
`rule_lookup.effective_rule(..., *, 수치=None, ...)` 에 통로가 있는데
`orchestrate.py:207·539` 의 `_effective(cur, 사업명, 비목, 기관ID)` 가 **안 넘긴다.**
그래서 한도가 붙은 비목이면 조립 프롬프트에 늘 「금액 비교 결과: 비교 불가」가 나간다 —
CLAUDE.md 확정 원칙 «검색·룰 조회·**금액 비교**·효력 결정은 코드가 한다» 위반이다.

**고치는 쪽은 `scripts/orchestrate.py` 소유자다. 이 파일은 「고쳐진 뒤 다시 안 풀리게」만 잠근다.**
여기서는 `orchestrate` 를 읽기만 한다 — 프로덕션 0줄.

■ 문자열을 보지 않는다
  「비교 불가」라는 **문구는 바뀐다.** `룰["금액비교"]["초과"]` 가 `None` 인지를 본다.
  🔴 `금액비교` **키는 수치를 안 넘겨도 이미 생긴다** (`{"초과": None, "사유": "…입력되지 않았다"}`).
     그래서 키 유무가 아니라 **`초과` 가 정해졌는가**가 축이다.

■ 왜 xfail(strict) 인가
  지금 이 둘은 **실제로 실패한다** — 아래 실측이 근거다. `strict` 라서 결함이 닫히는 순간
  XPASS 로 **빨개진다**: 「이제 xfail 을 떼고 잠가라」는 신호다. 조용히 초록이 되지 않는다.

■ 실측 (2026-09-03 · 로컬 DB · `corpus.rules`)
  - `판정(dry=True)` 로 외주용역비 질문을 태우면 `_effective` 가 **`수치` 없이** 불린다
    (`kw: []` · 결과 `{"초과": None, "사유": "비율 비교에 필요한 선급금과 계약총액 이 입력되지 않았다"}`)
  - 🔴 **금액만 이어서는 어느 한도도 안 풀린다.** 한도 유형별로 더 필요하다 —
    비율(외주용역비)=계약총액 · 개수(기계장치)=수량·인원 · 금액(창업활동비)=월수 ·
    지급수수료=하위항목. `정규화` 스키마에는 금액뿐이라(`llm_schema.py:156`) 나머지는
    F1 프로필·폼에서 와야 한다. **「수치를 넘기기만 하면 된다」가 아니다.**
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

실DB = True                      # conftest 에 알린다 — 이 파일은 로컬 Postgres 를 읽는다

import orchestrate as 오케                                     # noqa: E402
import rule_lookup as 룰조회                                   # noqa: E402

# 비율 한도가 붙어 있고 하위항목 한정이 없는 쌍 — 실측으로 고른 것이다.
# (기계장치는 수량·인원, 창업활동비는 월수, 지급수수료는 하위항목이 더 필요해 축이 흐려진다)
사업 = "예비창업패키지"
비목 = "외주용역비"
질문 = "외주용역비로 앱 개발 외주를 주는데 선급금 3000만원을 먼저 줍니다"


@pytest.fixture
def 실경로_포착(monkeypatch):
    """`_effective` 를 감싸 **실제 호출 인자와 실제 반환 룰**을 잡는다. 대체하지 않는다."""
    포착: list[dict] = []
    진짜 = 오케._B_effective
    assert 진짜 is not None, "rule_lookup 을 못 붙였다 — 이 테스트는 성립하지 않는다"

    def 스파이(cur, 사업명, 비목명, 기관ID=None, **kw):
        룰 = 진짜(cur, 사업명, 비목명, 기관ID, **kw)
        포착.append({"비목": 비목명, "받은키": sorted(kw), "수치": kw.get("수치"), "룰": 룰})
        return 룰

    monkeypatch.setattr(오케, "_B_effective", 스파이)
    return 포착


def _외주용역비_호출(포착: list) -> dict:
    """LLM·GPU 를 안 탄다 — `dry=True` 는 프롬프트 조립까지만 간다. `기록=False` 로 DB 쓰기 없음."""
    오케.판정(질문, 사업명=사업, dry=True, 기록=False)
    본 = [c for c in 포착 if c["비목"] == 비목]
    assert 본, f"실경로가 {비목} 으로 `_effective` 를 부르지 않았다 — 잡을 것이 없다"
    return 본[-1]


# 🔴 xfail 을 뗐다 (2026-09-03 · #17 머지 dc9d63f · 67ef75f). ①은 닫혔다 — 통로가 이어졌다.
#    아래 ②는 «여전히» xfail 이다. F1 은 필요조건이고 충분조건이 아니다 —
#    한도가 요구하는 재료(계약총액·수량·월수·하위항목)를 정규화 스키마가 애초에 안 뽑는다.
def test_실경로가_effective_rule에_수치를_넘긴다(실경로_포착):
    """① 통로. 이것만으로는 부족하지만, 이게 안 되면 그 뒤는 볼 것도 없다."""
    본 = _외주용역비_호출(실경로_포착)
    assert 본["수치"], (
        f"`_effective` 가 받은 키워드 인자: {본['받은키']} — `수치` 가 없다. "
        f"`rule_lookup.effective_rule` 의 `수치=` 통로가 실경로에서 안 쓰인다")


@pytest.mark.xfail(strict=True, reason=
                   "금액 비교가 실경로에서 한 번도 안 돈다 (F1 계약/협약총액 미전달). "
                   "고쳐지면 XPASS 로 빨개진다 — 그때 이 표시를 떼라")
def test_한도_붙은_비목의_금액비교가_정해진다(실경로_포착):
    """② 축. 🔴 「비교 불가」라는 **문구**가 아니라 `초과` 가 `None` 인지를 본다.

    한도가 있는데 `초과` 가 `None` 이면 LLM 프롬프트에 「비교 불가」가 실려 나간다 —
    금액 비교를 코드가 안 하고 모델에게 떠넘긴 상태다.
    """
    본 = _외주용역비_호출(실경로_포착)
    룰 = 본["룰"]
    assert 룰 and 룰.get("한도_값") is not None, (
        f"{사업}/{비목} 에 한도가 없다 — 축이 성립하지 않는 쌍이다. `corpus.rules` 를 다시 골라라")
    비교 = 룰.get("금액비교") or {}
    assert 비교.get("초과") is not None, (
        f"한도 {룰['한도_값']}{룰.get('한도_단위') or ''} 가 있는데 비교가 안 됐다 — "
        f"사유: {비교.get('사유')!r} · 넘어온 수치: {본['수치']!r}")


def test_통로만_이으면_실제로_풀린다():
    """🔴 ②가 **닫을 수 있는 결함**인지 확인한다 — 못 닫을 것을 잠그면 영구 빨강이다.

    같은 룰에 `수치` 를 직접 주면 `초과` 가 정해진다. 즉 남은 것은 **전달**뿐이다.
    동시에 「금액만으로는 안 풀린다」도 여기서 잠근다 — 그게 실측이다.
    """
    import psycopg
    from server._common import DSN

    with psycopg.connect(DSN, connect_timeout=3) as conn, conn.cursor() as cur:
        금액만 = 룰조회.effective_rule(cur, 사업, 비목, None, 수치={"금액": 30_000_000})
        온전 = 룰조회.effective_rule(cur, 사업, 비목, None,
                                   수치={"금액": 30_000_000, "계약총액": 50_000_000})

    assert (금액만 or {}).get("금액비교", {}).get("초과") is None, (
        "금액만으로 비율 한도가 풀렸다 — 실측(2026-09-03)과 다르다. 이 파일의 전제를 다시 재라")
    assert (온전 or {}).get("금액비교", {}).get("초과") is not None, (
        "계약총액까지 줘도 비교가 안 된다 — ②는 전달 문제가 아니다. 잠그기 전에 원인을 다시 찾아라")
