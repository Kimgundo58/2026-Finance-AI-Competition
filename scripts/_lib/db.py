# -*- coding: utf-8 -*-
"""scripts/ 의 파이프라인·CLI 모듈이 쓰는 psycopg 커넥션 헬퍼.

orchestrate, retrieve, rule_lookup 등이 `from _lib import db` 후 db.connect(...) 로 쓴다.
DSN 은 환경변수 SUDDOE_DSN, 없으면 로컬 개발 DSN 이다. 이 헬퍼는 GUC app.org_id 를 설정하지
않는다. tenant.* 를 읽고 쓰는 호출부가 직접 설정한다.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# DB 에 닿지 못할 때 무한정 기다리지 않도록 기본 접속 타임아웃을 둔다(초).
_기본_접속타임아웃 = 5


def connect(dsn: str | None = None, *, autocommit: bool = False,
            connect_timeout: int | None = None) -> psycopg.Connection:
    """psycopg.connect 래퍼. dsn 을 생략하면 DSN, connect_timeout 을 생략하면 5초.

    autocommit 기본값은 False 다. 읽기 전용 경로만 True 를 넘긴다. 트랜잭션을 오래 열어 두면
    다른 세션의 DDL 과 락 대기가 생긴다.
    """
    kwargs: dict = {"connect_timeout": _기본_접속타임아웃 if connect_timeout is None
                    else connect_timeout}
    if autocommit:
        kwargs["autocommit"] = True
    return psycopg.connect(dsn or DSN, **kwargs)


@contextmanager
def borrow(conn: psycopg.Connection | None = None, **connect_kwargs) -> Iterator[psycopg.Connection]:
    """conn 이 있으면 그대로 쓰고, 없으면 새로 열어 블록이 끝날 때 닫는다."""
    owns = conn is None
    conn = conn or connect(**connect_kwargs)
    try:
        yield conn
    finally:
        if owns:
            conn.close()
