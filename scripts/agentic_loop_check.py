# -*- coding: utf-8 -*-
"""고정 LLM·외부 호출 없는 확인 정보와 추가 조회 연결 검사."""
from __future__ import annotations

from unittest.mock import patch
import sys
import time

import ontology_research_plan as plan
import ontology_query
import ontology_query_map
import ontology_query_plan
import research_runtime
from server import research_sessions


def _조사(답, 정정=None):
    row = {"id": "c1", "유형": "판매자", "내용": "원래 판매자 안내", "출처": [], "상태": "확인필요"}
    session = research_sessions.Session((None, None), 999999999)
    session.조사 = {"상태": "확인필요", "후보": [row], "출처": []}
    answer = {"id": "c1", "답": 답}
    if 정정:
        answer["정정"] = 정정
    return research_sessions.후보_답변(session, [answer])


def main():
    확인 = _조사("맞아요")
    정정 = _조사("직접입력", "새 판매자")
    # ② 재설계(11-11): 답은 맞아요 | 직접입력(설명 필수). 설명 없는 직접입력 · 답하지 않은 후보는 다음 단계로 못 간다
    for 잘못 in (lambda: _조사("직접입력"), lambda: _조사("아니에요")):
        try:
            잘못()
            raise AssertionError("잘못된 조사 답 허용")
        except ValueError:
            pass
    빈세션 = research_sessions.Session((None, None), 999999999)
    빈세션.조사 = {"상태": "확인필요", "후보": [{"id": "c1", "유형": "제품성격", "내용": "x", "출처": [], "상태": "확인필요"}], "출처": []}
    try:
        research_sessions.후보_답변(빈세션, [])
        raise AssertionError("답하지 않은 후보 허용")
    except ValueError:
        pass
    assert "원래 판매자 안내" in plan.확인정보_문맥(확인)
    assert "새 판매자" in plan.확인정보_문맥(정정)
    assert "원래 판매자 안내" not in plan.확인정보_문맥(정정)
    import question_generate
    질문프롬프트 = []
    def 질문llm(prompt, schema, **kwargs):
        질문프롬프트.append(prompt)
        selected = "item-a" if "원래 판매자 안내" in prompt else "item-b"
        return {"질문": [], "품목노드": {"id": selected, "확신": "동일"}, "확인질문": None}, {}
    품목후보 = [{"id": "item-a", "정식명": "A품목", "별칭": []}, {"id": "item-b", "정식명": "B품목", "별칭": []}]
    with patch.object(question_generate, "llm_호출", side_effect=질문llm):
        결정들 = [question_generate._질문_작성({"품목": "검색 서비스", "_조사확인": info},
                   "[S01] 규정 원문", {"S01": ("article", 1, "①")}, {}, "fixture", 5,
                   {"버린질문": {}}, time.time(), 온톨로지경로=True, 품목후보=품목후보)
                for info in (확인, 정정)]
    assert [x["노드결정"]["id"] for x in 결정들] == ["item-a", "item-b"]
    assert "새 판매자" in 질문프롬프트[1] and "원래 판매자 안내" not in 질문프롬프트[1]
    calls = []
    def llm(prompt, schema, **kwargs):
        calls.append(prompt)
        return {"행동": "조회", "질의": "MATCH (n:Norm)-[r:TARGETS]->(e:Entity) WHERE e.id = $id RETURN DISTINCT n.id AS norm_id LIMIT 20",
                "인자": [{"이름": "id", "값들": ["선택1"]}]}, {}
    조회 = {"대상": {"비목_id": "분류1"}, "자금": "자금1", "게이트": {"종류": None},
          "적용": {"규범": []}, "근거행": []}
    base = {"사업명": "사업1", "품목": "품목1", "용도": "용도1", "질문": "질문1"}
    규범결과 = {"완료": True, "규범": [{"norm_id": "n1"}],
              "근거행": [{"doc_id": "문서1", "조번호": "1", "항호": "①", "article_id": 1}]}
    from types import SimpleNamespace
    지도 = SimpleNamespace(version="v1", program="사업1", category="분류1", fund="자금1",
                           view={"지도형식": ontology_query_map.지도_버전})
    b = research_runtime.Budget()
    with research_runtime.activate(b), patch.object(plan.ontology_age, "준비_버전", return_value="v1"), \
         patch.object(ontology_query_map, "map_for", return_value=지도), \
         patch.object(ontology_query, "execute", return_value={"규범ID": ["규범:선택1"], "상한도달": False, "버전": "v1"}), \
         patch.object(plan.ontology_research, "규범ID_검증조회", return_value=규범결과), \
         patch.object(ontology_query_plan, "llm_호출", side_effect=llm):
        first = plan.추가_조회(None, 조회, {**base, "_조사확인": 확인})
        second = plan.추가_조회(None, 조회, {**base, "_조사확인": 정정})
        assert first["게이트"] == 조회["게이트"] and first["적용"] == 조회["적용"]
        assert first["근거행"] and second["근거행"]
        assert len(calls) == 2, "확인 답이 바뀌면 추가 규정 계획도 다시 계산해야 한다"
        assert "원래 판매자 안내" in calls[0]
        assert "새 판매자" in calls[1] and "원래 판매자 안내" not in calls[1]
        ended = plan.추가_조회(None, {**조회, "게이트": {"종류": "금지"}}, base)
        assert ended["게이트"]["종류"] == "금지" and len(calls) == 2
    print("agentic loop fixture checks passed")


def db_check():
    import ontology_query_plan_check
    ontology_query_plan_check.실행()


def diagnosis():
    import os
    import psycopg
    import orchestrate
    import ontology_fund
    import ontology_lookup
    from server.catalog import _사업명_표준화
    with psycopg.connect("postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5",
                         options="-c default_transaction_read_only=on", autocommit=True) as conn, conn.cursor() as cur:
        단축 = "모두의 창업"
        표준 = _사업명_표준화(단축)
        os.environ["SUDDOE_ONTOLOGY_PROGRAMS"] = 단축
        기존경로 = ontology_lookup.새경로_대상(cur, 단축)
        os.environ["SUDDOE_ONTOLOGY_PROGRAMS"] = 표준
        새경로 = ontology_lookup.새경로_대상(cur, 표준)
        print("normalized_program", 표준, "short_program_new_path", 기존경로, "canonical_new_path", 새경로)
        입력 = {"사업명": 표준, "품목": "APICK API검색크레딧", "용도": "제품개발용AI검색",
              "질문": "제품 개발용 AI 검색 API 크레딧 55000원"}
        조회 = orchestrate.온톨로지_조회(cur, 입력["사업명"], "창업활동비", "지급수수료",
                    orchestrate.게이트_입력(55000, "포함", None), 확정=True, 품목=입력["품목"])
        대상 = 조회.get("대상") or {}
        후보 = plan.ontology_research.후보_목록(cur, 사업=입력["사업명"], 비목_id=대상["비목_id"], 자금=조회.get("자금"))
        품목후보 = (ontology_lookup.품목_후보(cur, 입력["사업명"], 대상["비목_id"],
                       조회.get("자금") != ontology_fund.창업활동자금)
                    if not 조회["적용"].get("품목노드") else [])
        calls = []
        def 가짜_계획(*args, **kwargs):
            calls.append(True)
            return {"행동": "완료", "질의": "", "인자": []}, {}
        budget = research_runtime.Budget()
        with research_runtime.activate(budget), patch.object(ontology_query_plan, "llm_호출", side_effect=가짜_계획):
            plan.추가_조회(cur, 조회, 입력)
        print("diagnosis target", 대상.get("대상_id"), "category", 대상.get("비목_id"),
              "gate", 조회["게이트"].get("종류"), "alias", 조회["적용"].get("품목노드"),
              "additional_candidates", len(후보), "node_candidates", len(품목후보),
              "additional_plan_attempt", len(calls))


if __name__ == "__main__":
    main()
    if "--db" in sys.argv:
        db_check()
    if "--diagnose" in sys.argv:
        diagnosis()
