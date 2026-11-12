# -*- coding: utf-8 -*-
"""mock 모드(SUDDOE_MOCK=1)의 새 점검 흐름 고정 응답. 실제 검색 · LLM · DB 를 부르지 않는다.

프론트가 mock 서버로 새 지출 계획 화면(2-1절)의 가지를 끝까지 눌러 볼 수 있게 한다(스펙 8-5).
지출 항목(품목)에 아래 낱말이 있으면 그 가지를 돌려준다. 없으면 조사확인 → 고정 분류 → 질문 1개.
낱말 표와 응답 예: docs/8_운영/8-1b_mock_흐름.md.

소비처: routes_normalize(정규화) · routes_questions(품목조사 · 분류 · 판정조사 · 질문) · routes_judge(분류 없는 판정).
"""
from __future__ import annotations

import re
import threading
import time
import uuid

from server.selftest import mock_질문, mock_정규화

# 낱말 → 가지. 앞에서부터 처음 맞는 낱말 하나만 쓴다(낱말끼리 글자가 겹치지 않는다)
가지_낱말 = ("별칭", "검색실패", "재조사", "설명필요", "분류장애", "분류끝내없음", "처리실패", "질문없음", "입력칸만", "옛경로")

# 진행 줄 사이 지연. 화면에서 줄이 하나씩 쌓이는 것을 보이려는 값이다. selftest 는 0 으로 둔다
지연초 = 0.4

고정분류 = {"비목": "지급수수료", "세목": "소프트웨어 구독료", "표시문": "지급수수료 > 소프트웨어 구독료",
          "품목": "디자인 협업 소프트웨어"}
# 옛 경로 사업(TIPS 등) 정규화 응답의 비목후보. 화면은 품목확인 이 없으면 비목 고르기 단계를 보인다
옛경로_비목후보 = [{"비목": "기계장치", "신뢰도": 0.82}, {"비목": "재료비", "신뢰도": 0.31}]

재조사확인_안내 = "규정에서 맞는 분류를 찾지 못해 품목을 더 알아봤어요. 찾은 내용을 확인해 주세요."
분류없이판정_안내 = "규정에서 맞는 분류를 찾지 못했어요. 분류 없이 점검하고 집행 전에 확인할 곳을 알려 드릴게요."

# 「분류장애」 · 「처리실패」 는 조사ID 마다 처음 한 번만 실패하고 다시 보내면 성공한다(화면의 [다시 시도] 확인용)
_시도: dict[tuple, int] = {}
_시도_잠금 = threading.Lock()


def 가지(품목: str | None) -> str | None:
    글 = 품목 or ""
    return next((w for w in 가지_낱말 if w in 글), None)


def _쉼() -> None:
    if 지연초 > 0:
        time.sleep(지연초)


def _알림(단계: str, 문장: str, 상태: str = "진행중", id: str | None = None) -> str:
    import research_runtime
    event_id = id or uuid.uuid4().hex
    research_runtime.emit(단계, 문장, 상태, id=event_id)
    _쉼()
    return event_id


def _금액(문장: str) -> float | None:
    m = re.search(r"(\d[\d,]*)\s*(만)?\s*원", 문장)
    if not m:
        return None
    값 = float(m.group(1).replace(",", ""))
    return 값 * 10_000 if m.group(2) else 값


def 정규화(body) -> dict:
    """/api/normalize 결과. 보낸 지출 항목을 그대로 돌려준다(한 문장이면 그 문장이 지출 항목). 거래처는 문장의 「거래처 ○○」 만."""
    문장 = (body.질문 or "").strip()
    품목 = 문장 or (body.품목 or "")
    out = {**{k: v for k, v in mock_정규화.items() if k not in ("품목확인", "비목후보", "분류후보")},
           "품목": 품목,
           "금액": _금액(문장) if 문장 else body.금액,
           "금액_추정여부": False,
           "용도": "사업 운영에 사용" if 문장 else (body.용도 or ""),
           "사용일자": None,
           "수치": {"부가세구분": body.부가세구분 or "모름"},
           "거래처": (m.group(1) if (m := re.search(r"거래처\s*[:：]?\s*([^\s,.]+)", 문장)) else None) if 문장 else None,
           "비목후보": [], "심층질문": []}
    if 가지(품목) == "옛경로":            # 옛 경로 사업: 품목확인 · 분류후보 없이 비목후보만(routes_normalize._live_정규화 와 같은 모양)
        return {**out, "비목후보": list(옛경로_비목후보)}
    별칭 = 가지(품목) == "별칭"
    from item_classify import 안내문
    return {**out, "분류후보": [],
            "품목확인": {"별칭일치": 별칭, "분류": dict(고정분류) if 별칭 else None,
                     "안내": 안내문(고정분류) if 별칭 else None}}


def _후보(유형: str, 내용: str, 질문: str, 주소: str) -> dict:
    출처 = {"제목": 주소.split("/")[2], "url": 주소, "발췌": 내용, "수집시각": "2026-11-13T00:00:00+00:00"}
    return {"id": uuid.uuid4().hex, "유형": 유형, "내용": 내용, "확인문장": 질문.removesuffix(" 맞나요?"),
            "질문": 질문, "질문종류": "문장", "출처": [출처], "상태": "확인필요"}


def 품목조사(품목: str | None, 거래처: str | None) -> dict:
    """② 품목 조사 고정 응답(web_research.research 대신). 「검색실패」 면 상태 실패 · 후보 없음."""
    import research_runtime
    이름 = (품목 or "품목").strip()[:40]
    event_id = _알림("공개 조사", f"「{이름}」 검색 중")
    if 가지(품목) == "검색실패":
        _알림("공개 조사", research_runtime.공개조사_실패문장, "실패", id=event_id)
        return {"상태": "실패", "후보": [], "출처": []}
    _알림("공개 조사", "관련 페이지 3개를 찾았어요.", "완료", id=event_id)
    _알림("공개 조사", "example.com 본문을 확인했어요.", "완료")
    후보 = [_후보("제품성격", f"{이름}은(는) 월 구독형 디자인 협업 도구입니다. 1인 요금은 월 16달러입니다.",
                f"{이름}은(는) 월 16달러인 디자인 협업 도구예요. 맞나요?", "https://example.com/product")]
    if 거래처 and 거래처.strip():
        후보 += [_후보("판매자", f"{거래처.strip()}는 이 제품을 판매하는 국내 사업자입니다.",
                     f"{거래처.strip()}는 이 제품을 판매하는 국내 사업자예요. 맞나요?", "https://example.com/seller"),
               _후보("업체이용조건", "결제 후 7일 안에는 환불할 수 있습니다.",
                     "결제 후 7일 안에는 환불할 수 있어요. 맞나요?", "https://example.com/terms")]
    return {"상태": "확인필요", "후보": 후보, "출처": [s for c in 후보 for s in c["출처"]]}


def _재조사(품목: str | None) -> dict:
    """③-R 재조사 고정 응답. 후보 모양은 ② 와 같다."""
    이름 = (품목 or "품목").strip()[:40]
    event_id = _알림("재조사", "품목을 더 알아보고 있어요.")
    _알림("재조사", "관련 페이지 2개를 더 찾았어요.", "완료", id=event_id)
    return {"후보": [_후보("제품성격", f"{이름}은(는) 팀 단위로 쓰는 온라인 디자인 소프트웨어 사용권입니다.",
                        f"{이름}은(는) 팀이 함께 쓰는 온라인 디자인 소프트웨어 사용권이에요. 맞나요?",
                        "https://example.com/more")]}


def _몇번째(조사ID, 가지명: str) -> int:
    with _시도_잠금:
        k = (조사ID, 가지명)
        _시도[k] = _시도.get(k, 0) + 1
        return _시도[k]


def 분류(body, 조사확인: dict | None, 조사ID) -> dict:
    """③ 분류 고정 응답. routes_questions._분류 와 같은 모양에 분류없음 일 때 다음행동(+재조사)을 싣는다.

    서버 순서(8-2): 재조사를 아직 안 했으면 재조사 → 찾으면 재조사확인 / 설명을 받은 적 없으면 설명입력 / 둘 다 했으면 판정.
    """
    from item_classify import 안내문
    from server.routes_questions import _분류실패_안내
    조사확인 = 조사확인 or {}
    g = 가지(body.품목)
    재조사함 = bool(조사확인.get("재조사확인"))
    설명받음 = bool(조사확인.get("품목설명")) or any(
        (x.get("정정") or "").strip() for x in [*(조사확인.get("확인한정보") or []), *(조사확인.get("재조사확인") or [])])
    없음 = {"분류": None, "노드결정": None}
    if g == "처리실패" and _몇번째(조사ID, g) == 1:
        raise RuntimeError("mock 요청 처리 실패")
    if g == "분류장애" and _몇번째(조사ID, g) == 1:
        return {**없음, "안내": _분류실패_안내, "실패": True, "다음행동": "다시시도"}
    if g in ("재조사", "분류끝내없음") and not 재조사함:
        return {**없음, "안내": 재조사확인_안내, "다음행동": "재조사확인", "재조사": _재조사(body.품목)}
    if g in ("설명필요", "분류끝내없음") and not 설명받음:
        return {**없음, "안내": 안내문(None), "다음행동": "설명입력"}
    if g == "분류끝내없음":
        return {**없음, "안내": 분류없이판정_안내, "다음행동": "판정"}
    return {"분류": dict(고정분류), "노드결정": None, "안내": 안내문(고정분류)}


def 판정조사(비목: str | None) -> None:
    """④-2 판정용 조사 고정 진행 줄(question_generate._판정용_조사 대신). 판정에 넘길 값은 없다(mock 판정은 고정 응답)."""
    # 문구는 question_generate 의 실제 문장과 같다. question_generate 는 검색 · DB 모듈을 끌어와 mock 에서 import 하지 않는다
    판정조사_시작문장 = "규정에 필요한 정보를 공개 웹에서 확인하고 있어요."
    event_id = _알림("규정조회", f"{비목 or '이 지출'}의 적용 규정과 필요한 확인 항목을 조회하고 있어요.")
    _알림("규정조회", "규정 원문 3개에서 확인할 내용을 준비했어요.", "완료", id=event_id)
    event_id = _알림("판정조사", 판정조사_시작문장)
    _알림("판정조사", "공개 웹에서 규정에 필요한 정보 2개를 확인했어요.", "완료", id=event_id)


def 질문(품목: str | None) -> dict:
    """확인 질문 고정 응답. 「질문없음」 은 바로판정, 「입력칸만」 은 질문 0개 + 추가입력 1개."""
    g = 가지(품목)
    if g == "질문없음":
        return {**mock_질문, "상태": "바로판정", "질문": [], "추가입력": [], "용어풀이": []}
    if g == "입력칸만":
        return {**mock_질문, "상태": "있음", "질문": []}
    return mock_질문
