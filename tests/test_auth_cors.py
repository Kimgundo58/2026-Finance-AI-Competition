# -*- coding: utf-8 -*-
"""회귀 — **미들웨어가 거부한 401 에도 CORS 헤더가 붙는다.**

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_auth_cors.py -q

🔴 이 파일은 «고친 것» 이 아니라 «닫힌 것을 못 박는» 파일이다 (2026-09-03).

지적은 「`auth._거부()` 가 CORS 헤더를 안 붙인다」였다. 확인해 보니 `_거부()` 는
`OrgId주입.__call__` 안에서«만» 불리고(레포 전체 2곳) 다른 진입로가 없다. 그래서
CORS 헤더가 붙느냐는 `_거부()` 자신이 아니라 **미들웨어 순서**가 정한다.
실측한 값 — 같은 `_거부()`, 순서만 다르다:

                                   틀린 Bearer GET   preflight   preflight
                                                     (Auth 없음)  (Auth 붙임)
    인증을 먼저 add → CORS 가 바깥   401 · ACAO 있음   200         200
    CORS 를 먼저 add → 인증이 바깥   401 · ACAO 없음 🔴 200         401

🔴 **여기서 한 번 틀렸다가 정정했다.** 처음엔 「뒤집으면 preflight 도 401 로 죽는다」고
   적었는데, 그건 재현 스크립트가 OPTIONS 에 Authorization 을 «인위적으로» 붙였기
   때문이다. 브라우저는 preflight 에 Authorization 을 싣지 않는다 — 헤더가 없으면
   `토큰해석()` 이 None 을 주고 그대로 통과한다. **뒤집혀도 preflight 는 안 깨진다.**
   깨지는 것은 401 응답의 CORS 헤더 «하나» 다. (관측은 맞았고 추론이 틀렸다)

그래도 그 하나가 중요하다 — 헤더가 없으면 브라우저는 401 을 «401 로» 읽지 못하고
네트워크 오류로 처리한다. 프론트가 「로그인하세요」를 못 띄우고 원인 없는 실패가 된다.

그러므로 `_거부()` 에 헤더를 손으로 붙이는 건 **틀린 수리**다 — 붙이면 CORS 설정
(허용 오리진 목록)을 두 벌로 만들게 되고, 순서가 바르면 애초에 필요가 없다.
고칠 것은 순서고, 순서는 이미 바르다. 🔴 **없는 문제를 고치는 대신 순서를 못 박는다.**

■ 아래 ①②는 앱을 «복제» 해서 성질만 잰다 (`server/main.py` 와 무관하게 항상 돈다).
■ ③ 은 실제 `main.py` 의 배선 순서를 잰다 — 소스를 읽지 않고 `app.user_middleware`
  목록만 본다. 🔴 이 줄이 빨개지면 그건 「테스트가 낡았다」가 아니라
  **「401·preflight 가 브라우저에서 깨졌다」** 는 뜻이다.
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
    """🔴 Starlette 은 «나중에 add 한 것이 바깥» 이다 (`add_middleware` 가 insert(0)).
    그래서 인증을 먼저 add 해야 CORS 가 바깥에 선다."""
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
    """preflight 는 Authorization 이 붙든 안 붙든 200 이어야 한다.

    ⚠️ 브라우저는 preflight 에 Authorization 을 «안» 싣는다 — `인증_붙임=True` 는
       실제 브라우저 경로가 아니라 프록시·확장이 끼어드는 경우를 덮는 여분이다.
    """
    머리 = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
    if 인증_붙임:
        머리["Authorization"] = "Bearer not.a.real.jwt"
    r = _앱(인증을_먼저=True).options("/api/plans", headers=머리)
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == ORIGIN


def test_순서를_뒤집으면_401_의_CORS_헤더가_사라진다():
    """🔴 위 두 줄이 «순서 덕분» 이라는 증거. 이게 통과해야 위가 우연이 아니다.

    ⚠️ 뒤집어도 «브라우저의» preflight(Authorization 없음)는 200 이다 — 그래서
       이 회귀는 preflight 로는 안 잡힌다. 401 의 ACAO 로만 잡힌다.
    """
    c = _앱(인증을_먼저=False)
    r = c.get("/api/plans", headers=틀린토큰)
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") is None


def test_main_의_배선에서_CORS_가_인증보다_바깥이다():
    """실제 앱의 순서를 잰다. `user_middleware[0]` 이 «가장 바깥» 이므로
    CORS 의 첨자가 인증보다 «작아야» 한다.

    🔴 소스를 읽지 않고 배선된 객체만 본다 — `main.py` 는 다른 세션 소유다.
    🔴 **`SUDDOE_MOCK=0` 이어야 보인다.** `main.py` 는 목 모드에서 인증 미들웨어를
       «일부러» 안 붙인다 — 목 서버엔 JWKS 도 DB 도 없어서 붙이면 Bearer 를 든 요청이
       `/api` 전 경로에서 503/403 이 된다. 기본이 MOCK=1 이라 그냥 import 하면 CORS
       하나만 나오고, 그건 결함이 아니라 설계다.
    🔴 **서브프로세스로 격리한다.** 같은 프로세스에서 `server.main` 을 재로드하면
       이미 그 모듈을 잡고 있는 «뒷» 테스트들이 낡은 객체를 보게 된다 —
       2026-09-03 에 그렇게 했다가 무관한 테스트 5건이 빨개졌다.
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
