# -*- coding: utf-8 -*-
"""회귀 — 미들웨어가 거부한 401 에도 CORS 헤더가 붙는다 (CORS 가 인증보다 바깥).

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_auth_cors.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from server import auth                          # noqa: E402

ORIGIN = "http://localhost:5173"
틀린토큰 = {"Origin": ORIGIN, "Authorization": "Bearer not.a.real.jwt"}


def _앱(인증을_먼저: bool) -> TestClient:
    """Starlette 은 나중에 add 한 미들웨어가 바깥이다 — 인증을 먼저 add 해야 CORS 가 바깥에 선다."""
    app = FastAPI()

    @app.get("/api/plans")
    def _p():
        return {"ok": True}

    cors = dict(allow_origins=[ORIGIN], allow_credentials=True,
                allow_methods=["*"], allow_headers=["*"])
    if 인증을_먼저:
        app.add_middleware(auth.OrgId주입)
        app.add_middleware(CORSMiddleware, **cors)
    else:
        app.add_middleware(CORSMiddleware, **cors)
        app.add_middleware(auth.OrgId주입)
    return TestClient(app)


def test_CORS_가_바깥이면_401_에도_헤더가_붙는다():
    r = _앱(인증을_먼저=True).get("/api/plans", headers=틀린토큰)
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == ORIGIN, (
        "미들웨어가 거부한 401 에 CORS 헤더가 없다 — 브라우저는 이걸 401 이 아니라 "
        "네트워크 오류로 본다. 프론트가 「로그인하세요」를 못 띄운다"
    )


@pytest.mark.parametrize("인증_붙임", [False, True])
def test_CORS_가_바깥이면_preflight_는_토큰과_무관하게_통과한다(인증_붙임):
    """preflight 는 Authorization 이 붙든 안 붙든 200 이어야 한다."""
    머리 = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
    if 인증_붙임:
        머리["Authorization"] = "Bearer not.a.real.jwt"
    r = _앱(인증을_먼저=True).options("/api/plans", headers=머리)
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == ORIGIN


def test_순서를_뒤집으면_401_의_CORS_헤더가_사라진다():
    """순서를 뒤집으면 401 의 CORS 헤더가 사라진다 — 위 통과가 순서 덕분임을 잰다."""
    c = _앱(인증을_먼저=False)
    r = c.get("/api/plans", headers=틀린토큰)
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") is None


def test_main_의_배선에서_CORS_가_인증보다_바깥이다():
    """실제 `main.py` 배선에서 CORS 첨자가 인증보다 작은지(바깥인지) 잰다.

    목 모드에선 인증 미들웨어를 안 붙이므로 SUDDOE_MOCK=0 으로 서브프로세스에서 import 한다.
    같은 프로세스에서 재로드하면 뒷 테스트가 낡은 모듈 객체를 본다.
    """
    import json                                   # noqa: PLC0415
    import os                                     # noqa: PLC0415
    import subprocess                             # noqa: PLC0415
    import sys                                    # noqa: PLC0415

    코드 = ("import json,sys;"
           "sys.path.insert(0, r'%s');"
           "from server.main import app;"
           "print(json.dumps([m.cls.__name__ for m in app.user_middleware]))"
           % str(ROOT))
    env = {**os.environ, "SUDDOE_MOCK": "0", "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", 코드], capture_output=True,
                         text=True, env=env, cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, "하위 프로세스가 죽었다: " + out.stderr[-2000:]
    이름 = json.loads(out.stdout.strip().splitlines()[-1])

    assert "CORSMiddleware" in 이름, f"CORS 미들웨어가 안 보인다: {이름}"
    assert "OrgId주입" in 이름, f"인증 미들웨어가 안 보인다: {이름}"
    assert 이름.index("CORSMiddleware") < 이름.index("OrgId주입"), (
        f"인증이 CORS 바깥에 섰다 (바깥→안쪽 순: {이름}). "
        "401 응답에서 CORS 헤더가 사라진다"
    )
