# -*- coding: utf-8 -*-
"""회귀 — **DB 장애를 「등록되지 않은 계정」으로 보고하지 않는다.**

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_auth_db_fail.py -q

🔴 왜 (2026-09-03)

`_common._질의` 는 기본이 «실패하면 빈 리스트» 다. 그런데 `auth._계정조회()` 에서
빈 리스트는 곧 403「등록되지 않은 계정이다」 다. 그래서 **DB 가 통째로 죽어도
사용자에게는 「너는 등록 안 됐다」로 보였다.** 재현했을 때 (수정 전):

    DB 정상 + 미등록 email   → HTTP 403  등록되지 않은 계정이다
    DB 다운 (죽은 포트)      → HTTP 403  등록되지 않은 계정이다     🔴 «완전히» 같다

상태코드도 문구도 같으면 로그에서 영영 안 갈린다. 판단불가율을 «모델선택 / 실패경로»
로 갈라 세야 하는 것과 같은 문제다 — 한 값으로 뭉치면 잘림이 판단으로 읽힌다.
운영에서는 「시연 계정 등록이 빠졌나」를 몇 시간 뒤지게 만드는 종류다.

🔴 «기본값을 뒤집지» 않았다. `_질의` 호출부가 37곳이라 전역으로 예외를 던지게 하면
   나머지 36곳이 500 이 된다. 인증 경로만 `예외전파=True` 로 켠다 — 그 선택이
   유지되는지도 아래에서 잰다.

■ DB 를 «죽여서» 잰다. 아무도 안 듣는 포트로 DSN 을 돌린다 (읽기도 쓰기도 없다).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from server import _common, auth                 # noqa: E402

# 아무도 안 듣는 포트. 🔴 실 DB 를 만지지 않는다 — 이 파일은 «접속 실패» 만 만든다.
죽은DSN = "postgresql://postgres:devpw@localhost:59999/suddoe"


@pytest.fixture
def DB없음(monkeypatch):
    monkeypatch.setattr(_common, "DSN", 죽은DSN)
    yield


def test_DB_가_죽으면_403_이_아니라_503_이다(DB없음):
    """🔴 이 파일의 핵심. 403 이 나오면 DB 장애가 「미등록 계정」으로 위장된다."""
    with pytest.raises(HTTPException) as e:
        auth._계정조회("nobody@example.com")
    assert e.value.status_code == 503, (
        f"DB 접속 실패인데 {e.value.status_code} 가 나왔다 — "
        "「등록 안 된 계정」과 「DB 다운」이 같은 응답이면 로그에서 안 갈린다"
    )


def test_503_사유에_접속정보가_새지_않는다(DB없음):
    """psycopg 의 접속 오류 본문에는 host·port·user 가 그대로 들어 있다.
    🔴 이 응답은 인증 «전» 이라 아무나 받아 본다 — 예외 문자열을 실으면 안 된다."""
    with pytest.raises(HTTPException) as e:
        auth._계정조회("nobody@example.com")
    사유 = str(e.value.detail)
    for 조각 in ("59999", "postgres", "devpw", "localhost", "psycopg"):
        assert 조각 not in 사유, f"503 사유에 접속정보 «{조각}» 이 실려 나간다: {사유!r}"


def test_DB_는_살아_있는데_계정이_없으면_그대로_None_이다():
    """수정의 반대쪽 — 진짜 미등록은 예외가 아니라 None 이어야 403 이 유지된다.
    🔴 여기서 예외가 나면 「미등록」이 「장애」로 위장된다. 반대 방향의 같은 오류다."""
    assert auth._계정조회("아무도아닌사람@example.invalid") is None


def test_기본_질의는_여전히_삼킨다(DB없음):
    """🔴 기본값을 뒤집지 않았다는 것을 못 박는다. 나머지 36개 호출부는
    「DB 없어도 서버는 뜬다」에 기대고 있다 — 그 계약이 안 깨졌는지 잰다."""
    assert _common._질의("SELECT 1") == []


def test_예외전파를_켜면_던진다(DB없음):
    """스위치 자체가 동작하는지. 이게 죽으면 위 503 이 조용히 403 으로 돌아간다."""
    with pytest.raises(Exception):
        _common._질의("SELECT 1", 예외전파=True)
