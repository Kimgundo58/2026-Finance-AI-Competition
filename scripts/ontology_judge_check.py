#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""판정 LLM 출력을 고정하고 화면이 쓰는 경로(`routes_judge._live_판정`)로 온톨로지 경로 동작을 막는 시험.

LLM · 임베딩 모델을 쓰지 않는다. 필요한 것은 온톨로지와 조문이 적재된 DB 다(`SUDDOE_DSN`, CI 는 ontology-db 잡).
고정: `orchestrate.llm_호출`(판정 출력) · `retrieve.검색`(참조 확장으로 시험 조문을 넣는다) · `적용_규범`(시험 규범).
추출 작업으로 규범 데이터가 바뀌어도 흔들리지 않게 규범만 고정하고, 조문 읽기 · 자금 거르기 · 게이트 · 검증 · 응답 매핑은 실제 코드다.
판정 기록(tenant.decisions)은 남기지 않는다.
실행: PYTHONIOENCODING=utf-8 python scripts/ontology_judge_check.py --self-test
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

사업 = "모두의 창업 프로젝트"
os.environ["SUDDOE_MOCK"] = "0"
os.environ["SUDDOE_ONTOLOGY_PROGRAMS"] = 사업
os.environ.setdefault("SUDDOE_DEMO_SECRET", "ontology-judge-check")

import ontology_context as C  # noqa: E402
import ontology_gate as G  # noqa: E402
import ontology_lookup as L  # noqa: E402
import orchestrate as O  # noqa: E402
import question_generate as Q  # noqa: E402
import retrieve  # noqa: E402
from _lib import db  # noqa: E402
from server import routes_judge  # noqa: E402
from server.models import 판정요청  # noqa: E402

창업활동비_id = "비목:창업활동자금/*@모두의창업"
재료비_id = "비목:창업/재료비"
문서 = L._스키마()["문서키"]
정상요약 = "멘토기관 승인을 받은 뒤 쓸 수 있습니다."


def _규범(이름: str, 종류: str, 대상: str, 근거: list[tuple[str, str, str | None]], *, 수치=None, 결과=None, verified=True) -> dict:
    return {"norm_id": f"규범:시험/{이름}", "종류": 종류, "계층": "L2", "적용사업": 사업, "대상": [대상], "수범자": [], "승인주체": [],
            "조건": {"구조화": [], "자연어": []}, "수치": 수치, "결과": 결과 or {}, "예외": [], "해석미결": False,
            "검수단계": "AI검증" if verified else "미검증", "verified": verified,
            "근거": [{"칸": "대상", "doc_id": 문서[k], "조번호": 조, "항호": 항, "발췌": ""} for k, 조, 항 in 근거]}


def _한도(이름: str, 대상: str, 값: int, 부가세="포함", verified=False) -> dict:
    return _규범(이름, "한도", 대상, [("안내책자", "p9-1", None)], verified=verified,
                 수치={"값": 값, "단위": "원", "분모": [], "분모주체": None, "누적범위": None, "부가세": 부가세, "지역": None, "위반연산": "초과"},
                 결과={"종류": "불가"})


공통문장 = "시험용 공통 의무 문장입니다."
_공통 = _규범("공통의무", "의무", L.모든비목, [("운영요령", "제9조", None)], 결과={"문장": 공통문장, "시점": "집행 전"})
# 창업활동 규범의 근거에 시제품 조문을 일부러 섞는다(근거행 거르기가 빼야 한다)
_창업의무 = _규범("창업의무", "의무", 창업활동비_id, [("안내책자", "p11-1", None), ("모두의창업", "제33조", None)], 결과={"문장": "시험용 창업활동 의무 문장입니다.", "시점": "집행 전"})
_재료의무 = _규범("재료의무", "의무", 재료비_id, [("모두의창업", "제34조", None), ("모두의창업", "제35조", None), ("안내책자", "p9-1", None)], 결과={"문장": "시험용 재료비 의무 문장입니다.", "시점": "집행 전"})
규범표 = {창업활동비_id: [_한도("한도", 창업활동비_id, 2_000_000), _창업의무, _공통], 재료비_id: [_재료의무]}


class _고정:
    """시험 하나의 판정 출력과 잡아 둔 프롬프트 · 문장맵."""
    출력: dict = {}
    프롬프트 = ""
    문장맵: dict = {}
    호출수 = 0


def _llm(프롬프트, 스키마, **k):
    _고정.호출수 += 1
    출력 = dict(_고정.출력)
    인용 = 출력.pop("인용", None)
    if callable(인용):                 # 프롬프트의 문장에서 고른다
        인용 = 인용(프롬프트)
    ids = (스키마["properties"]["인용"]["items"].get("enum") or [])[:1]
    인용 = 인용 or ids
    전제 = 출력.pop("전제", None)
    return {"해야할일": [], "인용": 인용, **출력,
            "전제": [{"사실": 전제, "근거조항": 인용[0], "매핑": [], "미충족시": "판단불가"}] if 전제 else []}, {"모델": "고정", "입력토큰": 0, "출력토큰": 0}


_원_프롬프트 = O.프롬프트_구성


def _프롬프트(*a, **k):
    프롬프트, 문장맵, 참조 = _원_프롬프트(*a, **k)
    _고정.프롬프트, _고정.문장맵 = 프롬프트, 문장맵
    return 프롬프트, 문장맵, 참조


_원_판정 = O.판정
_원_적용 = L.적용_규범


def _적용(cur, 사업명, 대상_id):
    return {"규범": [dict(n) for n in 규범표.get(대상_id, [])], "경로": [], "우선순위_적용": []}


def _조문id(cur, 키: str, 조번호: str) -> int:
    행 = cur.execute('SELECT article_id FROM corpus.doc_articles WHERE doc_id = %s AND "조번호" = %s', (문서[키], 조번호)).fetchone()
    if not 행:
        raise SystemExit(f"시험 조문이 DB 에 없습니다: {키} {조번호}")
    return 행[0]


def _고정_걸기(cur) -> None:
    """검색 후보: AI 솔루션 쪽 · 시제품 조 · 창업활동 쪽을 모두 넣어 자금 거르기를 시험한다."""
    후보 = [_조문id(cur, *x) for x in (("안내책자", "p21-1"), ("모두의창업", "제33조"), ("안내책자", "p9-1"))]
    O.llm_호출 = _llm
    O.프롬프트_구성 = _프롬프트
    O.판정 = lambda 질, **k: _원_판정(질, **{**k, "기록": False})
    검색결과 = {**O._빈_검색결과(), "참조확장": 후보}
    retrieve.검색 = lambda *a, **k: dict(검색결과)
    L.적용_규범 = _적용
    L.비목_품목금지 = lambda *a, **k: []
    L.세목_한도_있음 = lambda *a, **k: False


def 판정(비목: str, 금액: int, 출력: dict, 구분: str = "포함") -> dict:
    _고정.출력, _고정.프롬프트, _고정.문장맵, _고정.호출수 = 출력, "", {}, 0
    정 = {"품목": "시험품목", "금액": 금액, "금액_추정여부": False, "용도": "시제품", "질문원문": f"시험품목 {금액}원", "질의문": "시제품 시험품목",
         "분류후보": [{"비목": 비목, "세목": None, "표시문": 비목}], "비목후보": [{"비목": 비목, "신뢰도": 1.0}], "수치": {"부가세구분": 구분}, "누락필드": []}
    return routes_judge._live_판정(판정요청(정규화=정, 확정비목=비목, 사업명=사업, org_id=None, 추가입력값={}, 부가세구분=구분), None, None, False)


def _할일(r: dict) -> list[str]:
    return [t.get("항목") or "" for t in r.get("해야할일") or []]


def _금액_든_문장(프롬프트: str) -> tuple[str, float] | None:
    """프롬프트의 `[S01] 머리\\n조각` 에서 금액이 있는 문장을 찾는다."""
    for m in re.finditer(r"\[(S\d+)\] (.*?)(?=\n\n|\Z)", 프롬프트, re.S):
        값들 = sorted(x for x in G.금액집합(m.group(2)) if x >= 10000)
        if 값들:
            return m.group(1), 값들[0]
    return None


def _시험() -> list[tuple[str, bool]]:
    t: list[tuple[str, bool]] = []
    멘토 = "멘토기관 담당자에게"

    r = 판정("창업활동비", 2_500_000, {"판정": "조건부", "요약": 정상요약})
    안내 = next((x for x in _할일(r) if "한도" in x), "")
    t.append(("미검증 한도 초과 + LLM 조건부 → 판단불가", r["판정"] == "판단불가"))
    t.append(("미검증 한도 초과: 요약이 안내 문장이고 물을 곳을 말한다", "넘" in r["요약"] and 멘토 in r["요약"] and r["요약"] == 안내))
    t.append(("미검증 한도 초과: 안내는 할 일에 있고 조건 칸에는 두 번 내지 않는다", bool(안내) and 안내 not in (r.get("조건") or [])))
    t.append(("미검증 한도 초과: 실패가 아니라 저장된다", not r.get("_실패")))
    t.append(("문의처가 응답 칸으로 나간다", r.get("문의처") == 멘토))
    t.append(("판단불가라 문의 초안이 나간다", bool(r.get("문의초안"))))
    t.append(("판정 LLM 은 한 번", _고정.호출수 == 1))
    t.append(("미검증 한도 초과 + LLM 가능 → 판단불가", 판정("창업활동비", 2_500_000, {"판정": "가능", "요약": 정상요약})["판정"] == "판단불가"))
    r = 판정("창업활동비", 2_500_000, {"판정": "불가", "요약": "규정상 쓸 수 없습니다."})
    t.append(("미검증 한도 초과 + LLM 불가 → 불가 그대로(올리지 않는다) · 할 일 없음", r["판정"] == "불가" and not _할일(r)))

    r = 판정("창업활동비", 1_000_000, {"판정": "가능", "요약": 정상요약})
    t.append(("공통 규범 미정: 창업활동자금은 가능이 조건부로 낮아진다", r["판정"] == "조건부"))
    t.append(("공통 규범 나누기: 공통 의무는 조건 · 할 일에 안 나가고 문맥 B2.6 에 있다",
              공통문장 not in [*(r.get("조건") or []), *_할일(r)] and "B2.6" in _고정.프롬프트 and "운영요령 제9조" in _고정.프롬프트))
    t.append(("창업활동 직접 의무는 할 일로 나간다", "시험용 창업활동 의무 문장입니다." in _할일(r)))
    t.append(("판정 문맥 자금: 창업활동 문맥에 AI 솔루션 · 시제품 조가 없다", "안내 책자 21쪽" not in _고정.프롬프트 and "제33조" not in _고정.프롬프트 and "안내 책자 9쪽" in _고정.프롬프트))
    t.append(("근거행 거르기: 화면 근거 경로에 시제품 조가 없다", not any("제33조" in x for x in r.get("근거경로") or []) and any("11쪽" in x for x in r.get("근거경로") or [])))

    r = 판정("재료비", 300_000, {"판정": "조건부", "요약": "납품 확인 뒤 집행합니다."})
    t.append(("판정 문맥 자금: 재료비 문맥에 책자가 없다", "안내 책자" not in _고정.프롬프트 and "제34조" in _고정.프롬프트))
    t.append(("문의처: 재료비는 주관기관", r.get("문의처") == "주관기관에"))
    t.append(("정상 요약은 그대로", r["판정"] == "조건부" and r["요약"] == "납품 확인 뒤 집행합니다." and not r.get("_실패")))

    r = 판정("창업활동비", 2_500_000, {"판정": "조건부", "요약": "한도 안에서 쓸 수 있는 금액은 240만원이고 나머지는 자부담입니다."})
    t.append(("요약 금액 틀림 → 판단불가 · 실패(캐시 · 저장 안 함)", r["판정"] == "판단불가" and r.get("_실패") is True))
    t.append(("요약 금액 틀림: 틀린 금액이 화면 · 문의 초안에 없고 물을 곳을 말한다", "240" not in r["요약"] and 멘토 in r["요약"] and "240" not in (r.get("문의초안") or "")))

    r = 판정("창업활동비", 2_500_000, {"판정": "조건부", "요약": "초과분 50만원은 자부담입니다."})
    t.append(("넘은 규범의 차액은 맞는 금액이다: 실패로 두지 않는다", r["판정"] == "판단불가" and not r.get("_실패")))
    r = 판정("창업활동비", 2_500_000, {"판정": "조건부", "요약": "초과분 75만원은 자부담입니다."})
    t.append(("다른 기준의 차액은 틀린 금액이다", r.get("_실패") is True and "75" not in r["요약"]))

    r = 판정("재료비", 300_000, {"판정": "불가", "요약": "7천7백만원을 초과하는 외주이므로 집행할 수 없습니다."})
    t.append(("불가 + 틀린 금액 → 불가 유지, 요약만 바꾼다", r["판정"] == "불가" and "7천7백만" not in r["요약"] and not r.get("_실패")))

    r = 판정("재료비", 300_000, {"판정": "조건부", "요약": "납품 확인 뒤 집행합니다.", "전제": "7천7백만원을 넘는 계약인지 확인이 필요합니다"})
    t.append(("전제 금액 틀림 + 조건부 → 판단불가 · 실패, 전제 제거", r["판정"] == "판단불가" and r.get("_실패") is True and not r.get("전제")))
    t.append(("전제 금액 틀림: 문의 초안에 그 금액이 없다", "7천7백만" not in (r.get("문의초안") or "")))
    r = 판정("재료비", 300_000, {"판정": "판단불가", "요약": "외주 계약 여부를 알 수 없어 판정을 내리지 않았습니다.", "전제": "7천7백만원을 넘는 계약인지 확인이 필요합니다"})
    t.append(("전제 금액 틀림 + 판단불가 → 요약 유지, 실패 아님, 전제 제거", r["요약"].startswith("외주 계약") and not r.get("_실패") and not r.get("전제")))

    # 인용 원문의 금액은 틀린 금액이 아니다. 응답["인용목록"] 키를 잘못 읽으면 여기서 판단불가가 된다
    찾음: list = []

    def 고르기(프롬프트: str) -> list[str]:
        x = _금액_든_문장(프롬프트)
        if x:
            찾음.append(x)
            return [x[0]]
        return []
    판정("재료비", 12_345, {"판정": "조건부", "요약": "준비", "인용": 고르기})
    t.append(("금액 든 문장을 찾았다", bool(찾음)))
    if 찾음:
        값 = int(찾음[0][1])
        r = 판정("재료비", 12_345, {"판정": "조건부", "요약": f"규정의 {값:,}원 기준을 확인한 뒤 집행합니다.", "인용": lambda _: [찾음[0][0]]})
        t.append(("인용 원문의 금액은 통과한다", r["판정"] == "조건부" and f"{값:,}" in r["요약"]))
        r = 판정("재료비", 12_345, {"판정": "조건부", "요약": f"규정의 {값 + 7_000_000:,}원 기준을 확인한 뒤 집행합니다.", "인용": lambda _: [찾음[0][0]]})
        t.append(("인용 원문에 없는 금액은 막는다", r["판정"] == "판단불가"))

    # 코드 판정의 문구도 자금별 문의처를 쓴다
    원본 = 규범표[창업활동비_id]
    규범표[창업활동비_id] = [_한도("한도", 창업활동비_id, 2_000_000, 부가세="정의없음", verified=True), _창업의무]
    r = 판정("창업활동비", 2_100_000, {"판정": "조건부", "요약": 정상요약})
    t.append(("부가세 기준 미정: LLM 없이 판단불가 · 물을 곳은 멘토기관", r["판정"] == "판단불가" and 멘토 in r["요약"] and _고정.호출수 == 0 and r.get("문의처") == 멘토))
    규범표[창업활동비_id] = 원본

    # 문맥이 넘치면 판단불가, 문구는 자금별 문의처
    원_문맥 = C.문맥_구성
    C.문맥_구성 = lambda *a, **k: 원_문맥(*a, **{**k, "상한": 1})
    r = 판정("창업활동비", 1_000_000, {"판정": "가능", "요약": 정상요약})
    C.문맥_구성 = 원_문맥
    t.append(("B2.6 이 잘리면 LLM 없이 판단불가 · 내부 말 없이 물을 곳을 말한다", r["판정"] == "판단불가" and 멘토 in r["요약"] and _고정.호출수 == 0 and "B2.6" not in r["요약"]))

    # 확인 질문은 판정과 같은 자금 문맥을 쓴다
    with db.connect(autocommit=True) as c:
        준비 = Q._새경로_준비(c.cursor(), {"사업명": 사업, "비목": "창업활동비", "금액": 1_000_000, "부가세구분": "포함", "품목": "시험품목", "질문": "시험품목"}, None)
    머리 = [x[2] for _, 항들 in 준비.get("문맥", {}).get("블록", []) for x in 항들]
    t.append(("질문 문맥 자금: 창업활동 질문 재료에 AI 솔루션 · 시제품 조가 없다", bool(머리) and not any("21쪽" in h or "제33조" in h for h in 머리) and any("9쪽" in h for h in 머리)))
    # 온톨로지 경로 질문은 금액집합으로 숫자를 본다(`3천5백만원` 을 옛 읽기는 5,003,000 으로 본다)
    받음: dict = {}
    원_작성 = Q._질문_작성
    Q._질문_작성 = lambda *a, **k: (받음.update(k), {"상태": "없음", "질문": [], "메타": {}})[1]
    with db.connect(autocommit=True) as c:
        Q._새경로_질문(c.cursor(), c, {"사업명": 사업, "비목": "창업활동비", "금액": 1_000_000, "부가세구분": "포함", "품목": "시험품목", "질문": "시험품목"}, None, "고정", 1, {}, 0.0)
    Q._질문_작성 = 원_작성
    t.append(("온톨로지 경로 질문은 금액 읽기를 하나로 쓴다", 받음.get("온톨로지경로") is True))
    # 미검증 한도 초과: 답해도 가능 · 조건부가 나오지 않으니 묻지 않는다. 부가세를 모르면 답으로 풀릴 수 있어 묻는다
    with db.connect(autocommit=True) as c:
        입력 = {"사업명": 사업, "비목": "창업활동비", "금액": 2_500_000, "품목": "시험품목", "질문": "시험품목"}
        넘음, 모름 = (Q._새경로_준비(c.cursor(), {**입력, "부가세구분": x}, None) for x in ("포함", "모름"))
    t.append(("미검증 한도 초과 + 부가세 확정: 질문 없이 판정으로 간다", 넘음.get("바로") == "미확정한도초과"))
    t.append(("미검증 한도 초과 + 부가세 모름: 질문 단계를 건너뛰지 않는다", "바로" not in 모름))
    # 캐시 적중 판정을 계획에 연결해도 화면 값이 남는다(routes_plans._live_상세 가 읽는다)
    from server.persist import _캐시판정_행
    r = 판정("창업활동비", 2_500_000, {"판정": "조건부", "요약": 정상요약})
    몸 = 판정요청(정규화={"금액": 2_500_000}, 확정비목="창업활동비", 사업명=사업, org_id=None, 추가입력값={}, 부가세구분="포함")
    행 = _캐시판정_행(몸, r, None, None, 1)
    저장값 = json.loads(행.get("온톨로지결과") or "{}")
    t.append(("캐시 연결: 온톨로지 칸이 판정 행에 남는다", 저장값.get("문의처") == 멘토 and 저장값.get("분류") == r.get("분류") and bool(r.get("분류")) and 행.get("경로") == "온톨로지"))
    t.append(("캐시 연결: 옛 경로 응답은 온톨로지 칸을 쓰지 않는다", "온톨로지결과" not in _캐시판정_행(몸, {"판정": "조건부", "분류": None}, None, None, 1)))
    return t


def main() -> int:
    if "--self-test" not in sys.argv:
        print(__doc__)
        return 0
    with db.connect(autocommit=True) as c:
        _고정_걸기(c.cursor())
    결과 = _시험()
    실패 = [x for x, ok in 결과 if not ok]
    for x, ok in 결과:
        print("ok  " if ok else "FAIL", x)
    print(f"selftest: {len(결과) - len(실패)}/{len(결과)} 통과")
    return 1 if 실패 else 0


if __name__ == "__main__":
    sys.exit(main())
