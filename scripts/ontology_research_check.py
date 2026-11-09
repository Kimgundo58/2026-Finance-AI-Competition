# -*- coding: utf-8 -*-
"""추가 조사 예산과 범위, 원문 검증의 실패 동작을 독립적으로 확인한다."""
from datetime import date
from unittest.mock import patch

import ontology_research as 연구


class Budget:
    def __init__(self, evidence=True):
        self.calls = 0
        self.evidence = evidence

    def reserve_tool(self):
        self.calls += 1
        return self.calls <= 6

    def remaining_seconds(self):
        return 10

    def add_evidence(self, text):
        return self.evidence


class Cursor:
    def __init__(self, status='active', text='원문 발췌 내용', 시행일=date(2026, 1, 1)):
        self.status, self.text, self.시행일 = status, text, 시행일

    def execute(self, sql, params):
        self.sql, self.params = sql, params
        return self

    def fetchall(self):
        if 'age_norms' in self.sql:
            values = {'norm_id': 'n', '종류': '허용', '대상': ['item'], '예외': [], 'verified': False}
            return [tuple(values.get(k) for k in 연구.ontology_lookup._규범_컬럼)]
        raise AssertionError(self.sql)

    def fetchone(self):
        assert self.params == ('doc', '제1조')
        return (1, self.text, self.status, self.시행일)


def 실행():
    def 근거(cur, norms):
        norms[0]['근거'] = [{'doc_id': 'doc', '조번호': '제1조', '항호': None, '발췌': '원문 발췌'}]

    def run(cur=None, ids=None, budget=None, version='v', calls=0):
        state = {'budget': budget or Budget(), '호출수': calls, '버전': version, '기준일': date(2026, 11, 9)}
        result = 연구.후보_조회(cur or Cursor(), 사업='사업', 비목_id='비목', 후보_ids=ids or ['item'], 자금=None, 상태=state)
        return result, state

    with patch.object(연구.ontology_age, '준비_버전', return_value='v'), \
         patch.object(연구, '후보_목록', return_value=[{'id': 'item'}]), \
         patch.object(연구.ontology_lookup, '_근거_붙이기', side_effect=근거), \
         patch.object(연구.ontology_lookup, '_스키마', return_value={'문서키': {'키': 'doc'}}), \
         patch.object(연구.ontology_lookup, '_다른사업_문서', return_value=[]):
        good, state = run()
        assert good['완료'] and good['규범'][0]['verified'] is False and good['근거행'][0]['article_id'] == 1
        assert state['호출수'] == 1
        assert not run(ids=['forged'])[0]['완료']
        assert not run(cur=Cursor(status='inactive'))[0]['완료']
        assert not run(cur=Cursor(text='다른 본문'))[0]['완료']
        assert not run(cur=Cursor(시행일=date(2027, 1, 1)))[0]['완료']
        assert not run(cur=Cursor(시행일=None))[0]['완료']
        assert not run(budget=Budget(False))[0]['완료']
        assert not run(version='old')[0]['완료']
        result, state = run(calls=2)
        assert not result['완료'] and state['budget'].calls == 0
    print('ontology_research_check 통과')


if __name__ == '__main__':
    실행()
    import sys
    if '--db' in sys.argv:
        import psycopg
        import ontology_fund
        with psycopg.connect('postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5',
                             options='-c default_transaction_read_only=on') as conn:
            with conn.cursor() as cur:
                버전 = 연구.ontology_age.준비_버전(cur)
                목록 = 연구.후보_목록(cur, 사업=ontology_fund._사업,
                              비목_id=ontology_fund._창업활동자금_비목, 자금=ontology_fund.창업활동자금)
                assert 버전 and 목록
                print('읽기 전용 AGE 후보 조회 통과:', len(목록))
                print('API 후보:', [x['정식명'] for x in 목록 if 'API' in str(x)])
                선택 = [x['id'] for x in 목록 if 'API' in x['정식명']]
                결과 = 연구.후보_조회(cur, 사업=ontology_fund._사업,
                    비목_id=ontology_fund._창업활동자금_비목, 후보_ids=선택,
                    자금=ontology_fund.창업활동자금,
                    상태={'budget': Budget(), '버전': 버전, '기준일': date(2026, 11, 9)})
                print('API 근거 검증:', 결과['완료'], 결과['사유'])
                for 선택id in 선택:
                    결과 = 연구.후보_조회(cur, 사업=ontology_fund._사업,
                        비목_id=ontology_fund._창업활동자금_비목, 후보_ids=[선택id],
                        자금=ontology_fund.창업활동자금,
                        상태={'budget': Budget(), '버전': 버전, '기준일': date(2026, 11, 9)})
                    print('개별 근거 검증:', 결과['완료'], 결과['사유'])
