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

# DB 에 연결할 수 없을 때 무한정 기다리지 않도록 기본 접속 타임아웃을 둔다(초).
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


# ── 시스템 오류 재시도(v053, 지시서 4절) ─────────────────────────────────
# DB 연결 끊김(OperationalError · InterfaceError)과 쿼리 시간 초과(QueryCanceled, OperationalError 의 하위)는 시스템 오류다.
# 확인 필요로 바꾸지 않고 서버 예산이 끝날 때까지 그 단계를 새 연결로 다시 한다. 예산이 끝나면 DB시스템오류 를 올리고
# 라우트가 판정실패 · 다시시도로 응답한다(research_runtime.시스템오류). 그 밖의 DB 오류(문법 · 제약 위반)는 코드 오류라 그대로 올린다.
재시도_오류 = (psycopg.OperationalError, psycopg.InterfaceError)
_재시도_백오프초 = (1.0, 2.0, 4.0, 8.0)      # 다섯 번째부터는 8초를 되풀이한다
_예산없음_마감초 = 60.0                      # 서버 예산 밖(CLI · 검사 · 판정 워커 전 읽기)에서 다시 시도하는 상한

try:
    from research_runtime import current_budget as _현재예산, 시스템오류 as _시스템오류, 재시도_기록 as _재시도_기록
except ImportError:                          # scripts/ 가 sys.path 에 없는 호출(이 모듈만 쓰는 CLI)
    _현재예산, _재시도_기록 = (lambda: None), (lambda *a, **k: None)

    class _시스템오류(Exception):            # type: ignore[no-redef]
        def __init__(self, 단계, 원인, 시도=0):
            super().__init__(f"{단계}: {원인}({시도}회 시도)")
            self.단계, self.원인, self.시도 = 단계, 원인, 시도


class DB시스템오류(_시스템오류):
    """DB 연결 끊김 · 쿼리 시간 초과가 마감까지 이어졌다. 단계 · 원인(예외 종류) · 시도 횟수를 갖는다."""


class 연결:
    """autocommit 연결 하나를 들고 있다가 끊기면 닫고, 다음 `cur()` 에서 새로 열어 RLS GUC 를 다시 건다.

    app.org_id · app.account_id 는 세션 단위(set_config(..., false))로 건다: autocommit 이라 문장마다 트랜잭션이 끝나
    트랜잭션 한정 값은 다음 문장에서 사라진다(orchestrate.판정() · question_generate.질문생성() 과 같은 방식).
    새 연결에는 GUC 가 없으므로 다시 열 때마다 건다(빠지면 tenant.* 가 0행).
    """

    def __init__(self, org_id=None, account_id=None, *, dsn: str | None = None, connect_timeout: int | None = None):
        self.org_id, self.account_id = org_id, account_id
        self._dsn, self._timeout = dsn, connect_timeout
        self._conn: psycopg.Connection | None = None
        self._cur = None

    def _열기(self) -> None:
        self._conn = connect(dsn=self._dsn, autocommit=True, connect_timeout=self._timeout)
        if self.org_id:
            self._conn.execute("SELECT set_config('app.org_id', %s, false), set_config('app.account_id', %s, false)",
                               (str(self.org_id), str(self.account_id) if self.account_id else ""))
        self._cur = self._conn.cursor()

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or getattr(self._conn, "closed", False):
            self._열기()
        return self._conn

    def cur(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._열기()
        return self._cur

    def 닫기(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:                 # noqa: BLE001 — 이미 끊긴 연결은 닫기도 실패할 수 있다
                pass
        self._conn = self._cur = None

    def __enter__(self) -> "연결":
        return self

    def __exit__(self, *exc) -> None:
        self.닫기()


class 고정연결(연결):
    """이미 연 연결을 그대로 쓰는 `연결`(검사 스크립트 · 트랜잭션을 따로 관리하는 호출자). 닫거나 다시 열지 않는다."""

    def __init__(self, conn: psycopg.Connection):
        super().__init__()
        self._conn, self._cur = conn, conn.cursor()

    def 닫기(self) -> None:
        pass


def 재시도(작업, *, 단계: str, 연결: "연결 | None" = None, 마감초: float | None = None):
    """작업() 을 부르고, DB 연결 끊김 · 쿼리 시간 초과(`재시도_오류`)면 지수 백오프 1 · 2 · 4 · 8 · 8 …초 뒤 다시 부른다.

    마감: 서버 예산(research_runtime)의 남은 시간, `마감초` 가 더 짧으면 그 값. 예산이 없으면 `마감초` 또는 `_예산없음_마감초`.
    `연결` 을 주면 실패할 때마다 닫아 작업이 다음 `연결.cur()` 에서 새 연결(GUC 다시 설정)을 받게 한다. 연결을 주지 않으면
    작업이 시도마다 스스로 연결 · 트랜잭션을 연다(server._common.트랜잭션 처럼 GUC 를 트랜잭션마다 거는 함수).
    작업은 다시 불러도 결과가 같아야 한다(읽기, 또는 멱등한 쓰기). 다시 시도할 때마다 경고 로그와 재시도 기록을 남긴다.
    마감까지 실패하면 DB시스템오류(단계, 원인, 시도). 재시도 대상이 아닌 오류는 그대로 올린다.
    """
    import logging
    import time
    로그 = logging.getLogger("suddoe.db")
    budget = _현재예산()
    시작 = time.monotonic()
    상한 = 마감초 if 마감초 is not None else (None if budget is not None else _예산없음_마감초)

    def 남은초() -> float:
        남 = [budget.remaining_seconds()] if budget is not None else []
        if 상한 is not None:
            남.append(max(0.0, 상한 - (time.monotonic() - 시작)))
        return min(남)

    n = 0
    while True:
        n += 1
        try:
            결과 = 작업()
            if n > 1:
                로그.warning("[%s DB] %d번째 시도에 성공", 단계, n)
            return 결과
        except 재시도_오류 as e:
            원인 = f"{type(e).__name__}(sqlstate={getattr(e, 'sqlstate', None)})"   # 오류 본문에는 접속 정보가 있어 남기지 않는다
            if 연결 is not None:
                연결.닫기()
            대기 = _재시도_백오프초[min(n - 1, len(_재시도_백오프초) - 1)]
            if 남은초() <= 대기:
                로그.error("[%s DB] 실패 — 마감, %d회 시도, 마지막 오류: %s", 단계, n, 원인)
                raise DB시스템오류(f"{단계} DB", 원인, n) from e
            로그.warning("[%s DB] %d번째 시도 실패 — %.0f초 뒤 재시도: %s", 단계, n, 대기, 원인)
            _재시도_기록(f"{단계} DB", 원인, n, 대기)
            time.sleep(대기)
