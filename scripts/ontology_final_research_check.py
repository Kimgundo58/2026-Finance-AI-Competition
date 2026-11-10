# -*- coding: utf-8 -*-
"""판정 전 규정 보강의 예약·실패·재검증 계약을 외부 호출 없이 검사한다."""
import json
import sys
from unittest.mock import patch

import orchestrate as O
import research_runtime as R


def 상태():
    s = O._판정상태(질문='결제', 사업명='사업', org_id=None, account_id=None,
                 plan_id=None, 기록=False, dry=False, 주입=None, 참조확장_사용=True)
    s.정규화결과 = {'품목': '품목', '용도': '용도'}
    s.온톨로지 = {'근거행': [], '추가규범': []}
    s.cur = object()
    return s


def 실행(준비, 결과, 가능=True):
    s, events, 재구성 = 상태(), [], []
    with R.activate(R.Budget(), emit=events.append), \
         patch.object(O.ontology_research_plan, '추가조회_가능', return_value=가능, create=True), \
         patch.object(O.ontology_research_plan, '추가_조회', return_value=결과) as 조회, \
         patch.object(O, '_판정준비_호출', return_value=준비) as 호출, \
         patch.object(O, '_새_검색단계', side_effect=lambda s, **kw: 재구성.append(kw)), \
         patch.object(O, '_새_프롬프트단계', side_effect=lambda s: 재구성.append('프롬프트')), \
         patch.object(O, '_실패응답', return_value={'판정': '판단불가'}):
        응답 = O._새_판정준비단계(s)
    return 응답, 조회, 호출, 재구성, events


def main():
    요청 = {'행동': '추가조회', '요청': '별도 허용 근거'}
    정상 = {'추가조사상태': {'상태': '완료', '추가규범수': 1, '추가근거수': 0},
          '근거행': [{'article_id': 1, 'doc_id': 'd', '조번호': '제1조', '항호': None}], '추가규범': []}
    응답, 조회, 호출, 재구성, _ = 실행(요청, 정상)
    assert 응답 is None and 조회.call_args.kwargs == {'요청': '별도 허용 근거', '최대조회': 1}
    assert 재구성 == [{'재구성': True}, '프롬프트']
    # 조회 실패·예산 소진·결과 상한은 판단불가.
    for 결과 in ({'추가조사상태': {'상태': '실패'}},
              {'추가조사상태': {'상태': '건너뜀'}},
              {'추가조사상태': {'상태': '부분', '추가규범수': 1, '추가근거수': 1},
               '근거행': [{'article_id': 1, 'doc_id': 'd', '조번호': '제1조', '항호': None}]}):
        응답, _, _, 재구성, _ = 실행(요청, 결과)
        assert 응답['판정'] == '판단불가' and not 재구성
    # 정상 조회의 새 근거 0건은 필요한 규정이 이미 제시된 것: 기존 근거로 판정하고 문맥을 바꾸지 않는다.
    for 결과 in ({'추가조사상태': {'상태': '0건'}},
              {'추가조사상태': {'상태': '추가없음'}},
              {'추가조사상태': {'상태': '완료', '추가규범수': 0, '추가근거수': 0}},
              {'추가조사상태': {'상태': '완료'}, '근거행': [{'건너뜀': '조문 미특정'}]}):
        응답, _, _, 재구성, events = 실행(요청, 결과)
        assert 응답 is None and not 재구성, 결과
        assert [e for e in events if e.get('단계') == '규정추가조회'][-1].get('상태') == '완료', events
    응답, 조회, 호출, 재구성, events = 실행(요청, 정상, 가능=False)
    assert 응답 is None and not 조회.called and not 호출.called and not 재구성 and not events
    응답, 조회, _, 재구성, _ = 실행({'행동': '판정', '요청': ''}, 정상)
    assert 응답 is None and not 조회.called and not 재구성
    s = 상태()
    s.질문 = '\\"' * 5000
    s.제시원문 = {f'S{i:03}': '원문' * 3000 for i in range(100)}
    s.온톨로지['적용'] = {'규범': [{'종류': '의무', '대상': ['품목'], '조건': ['조건']}] * 500}
    with patch.object(O, 'llm_호출', return_value=({'행동': '판정', '요청': ''}, {})) as 모델:
        O._판정준비_호출(s, R.Budget())
        자료 = json.loads(모델.call_args.args[0].split('\n', 1)[1])
        assert len(json.dumps(자료, ensure_ascii=False)) <= 12000
        assert len(json.dumps({'규범요약': 자료['규범요약'], '제시근거': 자료['제시근거']}, ensure_ascii=False)) <= 6000
        assert 자료['입력잘림'] and 자료['규범요약잘림'] and 자료['근거색인잘림']
        assert len(자료['제시근거']) > 1 and all(len(x['내용']) <= 100 for x in 자료['제시근거'])
    s.초기조회상태 = {'상태': '부분'}
    s.온톨로지['대상'] = None
    s.프로필읽음 = True
    s.검색결과 = {'참조관계': []}
    with patch.object(O.ontology_context, '규범_요약', return_value='요약'), \
         patch.object(O, '프롬프트_구성', return_value=('원래 프롬프트', {'S01': ('l1', 1, None)}, [])):
        assert O._새_프롬프트단계(s) is None
        assert '원래 프롬프트' in s.프롬프트 and '나타나지 않은 규정이나 예외가 없다고 추론하지 않는다' in s.프롬프트
    for 상태값 in ({'상태': '실패'}, {'상태': '건너뜀'}, {'상태': '완료', '지도부분': True},
               {'상태': '완료', '입력잘림': True}, {'상태': '부분'}):
        s.초기조회상태 = 상태값
        assert '규정이나 예외가 없다고 추론하지 않는다' in O._추가조회_주의(s)
    for 상태값 in ({'상태': '완료', '지도부분': False}, {'상태': '0건'}, {'상태': '추가없음'}):
        s.초기조회상태 = 상태값
        assert not O._추가조회_주의(s), 상태값
    print('판정 전 추가 조회·실패·예산 검사 통과')


def 실제_보강():
    import psycopg
    import ontology_fund
    import ontology_query_plan
    budget = R.Budget()
    s = 상태()
    s.사업명 = ontology_fund._사업
    s.질문 = 'API 이용료'
    s.정규화결과 = {'품목': 'API', '용도': '개발'}
    s.확정비목 = s.비목 = '창업활동비'
    s.온톨로지 = {'대상': {'비목_id': ontology_fund._창업활동자금_비목},
               '자금': ontology_fund.창업활동자금, '게이트': {},
               '적용': {'규범': [], '경로': [], '우선순위_적용': []},
               '근거행': [], '노드들': set()}
    s.게이트결과 = {}
    s.프로필읽음 = True
    s.문장맵 = {'기존': ('article', -1, None)}
    s.제시원문 = {'기존': '기존 합성 근거'}
    def 준비(s, budget):
        assert budget.reserve_llm()
        return {'행동': '추가조회', '요청': 'API 이용료의 규정 확인'}
    def 계획(prompt, schema, **kwargs):
        assert budget.reserve_llm()
        지도 = json.loads(prompt.split('\n', 1)[1])['구조지도']
        후보 = next(x['id'] for x in 지도['후보'] if x['정식명'] == 'AI API 이용료')
        return ({'행동': '조회', '질의': 'MATCH (n:Norm)-[r:TARGETS]->(e:Entity) '
                 'WHERE e.id = $id RETURN DISTINCT n.id AS norm_id LIMIT 20',
                 '인자': [{'이름': 'id', '값들': [후보]}]}, {})
    dsn = 'postgresql://suddoe_app:age-test-app@127.0.0.1:5462/suddoe?connect_timeout=5'
    with psycopg.connect(dsn, options='-c default_transaction_read_only=on', autocommit=True) as conn:
        s.conn, s.cur = conn, conn.cursor()
        with R.activate(budget), patch.object(O, '_판정준비_호출', side_effect=준비), \
             patch.object(ontology_query_plan, 'llm_호출', side_effect=계획):
            응답 = O._새_판정준비단계(s)
        assert 응답 is None, 응답
        assert s.온톨로지['추가규범'] and s.문장맵 and s.제시원문
        assert '기존' not in s.문장맵 and set(s.문장맵) == set(s.제시원문)
        assert all(f'[{sid}]' in s.프롬프트 for sid in s.문장맵)
        assert budget.tools_used == 2 and budget.llm_used == 2
    print('5462 읽기 전용 최종 보강→실제 원문 재검증·문맥·신규 SID 통과')


if __name__ == '__main__':
    main()
    if '--db' in sys.argv:
        실제_보강()
