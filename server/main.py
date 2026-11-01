# -*- coding: utf-8 -*-
"""「써도돼요」 백엔드 FastAPI 앱. 앱 생성·미들웨어·라우터 등록·예외 처리기만 둔다.

    SUDDOE_MOCK=1 (기본)  고정 응답을 돌려준다. DB·LLM 없이 뜬다 (server/selftest.py 의 목_정규화·목_판정)
    SUDDOE_MOCK=0         normalize_run.정규화() 와 orchestrate.판정() 을 실제로 호출한다

라우터
    catalog            GET  /api/vocab, /api/programs
    routes_normalize   POST /api/normalize (SSE)
    routes_judge       POST /api/judge (SSE)
    routes_profile     GET·PUT /api/profile
    routes_admin       GET /admin/cost, /admin/queue · POST /admin/warmup (X-Admin-Token)
    routes_plans · routes_tasks · routes_l3 · routes_orgs   지출계획·할일·기관 규정(L3)·기관 목록

실행:
    PYTHONIOENCODING=utf-8 python -m uvicorn server.main:app --reload --port 8080
    PYTHONIOENCODING=utf-8 SUDDOE_MOCK=1 python -m server.selftest      # 서버 없이 API 계약 검사
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))   # 판정 파이프라인 모듈(orchestrate·retrieve 등)을 바로 import 한다
sys.path.insert(0, str(ROOT))               # `python server/main.py` 로 실행해도 server 패키지가 보이게

from server import (auth, catalog, routes_admin, routes_judge,  # noqa: E402
                    routes_l3, routes_normalize, routes_orgs, routes_plans,
                    routes_profile, routes_tasks)
from server._common import MOCK, _int환경, 판정_ENUM  # noqa: E402

app = FastAPI(title="써도돼요 API", version="0.1.0",
              description="창업지원금 지출의 사업비 집행 가능 여부 판정 API.")


@app.on_event("startup")
def _기동시_워밍업() -> None:
    """기동 단계에서 임베딩·비목확정 모델을 동기로 로드해 첫 요청이 모델 로드를 기다리지 않게 한다.

    백그라운드 스레드로 미루지 않는 이유: Cloud Run 은 요청을 처리하는 동안에만 CPU 를 할당한다.
    mock 모드에서는 하지 않는다. SUDDOE_WARMUP_ON_START=0 으로 끌 수 있다.
    """
    if MOCK or os.environ.get("SUDDOE_WARMUP_ON_START", "1") == "0":
        return
    결과 = routes_admin.모델_워밍업()
    # uvicorn 기본 로깅 설정은 uvicorn.* 로거만 출력하므로 그 로거로 남긴다.
    logging.getLogger("uvicorn.error").info("[suddoe] 기동 워밍업 %s", 결과)


# add_middleware 는 나중에 추가한 것이 바깥이 된다. 인증 미들웨어를 먼저 추가해 CORS 안쪽에 두어야
# 401·503 응답에도 Access-Control-Allow-Origin 이 붙는다(없으면 브라우저가 인증 거부를 CORS 오류로 보여준다).
# mock 모드에서는 붙이지 않는다 — JWKS·DB 가 없어 Bearer 검증이 503·403 이 된다.
if not MOCK:
    app.add_middleware(auth.OrgId주입)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get(
        "SUDDOE_CORS", "http://localhost:3000,http://localhost:5173").split(",") if o],
    # DELETE 가 없으면 브라우저의 사전 요청(OPTIONS)이 400 으로 막혀 실제 DELETE 요청이 서버에 닿지 않는다.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

for _router_모듈 in (routes_plans, routes_tasks, routes_l3, routes_orgs, catalog,
                    routes_normalize, routes_judge, routes_profile, routes_admin):
    app.include_router(_router_모듈.router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "모드": "mock" if MOCK else "live",
            "판정_enum": list(판정_ENUM), "시각": datetime.now(timezone.utc).isoformat()}


@app.post("/api/gpu/wake", tags=["gpu"])
def gpu_wake() -> dict:
    """구버전 프론트 호환용 빈 응답. 자체 GPU 서버를 쓰지 않으므로 할 일이 없다."""
    return {}


@app.exception_handler(RequestValidationError)
def _검증오류(req: Request, exc: RequestValidationError):
    """422 를 다른 오류와 같은 {오류, 상태, 필드} 모양으로 돌려준다(pydantic 기본은 {"detail": [...]})."""
    첫 = (exc.errors() or [{}])[0]
    말 = str(첫.get("msg", "입력이 올바르지 않습니다")).replace("Value error, ", "")
    자리 = [str(x) for x in (첫.get("loc") or []) if x != "body"]
    return JSONResponse(status_code=422,
                        content={"오류": 말, "상태": 422,
                                 "필드": ".".join(자리) or None})


@app.exception_handler(HTTPException)
def _http오류(req: Request, exc: HTTPException):
    """모든 4xx·5xx 를 {오류, 상태} 로 통일한다(models.오류응답)."""
    return JSONResponse(status_code=exc.status_code,
                        content={"오류": exc.detail, "상태": exc.status_code})


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from server import selftest
        sys.exit(selftest.main())
    import uvicorn
    uvicorn.run(app, host=os.environ.get("SUDDOE_HOST", "127.0.0.1"),
                port=_int환경("SUDDOE_PORT", 8080))
