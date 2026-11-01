# -*- coding: utf-8 -*-
"""서버 공용 모듈: DB 헬퍼, mock/live 모드 플래그, 비목·판정 enum, SSE 응답, 요청 유틸.

server/main.py 와 모든 routes_*.py 가 import 한다. 라우터가 main.py 를 import 하면 순환 의존이
생기므로 공용 정의를 여기에 둔다. 읽는 환경변수: SUDDOE_DSN, SUDDOE_MOCK.
RLS 계약: tenant.* 의 RLS 정책은 org_id = tenant.current_org() 이고, current_org() 는 GUC
app.org_id 를 읽는다. auth.OrgId주입 미들웨어가 검증된 org 를 컨텍스트 변수 현재_org 에 넣고,
_질의/_실행/트랜잭션은 매 트랜잭션마다 app.org_id 를 트랜잭션 한정으로 설정한다.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import Request
from fastapi.responses import StreamingResponse

로그 = logging.getLogger("suddoe.db")

ROOT = Path(__file__).resolve().parent.parent

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# SUDDOE_MOCK=1 일 때만 DB·LLM 없이 고정 응답을 낸다. 미설정이면 live 모드다.
# mock 모드는 인증 미들웨어를 건너뛰고 고정된 '가능' 을 돌려주므로 기본값이 되면 안 된다.
MOCK = os.environ.get("SUDDOE_MOCK", "0") == "1"

# 운영 설정이 있는데 mock 모드면 배포 설정 실수다. 조용히 뜨지 않고 시작 시점에 실패시킨다.
_운영전용_환경변수 = [k for k in ("SUDDOE_JWKS_URL", "SUDDOE_ADMIN_TOKEN")
                      if os.environ.get(k, "").strip()]
if MOCK and _운영전용_환경변수:
    raise RuntimeError(
        f"SUDDOE_MOCK=1 인데 운영 설정({', '.join(_운영전용_환경변수)})이 있습니다. "
        "운영 배포라면 SUDDOE_MOCK=0 으로, 로컬 mock 실행이라면 해당 변수를 비우고 다시 시작하세요.")


# ── enum ────────────────────────────────────────────────────────────
# DB 없이도 요청 검증이 되도록 코드에 같은 값을 둔다.

# 프론트 라벨과 문자열이 정확히 일치해야 한다.
비목_ENUM = ["재료비", "외주용역비", "기계장치", "인건비", "지급수수료",
             "여비", "교육훈련비", "광고선전비", "특허권등무형자산취득비", "창업활동비"]

# 창업활동비는 예비창업패키지에만 있다. 다른 사업에서는 비목 목록에서 뺀다.
창업활동비_사업 = {"예비창업패키지"}

# 판정 결과는 이 네 값으로 제한한다.
판정_ENUM = ("가능", "조건부", "불가", "판단불가")

# 해야할일의 구분. 판정 결과와는 별개 값이다.
할일구분_ENUM = ("결제전", "결제후", "집행")

# 목록 탭 -> 해당 탭에 보일 판정 값. 프론트 배지는 3종이라 판정 4종을 여기서 묶는다.
탭_판정 = {
    "전체": None,
    "확인필요": ("조건부", "판단불가"),
    "위험": ("불가",),
    "특이사항없음": ("가능",),
    "점검전": (),          # 아직 판정이 없는 draft
}


# ── 테넌트 GUC ──────────────────────────────────────────────────────
# GUC 를 설정하지 않으면 RLS 정책에 의해 쓰기는 거부되고 읽기는 0행이 된다. 값은 검증된
# 주체만 넣는다(auth.OrgId주입). 이 모듈은 auth 를 import 하지 않으므로 클라이언트가 보낸
# org_id(검증되지 않은 값)가 여기로 들어올 경로가 없다.

현재_org: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "suddoe_현재_org", default=None)


@contextlib.contextmanager
def org_고정(org_id: str | None):
    """HTTP 요청 밖(스크립트, 백그라운드 작업, 테스트)에서 현재_org 를 설정한다."""
    토큰 = 현재_org.set(str(org_id) if org_id else None)
    try:
        yield
    finally:
        현재_org.reset(토큰)


def _rls_org_설정(conn_or_cur, org: str | None) -> None:
    """열린 트랜잭션에 app.org_id 를 트랜잭션 한정으로 설정한다. org 가 없으면 아무것도 안 한다.

    SET LOCAL 은 바인딩 파라미터를 받지 않으므로 set_config(..., true) 를 쓴다. 세션 GUC
    (is_local=false)로 설정하면 커넥션을 재사용할 때 앞 요청의 org 가 남는다.
    """
    if org is None:
        return
    conn_or_cur.execute("SELECT set_config('app.org_id', %s, true)", (str(org),))


# ── DB ──────────────────────────────────────────────────────────────
# 문장마다 새 커넥션을 연다. autocommit 을 켜면 트랜잭션 한정 GUC 가 다음 문장에서 사라져
# RLS 정책에 의해 0행이 되므로 끄고 쓴다.

def _문장머리(sql: str) -> str:
    return " ".join(sql.split())[:80]


def _실패기록(sql: str, e: Exception) -> None:
    # 예외 메시지는 남기지 않는다. psycopg 오류 본문에 호스트·포트·사용자명이 들어 있다.
    # mock 모드는 DB 가 없는 것이 정상이라 DEBUG 로 낮춘다.
    로그.log(logging.DEBUG if MOCK else logging.WARNING,
            "DB 문장 실패 sqlstate=%s 예외=%s 문장=%s",
            getattr(e, "sqlstate", None), type(e).__name__, _문장머리(sql))


def _질의(sql: str, 인자: tuple = (), *, 예외전파: bool = False) -> list[tuple]:
    """SELECT 를 실행하고 모든 행을 반환한다. 실패하면 빈 리스트를 반환한다.

    빈 리스트는 '데이터 없음' 과 'DB 장애' 를 구분하지 못한다. 둘을 구분해야 하는 호출부와
    새로 작성하는 코드는 예외전파=True 로 예외를 받는다.
    """
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            _rls_org_설정(conn, 현재_org.get())          # 질의와 같은 트랜잭션이어야 한다
            return conn.execute(sql, 인자).fetchall()
    except Exception as e:                                    # noqa: BLE001
        if 예외전파:
            raise
        _실패기록(sql, e)
        return []


def _실행(sql: str, 인자: tuple = (), *, 예외전파: bool = False) -> int:
    """INSERT/UPDATE/DELETE 를 실행하고 커밋한다. 성공하면 rowcount, 실패하면 -1.

    -1 은 '0행' 이 아니라 '예외가 발생했고 삼켰다' 는 뜻이다. 새로 작성하는 쓰기 코드는
    예외전파=True 를 쓰거나, 여러 문장이면 트랜잭션() 을 쓴다.
    """
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            _rls_org_설정(conn, 현재_org.get())          # 쓰기와 같은 트랜잭션, 커밋 전
            cur = conn.execute(sql, 인자)
            conn.commit()
            return cur.rowcount
    except Exception as e:                                    # noqa: BLE001
        if 예외전파:
            raise
        _실패기록(sql, e)
        return -1


@contextlib.contextmanager
def 트랜잭션(org_id: str | None = None) -> Iterator[Any]:
    """커넥션 하나로 여러 문장을 한 트랜잭션에서 실행한다. psycopg 커서를 yield 한다.

    org_id 를 넘기면 그 값을, 아니면 현재_org 컨텍스트 변수를 app.org_id 로 설정한다.
    org_id 인자에는 검증된 값만 넘긴다. 블록이 정상 종료하면 커밋하고, 예외가 나면 롤백한 뒤
    예외를 다시 던진다. RLS 적용 테이블에 여러 문장을 쓰는 새 코드는 이것을 쓴다.
    """
    import psycopg
    conn = psycopg.connect(DSN, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            _rls_org_설정(cur, org_id or 현재_org.get())
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _org조건(org_id: str | None, 별칭: str = "p") -> tuple[str, tuple]:
    """org_id 로 행을 거르는 WHERE 조각과 인자. None 이면 게스트 행(org_id IS NULL)만 고른다.

    RLS 와 별개로 코드에서도 거른다. 다른 기관 행이 섞이면 테넌트 유출이다.
    """
    if org_id is None:
        return f"{별칭}.org_id IS NULL", ()
    return f"{별칭}.org_id = %s", (org_id,)


def _jsonb(v):
    """JSONB 컬럼 바인딩용 래퍼. psycopg 는 dict 를 자동 변환하지 않는다.

    mock 모드에서는 호출되지 않으므로 psycopg 없이도 import 되도록 지연 import 한다.
    """
    from psycopg.types.json import Json
    return Json(v)


# ── 요청 유틸 ───────────────────────────────────────────────────────

def _int환경(키: str, 기본: int) -> int:
    """정수 환경변수. 없거나 정수가 아니면 기본값."""
    try:
        return int(os.environ.get(키, 기본))
    except ValueError:
        return 기본


def 클라이언트_ip(req: Request) -> str:
    """요청자 IP. X-Forwarded-For 의 첫 값을 쓰고, 없으면 소켓 주소를 쓴다.

    운영 앞단의 Caddy(deploy/ncp/Caddyfile)는 trusted_proxies 가 없어서 클라이언트가 보낸
    X-Forwarded-For 를 버리고 실제 접속 IP 로 다시 쓴다. 그래서 첫 값을 믿을 수 있다.
    """
    xff = req.headers.get("x-forwarded-for", "")
    return (xff.split(",")[0].strip() if xff else None) or (req.client.host if req.client else "?")


def ttl_캐시(ttl_env: str, 기본초: int, 빈값캐시: bool = False) -> Callable:
    """시간 기반 캐시 데코레이터. 인자 조합별로 결과를 TTL 동안 재사용한다.

    TTL 은 데코레이트 시점에 환경변수 ttl_env(없으면 기본초)에서 읽는다. 빈 값(falsy)은
    기본적으로 저장하지 않는다. DB 오류가 빈 결과로 돌아오는 _질의() 결과를 TTL 동안 들고
    있지 않기 위해서다. 예외도 저장하지 않는다. 인자 조합이 적은 함수에만 쓴다(크기 제한 없음).
    감싼 함수의 .무효화() 로 저장된 결과를 모두 지운다.
    """
    ttl = _int환경(ttl_env, 기본초)

    def 장식(함수: Callable) -> Callable:
        잠금 = threading.Lock()
        저장: dict[Any, tuple[float, Any]] = {}

        @functools.wraps(함수)
        def 감싼(*args, **kwargs):
            키 = (args, tuple(sorted(kwargs.items())))
            with 잠금:
                항목 = 저장.get(키)
            if 항목 is not None and time.monotonic() - 항목[0] < ttl:
                return 항목[1]
            값 = 함수(*args, **kwargs)
            if 값 or 빈값캐시:
                with 잠금:
                    저장[키] = (time.monotonic(), 값)
            return 값

        def 무효화() -> None:
            with 잠금:
                저장.clear()

        감싼.무효화 = 무효화
        return 감싼

    return 장식


# ── SSE ─────────────────────────────────────────────────────────────

def _sse(이름: str, 값: Any) -> str:
    return f"event: {이름}\ndata: {json.dumps(값, ensure_ascii=False, default=str)}\n\n"


def _sse응답(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive",
        "X-Accel-Buffering": "no",           # 리버스 프록시가 SSE 를 버퍼링하지 않게 한다
    })
