# -*- coding: utf-8 -*-
"""회귀 — **L3 업로드의 org_id 를 Form 으로 사칭할 수 없다.**

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_auth_l3_org.py -q

🔴 왜 이 파일이 따로 있나 (2026-09-03)

`auth.OrgId주입` 미들웨어는 `scope["query_string"]` «만» 갈아끼운다. 그래서
「토큰이 있으면 토큰이 이긴다」가 쿼리 파라미터 축에서는 성립하는데
**multipart Form 축에서는 성립하지 않았다.** L3 업로드가 그 축을 쓴다.

재현했을 때 나온 값 (수정 전) — 같은 토큰·같은 요청, 축만 다르다:

    쿼리스트링 org_id=<남의 org>  → SELECT 인자가 «토큰의 org»      막힘
    Form       org_id=<남의 org>  → INSERT 에 «남의 org» 가 박힘    🔴 안 막힘

박히는 자리가 `tenant.l3_documents.org_id` → `l3_articles.org_id` 이고 그게 판정
검색의 기관 축이라, **남의 기관에 규정을 심을 수 있었다.** 심어진 규정은 그 기관의
판정에 L3 로 얹힌다 — 조용히 틀리는 종류다.

🔴 `SUDDOE_ORG_PARAM=0`(자기신고 폴백 끄기)으로도 이 축은 «안» 닫혔다. 그 스위치는
   미들웨어의 쿼리 자기신고만 본다. 그래서 그 경우를 아래에서 따로 잰다 —
   「폴백을 끄면 다 막힌다」가 참이 아니라는 게 이 파일이 남기는 사실이다.

■ **DB 를 안 붙인다.** `_질의` 를 갈아끼워 «INSERT 에 바인딩되는 org_id» 를 잡는다.
  이 파일이 재는 건 「어느 org 가 박히는가」지 「DB 가 받아주는가」가 아니다.
  (`test_l3_파싱전이.py` 가 후자를 실 DB 로 잰다 — 축이 다르다)
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

# conftest 에 「이 파일은 목 경로가 아니다」를 알린다 — `_실_업로드` 를 태워야
# INSERT 인자를 잴 수 있다. 🔴 그렇다고 DB 에 붙지는 않는다 (`_질의` 를 갈아끼운다).
실DB = True

A = "426162ba-437b-57d0-be60-a492c64e4f57"      # 토큰 주인
B = "0148ccca-dab8-5fc5-b961-bf5ffde23e85"      # 남의 기관

_PDF = b"%PDF-1.4\n" + b"x" * 64


@pytest.fixture
def 태우기(monkeypatch, tmp_path):
    """업로드를 한 번 태우고 «INSERT 에 박힌 org_id» 와 상태코드를 돌려준다."""
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
    """🔴 이 파일의 핵심. 이 줄이 빨개지면 남의 기관에 규정을 심을 수 있다는 뜻이다."""
    코드, 박힌 = 태우기(form_org=B, 토큰_org=A)
    assert 코드 == 202
    assert 박힌 == A, (
        f"검증된 토큰의 org 는 {A} 인데 Form 이 신고한 {B} 가 박혔다 — "
        "TENANT_LEAK: 남의 기관에 L3 규정을 심을 수 있다"
    )


def test_토큰이_있으면_Form_이_같은_org_여도_그대로_통과한다(태우기):
    """정상 프론트 경로가 이 수정으로 안 깨지는지 — 회귀의 반대쪽."""
    코드, 박힌 = 태우기(form_org=A, 토큰_org=A)
    assert (코드, 박힌) == (202, A)


def test_폴백이_켜져_있으면_토큰_없는_업로드는_자기신고를_쓴다(태우기, monkeypatch):
    """R4 결정(마감 전까지 폴백 유지)이 지켜지는지. 🔴 이건 «막힌다» 가 아니라
    «아직 안 막는다» 를 못 박는 테스트다 — 스위치를 켜야 막힌다는 걸 아래가 잰다."""
    monkeypatch.setattr(auth, "ORG_PARAM_허용", True)
    코드, 박힌 = 태우기(form_org=B, 토큰_org=None)
    assert (코드, 박힌) == (202, B)


def test_폴백을_끄면_토큰_없는_업로드가_401_로_막힌다(태우기, monkeypatch):
    """🔴 수정 전에는 여기가 202 였다 — `SUDDOE_ORG_PARAM=0` 이 쿼리 축만 닫고
    Form 축은 열어 뒀기 때문이다. 「폴백을 끄면 다 막힌다」가 참이 되게 하는 줄이다."""
    monkeypatch.setattr(auth, "ORG_PARAM_허용", False)
    코드, 박힌 = 태우기(form_org=B, 토큰_org=None)
    assert 코드 == 401
    assert 박힌 is None, "401 인데 INSERT 까지 갔다 — 거부가 저장을 못 막았다"


def test_위조_토큰은_업로드에_닿지도_못한다(태우기, monkeypatch):
    """미들웨어가 앞에서 끊는 축. 여기서 401 이 아니면 위 세 줄의 전제가 무너진다."""
    monkeypatch.setattr(routes_l3, "_질의", lambda *a, **k: pytest.fail("DB 까지 갔다"))
    app = FastAPI()
    app.include_router(routes_l3.router)
    app.add_middleware(auth.OrgId주입)
    r = TestClient(app).post("/api/l3/upload",
                             files={"파일": ("규정.pdf", _PDF, "application/pdf")},
                             data={"org_id": B},
                             headers={"Authorization": "Bearer not.a.real.jwt"})
    assert r.status_code == 401
