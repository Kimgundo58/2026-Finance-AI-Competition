# -*- coding: utf-8 -*-
"""psycopg 연결 헬퍼 — scripts/ 전역에 63벌 흩어져 있던 `psycopg.connect(DSN)` 을 걷는다.

호출은 항상 모듈 접두어를 붙인다 (`_lib.db.connect(...)`) — `from _lib.db import
connect` 로 이름만 들여오지 않는다. 지역 변수 `conn`·함수 `연결()` 과 이름이
겹칠 자리가 많아서다.

DSN 은 환경변수 `SUDDOE_DSN` 이 우선이고, 없으면 기존 로컬 개발 DSN 으로 대체한다
(전환 전 각 파일에 박혀 있던 리터럴과 동일 — 여기로 값이 바뀌는 게 아니라
정의를 한 곳으로 모으는 것뿐이다).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")


def connect(dsn: str | None = None, *, autocommit: bool = False,
            connect_timeout: int | None = None) -> psycopg.Connection:
    """`psycopg.connect` 래퍼. dsn 생략 시 DSN(환경변수 SUDDOE_DSN 우선) 사용.

    🔴 `autocommit` 기본값은 False 로 유지한다. 읽기 전용 병렬 경로(retrieve.py·
    eval_retrieval.py·orchestrate.py 등)만 명시적으로 True 를 넘긴다 — 트랜잭션을
    붙들면 다른 세션의 DDL 과 교착한다는 게 2026-08-31 실측이지만, 쓰기 경로까지
    기본값으로 끌어올리면 문장 단위 원자성이 깨진다.
    """
    kwargs: dict = {}
    if autocommit:
        kwargs["autocommit"] = True
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    return psycopg.connect(dsn or DSN, **kwargs)


@contextmanager
def borrow(conn: psycopg.Connection | None = None, **connect_kwargs) -> Iterator[psycopg.Connection]:
    """기존 연결이 있으면 그대로 쓰고, 없으면 새로 열어 이 블록이 끝날 때만 닫는다.

    `eval_store.기록()`/`읽기()` 의 `닫기 = conn is None` 패턴을 그대로 흡수한 것 —
    호출자가 이미 연결을 들고 있으면(예: 한 트랜잭션으로 여러 헬퍼를 묶을 때)
    재사용하고, 직접 연 것만 책임지고 닫는다.
    """
    owns = conn is None
    conn = conn or connect(**connect_kwargs)
    try:
        yield conn
    finally:
        if owns:
            conn.close()
