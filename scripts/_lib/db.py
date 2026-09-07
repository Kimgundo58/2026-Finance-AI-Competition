# -*- coding: utf-8 -*-
"""psycopg 연결 헬퍼. 호출은 `db.connect(...)` 처럼 모듈 접두어를 붙인다.

DSN 은 환경변수 `SUDDOE_DSN` 이 우선, 없으면 로컬 개발 DSN.
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

    `autocommit` 기본값은 False. 읽기 전용 경로만 True 를 넘긴다 — 트랜잭션을 붙들면 다른 세션의 DDL 과 교착한다.
    """
    kwargs: dict = {}
    if autocommit:
        kwargs["autocommit"] = True
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    return psycopg.connect(dsn or DSN, **kwargs)


@contextmanager
def borrow(conn: psycopg.Connection | None = None, **connect_kwargs) -> Iterator[psycopg.Connection]:
    """기존 연결이 있으면 그대로 쓰고, 없으면 새로 열어 블록이 끝날 때 닫는다."""
    owns = conn is None
    conn = conn or connect(**connect_kwargs)
    try:
        yield conn
    finally:
        if owns:
            conn.close()
