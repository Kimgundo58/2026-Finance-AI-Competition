# -*- coding: utf-8 -*-
"""서버 공용 — DB 접속·질의, 목/실 모드 스위치, 비목·판정 enum, SSE 포맷터.

라우터가 `main.py` 를 import 하면 순환참조가 나므로 여기로 뺐다.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from pathlib import Path
from typing import Any

from fastapi.responses import StreamingResponse

ROOT = Path(__file__).resolve().parent.parent

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# SUDDOE_MOCK=1(기본) 이면 DB·GPU 없이 목 데이터로 응답한다.
MOCK = os.environ.get("SUDDOE_MOCK", "1") == "1"


# ── enum — DB 가 없어도 계약이 지켜지도록 코드에 한 벌 둔다 ──────────

# 프론트 라벨과 이 문자열이 그대로 일치해야 한다.
비목_ENUM = ["재료비", "외주용역비", "기계장치", "인건비", "지급수수료",
             "여비", "교육훈련비", "광고선전비", "특허권등무형자산취득비", "창업활동비"]

# 창업활동비는 예비창업패키지에만 있다. 나머지 사업에서는 목록에서 뺀다.
창업활동비_사업 = {"예비창업패키지"}

# 판정 4-way. 폐쇄 enum 이다.
판정_ENUM = ("가능", "조건부", "불가", "판단불가")

# 진행 상태. 판정과는 다른 축이다.
계획상태_ENUM = ("draft", "judged")

# 할일 축 — 판정 4-way 와 또 다른 축이다.
할일상태_ENUM = ("준비필요", "집행예정", "완료")
할일구분_ENUM = ("결제전", "결제후", "집행")
할일유형_ENUM = ("기타", "계약", "비교견적")

# 목록 탭 → 판정 매핑. 프론트 탭은 배지 3종이라 4-way 를 여기서 접는다.
탭_판정 = {
    "전체": None,
    "확인필요": ("조건부", "판단불가"),
    "위험": ("불가",),
    "특이사항없음": ("가능",),
    "점검전": (),          # 판정이 아직 없는 draft
}


# ── 테넌트 GUC — RLS 에 「나는 어느 기관인가」를 알린다 ─────────────
#
# `tenant.*` 의 RLS 정책은 `org_id = tenant.current_org()` 다. GUC 를 안 세우면
# 쓰기는 거부되고 읽기는 0행이 된다. 값은 검증된 주체(`auth.OrgId주입`)만 넣는다 —
# 이 파일은 `auth` 를 import 하지 않으므로 요청의 자기신고 값이 들어올 길이 없다.

현재_org: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "suddoe_현재_org", default=None)


@contextlib.contextmanager
def org_고정(org_id: str | None):
    """HTTP 밖(스크립트·배경작업·테스트)에서 org 를 세운다. HTTP 경로에서는 쓰지 않는다."""
    토큰 = 현재_org.set(str(org_id) if org_id else None)
    try:
        yield
    finally:
        현재_org.reset(토큰)


def _org_세우기(conn) -> None:
    """열린 트랜잭션에 `app.org_id` 를 트랜잭션 한정으로 건다.

    `SET LOCAL` 은 바인딩 파라미터를 못 받으므로 `set_config(..., true)` 를 쓴다.
    세션 GUC(local=false)로 하면 커넥션 재사용 시 앞 요청의 org 가 샌다.
    주체가 없으면 아무것도 안 세운다 — RLS 가 쓰기를 막고 읽기는 0행이 된다.
    """
    org = 현재_org.get()
    if org is None:
        return
    conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org),))


# ── DB ──────────────────────────────────────────────────────────────

def _질의(sql: str, 인자: tuple = (), *, 예외전파: bool = False) -> list[tuple]:
    """SELECT 용. 실패하면 빈 리스트 — «0건» 을 «데이터 없음» 으로 읽지 말 것.

    `예외전파=True` 면 삼키지 않고 예외를 올린다. DB 장애와 «없음» 을 갈라야 하는
    호출부만 켠다.
    """
    try:
        import psycopg
        # autocommit 을 켜지 마라 — 트랜잭션 한정 GUC 가 다음 문장에서 사라져 전부 0행이 된다.
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            _org_세우기(conn)          # 질의와 같은 트랜잭션이어야 한다
            return conn.execute(sql, 인자).fetchall()
    except Exception:                                         # noqa: BLE001
        if 예외전파:
            raise
        return []


def _실행(sql: str, 인자: tuple = (), *, 예외전파: bool = False) -> int:
    """INSERT/UPDATE 용. 성공하면 rowcount, 실패하면 -1.

    `-1` 은 「0행」이 아니라 「예외를 삼켰다」다. `예외전파=True` 계약은 `_질의()` 와 같다.
    """
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            _org_세우기(conn)          # INSERT/UPDATE 와 같은 트랜잭션. commit 전이다
            cur = conn.execute(sql, 인자)
            conn.commit()
            return cur.rowcount
    except Exception:                                         # noqa: BLE001
        if 예외전파:
            raise
        return -1


# ── SSE ─────────────────────────────────────────────────────────────

def _sse(이름: str, 값: Any) -> str:
    return f"event: {이름}\ndata: {json.dumps(값, ensure_ascii=False, default=str)}\n\n"


def _sse응답(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive",
        "X-Accel-Buffering": "no",           # nginx 뒤에서 SSE 가 버퍼링에 갇히는 걸 막는다
    })
