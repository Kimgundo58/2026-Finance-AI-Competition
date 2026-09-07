# -*- coding: utf-8 -*-
"""회귀 — DB 장애를 「등록되지 않은 계정」(403) 이 아니라 503 으로 보고한다.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_auth_db_fail.py -q

아무도 안 듣는 포트로 DSN 을 돌려 접속 실패만 만든다. 실 DB 는 만지지 않는다.
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

# 아무도 안 듣는 포트
죽은DSN = "postgresql://postgres:devpw@localhost:59999/suddoe"


@pytest.fixture
def DB없음(monkeypatch):
    monkeypatch.setattr(_common, "DSN", 죽은DSN)
    yield


def test_DB_가_죽으면_403_이_아니라_503_이다(DB없음):
    """DB 접속 실패는 503 이어야 한다."""
    with pytest.raises(HTTPException) as e:
        auth._계정조회("nobody@example.com")
    assert e.value.status_code == 503, (
        f"DB 접속 실패인데 {e.value.status_code} 가 나왔다 — "
        "「등록 안 된 계정」과 「DB 다운」이 같은 응답이면 로그에서 안 갈린다"
    )


def test_503_사유에_접속정보가_새지_않는다(DB없음):
    """503 사유에 psycopg 오류 본문(host·port·user) 이 실리지 않아야 한다 — 인증 전 응답이다."""
    with pytest.raises(HTTPException) as e:
        auth._계정조회("nobody@example.com")
    사유 = str(e.value.detail)
    for 조각 in ("59999", "postgres", "devpw", "localhost", "psycopg"):
        assert 조각 not in 사유, f"503 사유에 접속정보 «{조각}» 이 실려 나간다: {사유!r}"


def test_DB_는_살아_있는데_계정이_없으면_그대로_None_이다():
    """진짜 미등록은 예외가 아니라 None 이어야 403 이 유지된다."""
    assert auth._계정조회("아무도아닌사람@example.invalid") is None


def test_기본_질의는_여전히_삼킨다(DB없음):
    """`_질의` 기본값은 실패 시 빈 리스트다 — 다른 호출부가 이 계약에 기댄다."""
    assert _common._질의("SELECT 1") == []


def test_예외전파를_켜면_던진다(DB없음):
    """`예외전파=True` 면 접속 실패가 예외로 나온다."""
    with pytest.raises(Exception):
        _common._질의("SELECT 1", 예외전파=True)
