# -*- coding: utf-8 -*-
"""회귀 — L3 업로드의 org_id 를 multipart Form 으로 사칭할 수 없다 (토큰이 이긴다).

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_auth_l3_org.py -q

DB 는 안 붙인다. `_질의` 를 갈아끼워 INSERT 에 바인딩되는 org_id 만 잡는다.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI                      # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402

from server import auth, routes_l3               # noqa: E402

# conftest 에 실 경로(`_실_업로드`) 를 알린다. DB 에는 붙지 않는다.
실DB = True

A = "426162ba-437b-57d0-be60-a492c64e4f57"      # 토큰 주인
B = "0148ccca-dab8-5fc5-b961-bf5ffde23e85"      # 남의 기관

_PDF = b"%PDF-1.4\n" + b"x" * 64


@pytest.fixture
def 태우기(monkeypatch, tmp_path):
    """업로드를 한 번 태우고 (상태코드, INSERT 에 박힌 org_id) 를 돌려준다."""
    박힌: list = []

    def _가짜질의(sql, 인자=(), **_):
        if sql.lstrip().upper().startswith("INSERT"):
            박힌.append(인자[0])
            return [(uuid.uuid4(),)]
        return []

    monkeypatch.setattr(routes_l3, "_질의", _가짜질의)
    monkeypatch.setattr(routes_l3, "파싱_배경", lambda doc_id, org_id: None)
    monkeypatch.setattr(routes_l3, "L3_저장소", tmp_path)
    monkeypatch.setattr(routes_l3, "MOCK", False)       # conftest 뒤에 한 번 더 못 박는다

    app = FastAPI()
    app.include_router(routes_l3.router)
    app.add_middleware(auth.OrgId주입)
    client = TestClient(app)

    def _태우기(form_org: str, 토큰_org: str | None = None):
        박힌.clear()
        머리 = {}
        if 토큰_org:
            머리["Authorization"] = f"Bearer {auth.데모토큰_발급(토큰_org)[0]}"
        r = client.post("/api/l3/upload",
                        files={"파일": ("규정.pdf", _PDF, "application/pdf")},
                        data={"org_id": form_org}, headers=머리)
        return r.status_code, (str(박힌[0]) if 박힌 else None)

    return _태우기


def test_토큰이_있으면_Form_의_남의_org_는_무시된다(태우기):
    """토큰의 org 가 Form 의 org 를 이긴다."""
    코드, 박힌 = 태우기(form_org=B, 토큰_org=A)
    assert 코드 == 202
    assert 박힌 == A, (
        f"검증된 토큰의 org 는 {A} 인데 Form 이 신고한 {B} 가 박혔다 — "
        "TENANT_LEAK: 남의 기관에 L3 규정을 심을 수 있다"
    )


def test_토큰이_있으면_Form_이_같은_org_여도_그대로_통과한다(태우기):
    """정상 프론트 경로(토큰 org == Form org) 는 그대로 통과한다."""
    코드, 박힌 = 태우기(form_org=A, 토큰_org=A)
    assert (코드, 박힌) == (202, A)


def test_폴백이_켜져_있으면_토큰_없는_업로드는_자기신고를_쓴다(태우기, monkeypatch):
    """폴백이 켜져 있으면 토큰 없는 업로드는 Form 의 org 를 그대로 쓴다."""
    monkeypatch.setattr(auth, "ORG_PARAM_허용", True)
    코드, 박힌 = 태우기(form_org=B, 토큰_org=None)
    assert (코드, 박힌) == (202, B)


def test_폴백을_끄면_토큰_없는_업로드가_401_로_막힌다(태우기, monkeypatch):
    """폴백을 끄면 Form 축도 401 로 막힌다."""
    monkeypatch.setattr(auth, "ORG_PARAM_허용", False)
    코드, 박힌 = 태우기(form_org=B, 토큰_org=None)
    assert 코드 == 401
    assert 박힌 is None, "401 인데 INSERT 까지 갔다 — 거부가 저장을 못 막았다"


def test_위조_토큰은_업로드에_닿지도_못한다(태우기, monkeypatch):
    """위조 토큰은 미들웨어가 401 로 끊는다."""
    monkeypatch.setattr(routes_l3, "_질의", lambda *a, **k: pytest.fail("DB 까지 갔다"))
    app = FastAPI()
    app.include_router(routes_l3.router)
    app.add_middleware(auth.OrgId주입)
    r = TestClient(app).post("/api/l3/upload",
                             files={"파일": ("규정.pdf", _PDF, "application/pdf")},
                             data={"org_id": B},
                             headers={"Authorization": "Bearer not.a.real.jwt"})
    assert r.status_code == 401
