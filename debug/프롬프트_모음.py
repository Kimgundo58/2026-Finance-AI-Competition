# -*- coding: utf-8 -*-
"""판정 경로(화면 ① ~ ⑦)의 LLM 프롬프트를 코드에서 뽑아 한 Markdown 파일로 쓴다.

실행: python debug/프롬프트_모음.py   →  debug/판정_프롬프트_모음.md

- DB · 네트워크 · LLM 을 쓰지 않는다. SUDDOE_MOCK=1 로 모듈을 가져오고, LLM 호출 함수(llm_qwen.llm_호출)를 이 프로세스 안에서만
  가짜 함수로 바꿔 끼워 실제 호출 지점이 만드는 프롬프트 · 스키마 · 호출 설정을 그대로 받아 적는다. DB 를 읽는 함수는 예시 값을 돌려주는
  가짜 함수로 바꾼다. 파일은 고치지 않는다.
- 프롬프트 안의 자료(지출 · 후보 · 규정 원문 · 조사 본문 등)는 아래 「예시 입력」 이다. 지시 · 규칙 문장은 코드 그대로다.
- 호출 지점은 AST 로 찾는다(llm_호출 · 주입된 llm · llm_call 등). 목록의 항목이 맡지 않은 호출 지점은 md 끝 「덮지 못한 호출 지점」 에 남는다.
- 프롬프트 파일을 고친 뒤 다시 돌리면 md 가 새 코드 기준으로 바뀐다.
"""
from __future__ import annotations

import ast
import contextlib
import datetime
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
출력_경로 = ROOT / "debug" / "판정_프롬프트_모음.md"
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ["SUDDOE_MOCK"] = "1"
logging.disable(logging.CRITICAL)   # 가짜 호출로 생기는 경고 로그를 찍지 않는다

# 화면 경로 파일(판정 경로). 이 파일들의 LLM 호출 지점이 모두 아래 목록에 있어야 한다
경로_파일 = [
    "server/routes_normalize.py", "server/routes_questions.py", "server/routes_judge.py", "server/research_sessions.py",
    "scripts/normalize_run.py", "scripts/expense_input_check.py", "scripts/web_research.py", "scripts/item_classify.py",
    "scripts/question_generate.py", "scripts/orchestrate.py", "scripts/ontology_research_plan.py",
    "scripts/ontology_query_plan.py", "scripts/assemble_context.py", "scripts/ontology_terms.py", "scripts/ontology_context.py",
]
# LLM 을 부르는 이름: 실제 호출(llm_호출)과 그것을 감싼 · 주입받은 함수 이름
LLM_이름 = {"llm_호출", "llm", "llm_call", "_llm_호출", "_판정_LLM", "호출"}


# ───────────────────────────────────────────── 모듈 가져오기

def _가져오기(이름: str, 시도: int = 3):
    """다른 작업이 파일을 고치는 중이면 문법 오류가 날 수 있어 잠시 뒤 다시 가져온다."""
    for i in range(시도):
        try:
            if 이름 in sys.modules:
                return importlib.reload(sys.modules[이름])
            return importlib.import_module(이름)
        except SyntaxError:
            if i == 시도 - 1:
                raise
            print(f"[다시 가져오기] {이름}: 문법 오류, 10초 뒤 다시", file=sys.stderr)
            time.sleep(10)


def 기본모델_값() -> dict:
    """llm_qwen 의 기본 모델 값을 두 연결(게이트웨이 · DashScope 직접)로 읽는다. 환경변수는 원래대로 되돌린다."""
    저장 = {k: os.environ.pop(k, None) for k in ("SUDDOE_LLM_BASE_URL", "SUDDOE_QWEN_MODEL", "SUDDOE_QWEN_FALLBACK_MODEL")}
    try:
        os.environ["SUDDOE_LLM_BASE_URL"] = "https://gateway.invalid/v1"
        m = _가져오기("llm_qwen")
        게이트 = m.기본_모델
        폴백 = m.폴백_모델
        del os.environ["SUDDOE_LLM_BASE_URL"]
        m = _가져오기("llm_qwen")
        직접 = m.기본_모델
    finally:
        os.environ.pop("SUDDOE_LLM_BASE_URL", None)
        for k, v in 저장.items():
            if v is not None:
                os.environ[k] = v
        _가져오기("llm_qwen")
    return {"게이트웨이": 게이트, "직접": 직접, "폴백": 폴백}


모델기본 = 기본모델_값()
llm_qwen = _가져오기("llm_qwen")
llm_schema = _가져오기("llm_schema")
assemble_context = _가져오기("assemble_context")
ontology_terms = _가져오기("ontology_terms")
ontology_lookup = _가져오기("ontology_lookup")
ontology_context = _가져오기("ontology_context")
ontology_gate = _가져오기("ontology_gate")
ontology_research_plan = _가져오기("ontology_research_plan")
ontology_query_plan = _가져오기("ontology_query_plan")
normalize_run = _가져오기("normalize_run")
expense_input_check = _가져오기("expense_input_check")
web_research = _가져오기("web_research")
item_classify = _가져오기("item_classify")
orchestrate = _가져오기("orchestrate")
question_generate = _가져오기("question_generate")
_원래_llm = llm_qwen.llm_호출


# ───────────────────────────────────────────── 가짜 LLM · 바꿔 끼우기

class _멈춤(BaseException):
    """프롬프트를 받아 적은 뒤 호출자를 멈춘다(호출자의 except Exception 에 걸리지 않는다)."""


@contextlib.contextmanager
def 바꿔(대상, 이름: str, 값):
    옛 = getattr(대상, 이름)
    setattr(대상, 이름, 값)
    try:
        yield
    finally:
        setattr(대상, 이름, 옛)


@contextlib.contextmanager
def 가로채기(응답=None):
    """llm_qwen.llm_호출 과 그것을 `from llm_qwen import llm_호출` 로 가져간 모든 모듈의 참조를 가짜로 바꾼다.
    응답(프롬프트, 스키마, 설정) → 출력. 응답이 없거나 _멈춤 을 돌려주면 받아 적은 뒤 멈춘다."""
    기록: list[dict] = []

    def 가짜(프롬프트, 스키마, **설정):
        기록.append({"프롬프트": 프롬프트, "스키마": 스키마, "설정": 설정})
        out = 응답(프롬프트, 스키마, 설정) if 응답 else _멈춤
        if out is _멈춤:
            raise _멈춤()
        return out, {"지연ms": 0, "시도": 1, "모델": "가짜"}
    바뀐 = [m for m in list(sys.modules.values()) if getattr(m, "llm_호출", None) is _원래_llm]
    for m in 바뀐:
        m.llm_호출 = 가짜
    try:
        yield 기록
    finally:
        for m in 바뀐:
            m.llm_호출 = _원래_llm


def 멈추며(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except _멈춤:
        return None


# ───────────────────────────────────────────── 소스 위치 (AST)

_ast_표: dict[str, tuple[ast.Module, list[str]]] = {}


def _트리(rel: str):
    if rel not in _ast_표:
        글 = (ROOT / rel).read_text(encoding="utf-8")
        _ast_표[rel] = (ast.parse(글), 글.splitlines())
    return _ast_표[rel][0]


def 정의줄(rel: str, 이름: str) -> int | None:
    """함수 · 상수 정의 줄. 맨 위 수준을 먼저 보고, 없으면 안쪽 함수까지 본다."""
    트리 = _트리(rel)
    def 맞음(n) -> bool:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return n.name == 이름
        if isinstance(n, ast.Assign):
            return any(isinstance(t, ast.Name) and t.id == 이름 for t in n.targets)
        if isinstance(n, ast.AnnAssign):
            return isinstance(n.target, ast.Name) and n.target.id == 이름
        return False
    for n in 트리.body:
        if 맞음(n):
            return n.lineno
    for n in ast.walk(트리):
        if 맞음(n):
            return n.lineno
    return None


def _피호출(n: ast.Call) -> str | None:
    if isinstance(n.func, ast.Name):
        return n.func.id
    if isinstance(n.func, ast.Attribute):
        return n.func.attr
    return None


def 호출지점_모두(rel: str) -> list[dict]:
    """파일 안의 LLM 이름 호출 [{줄, 피호출, 함수들(바깥→안)}]."""
    out = []
    def 걷기(n, 스택):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            스택 = [*스택, n.name]
        if isinstance(n, ast.Call) and _피호출(n) in LLM_이름:
            out.append({"파일": rel, "줄": n.lineno, "피호출": _피호출(n), "함수들": 스택})
        for c in ast.iter_child_nodes(n):
            걷기(c, 스택)
    걷기(_트리(rel), [])
    return out


def 맡음(x: dict, rel: str, 함수경로: str) -> bool:
    """호출 지점 x 가 (파일, 함수 경로) 에 있는가. 함수 경로는 안쪽 함수 이름 또는 「바깥.안쪽」 이고, 호출을 감싼 함수 목록의 끝과 맞춘다."""
    조각 = 함수경로.split(".")
    return x["파일"] == rel and x["함수들"][-len(조각):] == 조각


def 위치(rel: str, 줄: int | None) -> str:
    return f"`{rel}:{줄}`" if 줄 else f"`{rel}` (줄 못 찾음)"


# ───────────────────────────────────────────── 예시 입력

사업명 = "모두의 창업"
질문원문 = "QA 진행하면서 회의할 회의실 1일 대여 15만원"
품목, 용도, 금액 = "회의실 1일 대여", "QA 회의", 150000
거래처 = "가나스페이스"
정규화결과 = {"품목": 품목, "금액": 금액, "금액_추정여부": False, "용도": 용도, "질의문": "QA 회의 회의실 1일 대여",
          "수치": {"부가세구분": "모름"}, "거래처유형": None, "거래처": None, "사용일자": None}
레이블 = [{"표시문": "창업활동비 > 회의비", "별칭": ["회의 비용"]}, {"표시문": "창업활동비 > 임차료", "별칭": ["공간 임차"]},
       {"표시문": "외주용역비", "별칭": []}]
조사확인 = {"확인한정보": [{"유형": "제품성격", "답": "맞아요", "확인문장": "회의실 대여는 회의 공간을 빌려 쓰는 거예요. 맞나요?",
                     "내용": "회의실 대여는 회의 공간을 빌려 쓰는 거예요."}]}
판단줄 = [{"번호": "C1", "조건키": "K1", "구분": "금지조건", "금지": True, "상한": True, "규범ID": ["N1"], "규범종류": ["금지"],
        "문장": "(예시) 유흥 · 사행 업종 가맹점에서 쓴 비용은 집행할 수 없다", "근거발췌": "(예시 원문) 유흥업소 등에서 사용한 경비는 집행할 수 없다.",
        "시점": "집행 전"},
       {"번호": "C2", "조건키": "K2", "구분": "의무", "상한": True, "규범ID": ["N2"], "규범종류": ["의무"],
        "문장": "(예시) 협약 기간 안에 이용을 마쳐야 한다", "근거발췌": "(예시 원문) 사업비는 협약 기간 내에 집행하여야 한다.", "시점": ""}]
판정조사_예 = {"상태": "일부", "요건결과": {
    "K1": {"결과": "못찾음", "값": "", "발췌": "", "URL": "", "이유": "가맹점 업종은 결제 전 공개 웹으로 알 수 없음"},
    "K2": {"결과": "확인됨", "값": "1일 단위로 예약해 이용한다", "발췌": "(예시 본문) 회의실은 1일 단위로 예약할 수 있습니다.",
           "URL": "https://example.com/space/1"}}, "출처": ["https://example.com/space/1"]}


class 가짜예산:
    def __init__(self):
        self.tools = 40

    def reserve_tool(self):
        self.tools -= 1
        return self.tools >= 0

    def remaining_seconds(self):
        return 300.0

    def remaining_tools(self):
        return max(0, self.tools)

    def remaining_nonfinal_llm(self):
        return 10

    def add_evidence(self, _text):
        return True


class 가짜검색:
    """APICK 대신. 검색은 예시 주소 하나, 본문은 예시 글."""
    주소 = "https://example.com/space/1"

    def call(self, name, args, timeout):
        if name == "google_search":
            return {"content": [{"type": "text", "text": json.dumps(
                {"items": [{"link": self.주소, "title": "(예시) 회의실 1일 대여 안내"}]}, ensure_ascii=False)}]}
        return {"content": [{"type": "text", "text":
                "(예시 본문) 회의실은 1일 단위로 예약할 수 있습니다. 6인용 회의 공간이며 화이트보드와 모니터를 제공합니다. 결제는 원화로 카드 결제할 수 있습니다."}]}


# ───────────────────────────────────────────── 렌더 함수 (항목마다 [ {제목, 프롬프트, 스키마, 설정, 메모} ])

def _조각(기록: dict, 제목: str = "", 메모: str = "") -> dict:
    return {"제목": 제목, "프롬프트": 기록["프롬프트"], "스키마": 기록["스키마"], "설정": 기록["설정"], "메모": 메모}


def r_질의변환():
    with 가로채기() as 기록:
        멈추며(normalize_run.질의변환, {"질문": 질문원문, "부가세구분": "모름", "사업명": 사업명}, 레이블)
    return [_조각(기록[0])]


def r_입력검증():
    with 가로채기() as 기록:
        멈추며(expense_input_check.검증, 품목, 용도, 금액)
    return [_조각(기록[0])]


def r_정규화_옛():
    with 가로채기() as 기록, 바꿔(normalize_run, "_별칭_행", lambda: [("회의실", "창업활동비")]):
        멈추며(normalize_run.정규화, 질문원문)
    return [_조각(기록[0], 메모="「질문에 나온 품목 중 이미 등록된 것」 줄은 corpus.item_alias(DB)에서 질문에 나온 품목만 넣는다(예시 1줄).")]


def r_비목후보_옛():
    with 가로채기() as 기록, 바꿔(normalize_run, "_별칭_행", lambda: [("회의실", "창업활동비")]):
        멈추며(normalize_run.비목후보_추출, 품목, 용도, 금액, 사업명)
    return [_조각(기록[0])]


def _조사_실행(*, 요건: bool = False, **kw):
    """web_research.research 를 가짜 검색 · 가짜 LLM 으로 한 번 돌려 계획 · 후보(정리) 프롬프트를 받는다.
    계획 1: 검색, 계획 2: 본문, 그 뒤 후보 작성(또는 정리), 계획 3: 완료."""
    def 응답(프롬프트, 스키마, 설정):
        if web_research._후보역할(스키마):
            return {"읽지못함": [], "후보": []}
        if web_research._요건역할(스키마):
            return {"읽지못함": [], "결과": []}
        n = sum(1 for x in 기록 if not (web_research._후보역할(x["스키마"]) or web_research._요건역할(x["스키마"])))
        유형 = "결제안내" if 요건 else "제품성격"
        if n == 1:
            return {"행동": "검색", "유형": 유형, "검색어": "회의실 대여", "URL": []}
        if n == 2:
            return {"행동": "본문", "유형": 유형, "검색어": "", "URL": [가짜검색.주소]}
        return {"행동": "완료", "유형": 유형, "검색어": "", "URL": []}
    with 가로채기(응답) as 기록:
        web_research.research(None, budget=가짜예산(), client=가짜검색(), emit=None, **kw)
    계획 = [x for x in 기록 if not (web_research._후보역할(x["스키마"]) or web_research._요건역할(x["스키마"]))]
    정리 = [x for x in 기록 if web_research._후보역할(x["스키마"]) or web_research._요건역할(x["스키마"])]
    return 계획, 정리


def r_품목조사_계획():
    계획, _ = _조사_실행(requirements=[], context={"품목": 품목, "용도": 용도, "금액": 금액}, public_url=None, vendor=거래처)
    return [_조각(계획[1], "예시: 두 번째 계획 차례(검색 한 번 뒤, 주소목록 · 검색기록이 찬 상태)",
                 "첫 차례는 검색기록 · 주소목록이 빈 같은 프롬프트다. 거래처를 적지 않으면 유형 enum 이 제품성격 하나다.")]


def r_품목조사_후보():
    _, 정리 = _조사_실행(requirements=[], context={"품목": 품목, "용도": 용도, "금액": 금액}, public_url=None, vendor=거래처)
    return [_조각(정리[0])]


def r_재조사_계획():
    계획, _ = _조사_실행(requirements=[], context={"품목": 품목, "용도": 용도, "금액": 금액,
                                                "필요정보": ["하루 단위로 빌리는 공간인지 월 단위 임차인지"]}, public_url=None, vendor=None)
    return [_조각(계획[1], "예시: 두 번째 계획 차례")]


def r_재조사_후보():
    _, 정리 = _조사_실행(requirements=[], context={"품목": 품목, "용도": 용도, "금액": 금액,
                                                "필요정보": ["하루 단위로 빌리는 공간인지 월 단위 임차인지"]}, public_url=None, vendor=None)
    return [_조각(정리[0])]


def _사실확인():
    return [{"번호": c["번호"], "조건": c["문장"], "원문": c["근거발췌"], "시점": c.get("시점") or ""} for c in 판단줄]


def r_요건_계획():
    계획, _ = _조사_실행(요건=True, 사실확인=_사실확인(), context={"품목": 품목, "용도": 용도, "금액": 금액},
                     public_url=None, vendor=None, 제한초=60)
    return [_조각(계획[1], "예시: 두 번째 계획 차례")]


def r_요건_정리():
    _, 정리 = _조사_실행(요건=True, 사실확인=_사실확인(), context={"품목": 품목, "용도": 용도, "금액": 금액},
                     public_url=None, vendor=None, 제한초=60)
    return [_조각(정리[0])]


_후보 = [{"id": "세목:창업/창업활동비/회의비", "정식명": "회의비", "별칭": ["회의 비용"], "비목": ["창업활동비"]},
       {"id": "세목:창업/창업활동비/임차료", "정식명": "임차료", "별칭": ["공간 임차", "사무실 임차료"], "비목": ["창업활동비"]},
       {"id": "비목:창업/창업활동비", "정식명": "창업활동비", "별칭": [], "비목": ["창업활동비"], "비목노드": True},
       {"id": "세목:주관기관/회의비", "정식명": "주관기관 회의비", "별칭": [], "비목": ["회의비"]}]
_발췌표 = {"세목:창업/창업활동비/회의비": "(예시 발췌) 사업 관련 회의에 드는 다과 · 장소 비용",
        "세목:창업/창업활동비/임차료": "(예시 발췌) 사무실 · 공간을 월 단위로 임차하는 비용"}


def _분류자료():
    return [item_classify.조사문장("회의실 대여는 회의 공간을 빌려 쓰는 거예요."),
            "사용자 설명(정리): " + json.dumps({"품목": "회의실", "거래유형": "임차", "설명": "회의 공간 하루 대여", "용도": "QA 회의"},
                                           ensure_ascii=False)]


def r_설명정리():
    응답 = lambda p, s, k: {"품목": "회의실", "거래유형": "임차", "설명": "회의 공간 하루 대여", "용도": "QA 회의"}
    with 가로채기(응답) as 기록:
        item_classify.설명_정리("QA 회의 때 쓸 회의실을 하루 빌리는 비용이에요", item_classify._운영_llm)
    return [_조각(기록[0])]


def r_분류_변형():
    응답 = lambda p, s, k: {"거래유형": "임차", "거래유형이같은후보": ["N1"], "번호": "N1", "확신": "유사", "이유": "예시", "필요정보": []}
    out = []
    for v in item_classify.프롬프트_변형:
        with 가로채기(응답) as 기록:
            item_classify._변형_실행(v, {"품목": 품목, "용도": 용도, "금액": 금액, "거래처": 거래처}, _분류자료(), _후보,
                                 "임차", _발췌표, item_classify._운영_llm)
        out.append(_조각(기록[0], f"변형 {v}: {item_classify.변형_설명.get(v, '')}"))
    return out


def r_분류_조정():
    응답 = lambda p, s, k: {"거래유형": "임차", "거래유형이같은후보": ["N1"], "번호": "N1", "확신": "유사", "이유": "예시", "필요정보": []}
    표 = {c["id"]: c for c in _후보}
    고름 = [{"변형": "A", "노드": _후보[0]["id"], "확신": "유사", "거래유형": "임차", "이유": "회의에 쓰는 비용이라 회의비", "필요정보": []},
          {"변형": "B", "노드": _후보[1]["id"], "확신": "유사", "거래유형": "임차", "이유": "공간을 빌리므로 임차료", "필요정보": []},
          {"변형": "C", "노드": None, "확신": None, "거래유형": "임차", "이유": "맞는 후보 없음", "필요정보": ["하루 대여인지 월 임차인지"]}]
    경로 = {_후보[0]["id"]: {"표시문": "창업활동비 > 회의비"}, _후보[1]["id"]: {"표시문": "창업활동비 > 임차료"}}
    with 가로채기(응답) as 기록, \
            바꿔(ontology_lookup, "상위_노드", lambda cur, 사업, n: ["비목:창업/창업활동비"]), \
            바꿔(ontology_lookup, "규정_발췌", lambda cur, 사업, ids, **kw: {**_발췌표, "비목:창업/창업활동비": "(예시 발췌) 창업 활동에 드는 비용"}), \
            바꿔(ontology_lookup, "분류_규범문장", lambda cur, 사업, ids: {_후보[1]["id"]: ["- 요건: (예시) 월 단위 임차 계약이어야 한다"]}):
        item_classify._조정(None, 사업명, {"품목": 품목, "용도": 용도, "금액": 금액, "거래처": 거래처}, _분류자료(), 고름, 표, 경로,
                          None, item_classify._운영_llm)
    return [_조각(기록[0], "예시: 세 변형이 회의비 · 임차료 · 없음으로 갈린 경우(null 허용, 규칙 10 포함)",
                 "변형이 모두 후보를 골랐으면 규칙 10(null) 줄이 빠지고 스키마의 번호에서 null 이 빠진다.")]


def _질문_재료():
    번호 = assemble_context._문장ID_생성기()
    블록 = "\n\n".join([
        번호.항목블록("B1. (예시) 이 비목 · 세목의 규정", [
            ("article", 1, "(예시) 모두의 창업 안내 책자 3쪽(창업활동비)", "(예시 원문) 창업활동비는 사업 관련 회의 · 공간 임차 비용으로 쓸 수 있다.", None),
            ("article", 2, "(예시) 사업비 관리지침 제00조(집행 제한)", "(예시 원문) 유흥업소 등에서 사용한 경비는 집행할 수 없다.", "②")]),
        번호.항목블록("B2. (예시) 공통 규정", [
            ("article", 3, "(예시) 사업비 관리지침 제00조(집행 기간)", "(예시 원문) 사업비는 협약 기간 내에 집행하여야 한다.", None)])])
    return 번호, 블록


def r_확인질문():
    번호, 블록 = _질문_재료()
    용어 = ontology_terms.프롬프트_블록([{"용어": "협약 기간", "풀이": "(예시) 지원 사업 계약에 적힌 사업 수행 기간"}])
    조건목록 = [{"규범ID": "N1", "조건번호": 1, "종류": "금지", "조건": "유흥 · 사행 업종 가맹점에서 결제", "발췌": "(예시 원문) 유흥업소 등에서 사용한 경비",
              "근거": {}, "조건키": "K1", "규범종류": ["금지"], "시점": "집행 전", "금지줄": True},
             {"규범ID": "N2", "조건번호": 0, "종류": "의무", "조건": "(예시) 협약 기간 안에 이용을 마쳐야 한다", "발췌": "(예시 원문) 협약 기간 내에 집행",
              "근거": {}, "조건키": "K2", "규범종류": ["의무"], "시점": "", "금지줄": False}]
    유형목록 = [{"조건키": "K3", "규범ID": "N3", "조건번호": 0, "종류": "금지", "조건": "(예시) 대표자 · 가족이 운영하는 업체와의 거래",
              "사유": "목록밖", "규범종류": ["금지"], "시점": "", "금지줄": True}]
    품목후보 = [{"id": "품목:창업/회의실대여", "정식명": "회의실 대여", "별칭": ["회의 공간 대여"]}]
    상황후보 = [{"id": "절차:협약변경", "정식명": "협약 변경"}]
    l3 = [{"id": "L1", "조번호": "제7조", "조제목": "회의비", "글": "1인당 30,000원", "연산": "이하", "문장": "(예시) 회의비는 1인당 30,000원 이하로 집행한다.",
           "해시": "x"}]
    입력 = {"사업명": 사업명, "비목": "창업활동비", "세부유형": "회의비", "품목": 품목, "용도": 용도, "금액": 금액, "질문": 질문원문,
          "_조사확인": 조사확인}
    with 가로채기() as 기록:
        멈추며(question_generate._질문_작성, 입력, 블록, 번호.문장맵, {}, "<SUDDOE_QUESTION_MODEL 또는 기본 모델>", 20, {}, time.time(),
             온톨로지경로=True, 용어블록=용어, 품목후보=품목후보, 조건목록=조건목록, 상황후보=상황후보, L3숫자후보=l3,
             판정조사=판정조사_예, 판단조건=판단줄, 유형목록=유형목록, 관계힌트_있음=False, 칸규범=set(), 세목검사=None)
    out = [_조각(기록[0], "온톨로지 경로(모든 덧붙임 블록이 있는 예시)")]
    with 가로채기() as 기록:
        멈추며(question_generate._질문_작성, {k: v for k, v in 입력.items() if k != "_조사확인"}, 블록, 번호.문장맵, {},
             "<SUDDOE_QUESTION_MODEL 또는 기본 모델>", 20, {}, time.time(), 온톨로지경로=False)
    out.append(_조각(기록[0], "옛 경로(온톨로지 밖 사업): 프롬프트_틀 그대로, 덧붙임 블록 없음",
                    "옛 경로의 원문 블록은 retrieve 검색 결과 · L3 조문으로 만든다(위 예시 블록을 그대로 썼다)."))
    return out


def _판정상태():
    return SimpleNamespace(
        사업명=사업명, 질문=질문원문, 정규화결과=정규화결과, 사용자답변=[{"질문문": "협약 기간 안에 이용을 마치나요?", "답": "예"}],
        조사=조사확인, 초기조회상태={"상태": "부분", "입력잘림": True},
        제시원문={"S01": "(예시 원문) 창업활동비는 사업 관련 회의 · 공간 임차 비용으로 쓸 수 있다.",
              "S02": "(예시 원문) 유흥업소 등에서 사용한 경비는 집행할 수 없다."},
        온톨로지={"적용": {"규범": [{"norm_id": "N1", "종류": "금지", "대상": ["세목:창업/창업활동비/회의비"], "조건": {"x": 1}, "예외": []},
                               {"norm_id": "N2", "종류": "의무", "대상": ["비목:창업/*"], "조건": {}, "예외": []}]},
                "대상": {"표시문": "창업활동비 > 회의비", "비목_id": "비목:창업/창업활동비"},
                "노드들": ["세목:창업/창업활동비/회의비"]},
        확정세목="회의비", 할일원문={"H1": "(예시 원문) 회의비는 회의록과 참석자 명단을 첨부하여 증빙한다.",
                             "H2": "(예시 원문) 교통비와 숙박비는 출장 신청서를 사전에 제출한다."},
        게이트결과={"조건": 판단줄, "답변": {}}, 조건판단={"K1": {"종류": "해당없음", "이유": "회의실 대여는 유흥 업종이 아님"}},
        처리단계=[])


def r_판정준비():
    with 가로채기(lambda p, s, k: {"행동": "판정", "요청": ""}) as 기록:
        orchestrate._판정준비_호출(_판정상태(), 가짜예산())
    return [_조각(기록[0], 메모="예시 상태의 초기조회상태(부분 · 입력잘림)는 「초기추가조회상태」 칸을 보이려고 넣었다.")]


def r_조회계획():
    from ontology_query import _EDGES
    지도 = SimpleNamespace(view={
        "지도형식": "(예시)", "버전": "(예시)", "사업": 사업명, "자금": None, "비목_id": "비목:창업/창업활동비",
        "후보": [{"id": "세목:창업/창업활동비/회의비", "이름": "회의비"}], "후보잘림": False,
        "문법": "MATCH (e:Entity)<-[t:TARGETS]-(n:Norm) WHERE e.id IN $ids RETURN DISTINCT n.id AS norm_id LIMIT 20",
        "구조": [{"간선": e, "방향": "출발→도착", "출발": a, "도착": b} for e, pairs in _EDGES.items() for a, b in sorted(pairs)][:6],
        "규범종류": ["금지", "요건"], "문서ID": ["(예시) doc"], "조번호": ["제00조"]})
    with 가로채기(lambda p, s, k: {"행동": "완료", "질의": "", "인자": []}) as 기록:
        ontology_query_plan.계획(None, 지도=지도, 입력={"사업명": 사업명, "품목": 품목, "용도": 용도, "질문": 질문원문,
                                                 "사용자확인": ontology_research_plan.확인정보_문맥(조사확인)},
                               요청="회의실 대여가 임차료 세목의 금지 · 예외에 걸리는지", 이전결과=None, budget=가짜예산())
    return [_조각(기록[0], 메모="구조지도(지도.view)는 ontology_query_map 이 DB 에서 만든다. 예시는 키 구성만 맞추고 값을 줄였다(구조 6줄만).")]


def _b4():
    """B4(룰 조회 결과) 예시. ontology_context.규범_요약 을 예시 규범 · 게이트 결과로 돌린다. 실패하면 고정 예시 글."""
    try:
        규범 = {"규범": [{"norm_id": "N1", "종류": "금지", "결과": {"문장": "(예시) 유흥 · 사행 업종에서 쓴 경비는 집행할 수 없다."}},
                     {"norm_id": "N4", "종류": "한도", "결과": {"문장": "(예시) 회의비는 1인당 30,000원 이하로 집행한다."}}]}
        게이트 = {"조건": 판단줄, "답변": {}, "쓴규범": ["N4"], "한도비교": {"N4": "안넘음"}}
        글 = ontology_context.규범_요약(규범, 게이트, 분류표시="창업활동비 > 회의비", 확정=True, l3있음=False, 분류확신="동일")
        return 글 or "(예시) - 금지: …"
    except Exception as e:  # noqa: BLE001
        return f"(예시 B4 — ontology_context.규범_요약 실행 실패 {type(e).__name__})"


def _판정프롬프트():
    번호 = assemble_context._문장ID_생성기()
    문맥 = {"블록": [("B1. (예시) 이 비목 · 세목의 규정", [
        ("article", 1, "(예시) 모두의 창업 안내 책자 3쪽(창업활동비)", "(예시 원문) 창업활동비는 사업 관련 회의 · 공간 임차 비용으로 쓸 수 있다.", None),
        ("article", 2, "(예시) 사업비 관리지침 제00조(집행 제한)", "(예시 원문) 유흥업소 등에서 사용한 경비는 집행할 수 없다.", "②")])]}
    상태 = _판정상태()
    프롬프트, 문장맵, _ = assemble_context.프롬프트_구성(
        None, 질문원문, 정규화결과, 룰결과=_b4(), 프로필요약="(예시) 협약 기간: 2026-05-01 ~ 2026-12-31 · 정부지원사업비 잔액 1,200,000원",
        사용자답변=[{"질문문": "협약 기간 안에 이용을 마치나요?", "답": "예", "조건번호": "C2"},
                {"질문문": "입력한 금액은 부가세가 포함된 금액인가요?", "답": "모름"}],
        문맥=문맥, 제시원문={}, 조건판단=True,
        추가지시=[orchestrate._추가조회_주의(상태), orchestrate._시행일_주의],
        추가자료=[("B5.8. 공개 정보에 대한 사용자 확인", ontology_research_plan.확인정보_문맥(조사확인)),
              ("B5.9. 판정용 조사 결과", orchestrate.판정조사_문맥(판정조사_예, 판단줄))])
    del 번호
    return 프롬프트, 문장맵


def r_판정():
    프롬프트, 문장맵 = _판정프롬프트()
    스키마 = llm_schema.판정_스키마(문장ID들=list(문장맵), 체크코드=None, 조건ID들=[c["번호"] for c in 판단줄])
    with 가로채기(lambda p, s, k: {"판정": "가능", "인용": ["S01"]}) as 기록:
        orchestrate._판정_LLM(프롬프트, 스키마, len(판단줄))
        orchestrate._판정_LLM(프롬프트, 스키마, len(판단줄), 모델="<SUDDOE_JUDGE_MODELS 의 모델 하나>")
    out = [_조각(기록[0], "단일 모델(SUDDOE_JUDGE_MODELS 비었음)",
                 "앙상블이면 같은 프롬프트 · 스키마로 모델마다 한 번씩 동시에 부른다. 모델별 설정은 아래 두 번째 설정 줄과 같다."
                 f"\n\n앙상블 호출 설정: `{json.dumps(기록[1]['설정'], ensure_ascii=False, default=str)}`")]
    return out


def r_판정_재요청():
    프롬프트, 문장맵 = _판정프롬프트()
    스키마 = llm_schema.판정_스키마(문장ID들=list(문장맵), 체크코드=None, 조건ID들=[c["번호"] for c in 판단줄])
    with 가로채기(lambda p, s, k: {"판정": "가능", "인용": ["S01"]}) as 기록:
        orchestrate._형식문제_재요청(프롬프트, 문장맵, {"판정": "가능", "인용": ["S99"]}, {}, 스키마, len(판단줄))
    재 = 기록[0]
    꼬리 = 재["프롬프트"][len(프롬프트):] if 재["프롬프트"].startswith(프롬프트) else 재["프롬프트"]
    return [{"제목": "판정 프롬프트 끝에 덧붙는 부분만(예시: 인용 S99 가 목록 밖이고 맵 안 인용 0건)", "프롬프트": 꼬리, "스키마": 재["스키마"],
             "설정": 재["설정"], "메모": "고칠 점 줄은 `orchestrate._출력_문제` 가 검증코드(INVALID_JUDGMENT · CITE_NOT_IN_MAP · NO_CITATION)마다 정한 문장이다. "
                                     "스키마는 원래 판정 스키마와 같다."}]


def r_근거재요청():
    import inspect, re
    src = inspect.getsource(orchestrate._근거_재요청)
    m = re.search(r"재프롬프트 = \((.*?)\)\n\s*스키마", src, re.S)
    틀 = m.group(1) if m else src
    return [{"제목": "판정 프롬프트 끝에 덧붙는 부분(코드 원문, f-string 자리는 `{…}`)", "프롬프트": 틀,
             "스키마": "`llm_schema.근거재요청_스키마(틀린 조건 번호들)`", "설정": {"설명": "판정과 같은 온도 · 스트리밍, 폴백 없음, 마감까지 재시도"},
             "메모": "고칠점 줄은 `ontology_gate.조건판단_검사` 가 쓴 인용 검사 사유다(LLM 글을 다시 넣지 않는다)."}]


def r_할일다듬기():
    응답 = lambda p, s, k: {"할일다듬기": []}
    with 가로채기(응답) as 기록:
        orchestrate._할일다듬기_호출(_판정상태())
    return [_조각(기록[0])]


def r_금지재확인():
    상태 = _판정상태()
    c = 판단줄[0]
    n = 상태.온톨로지["적용"]["규범"][0]
    v = 상태.조건판단["K1"]
    with 가로채기(lambda p, s, k: {"이유": "예시", "결과": "해당없음"}) as 기록, \
            바꿔(orchestrate, "_경로_금지줄", lambda 상태: [(c, n, v)]):
        orchestrate._금지_재확인(상태)
    return [_조각(기록[0])]


def 문장틀_부록(rel: str, 함수들: list[str], 제목: str, 설명: str) -> str:
    """함수 안의 글 상수 · f-string 을 줄 번호와 함께 원문 그대로 모은다(docstring · 15자 미만 · 다른 f-string 안의 조각 제외)."""
    트리 = _트리(rel)
    줄들 = []
    for 노드 in ast.walk(트리):
        if not (isinstance(노드, ast.FunctionDef) and 노드.name in 함수들):
            continue
        문서들 = {ast.get_docstring(f, clean=False) for f in ast.walk(노드) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        안쪽 = {id(c) for n in ast.walk(노드) if isinstance(n, ast.JoinedStr) for c in ast.walk(n) if c is not n}
        for n in ast.walk(노드):
            if id(n) in 안쪽:
                continue
            if isinstance(n, ast.JoinedStr):
                글 = ast.unparse(n)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in 문서들:
                글 = n.value
            else:
                continue
            if len(글) >= 15:
                줄들.append((n.lineno, 글))
    본문 = "\n".join(f"L{줄}: {글}" for 줄, 글 in sorted(dict.fromkeys(줄들)))
    return f"#### {제목}\n\n{설명}\n\n{_펜스(본문)}\n"


# ───────────────────────────────────────────── 목록 (흐름 순서)


목록: list[dict] = [
    # ① 입력 정리
    {"단계": "① 입력 정리", "이름": "질의 변환", "렌더": r_질의변환,
     "정의": [("scripts/normalize_run.py", "_질의변환_지시"), ("scripts/normalize_run.py", "레이블_문자열"), ("scripts/normalize_run.py", "질의변환")],
     "호출": [("scripts/normalize_run.py", "질의변환")],
     "언제": "`/api/normalize` 온톨로지 사업 · 자연어(한 문장) 입력일 때. 폼 입력은 부르지 않는다(`server/routes_normalize.py` `_온톨로지_정규화`).",
     "횟수": "요청당 1회(시스템 오류는 서버 예산까지 다시 보냄)",
     "모델": "`SUDDOE_QUERY_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`llm_schema.질의변환_스키마(표시문들)`",
     "자리": "`{레이블}` = 이 사업의 분류 목록(표시문 · 별칭, DB `ontology_lookup.레이블_목록`). `{입력}` = 사용자 입력 JSON(질문 · 부가세구분(모름이면 뺌) · 사업명, 꺾쇠는 전각으로)."},
    {"단계": "① 입력 정리", "이름": "지출 항목 입력 검증", "렌더": r_입력검증,
     "정의": [("scripts/expense_input_check.py", "_규칙"), ("scripts/expense_input_check.py", "_스키마")],
     "호출": [("scripts/expense_input_check.py", "_llm_호출"), ("scripts/expense_input_check.py", "검증")],
     "언제": "`/api/normalize` 온톨로지 사업에서 품목이 별칭과 글자 그대로 같지 않고 비어 있지 않을 때(질의 변환 뒤).",
     "횟수": "요청당 1회", "모델": "기본 모델(`SUDDOE_QWEN_MODEL`), 실패 시 폴백 모델", "스키마": "`expense_input_check._스키마`",
     "자리": "`{입력}` = {품목, 용도, 금액} JSON(이스케이프)."},
    {"단계": "① 입력 정리", "이름": "정규화 (옛 경로, 자연어)", "렌더": r_정규화_옛,
     "정의": [("scripts/normalize_run.py", "_지시"), ("scripts/normalize_run.py", "별칭_힌트")],
     "호출": [("scripts/normalize_run.py", "정규화")],
     "언제": "온톨로지 밖 사업의 자연어 입력(`server/routes_normalize.py` `_live_정규화`). 온톨로지 7개 사업은 이 경로를 타지 않는다.",
     "횟수": "요청당 1회", "모델": "기본 모델, 실패 시 폴백 모델", "스키마": "`llm_schema.정규화_스키마(비목목록)`",
     "자리": "`{비목}` = 이 사업 비목 enum + item_alias 힌트(DB). `{질문}` = 사용자 한 문장(이스케이프 없음)."},
    {"단계": "① 입력 정리", "이름": "비목 후보 추출 (옛 경로, 폼)", "렌더": r_비목후보_옛,
     "정의": [("scripts/normalize_run.py", "비목후보_추출")],
     "호출": [("scripts/normalize_run.py", "비목후보_추출")],
     "언제": "온톨로지 밖 사업의 폼 입력.", "횟수": "요청당 1회(재시도 규칙 없음, 실패하면 빈 후보)",
     "모델": "기본 모델, 실패 시 폴백 모델", "스키마": "`{비목후보: llm_schema.정규화_스키마().비목후보}`",
     "자리": "품목 · 금액 · 용도 · 사업명 · 비목 목록 · item_alias 힌트를 f-string 으로 넣는다."},
    # ② 품목 조사
    {"단계": "② 품목 조사", "이름": "조사 계획", "렌더": r_품목조사_계획,
     "정의": [("scripts/web_research.py", "_계획_지시"), ("scripts/web_research.py", "_주소선택_기준"),
            ("scripts/web_research.py", "_거절검색어_안내"), ("scripts/web_research.py", "_설명_계획_지시"), ("scripts/web_research.py", "_스키마")],
     "호출": [("scripts/web_research.py", "research"), ("scripts/web_research.py", "_기본_계획호출.호출")],
     "언제": "`/api/questions` 첫 요청(웹조사, 조사답변 없음). 차례마다 검색어 · 읽을 주소 · 완료를 정한다.",
     "횟수": lambda: f"조사당 최대 {web_research._계획_최대}회(도구 {web_research._도구_최대}회, 웹 {web_research._웹_제한초}초 · 전체 {web_research._전체_제한초}초)",
     "모델": "`SUDDOE_RESEARCH_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`web_research._스키마(유형들)`",
     "자리": "지시 뒤에 자료 JSON(공개검색어 · 필요요건 · 지출문맥(품목 · 용도 · 금액 · 거래처 · 품목설명) · 검색용어 · 남은계획 · 남은도구 · 검색기록 · 주소목록 · 본문판단)이 붙는다. "
             "사용자가 품목 설명을 적었으면 지시 끝에 `_설명_계획_지시` 가 붙는다."},
    {"단계": "② 품목 조사", "이름": "확인 문장(후보) 작성", "렌더": r_품목조사_후보,
     "정의": [("scripts/web_research.py", "_후보_지시"), ("scripts/web_research.py", "_설명_후보_지시"), ("scripts/web_research.py", "_후보_스키마")],
     "호출": [("scripts/web_research.py", "research.후보작성"), ("scripts/web_research.py", "_기본_계획호출.호출")],
     "언제": "본문을 새로 읽은 차례마다(지금까지 읽은 본문 전체로). 코드 검사를 넘은 후보가 나오면 조사를 끝낸다.",
     "횟수": lambda: f"조사당 본문을 읽은 차례 수만큼(도구 최대 {web_research._도구_최대}회 안)",
     "모델": "`SUDDOE_RESEARCH_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`web_research._후보_스키마(유형들)`",
     "자리": "지시 뒤에 자료 JSON(공개검색어 · 지출문맥 · 본문{주소: 본문 글}). 품목 설명이 있으면 `_설명_후보_지시` 가 붙는다."},
    # ③ 분류
    {"단계": "③ 분류", "이름": "사용자 설명 정리", "렌더": r_설명정리,
     "정의": [("scripts/item_classify.py", "_설명_규칙"), ("scripts/item_classify.py", "_설명_스키마")],
     "호출": [("scripts/item_classify.py", "설명_정리"), ("scripts/item_classify.py", "_운영_llm")],
     "언제": "분류 LLM 을 부르기 전, 확인정보에 사용자가 직접 쓴 설명(직접 입력하기 · 품목 설명)이 있을 때.",
     "횟수": "설명 문장마다 1회(조사 세션 캐시에 있으면 0회)", "모델": "`SUDDOE_CLASSIFY_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`item_classify._설명_스키마`",
     "자리": "`{설명}` = 사용자 한 문장(이스케이프)."},
    {"단계": "③ 분류", "이름": "분류 (변형 A · B · C)", "렌더": r_분류_변형,
     "정의": [("scripts/item_classify.py", "_규칙"), ("scripts/item_classify.py", "_기관_규칙"), ("scripts/item_classify.py", "_프롬프트"),
            ("scripts/item_classify.py", "_후보_줄"), ("scripts/item_classify.py", "_스키마")],
     "호출": [("scripts/item_classify.py", "_변형_실행"), ("scripts/item_classify.py", "_운영_llm")],
     "언제": "`/api/questions` 조사 답 뒤(확정비목 없음), 품목이 별칭과 글자 그대로 같지 않을 때. 세 변형을 동시에 부른다.",
     "횟수": "분류 1번에 3회 동시. ③-R 재조사 뒤 다시 분류하면 한 번 더", "모델": "`SUDDOE_CLASSIFY_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)",
     "스키마": "`item_classify._스키마(후보수)`",
     "자리": "`{지출}` 품목 · 용도 · 금액 · 거래처 JSON. `{확인}` 사용자가 확인한 조사 문장 · 정리한 설명(변형 C 는 조사 문장 빼고 설명만). "
             "`{거래유형}` 확인된 거래 유형(변형 C 는 (없음)). `{후보}` 이 사업의 분류 후보(DB) 줄과 규정 발췌(DB). 변형 B 는 후보 역순."},
    {"단계": "③ 분류", "이름": "분류 조정", "렌더": r_분류_조정,
     "정의": [("scripts/item_classify.py", "_조정_규칙"), ("scripts/item_classify.py", "_조정_null규칙"), ("scripts/item_classify.py", "_조정")],
     "호출": [("scripts/item_classify.py", "_조정"), ("scripts/item_classify.py", "_운영_llm")],
     "언제": "세 변형의 비목 > 세목이 갈리고, 확인된 거래 유형 · 거래 유형 확인 질문으로 정해지지 않을 때.",
     "횟수": "분류 1번에 최대 1회(추론 켬, 스트리밍)", "모델": "`SUDDOE_CLASSIFY_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`item_classify._스키마(후보수)` (null 불가면 번호 enum 에서 null 뺌)",
     "자리": "`{지출}` · `{확인}`(출처 표시 붙임) · `{거래유형}` · `{기록}`(세 변형의 결과와 이유) · `{후보}`(갈린 후보와 상위 3단계의 규정 발췌 · 규범 문장, DB)."},
    {"단계": "③ 분류", "이름": "③-R 재조사 계획", "렌더": r_재조사_계획,
     "정의": [("scripts/web_research.py", "_계획_지시"), ("scripts/web_research.py", "_재조사_계획_지시")],
     "호출": [("scripts/web_research.py", "research"), ("scripts/web_research.py", "_기본_계획호출.호출")],
     "언제": "분류가 맞는 후보를 못 찾고 필요정보를 냈을 때(`server/routes_questions.py` `_재조사`). ② 와 같은 함수, 지출문맥에 필요정보가 있다.",
     "횟수": lambda: f"세션당 1번의 재조사, 그 안에서 최대 {web_research._계획_최대}회(최대 60초)",
     "모델": "`SUDDOE_RESEARCH_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`web_research._스키마(유형들)`",
     "자리": "② 계획과 같은 자료 JSON + 지출문맥의 필요정보. 지시 끝에 `_재조사_계획_지시` 가 붙는다."},
    {"단계": "③ 분류", "이름": "③-R 재조사 후보 작성", "렌더": r_재조사_후보,
     "정의": [("scripts/web_research.py", "_후보_지시"), ("scripts/web_research.py", "_재조사_후보_지시")],
     "호출": [("scripts/web_research.py", "research.후보작성"), ("scripts/web_research.py", "_기본_계획호출.호출")],
     "언제": "재조사에서 본문을 새로 읽은 차례마다.", "횟수": "본문을 읽은 차례 수만큼",
     "모델": "`SUDDOE_RESEARCH_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`web_research._후보_스키마(유형들)`",
     "자리": "② 후보와 같은 자료 JSON. 지시 끝에 `_재조사_후보_지시` 가 붙는다."},
    # ④-2 판정용 조사
    {"단계": "④-2 판정용 조사", "이름": "요건 조사 계획", "렌더": r_요건_계획,
     "정의": [("scripts/web_research.py", "_요건_계획_지시"), ("scripts/web_research.py", "_주소선택_기준"), ("scripts/web_research.py", "_거절검색어_안내")],
     "호출": [("scripts/web_research.py", "research"), ("scripts/web_research.py", "_기본_계획호출.호출")],
     "언제": "⑤ 확인 질문 앞, 조건 줄(C1…)이 있을 때(`question_generate.판정용_조사`). 남은 예산이 모자라면 건너뛴다.",
     "횟수": lambda: f"최대 {web_research._요건_계획_최대}회(도구 {web_research._요건_도구_최대}회, 웹 {web_research._요건_웹_제한초}초)",
     "모델": "`SUDDOE_RESEARCH_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`web_research._스키마(KINDS 전체)`",
     "자리": "지시 뒤에 자료 JSON(공개검색어 · 필요요건(조건 줄 번호 · 조건 · 원문 · 시점) · 지출문맥 · 요건상태 · 검색용어 · 남은계획 · 남은도구 · 검색기록 · 주소목록 · 본문판단)."},
    {"단계": "④-2 판정용 조사", "이름": "요건 정리", "렌더": r_요건_정리,
     "정의": [("scripts/web_research.py", "_요건_정리_머리"), ("scripts/web_research.py", "_요건_정리_지시"), ("scripts/web_research.py", "_요건_정리_끝"),
            ("scripts/web_research.py", "_요건정리_프롬프트"), ("scripts/web_research.py", "_요건_스키마")],
     "호출": [("scripts/web_research.py", "research.요건정리"), ("scripts/web_research.py", "_기본_계획호출.호출")],
     "언제": "요건 조사에서 본문을 새로 읽은 차례마다(이번에 읽은 본문만).", "횟수": "본문을 읽은 차례 수만큼",
     "모델": "`SUDDOE_RESEARCH_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)", "스키마": "`web_research._요건_스키마(조건 번호들)`",
     "자리": "머리말 → `<본문 주소=…>` 들(본문 하나 최대 15,000자) → 자료 JSON(공개검색어 · 지출문맥 · 필요요건 · 이미확인한조건) → 할 일 → 끝 규칙."},
    # ⑤ 확인 질문
    {"단계": "⑤ 확인 질문", "이름": "확인 질문 + 조건 유형 + 노드 선택", "렌더": r_확인질문,
     "정의": [("scripts/question_generate.py", "프롬프트_틀"), ("scripts/question_generate.py", "새경로_프롬프트_틀"),
            ("scripts/question_generate.py", "_용어_규칙"), ("scripts/ontology_terms.py", "프롬프트_블록"),
            ("scripts/question_generate.py", "_노드_규칙"), ("scripts/question_generate.py", "노드_프롬프트"),
            ("scripts/question_generate.py", "_조건_규칙"), ("scripts/question_generate.py", "조건_프롬프트"),
            ("scripts/question_generate.py", "_상황_규칙"), ("scripts/question_generate.py", "상황_프롬프트"),
            ("scripts/question_generate.py", "_L3_규칙"), ("scripts/question_generate.py", "L3숫자_프롬프트"),
            ("scripts/ontology_research_plan.py", "확인정보_문맥"), ("scripts/orchestrate.py", "판정조사_문맥"),
            ("scripts/question_generate.py", "질문_스키마")],
     "호출": [("scripts/question_generate.py", "_질문_작성")],
     "언제": "`/api/questions` 분류 · ④ 조회 · ④-2 뒤. 금지 목록 일치 · 근거 없음이면 부르지 않는다(바로판정).",
     "횟수": "요청당 1회(덧붙임 블록은 모두 같은 호출에 들어간다)", "모델": "`SUDDOE_QUESTION_MODEL`, 비면 기본 모델(기본 모델일 때만 폴백)",
     "스키마": "`question_generate.질문_스키마` + `노드_스키마` · `조건_스키마` · `상황_스키마` · `L3숫자_스키마`(후보가 있을 때만)",
     "자리": "`{원문}` 규정 원문 블록(S01…, `ontology_context.문맥_구성` 이 DB 에서). `{용어}` 용어 풀이(DB, AI검증 이상). `{지출}` 사업명 · 비목 · 세부유형 · 품목 · 용도 · 금액 · 질문원문 JSON. "
             "뒤에 붙는 블록: 품목후보(5-13) · 조건목록과 유형만 목록(5-14 B, 2026-11-17 조건 유형) · 상황후보(5-14 C) · 기관규정숫자(5-15 ⑤) · 공개 정보 사용자 확인 · 판정용 조사 결과. 없으면 그 블록이 빠진다."},
    # ⑥ 판정
    {"단계": "⑥ 판정", "이름": "판정 준비 (추가 조회 필요 여부)", "렌더": r_판정준비,
     "정의": [("scripts/orchestrate.py", "_판정준비_호출")],
     "호출": [("scripts/orchestrate.py", "_판정준비_호출")],
     "언제": "판정 LLM 앞. 예산 · 조회 대상이 있을 때만(`_새_판정준비단계`). 「추가조회」 면 아래 규정 조회 계획으로 이어진다.",
     "횟수": "판정당 최대 1회", "모델": "기본 모델, 실패 시 폴백 모델", "스키마": "`orchestrate._판정준비_호출` 안의 지역 스키마",
     "자리": "지시 한 문단 뒤에 자료 JSON(입력 1,800자 · 확인정보 1,000자 · 초기추가조회상태 · 규범요약 · 제시근거 색인(문장마다 100자, 전체 12,000자 상한))."},
    {"단계": "⑥ 판정", "이름": "규정 조회 계획", "렌더": r_조회계획,
     "정의": [("scripts/ontology_query_plan.py", "계획_프롬프트"), ("scripts/ontology_query_plan.py", "_스키마")],
     "호출": [("scripts/ontology_query_plan.py", "계획")],
     "언제": "판정 준비가 「추가조회」 를 고른 뒤(`ontology_research_plan.추가_조회`, 최대조회 1).",
     "횟수": "판정당 최대 1회", "모델": "기본 모델(`모델=기본_모델` 로 넘겨 폴백 규칙은 기본 모델과 같다)", "스키마": "`ontology_query_plan._스키마`",
     "자리": "지시 뒤에 자료 JSON(구조지도 · 지출 · 추가확인요청 · 이전조회 · 입력잘림). 18,000자 넘으면 이전조회 · 지출 칸부터 줄인다."},
    {"단계": "⑥ 판정", "이름": "판정", "렌더": r_판정,
     "정의": [("scripts/assemble_context.py", "시스템_프롬프트"), ("scripts/assemble_context.py", "조건판단_규칙"),
            ("scripts/assemble_context.py", "프롬프트_구성"), ("scripts/assemble_context.py", "_사용자답변_블록"),
            ("scripts/assemble_context.py", "_질문_블록"), ("scripts/ontology_context.py", "규범_요약"),
            ("scripts/orchestrate.py", "_추가조회_주의"), ("scripts/orchestrate.py", "_시행일_주의"),
            ("scripts/orchestrate.py", "판정조사_문맥"), ("scripts/ontology_research_plan.py", "확인정보_문맥"),
            ("scripts/orchestrate.py", "_새_프롬프트단계"), ("scripts/llm_schema.py", "판정_스키마")],
     "호출": [("scripts/orchestrate.py", "_판정_LLM"), ("scripts/orchestrate.py", "_판정호출"), ("scripts/orchestrate.py", "_모델_판정")],
     "언제": "`/api/judge`. `SUDDOE_JUDGE_MODELS` 에 2개 이상이면 모델마다 동시에 부르고 다수결.",
     "횟수": lambda: f"모델 수만큼(단일 1회 · 앙상블 N회) + 모델마다 형식 재요청 최대 {orchestrate.판정_재요청_최대}회",
     "부록": lambda: 문장틀_부록("scripts/ontology_context.py", ["규범_요약", "_조건줄_말"],
                              "B4 룰 조회 결과의 문장 틀 (`ontology_context.규범_요약` · `_조건줄_말`, 규범 종류 · 비교 결과마다 고르는 고정 문장)",
                              "위 예시 프롬프트의 B4 는 이 중 몇 줄만 보인다. 판정 LLM 이 읽는 지시 문장이 여기에 섞여 있어 원문 그대로(f-string 자리는 `{…}`) 싣는다."),
     "모델": "`SUDDOE_JUDGE_MODELS`(쉼표 구분), 비면 기본 모델 하나. 폴백 없음", "스키마": "`llm_schema.판정_스키마(문장ID들, 체크코드, 조건ID들)`",
     "자리": "지시: B0 시스템 프롬프트 → 조건판단 규칙(조건 줄이 있을 때) → 이번 판정의 주의(추가 조회 상태 · 시행일 미확인). "
             "자료(각각 `<자료>` 태그): B1~B3 규정 원문(새 경로는 `ontology_context.문맥_구성`, 옛 경로는 L3 · 검색 청크 · 참조 조항) · B4 룰 조회 결과(`ontology_context.규범_요약`) · "
             "B5 협약 · 집행 현황(프로필, DB) · B5.5 해야할일 후보(옛 경로) · B5.7 사용자 답 · B5.8 공개 정보 확인 · B5.9 판정용 조사 결과. 끝: B6 판정할 지출 + 핵심 규칙 다시."},
    {"단계": "⑥ 판정", "이름": "판정 형식 재요청", "렌더": r_판정_재요청,
     "정의": [("scripts/orchestrate.py", "_형식문제_재요청"), ("scripts/orchestrate.py", "_출력_문제")],
     "호출": [("scripts/orchestrate.py", "_형식문제_재요청")],
     "언제": "판정 출력이 형식 검사(`_출력_문제`)를 못 넘을 때. 같은 판정 프롬프트 끝에 고칠 점을 붙여 다시 보낸다.",
     "횟수": lambda: f"모델마다 최대 {orchestrate.판정_재요청_최대}회", "모델": "판정과 같은 모델", "스키마": "판정과 같음",
     "자리": "판정 프롬프트 전체 + 아래 꼬리."},
    {"단계": "⑥ 판정", "이름": "조건판단 근거 재요청", "렌더": r_근거재요청,
     "정의": [("scripts/orchestrate.py", "_근거_재요청")],
     "호출": [("scripts/orchestrate.py", "_근거_재요청")],
     "언제": "판정 뒤, 조건판단 근거내용(입력 구절 · 답 번호 · 조사 번호 · 문장 번호)이 자료와 맞지 않은 줄이 있을 때. 남은 예산이 모자라면 건너뜀.",
     "횟수": "판정당 최대 1회", "모델": "앙상블 대표 모델, 없으면 SUDDOE_JUDGE_MODELS 첫 모델, 없으면 기본 모델",
     "스키마": "`llm_schema.근거재요청_스키마`", "자리": "판정 프롬프트 전체 + 틀린 줄 번호와 사유 + 다시 쓰기 지시."},
    {"단계": "⑥ 판정", "이름": "할 일 다듬기", "렌더": r_할일다듬기,
     "정의": [("scripts/orchestrate.py", "_할일다듬기_지시"), ("scripts/llm_schema.py", "할일다듬기_스키마")],
     "호출": [("scripts/orchestrate.py", "_할일다듬기_호출")],
     "언제": "판정 LLM 과 같은 시간에 따로(다듬을 의무 조건 줄이 있을 때). 실패하면 원문을 그대로 쓴다.",
     "횟수": lambda: f"판정당 1회({orchestrate.할일다듬기_초}초 안 재시도)", "모델": "기본 모델, 실패 시 폴백 모델", "스키마": "`llm_schema.할일다듬기_스키마(번호들)`",
     "자리": "지시 뒤에 JSON {지출(품목 · 용도 · 금액 · 세목), 할일[{번호 H1…, 문장(규정 원문)}]}."},
    # ⑦ 검증 · 합치기
    {"단계": "⑦ 검증 · 합치기", "이름": "금지 재확인", "렌더": r_금지재확인,
     "정의": [("scripts/orchestrate.py", "_금지재확인_지시"), ("scripts/orchestrate.py", "_금지재확인_스키마")],
     "호출": [("scripts/orchestrate.py", "_금지_재확인"), ("scripts/orchestrate.py", "_금지_재확인.하나")],
     "언제": "판정 뒤 검증 단계. 판정 LLM 이 「해당없음」 으로 본 금지 줄 중 이 지출의 품목 · 세목 노드에 바로 붙은 금지(`_경로_금지줄`)마다.",
     "횟수": lambda: f"해당 줄마다 1회(동시 {orchestrate.금지재확인_동시}개, 줄마다 {orchestrate.금지재확인_초}초)", "모델": "기본 모델, 실패 시 폴백 모델",
     "스키마": "`orchestrate._금지재확인_스키마`",
     "자리": "지시 뒤에 JSON {지출(질문 · 품목 · 용도 · 금액 · 거래처), 금지(조건 줄 문장), 원문(근거 발췌), 앞선판단이유} + 공개 정보 사용자 확인 블록."},
]


# ───────────────────────────────────────────── md 쓰기

def _펜스(글: str, 언어: str = "text") -> str:
    가장긴 = max([len(m) for m in re.findall(r"`+", 글)] or [0])
    f = "`" * max(3, 가장긴 + 1)
    return f"{f}{언어}\n{글}\n{f}"


def _git(*args, strip: bool = True) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20).stdout
        return out.strip() if strip else out
    except Exception:  # noqa: BLE001
        return ""


def _배포_모델값() -> list[tuple[str, str]]:
    """deploy/ncp/docker-compose.yml 의 SUDDOE_*MODEL* 기본값."""
    p = ROOT / "deploy" / "ncp" / "docker-compose.yml"
    if not p.exists():
        return []
    out = []
    for m in re.finditer(r"^\s*(SUDDOE_\w*MODELS?):\s*\$\{\w+:?-([^}]*)\}", p.read_text(encoding="utf-8"), re.M):
        out.append((m.group(1), m.group(2)))
    return out


def _설정글(설정: dict) -> str:
    return json.dumps({k: v for k, v in 설정.items()}, ensure_ascii=False, default=str)


def main() -> int:
    시각 = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    head = _git("rev-parse", "--short", "HEAD") or "(git 없음)"
    바뀐 = [x[3:] for x in _git("status", "--porcelain", "--", *경로_파일, "scripts/llm_qwen.py", "scripts/llm_schema.py",
                              strip=False).splitlines() if len(x) > 3]

    # 렌더
    결과 = []
    for e in 목록:
        try:
            조각들 = e["렌더"]()
            오류 = None
        except Exception as ex:  # noqa: BLE001
            조각들, 오류 = [], f"{type(ex).__name__}: {ex}\n" + traceback.format_exc(limit=3)
        결과.append((e, 조각들, 오류))

    # 호출 지점(AST)과 덮기
    지점 = [x for rel in 경로_파일 if (ROOT / rel).exists() for x in 호출지점_모두(rel)]
    덮음: dict[tuple, list[str]] = {}
    for e in 목록:
        for rel, fn in e["호출"]:
            for x in 지점:
                if 맡음(x, rel, fn):
                    덮음.setdefault((x["파일"], x["줄"]), []).append(f'{e["단계"]} {e["이름"]}')
    안덮음 = [x for x in 지점 if (x["파일"], x["줄"]) not in 덮음]
    # 판정 경로 밖에서 llm_호출 을 부르는 파일
    밖 = []
    for d in ("scripts", "server", "debug"):
        for p in sorted((ROOT / d).glob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            if rel in 경로_파일 or rel in ("scripts/llm_qwen.py", "debug/프롬프트_모음.py"):
                continue
            try:
                if any(x["피호출"] == "llm_호출" for x in 호출지점_모두(rel)):
                    밖.append(rel)
            except SyntaxError:
                밖.append(rel + "(문법 오류로 못 읽음)")

    L: list[str] = []
    w = L.append
    w("# 판정 경로 LLM 프롬프트 모음")
    w("")
    w(f"- 만든 시각: {시각}")
    w(f"- 코드: `{head}` (git rev-parse --short HEAD) — 작업 트리 미커밋 변경 포함")
    if 바뀐:
        w(f"- 미커밋 변경이 있는 관련 파일: {', '.join(f'`{x}`' for x in 바뀐)}")
    w("- 다시 만들기: `python debug/프롬프트_모음.py` (DB · 네트워크 · LLM 없이 돈다. 프롬프트 파일을 고친 뒤 다시 돌린다)")
    w("- 프롬프트는 실제 호출 지점이 만든 글을 그대로 받아 적었다. 지시 · 규칙 문장은 코드 그대로이고, 그 안의 자료(지출 · 후보 · 규정 원문 · 웹 본문 · 조사 결과)는 "
      "고정 예시 입력이다. 예시 값에는 「(예시)」 를 붙였다.")
    w("- 범위: 화면 경로(`server/routes_normalize.py` · `server/routes_questions.py` · `server/routes_judge.py` 와 그 아래 호출). "
      "흐름 이름은 `판정_BEFORE_AFTER_1117.md` 1절(① 입력 정리 → ② 품목 조사 → ③ 분류 → ④ 규정+게이트 → ④-2 판정용 조사 → ⑤ 확인 질문 → ⑥ 판정 → ⑦ 검증 · 합치기). "
      "④ 규정+게이트는 코드만 있고 LLM 호출이 없다.")
    w("")
    w("## 모델 설정")
    w("")
    w(f"- 기본 모델(`SUDDOE_QWEN_MODEL`): 비어 있으면 게이트웨이(`SUDDOE_LLM_BASE_URL` 설정)일 때 `{모델기본['게이트웨이']}`, "
      f"DashScope 직접 연결일 때 `{모델기본['직접']}` (`scripts/llm_qwen.py:{정의줄('scripts/llm_qwen.py', '기본_모델')}`).")
    w(f"- 폴백 모델(`SUDDOE_QWEN_FALLBACK_MODEL`): 비면 `{모델기본['폴백']}`. 호출이 모델을 직접 지정하지 않았고 폴백을 끄지 않았을 때만 쓴다.")
    w(f"- 판정 모델(`SUDDOE_JUDGE_MODELS`): 쉼표 구분, 2개 이상이면 앙상블(`scripts/llm_qwen.py:{정의줄('scripts/llm_qwen.py', '판정_모델들')}`).")
    배포 = _배포_모델값()
    if 배포:
        w("")
        w("| 환경변수 | 배포 기본값 (`deploy/ncp/docker-compose.yml`) |")
        w("|---|---|")
        for k, v in 배포:
            w(f"| `{k}` | `{v}` |" if v else f"| `{k}` | (비어 있음) |")
    쓰는변수 = sorted({m for e in 목록 for m in re.findall(r"SUDDOE_[A-Z_]+", e["모델"])} - {k for k, _ in 배포}
                  - {"SUDDOE_QWEN_MODEL", "SUDDOE_JUDGE_MODELS", "SUDDOE_QWEN_FALLBACK_MODEL"})
    if 쓰는변수:
        w("")
        w(f"- 배포 설정에 없는 단계별 모델 변수({', '.join(f'`{x}`' for x in 쓰는변수)})는 운영에서 비어 있어 기본 모델을 쓴다.")
    w("")
    w("## 호출 목록")
    w("")
    w("| # | 단계 | 이름 | 파일 | 횟수 | 출력 스키마 |")
    w("|---|---|---|---|---|---|")
    for i, (e, 조각들, 오류) in enumerate(결과, 1):
        횟수 = e["횟수"]() if callable(e["횟수"]) else e["횟수"]
        파일 = ", ".join(dict.fromkeys(f"`{rel}`" for rel, _ in e["호출"]))
        w(f"| {i} | {e['단계']} | {e['이름']}{' (렌더 실패)' if 오류 else ''} | {파일} | {횟수} | {e['스키마']} |")
    w("")

    현재단계 = None
    for i, (e, 조각들, 오류) in enumerate(결과, 1):
        if e["단계"] != 현재단계:
            현재단계 = e["단계"]
            w(f"## {현재단계}")
            w("")
        w(f"### {i}. {e['이름']}")
        w("")
        정의 = ", ".join(f"`{n}` {위치(rel, 정의줄(rel, n))}" for rel, n in e["정의"])
        호출 = ", ".join(f"{위치(x['파일'], x['줄'])} (`{x['함수들'][-1] if x['함수들'] else '-'}` → `{x['피호출']}`)"
                       for rel, fn in e["호출"] for x in 지점 if 맡음(x, rel, fn))
        w(f"- 위치(정의): {정의}")
        w(f"- 위치(호출): {호출 or '(호출 지점을 찾지 못함)'}")
        w(f"- 언제: {e['언제']}")
        w(f"- 횟수 상한: {e['횟수']() if callable(e['횟수']) else e['횟수']}")
        w(f"- 모델: {e['모델']}")
        w(f"- 출력 스키마: {e['스키마']}")
        w(f"- 런타임에 채워지는 자리: {e['자리']}")
        w("")
        if 오류:
            w("렌더 실패(이 항목은 아래 상수 원문만 싣는다):")
            w("")
            w(_펜스(오류.strip()))
            w("")
            for rel, n in e["정의"]:
                모듈 = sys.modules.get(Path(rel).stem)
                값 = getattr(모듈, n, None) if 모듈 else None
                if isinstance(값, str):
                    w(f"`{n}` 원문:")
                    w("")
                    w(_펜스(값))
                    w("")
            continue
        for 조각 in 조각들:
            if 조각.get("제목"):
                w(f"#### {조각['제목']}")
                w("")
            w(f"호출 설정: `{_설정글(조각['설정'])}`")
            w("")
            w("프롬프트 원문:")
            w("")
            w(_펜스(조각["프롬프트"]))
            w("")
            w("출력 스키마:")
            w("")
            w(_펜스(json.dumps(조각["스키마"], ensure_ascii=False, separators=(",", ":")), "json"))
            w("")
            if 조각.get("메모"):
                w(f"메모: {조각['메모']}")
                w("")
        if e.get("부록"):
            try:
                w(e["부록"]())
            except Exception as ex:  # noqa: BLE001
                w(f"(부록 만들기 실패: {type(ex).__name__}: {ex})")
                w("")

    w("## 호출 지점 대조")
    w("")
    w(f"판정 경로 파일 {len(경로_파일)}개에서 AST 로 찾은 LLM 이름 호출({', '.join(sorted(LLM_이름))}) {len(지점)}곳 중 "
      f"{len(지점) - len(안덮음)}곳을 위 항목이 맡았다.")
    w("")
    w("| 위치 | 함수 | 부르는 이름 | 맡은 항목 |")
    w("|---|---|---|---|")
    for x in 지점:
        맡은항목 = " · ".join(덮음.get((x["파일"], x["줄"]), [])) or "**없음**"
        w(f"| `{x['파일']}:{x['줄']}` | `{'.'.join(x['함수들']) or '-'}` | `{x['피호출']}` | {맡은항목} |")
    w("")
    if 안덮음:
        w("덮지 못한 호출 지점: " + ", ".join(f"`{x['파일']}:{x['줄']}`" for x in 안덮음))
    else:
        w("덮지 못한 호출 지점: 없음")
    실패 = [e["이름"] for e, _, 오류 in 결과 if 오류]
    w("")
    w("렌더 실패 항목: " + (", ".join(실패) if 실패 else "없음"))
    w("")
    w("판정 경로 밖(이 문서에 넣지 않음, `llm_호출` 을 부르는 파일): " + (", ".join(f"`{x}`" for x in 밖) if 밖 else "없음"))
    w("")

    출력_경로.write_text("\n".join(L), encoding="utf-8", newline="\n")
    호출수 = sum(len(c) for _, c, _ in 결과)
    print(f"썼다: {출력_경로} (항목 {len(목록)}개, 프롬프트 {호출수}개, 호출 지점 {len(지점)}곳 중 덮지 못함 {len(안덮음)}곳, 렌더 실패 {len(실패)}개)")
    for x in 안덮음:
        print(f"  덮지 못함: {x['파일']}:{x['줄']} {'.'.join(x['함수들'])} → {x['피호출']}")
    for e, _, 오류 in 결과:
        if 오류:
            print(f"  렌더 실패: {e['이름']}: {오류.splitlines()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
