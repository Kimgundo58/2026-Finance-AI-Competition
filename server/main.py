# -*- coding: utf-8 -*-
"""「써도돼요」 백엔드 FastAPI 앱. 앱 생성·미들웨어·라우터 등록·예외 처리기만 둔다.

    기본(SUDDOE_MOCK 미설정)  live 모드. normalize_run.정규화() 와 orchestrate.판정() 을 실제로 호출한다
    SUDDOE_MOCK=1             mock 모드(명시해야 켜진다). DB·LLM 없이 고정 응답을 돌려준다
                              (server/selftest.py 의 mock_정규화·mock_판정)

라우터
    catalog            GET  /api/vocab, /api/programs
    routes_normalize   POST /api/normalize (SSE)
    routes_judge       POST /api/judge (SSE)
    routes_questions   POST /api/questions (판정 전 확인 질문)
    routes_profile     GET·PUT /api/profile
    routes_admin       GET /admin/status, /admin/queue · POST /admin/warmup (X-Admin-Token)
    routes_feedback    PUT /api/decisions/{id}/feedback (맞았어요·틀렸어요), 틀렸어요 목록은 routes_admin GET /admin/feedback
    routes_ingest      /admin/documents — L1·L2 규정 적재 (X-Admin-Token, SUDDOE_INGEST_DSN 없으면 503)
    routes_plans · routes_tasks · routes_l3 · routes_orgs   지출계획·할일·기관 자체규정(L3)·기관 목록

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

from server import (auth, catalog, routes_accounts, routes_admin, routes_feedback, routes_ingest,  # noqa: E402
                    routes_judge, routes_l3, routes_normalize, routes_orgs,
                    routes_plans, routes_profile, routes_questions, routes_tasks)
from server._common import MOCK, _정수_환경변수, 판정_ENUM  # noqa: E402

app = FastAPI(title="써도돼요 API", version="0.1.0",
              description="창업지원금 지출의 사업비 집행 가능 여부 판정 API.")


@app.on_event("startup")
def _기동시_워밍업() -> None:
    """기동 단계에서 임베딩·비목 후보 조회 모델을 동기로 로드해 첫 요청이 모델 로드를 기다리지 않게 한다.

    백그라운드 스레드로 미루면 로드가 끝나기 전에 들어온 첫 판정 요청이 모델 로드 시간만큼 늦어진다.
    mock 모드에서는 하지 않는다. SUDDOE_WARMUP_ON_START=0 으로 끌 수 있다.
    """
    if MOCK or os.environ.get("SUDDOE_WARMUP_ON_START", "1") == "0":
        return
    결과 = routes_admin.모델_워밍업()
    # uvicorn 기본 로깅 설정은 uvicorn.* 로거만 출력하므로 그 로거로 남긴다.
    logging.getLogger("uvicorn.error").info("[suddoe] 기동 워밍업 %s", 결과)


# add_middleware 는 나중에 추가한 것이 바깥이 된다. 인증 미들웨어를 먼저 추가해 CORS 안쪽에 두어야
# 401·503 응답에도 Access-Control-Allow-Origin 이 붙는다(없으면 브라우저가 인증 거부를 CORS 오류로 보여준다).
# mock 모드에서는 붙이지 않는다. JWKS·DB 가 없어 Bearer 검증이 503·403 이 된다.
if not MOCK:
    app.add_middleware(auth.OrgId주입)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get(
        "SUDDOE_CORS", "http://localhost:3000,http://localhost:5173").split(",") if o],
    # 프론트 PR 미리보기 배포(주소가 PR마다 바뀜)와 로컬 개발 서버. 인증은 Bearer 토큰이라 출처를 열어도 토큰은 안 샌다.
    allow_origin_regex=r"https://2026-finance-ai-competition-frontend-[a-z0-9-]+-team-9ea8\.vercel\.app|http://localhost:3000",
    # DELETE 가 없으면 브라우저의 사전 요청(OPTIONS)이 400 으로 막혀 실제 DELETE 요청이 서버에 닿지 않는다.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

for _router_모듈 in (routes_plans, routes_tasks, routes_l3, routes_orgs, routes_accounts, catalog,
                    routes_normalize, routes_questions, routes_judge, routes_feedback, routes_profile,
                    routes_admin, routes_ingest):
    app.include_router(_router_모듈.router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "모드": "mock" if MOCK else "live",
            "판정값목록": list(판정_ENUM),
            "판정_enum": list(판정_ENUM),  # 구 이름 호환(제거 예정: 다음 릴리스)
            "시각": datetime.now(timezone.utc).isoformat()}


@app.post("/api/gpu/wake", tags=["gpu"])
def gpu_wake() -> dict:
    """구버전 프론트 호환용 빈 응답. 자체 GPU 서버를 쓰지 않으므로 할 일이 없다."""
    return {}


@app.exception_handler(RequestValidationError)
def _검증오류(req: Request, exc: RequestValidationError):
    """422 를 다른 오류와 같은 {오류, 상태, 필드} 모양으로 돌려준다(pydantic 기본은 {"detail": [...]})."""
    첫_오류 = (exc.errors() or [{}])[0]
    메시지 = str(첫_오류.get("msg", "입력이 올바르지 않습니다")).replace("Value error, ", "")
    필드경로 = [str(x) for x in (첫_오류.get("loc") or []) if x != "body"]
    return JSONResponse(status_code=422,
                        content={"오류": 메시지, "상태": 422,
                                 "필드": ".".join(필드경로) or None})


@app.exception_handler(HTTPException)
def _http오류(req: Request, exc: HTTPException):
    """모든 4xx·5xx 를 {오류, 상태} 로 통일한다. 프론트가 판정 화면과 같은 틀로 오류를 표시한다."""
    return JSONResponse(status_code=exc.status_code,
                        content={"오류": exc.detail, "상태": exc.status_code})


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from server import selftest
        sys.exit(selftest.main())
    import uvicorn
    uvicorn.run(app, host=os.environ.get("SUDDOE_HOST", "127.0.0.1"),
                port=_정수_환경변수("SUDDOE_PORT", 8080))
