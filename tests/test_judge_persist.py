# -*- coding: utf-8 -*-
"""`/api/judge` 저장 배선 테스트.   **[판정 저장 배선]**

`server/main.py::judge()` 의 `gen()` 이 `결과` 다음 · `완료` 앞에 `저장` 이벤트를
정확히 한 번 흘리는지, `decision_id` 가 `결과` 에는 안 실리고 `저장` 에만 실리는지,
저장이 실패해도(예외·`plan_id` 없음·캐시 적중) 스트림이 안 죽는지를 검증한다.

🔴 `server/persist.py` 는 2026-09-01 에 들어왔다 — `main.py` 는 그래서
   모듈 상단이 아니라 `gen()` 안에서 지연 import 한다(2026-09-01 ai-14 정정,
   이유: `_실_판정` 이 `orchestrate` 를 지연 import 하는 것과 같은 결. 저장 계층은
   판정 스트림의 부수 효과라 그게 없다고 앱 전체가 못 뜨면 의존 방향이 거꾸로다).
   그 파일이 있어야만 참이 되는 두 테스트만 `xfail(strict=False)` 로 걸어둔다 —
   A 가 올리면 자동으로 xpass 로 뒤집힌다. 나머지 다섯은 지금 초록이어야 한다.

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_judge_persist.py -q
"""
from __future__ import annotations

import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

import server
import server.main as main

client = TestClient(main.app)


def _sse_parse(text: str) -> tuple[list[str], dict[str, list]]:
    """SSE 본문 → (이벤트 이름이 나온 순서, {이름: [payload, ...]})."""
    순서: list[str] = []
    묶음: dict[str, list] = {}
    이름 = None
    for 줄 in text.splitlines():
        if 줄.startswith("event: "):
            이름 = 줄[7:]
            순서.append(이름)
        elif 줄.startswith("data: ") and 이름:
            묶음.setdefault(이름, []).append(json.loads(줄[6:]))
    return 순서, 묶음


def _judge(body: dict, 목: str | None = None) -> tuple[list[str], dict[str, list]]:
    url = "/api/judge" + (f"?목={목}" if 목 else "")
    r = client.post(url, json=body)
    assert r.status_code == 200, r.text
    return _sse_parse(r.text)


_기본몸 = {
    "정규화": {"품목": "맥북", "금액": 2_500_000, "용도": "디자이너 작업용", "비목후보": []},
    "확정비목": "기계장치", "사업명": "예비창업패키지",
    "f5": {"친족거래": False, "전직임직원업체": False},
}


# ════════════════════════════════════════════════════════════════════
# 지금 초록이어야 하는 다섯 — persist.py 없이도 돈다
# ════════════════════════════════════════════════════════════════════

def test_저장_이벤트는_결과와_완료_사이에_한_번():
    순서, 묶음 = _judge({**_기본몸, "plan_id": 1})
    assert 순서.count("저장") == 1
    assert 순서.index("결과") < 순서.index("저장") < 순서.index("완료")
    assert "완료" in 묶음                       # 스트림이 끝까지 갔다


def test_plan_id_없으면_저장_안_하고_사유를_남긴다():
    순서, 묶음 = _judge(_기본몸)                 # plan_id 생략
    assert 묶음["저장"][0] == {"저장": False, "사유": "plan_id 없음"}
    assert "완료" in 묶음


def test_캐시_적중은_새로_저장하지_않는다():
    몸 = {**_기본몸, "plan_id": 1, "정규화": {"품목": "캐시확인용품목", "금액": 999,
                                          "용도": "캐시 테스트", "비목후보": []}}
    _judge(몸)                                   # 1차 — 캐시를 채운다
    순서, 묶음 = _judge({**몸, "plan_id": 2})      # 2차 — 캐시 열쇠에 plan_id 가 없어 그대로 적중
    assert 묶음["완료"][0]["캐시"] is True
    assert 묶음["저장"][0] == {"저장": False, "사유": "캐시 적중 — 새 판정 기록 없음"}


def test_저장_예외가_나도_완료까지_간다(monkeypatch):
    """`판정_저장` 이 예외를 던지도록 가짜 `server.persist` 모듈을 주입한다.

    실제 파일 없이도 지연 import 의 예외 경로(`_판정_저장_시도`)를 그대로 태운다 —
    `from .persist import 판정_저장` 이 sys.modules 캐시를 먼저 본다.

    🔴 `sys.modules["server.persist"]` 만 지우면 안 된다 — 그 import 문이 한 번이라도
       성공하면 파이썬이 부모 패키지(`server`)에도 `persist` 속성을 박아 넣는다.
       그 속성이 남으면 다음 테스트에서 `from .persist import ...` 가 (sys.modules 에
       키가 없어도) getattr 폴백으로 이 가짜 모듈을 다시 집어써 버린다 — 그래서 속성도
       같이 monkeypatch 로 걸어 둘 다 테스트 끝나면 원상복구되게 한다.
    """
    fake = types.ModuleType("server.persist")

    def _터짐(*a, **kw):
        raise RuntimeError("DB 다운")

    fake.판정_저장 = _터짐
    monkeypatch.setitem(sys.modules, "server.persist", fake)
    monkeypatch.setattr(server, "persist", fake, raising=False)

    몸 = {**_기본몸, "plan_id": 1, "정규화": {"품목": "예외테스트품목", "금액": 1234,
                                          "용도": "예외 테스트", "비목후보": []}}
    순서, 묶음 = _judge(몸)
    assert 묶음["저장"][0] == {"저장": False, "사유": "저장 실패 (RuntimeError)"}
    assert "완료" in 묶음


def test_결과에는_decision_id가_없다(monkeypatch):
    """`_실_판정` 이 decision_id 를 얹어 돌려줘도 `결과` 이벤트에는 안 실린다."""
    monkeypatch.setattr(main, "MOCK", False)

    def _가짜_판정(_body):
        return {"판정": "가능", "요약": "테스트", "해야할일": [], "인용": [], "전제": [],
                "신뢰등급": "A", "버전스탬프": "test", "참조사슬": [], "decision_id": 555}

    monkeypatch.setattr(main, "_실_판정", _가짜_판정)
    몸 = {**_기본몸, "plan_id": 1, "정규화": {"품목": "decision_id테스트", "금액": 1,
                                          "용도": "decision_id 테스트", "비목후보": []}}
    _, 묶음 = _judge(몸)
    assert "decision_id" not in 묶음["결과"][0]


# ════════════════════════════════════════════════════════════════════
# `server/persist.py` — 2026-09-01 반영됨, xfail 걷어냄
# ════════════════════════════════════════════════════════════════════

def test_저장_성공하면_계약대로_담긴다():
    몸 = {**_기본몸, "plan_id": 1, "정규화": {"품목": "실저장테스트", "금액": 3000,
                                          "용도": "실저장 테스트", "비목후보": []}}
    _, 묶음 = _judge(몸)
    저장 = 묶음["저장"][0]
    assert 저장["저장"] is True
    assert {"decision_id", "plan_id", "할일"} <= set(저장)


# ════════════════════════════════════════════════════════════════════
# 🔴 2026-09-02 — 이 xfail 의 사유가 **세 번 틀리게 적혀 있었다.** 실측으로 좁혔다.
#
#   적혀 있던 것 ①「레인 A 의 persist.py 대기」 → 그 파일은 2026-09-01 에 들어왔다
#   적혀 있던 것 ②「GPU 판정이 있어야 한다」   → 🔴 **내가 오늘 밤 잘못 적은 것이다.**
#       인수인계 문서의 「GPU 팟 미가동」을 그대로 옮겼는데, 이 테스트는 `_실_판정` 을
#       monkeypatch 로 갈아끼워 **LLM 을 아예 안 부른다.** GPU 와 무관하다
#   추정됐던 것 ③「실 plan_id=1 행이 없어서」 → 행을 실제로 넣고 태워봤다. **여전히 xfail.**
#
#   실제 원인(태워서 확인): `monkeypatch.setattr(main, "MOCK", False)` 는 `main` 모듈의
#   MOCK 만 뒤집는다. `persist.py` 는 import 시점에 `from ._common import MOCK` 로
#   **자기 모듈 상수를 따로 들고 있어서** 그대로 True 다 → `판정_저장()` 이 목 분기를 타고
#   하드코딩 `decision_id: 9001` 을 돌려준다. 실패 메시지가 `assert 9001 == 777` 이다.
#
#   → 닫으려면 `persist.MOCK` 도 같이 뒤집고 계획 행을 하나 만들면 된다
#     (`test_contract.py` 의 `모드()` 가 전 모듈을 한 번에 뒤집는 그 방식이다).
#     **고치는 건 오너 판단 대기다 — 사유만 사실로 고쳐 둔다.**
# ════════════════════════════════════════════════════════════════════


@pytest.mark.xfail(reason="`persist.MOCK` 이 안 뒤집혀 목 분기(decision_id=9001)를 탄다. "
                          "GPU 와 무관하다", strict=False)
def test_저장_이벤트에_decision_id가_실제로_채워진다(monkeypatch):
    monkeypatch.setattr(main, "MOCK", False)

    def _가짜_판정(_body):
        return {"판정": "가능", "요약": "테스트", "해야할일": [], "인용": [], "전제": [],
                "신뢰등급": "A", "버전스탬프": "test", "참조사슬": [], "decision_id": 777}

    monkeypatch.setattr(main, "_실_판정", _가짜_판정)
    몸 = {**_기본몸, "plan_id": 1, "정규화": {"품목": "decision_id실저장", "금액": 1,
                                          "용도": "decision_id 실저장", "비목후보": []}}
    _, 묶음 = _judge(몸)
    assert 묶음["저장"][0].get("decision_id") == 777
