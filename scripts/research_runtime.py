# -*- coding: utf-8 -*-
"""요청 단위 조사 예산과 취소. contextvars는 동시 요청을 분리한다."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
import time
import uuid

_current: ContextVar["Budget | None"] = ContextVar("research_budget", default=None)
_final: ContextVar[bool] = ContextVar("research_final", default=False)
_emitter: ContextVar[object] = ContextVar("research_emitter", default=None)


class Budget:
    def __init__(self, *, seconds: float = 120, tools: int = 6, llm: int = 9,
                 evidence_chars: int = 60000, emit=None):
        self._seconds = max(0, seconds)
        self._elapsed = 0.0
        self._started = None
        self._active = 0
        self._tools_limit = max(0, tools)
        self._llm_limit = max(0, llm)
        self._evidence_limit = max(0, evidence_chars)
        self._lock = Lock()
        self._cancelled = False
        self.tools_used = 0
        self.llm_used = 0
        self.evidence_used = 0
        self.emit = emit

    def _remaining(self) -> float:
        elapsed = self._elapsed + (time.monotonic() - self._started if self._started is not None else 0)
        return 0.0 if self._cancelled else max(0.0, self._seconds - elapsed)

    def _enter(self):
        with self._lock:
            if self._active == 0:
                self._started = time.monotonic()
            self._active += 1

    def _exit(self):
        with self._lock:
            self._active -= 1
            if self._active == 0:
                self._elapsed += time.monotonic() - self._started
                self._started = None

    def remaining_seconds(self) -> float:
        with self._lock:
            return self._remaining()

    def remaining_nonfinal_llm(self) -> int:
        """최종 판정 한 번을 남긴 비최종 호출 가능 수. 예약은 별도로 한다."""
        with self._lock:
            return max(0, self._llm_limit - 1 - self.llm_used) if self._remaining() > 0 else 0

    def remaining_tools(self) -> int:
        """남은 도구 수의 관측값. 실제 호출 전 reserve_tool로 예약한다."""
        with self._lock:
            return max(0, self._tools_limit - self.tools_used) if self._remaining() > 0 else 0

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def reserve_tool(self) -> bool:
        with self._lock:
            if self._remaining() <= 0 or self.tools_used >= self._tools_limit:
                return False
            self.tools_used += 1
            return True

    def reserve_llm(self, *, final: bool = False) -> bool:
        with self._lock:
            limit = self._llm_limit if final else max(0, self._llm_limit - 1)
            if self._remaining() <= 0 or self.llm_used >= limit:
                return False
            self.llm_used += 1
            return True

    def add_evidence(self, text: str) -> bool:
        if not isinstance(text, str):
            return False
        with self._lock:
            if self._remaining() <= 0 or self.evidence_used + len(text) > self._evidence_limit:
                return False
            self.evidence_used += len(text)
            return True


@contextmanager
def activate(budget: Budget, emit=None):
    budget._enter()
    token = _current.set(budget)
    event_token = _emitter.set(emit if emit is not None else budget.emit)
    try:
        yield budget
    finally:
        _emitter.reset(event_token)
        _current.reset(token)
        budget._exit()


@contextmanager
def final_call():
    token = _final.set(True)
    try:
        yield
    finally:
        _final.reset(token)


def current_budget() -> Budget | None:
    return _current.get()


def is_final_call() -> bool:
    return _final.get()


def emit(단계: str, 문장: str, 상태: str = "진행중", **metadata):
    callback = _emitter.get()
    if callback is not None:
        callback({"id": metadata.pop("id", uuid.uuid4().hex), "단계": 단계,
                  "문장": 문장, "상태": 상태, **metadata})


@contextmanager
def task(단계: str, 시작: str, 완료: str, 실패: str | None = None):
    event_id = uuid.uuid4().hex
    emit(단계, 시작, id=event_id)
    try:
        yield
    except Exception:
        emit(단계, 실패 or f"{단계}을 완료하지 못했어요.", 상태="실패", id=event_id)
        raise
    else:
        emit(단계, 완료, 상태="완료", id=event_id)
