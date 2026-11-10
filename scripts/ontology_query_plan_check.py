# -*- coding: utf-8 -*-
"""고정 모델과 5462 읽기 전용 AGE로 추가 조회에서 최종 문장맵까지 확인한다."""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import ontology_context
import ontology_evidence
import ontology_fund
import ontology_lookup
import ontology_query_plan
import ontology_research_plan
import research_runtime
from assemble_context import 프롬프트_구성


def 단위():
    class 지도:
        view = {'후보': [{'id': '품목:x'}]}
    budget = research_runtime.Budget()
    질의 = 'MATCH (n:Norm)-[r:TARGETS]->(e:Entity) WHERE e.id = $id RETURN DISTINCT n.id AS norm_id LIMIT 20'
    def plan(출력):
        with patch.object(ontology_query_plan, 'llm_호출', return_value=(출력, {})):
            return ontology_query_plan.계획(None, 지도=지도(), 입력={}, 요청=None, 이전결과=[], budget=budget)
    유효 = {'행동': '조회', '질의': 질의, '인자': [{'이름': 'id', '값들': ['품목:x']}]}
    assert plan(유효)['params'] == {'id': '품목:x'}
    assert plan({'행동': '완료', '질의': '', '인자': []})['행동'] == '완료'
    for 값 in ({**유효, '질의': 질의 + ' DELETE n'},
              {**유효, '인자': [{'이름': 'id', '값들': ['a', 'b']}]},
              {**유효, '인자': [{'이름': '다른', '값들': ['a']}]},
              {'행동': '완료', '질의': 질의, '인자': []}):
        try:
            plan(값)
        except ValueError:
            continue
        raise AssertionError('잘못된 계획 허용')
    b = research_runtime.Budget(llm=3, tools=2)
    assert b.remaining_nonfinal_llm() == 2 and b.remaining_tools() == 2
    assert b.reserve_llm() and b.reserve_llm() and not b.reserve_llm()
    assert b.reserve_llm(final=True) and not b.reserve_llm(final=True)
    assert b.reserve_tool() and b.reserve_tool() and not b.reserve_tool()
    입력 = {'사업명': '사업', '품목': 'x' * 20000, '용도': 'y' * 20000,
          '질문': 'z' * 20000, '사용자확인': '확인' * 20000}
    이전 = {'항목': [{'원문': '별도 근거' * 1000}] * 4, '전체수': 4, '잘림': False}
    prompt = ontology_query_plan.계획_프롬프트(지도=지도(), 입력=입력, 요청='요청' * 1000, 이전결과=이전)
    자료 = json.loads(prompt.split('\n', 1)[1])
    assert len(prompt) <= 18000 and 자료['입력잘림'] and 자료['이전조회']['잘림']
    assert len(입력['사용자확인']) == 40000 and len(이전['항목']) == 4
    print('질의 계획 형식·최종 판정 예산 예약 통과')


def 실행():
    import psycopg
    url = 'postgresql://suddoe_app:age-test-app@127.0.0.1:5462/suddoe'
    budget = research_runtime.Budget()
    호출 = []

    def 고정모델(prompt, schema, **kwargs):
        assert budget.reserve_llm()
        자료 = json.loads(prompt.split('\n', 1)[1])
        지도 = 자료['구조지도']
        후보 = next(x['id'] for x in 지도['후보'] if x['정식명'] == 'AI API 이용료')
        호출.append({'지도형식': 지도['지도형식'], '이전조회': 자료['이전조회']})
        return ({'행동': '조회', '질의': 'MATCH (n:Norm)-[r:TARGETS]->(e:Entity) '
                 'WHERE e.id = $id RETURN DISTINCT n.id AS norm_id LIMIT 20',
                 '인자': [{'이름': 'id', '값들': [후보]}]}, {})

    입력 = {'사업명': ontology_fund._사업, '품목': 'API', '용도': '개발', '질문': 'API 이용료'}
    조회 = {'대상': {'비목_id': ontology_fund._창업활동자금_비목}, '자금': ontology_fund.창업활동자금,
          '게이트': {}, '적용': {'규범': []}, '근거행': [], '노드들': set()}
    with psycopg.connect(url, options='-c default_transaction_read_only=on', autocommit=True) as conn:
        cur = conn.cursor()
        with patch.object(ontology_query_plan, 'llm_호출', side_effect=고정모델), research_runtime.activate(budget):
            첫 = ontology_research_plan.추가_조회(cur, 조회, 입력)
            assert 첫['추가조사상태']['상태'] == '완료' and 첫['추가조사상태']['추가규범수'] > 0
            assert 첫['추가규범'] and 첫['근거행'] and budget.tools_used == 2 and budget.llm_used == 1
            재전송 = ontology_research_plan.추가_조회(cur, 조회, 입력)
            assert 재전송['추가규범'] and budget.tools_used == 2 and budget.llm_used == 1
            assert ontology_research_plan.추가조회_가능(budget)
            다음 = ontology_research_plan.추가_조회(cur, 첫, 입력, 요청='같은 품목의 근거를 더 확인')
            assert 다음['추가조사상태']['상태'] == '완료' and budget.tools_used == 3 and budget.llm_used == 2
            assert len(호출) == 2 and 호출[1]['이전조회']['전체수'] > 0
            assert not ontology_research_plan.추가조회_가능(budget)  # DB 조회 두 번 소진
        근거 = ontology_evidence.예외_근거(cur, 첫, ontology_fund._사업)
        문맥 = ontology_context.문맥_구성(cur, 근거행=첫['근거행'], 대상노드=set(),
            schema=ontology_lookup._스키마(), 자금=조회['자금'], 근거한정=True, 근거발췌=근거['발췌'])
        assert 문맥['항목수'] > 0 and not 문맥['근거초과']
        제시 = {}
        prompt, sid, _ = 프롬프트_구성(cur, 입력['질문'], {'품목': 'API'}, 문맥=문맥, 제시원문=제시)
        assert sid and 제시 and set(sid) == set(제시)
        assert any(발췌 in 글 for 발췌들 in 근거['발췌'].values() for 발췌 in 발췌들 for 글 in 제시.values())
        assert any(f'[{s}]' in prompt and 정보[0] == 'article' for s, 정보 in sid.items())
    print('지도→고정 모델→검증 AGE→규범·원문→SID, 재전송·공유 예산 통과')


if __name__ == '__main__':
    단위()
    if '--db' not in sys.argv:
        print('읽기 전용 5462 시험은 --db로 명시해 실행한다.')
    else:
        실행()
