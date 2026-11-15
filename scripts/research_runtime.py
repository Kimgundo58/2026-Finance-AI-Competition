# -*- coding: utf-8 -*-
"""요청 단위 조사 예산과 취소. contextvars는 동시 요청을 분리한다."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
import time
import uuid

_current: ContextVar["Budget | RequestBudget | None"] = ContextVar("research_budget", default=None)
_final: ContextVar[bool] = ContextVar("research_final", default=False)
_emitter: ContextVar[object] = ContextVar("research_emitter", default=None)

공개조사_실패문장 = "공개 정보를 확인하지 못했어요. 입력한 내용으로 계속 점검할 수 있어요."


class 시스템오류(Exception):
    """시스템 오류(LLM 연결 끊김 · 403 · 429 · 5xx · 응답 무응답 · JSON/스키마 깨짐 · DB 연결 끊김 · 쿼리 시간 초과)가
    서버 예산(또는 그 단계의 마감)이 끝날 때까지 이어졌다(v053, 지시서 2절). 근거 부족이 아니므로 확인 필요로 바꾸지 않는다:
    판정은 판정실패(「다시 점검」), 질문 · 정리 단계는 다음행동 다시시도로 응답하고 결과를 저장하지 않는다.
    llm_qwen.LLM시스템오류 · _lib.db.DB시스템오류 가 이 클래스를 잇는다."""

    def __init__(self, 단계: str, 원인: str, 시도: int = 0):
        super().__init__(f"{단계}: {원인}({시도}회 시도)")
        self.단계, self.원인, self.시도 = 단계, 원인, 시도


class 단계상한초과(시스템오류):
    """시스템 오류 재시도가 서버 예산보다 먼저 그 단계의 시간 상한(`마감초`)에 닿았다(v060). 예산은 남아 있다.
    받지 않은 호출자에게는 시스템오류 그대로다(다시시도). 공개 조사의 계획 LLM 만 「조사 시간 끝남」 으로 받는다:
    상한 뒤에 받은 계획으로는 새 검색 · 읽기를 시작하지 않으므로(web_research.research) 더 기다려도 쓸 곳이 없다."""


def 재시도_기록(단계: str, 원인: str, 시도: int, 대기초: float = 0.0) -> None:
    """시스템 오류로 다시 시도한 한 번을 현재 요청 예산에 남긴다(디버그 추적 · 판정 행 모델 칸 `재시도`).
    예산이 없는 호출(CLI · 검사)은 남기지 않는다. 원인은 예외 종류와 앞 120자만(사용자 입력 · 접속 정보를 남기지 않게)."""
    budget = _current.get()
    if budget is None:
        return
    기록 = getattr(budget, "재시도", None)
    if isinstance(기록, list):
        기록.append({"단계": 단계, "원인": 원인[:120], "시도": 시도, "대기초": round(대기초, 1)})


# 조사 세션 예산 기본값(정규화 · 질문 요청이 함께 쓴다). 시간 600초(v061, 기본값으로 정함: /api/judge 최소 예산과 같은 값):
# ② 품목 조사(최대 120초) + ③ 앙상블 · 조정(운영 스모크 최대 172초, 조정 한 번 90초 관찰) + ④-2 판정용 조사(120초) + ⑤ 질문을 담고,
# ③ 조정 LLM 의 시스템 오류 재시도가 이 예산까지 간다. 300초에서는 ③ 조정 403 재시도 뒤 ④-2 몫을 남길 수 없었다(운영 23e63ec 11-16).
# LLM 횟수는 시간과 같은 비율로 늘린다(100 → 200): 시간이 실제 상한이고 횟수가 먼저 막지 않게 한다(사용자 11-12 · 11-13)
세션_예산초 = 600
세션_LLM = 200
세션_도구 = 60


class Budget:
    # 횟수 상한(2026-11-12 사용자: 비용 · 호출 횟수는 상관없다, 막지 말 것). 실제로 묶는 건 시간(전체 세션_예산초)이다.
    # 도구 60(2026-11-13): ② 품목 조사(계획 6 · 도구 10 · 120초)에 ④-2 판정용 조사(계획 15 · 도구 30)가 더해졌다.
    # 판정 요청은 시작할 때 남은 몫을 600초 · LLM 40회 이상으로 늘린다(ensure_remaining, routes_judge).
    # LLM 횟수는 새 호출만 센다. 운영 경로(마감까지_재시도)의 시스템 오류 재시도는 세지 않고 시간만 본다(v062, reserve_llm 재시도)
    # 읽은 본문 글자 상한 150,000(2026-11-13): ④-2 가 본문을 최대 30번 더 읽는다
    def __init__(self, *, seconds: float = 세션_예산초, tools: int = 세션_도구, llm: int = 세션_LLM,
                 evidence_chars: int = 150000, emit=None):
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
        self.재시도: list[dict] = []     # 시스템 오류 재시도 기록(재시도_기록). 요청 예산(RequestBudget)은 따로 갖는다

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

    def reserve_llm(self, *, final: bool = False, 재시도: bool = False) -> bool:
        """LLM 호출 하나를 예약한다. `재시도`(같은 호출을 시스템 오류 때문에 다시 보냄, llm_qwen._마감까지_호출)는 호출 수를 세지 않고
        시간만 본다(v062): 호출 수 · 비용은 따지지 않고 시간이 상한이다(사용자 11-13). 호출 수 상한은 새 호출에만 걸린다."""
        with self._lock:
            if 재시도:
                return self._remaining() > 0
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

    def ensure_remaining(self, *, seconds: float, llm: int, tools: int = 0) -> None:
        """남은 시간 · LLM 호출 수(· 도구 수)가 적어도 이만큼 되게 상한을 늘린다(줄이지 않는다). 쓴 값과 세션 상태(ontology_state 등)는 그대로다.
        /api/judge 가 시작할 때 부른다(routes_judge, 사용자 2026-11-15: 판정이 시간 때문에 실패하지 않게 충분히 늘린다).
        /api/questions 는 앞 요청이 시스템 오류로 끝난 세션의 다시 시도 요청에서 부른다(routes_questions, v053)."""
        with self._lock:
            # 이미 상한을 넘긴 세션도 지금부터 `seconds` 가 남게 한다(_remaining 은 0 에서 잘리므로 경과 시간으로 잰다)
            elapsed = self._elapsed + (time.monotonic() - self._started if self._started is not None else 0)
            self._seconds = max(self._seconds, elapsed + max(0.0, seconds))
            self._llm_limit = max(self._llm_limit, self.llm_used + max(0, llm))
            self._tools_limit = max(self._tools_limit, self.tools_used + max(0, tools))

    def request(self) -> "RequestBudget":
        """이 예산(시간 · 호출 수 · 근거 글자)을 함께 쓰고 취소만 따로 갖는 요청 하나의 예산.
        끊기거나 시간이 지난 요청의 취소가 같은 조사 세션의 다음 요청을 막지 않는다(리뷰 28-6)."""
        return RequestBudget(self)


class RequestBudget:
    """세션 예산을 나눠 쓰는 요청 단위 보기. cancel() 은 이 요청만 멈춘다. 쓴 횟수 · 시간은 세션 예산에 쌓인다."""

    def __init__(self, parent: Budget):
        self._parent = parent
        self._cancelled = False
        self.emit = parent.emit
        self.재시도: list[dict] = []     # 이 요청의 시스템 오류 재시도 기록(재시도_기록). 세션 예산과 섞지 않는다

    def __getattr__(self, name):                 # tools_used · llm_used · evidence_used 같은 관측값은 세션 예산 값
        return getattr(self._parent, name)

    @property
    def ontology_state(self):                    # 추가 조회 상태(호출 수 · 결과)는 세션 단위다(ontology_research_plan)
        return getattr(self._parent, "ontology_state", None)

    @ontology_state.setter
    def ontology_state(self, value):
        self._parent.ontology_state = value

    def cancel(self) -> None:
        self._cancelled = True

    def _enter(self):
        self._parent._enter()

    def _exit(self):
        self._parent._exit()

    def remaining_seconds(self) -> float:
        return 0.0 if self._cancelled else self._parent.remaining_seconds()

    def remaining_nonfinal_llm(self) -> int:
        return 0 if self._cancelled else self._parent.remaining_nonfinal_llm()

    def remaining_tools(self) -> int:
        return 0 if self._cancelled else self._parent.remaining_tools()

    def reserve_tool(self) -> bool:
        return not self._cancelled and self._parent.reserve_tool()

    def reserve_llm(self, *, final: bool = False, 재시도: bool = False) -> bool:
        return not self._cancelled and self._parent.reserve_llm(final=final, 재시도=재시도)

    def add_evidence(self, text: str) -> bool:
        return not self._cancelled and self._parent.add_evidence(text)


@contextmanager
def activate(budget: "Budget | RequestBudget", emit=None):
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


def current_budget() -> "Budget | RequestBudget | None":
    return _current.get()


def is_final_call() -> bool:
    return _final.get()


def emit(단계: str, 문장: str, 상태: str = "진행중", **metadata):
    callback = _emitter.get()
    if callback is not None:
        callback({"id": metadata.pop("id", uuid.uuid4().hex), "단계": 단계,
                  "문장": 문장, "상태": 상태, **metadata})


def 실패문장(시작: str) -> str:
    """진행 문장(「…하고 있어요.」)에서 실패 문장(「…하지 못했어요.」)을 만든다. 화면에 내부 단계 이름이 나가지 않게 한다."""
    if 시작.endswith("고 있어요."):
        return 시작[: -len("고 있어요.")] + "지 못했어요."
    return "이 단계를 마치지 못했어요."


@contextmanager
def task(단계: str, 시작: str, 완료: str, 실패: str | None = None):
    event_id = uuid.uuid4().hex
    emit(단계, 시작, id=event_id)
    try:
        yield
    except Exception:
        emit(단계, 실패 or 실패문장(시작), 상태="실패", id=event_id)
        raise
    else:
        emit(단계, 완료, 상태="완료", id=event_id)
