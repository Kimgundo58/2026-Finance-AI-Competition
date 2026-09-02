# -*- coding: utf-8 -*-
"""회귀 — `rule_lookup.비목계통()` 폴백 제거 (2026-09-02).

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_rule_lookup.py -q

🔴 왜 이 테스트가 필요한가
`비목계통()` 은 "사업명을 안 줬다"(`None`, 공통 문항)와 "준 사업명을 못 찾았다"
(모르는 표기)를 갈라야 한다. 이전에는 후자도 조용히 `"창업"`으로 떨어졌다 — TIPS 를
프론트 표기(`"2026 민관공동 창업자 발굴·육성"`)로 넘기면 창업 계통 L1 지침이 근거를
달고 발화했다(2026-09-02 W3 조사: `corpus.rules` 를 창업계통=True 로 강제 재현하면
`rule_id 421`(조건부, 근거 `L1_중소기업창업_지원사업_통합관리지침` 제36조·제41조 —
"대표자 인건비 불가")이 그대로 붙었다). 이 파일은 그 폴백이 다시 살아나지 않는지를 잰다.
`ai-ae` 커밋 `dc39f28` 의 후속 — pytest 전체 실행에서 이 경로가 간접적으로는
`test_e2e_flow`·`test_persist` 를 타지만 **직접 잰 적은 없었다** — 이 파일이 그 직접
회귀다.

🔴 이 파일은 읽기만 한다 — `INSERT`/`UPDATE`/`DELETE` 없음. `corpus.rules`·
`corpus.programs` 의 기존 행을 그대로 쓴다. 8세션이 같은 DB 를 쓰는 밤이라 쓰기 없는
게 안전이다 — 픽스처를 만들고 지울 필요 자체가 없다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from _lib.db import connect          # noqa: E402
import rule_lookup as rl             # noqa: E402

실DB = True

프론트_TIPS = "2026 민관공동 창업자 발굴·육성"   # ai-c7 이 번들 원문에서 뽑은 실제 표기(U+00B7)
정본_TIPS = "TIPS"


def _db있음() -> bool:
    try:
        with connect(autocommit=True) as c:
            r = c.execute("SELECT to_regclass('corpus.rules')").fetchone()
            return r[0] is not None
    except Exception:                                          # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _db있음(), reason="corpus DB 미기동 — 룰 조회 회귀 스킵")


@pytest.fixture()
def cur():
    """읽기 전용. autocommit — 트랜잭션을 붙들고 있지 않는다(다른 세션과 안 부딪힌다)."""
    with connect(autocommit=True) as conn, conn.cursor() as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════
# ① 비목계통() 세 갈래 — 이게 이번 변경의 핵심이다
# ══════════════════════════════════════════════════════════════════════════

def test_비목계통_미지정이면_창업(cur):
    """사업명을 아예 안 주면(공통 문항) 기존대로 '창업'. 안 바뀐 갈래."""
    assert rl.비목계통(cur, None) == "창업"


def test_비목계통_정본이면_실제값(cur):
    """정본 표기는 실제 계통을 돌려준다 — TIPS 는 RND(창업이 아니다)."""
    assert rl.비목계통(cur, 정본_TIPS) == "RND"


def test_비목계통_모르는표기면_None_이지_창업이_아니다(cur):
    """🔴 핵심 단언 — '준 이름을 못 찾음'은 None 이지 '창업'이 아니다.
    이 둘이 같은 값으로 돌아오면 이번 수정은 아무 의미가 없다."""
    결과 = rl.비목계통(cur, 프론트_TIPS)
    assert 결과 is None
    assert 결과 != "창업"


# ══════════════════════════════════════════════════════════════════════════
# ② base_룰()
# ══════════════════════════════════════════════════════════════════════════

def test_base_룰_모르는사업명이면_빈리스트(cur):
    """조회 자체를 접는다 — 창업계통을 가정하고 L1 공통행을 붙이지 않는다."""
    assert rl.base_룰(cur, 프론트_TIPS, "인건비") == []


def test_base_룰_정본이면_예외없이_돈다(cur):
    """정본 표기는 최소한 예외 없이 조회가 돈다. TIPS 자체는 `corpus.rules` 0행이
    정상(위임 계통이 달라 rules 에 없다 — CLAUDE.md 8사업 스코프), 그래서 행수를
    단정하지 않고 '리스트가 돌아온다'만 잰다. 창업 계통 사업(예비창업패키지)은
    실제로 행이 있어야 한다 — 조회 자체가 죽은 게 아님을 대조로 확인."""
    행 = rl.base_룰(cur, 정본_TIPS, "인건비")
    assert isinstance(행, list)
    행2 = rl.base_룰(cur, "예비창업패키지", "인건비")
    assert len(행2) > 0


# ══════════════════════════════════════════════════════════════════════════
# ③ l3적용가능()
# ══════════════════════════════════════════════════════════════════════════

def test_l3적용가능_모르는사업명이면_False(cur):
    assert rl.l3적용가능(cur, 프론트_TIPS) is False


# ══════════════════════════════════════════════════════════════════════════
# ④ 회귀 — TIPS 프론트 표기 + 인건비 → 판단불가로 닫힌다
# ══════════════════════════════════════════════════════════════════════════

def test_회귀_TIPS_프론트표기_인건비는_판단불가로_닫힌다(cur):
    """2026-09-02 W3 조사 재현. 수정 전이었다면 rule_id 421(조건부, 근거
    L1_중소기업창업_지원사업_통합관리지침 제36조·제41조 — "대표자 인건비 불가")이
    창업 계통으로 오분류돼 근거를 달고 나갔다. **이 테스트가 깨지면(= None 이 아닌
    뭔가가 나오면) 그 "근거 붙은 오답" 경로가 돌아온 것이다** — 실패 자체가 그 뜻을
    말하도록 이름과 주석을 붙여 둔다."""
    결과 = rl.effective_rule(cur, 프론트_TIPS, "인건비", 기관ID=None)
    assert 결과 is None


# ══════════════════════════════════════════════════════════════════════════
# ⑤ corpus.programs 표가 없는 워킹트리 — "창업" 유지 갈래 (일부러 남긴 동작)
# ══════════════════════════════════════════════════════════════════════════

class _programs없는_커서:
    """corpus.programs 자체가 없는 워킹트리를 흉내낸다 — execute 가 예외를 던진다.
    표기 불일치와는 다른 문제(스키마 미비)라 기존 '창업' 폴백을 그대로 둔 자리다.
    DB 를 안 쓰므로 위 skipif 가드와 무관하게 항상 돈다."""

    class _가짜커넥션:
        def rollback(self):
            pass

    connection = _가짜커넥션()

    def execute(self, *a, **kw):
        raise Exception('relation "corpus.programs" does not exist')


def test_비목계통_programs표_자체가_없으면_창업_유지():
    assert rl.비목계통(_programs없는_커서(), "아무사업") == "창업"
