# -*- coding: utf-8 -*-
"""켜진 사업은 데이터 누락·AGE 장애에서 옛 경로와 옛 캐시로 가지 않는다."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

os.environ['SUDDOE_MOCK'] = '1'
from fastapi.testclient import TestClient
import ontology_lookup as lookup
import question_generate as 질문
import orchestrate
from server.main import app
from server import routes_judge, routes_normalize, routes_questions, research_sessions
from server.models import 정규화요청, 판정요청

사업 = '예비창업패키지'


class Cursor:
    def __init__(self, row):
        self.row = row
        self.calls = 0

    def execute(self, sql, params):
        self.calls += 1
        return self

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, cur):
        self.cur = cur
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def cursor(self):
        return self.cur


def assert_raises(fn, kind):
    try:
        fn()
    except kind:
        return
    raise AssertionError('예상한 실패가 반환되지 않음')


def 대상_검사():
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}):
        cur = Cursor(None)
        with patch.object(lookup.ontology_graph, '준비') as 준비:
            assert_raises(lambda: lookup.새경로_대상(cur, 사업), lookup.온톨로지_미준비)
            assert 준비.call_count == 1 and cur.calls == 1
        with patch.object(lookup.ontology_graph, '준비', side_effect=RuntimeError('AGE 장애')):
            assert_raises(lambda: lookup.새경로_대상(Cursor((1,)), 사업), RuntimeError)
        with patch.object(lookup.ontology_graph, '준비'):
            assert lookup.새경로_대상(Cursor((1,)), 사업) is True
        with patch.object(lookup.ontology_graph, '준비', side_effect=AssertionError('DB 불필요')):
            assert lookup.새경로_대상(Cursor(None), '다른 사업') is False
            assert lookup.새경로_대상(Cursor(None), None) is False


def 질문_검사():
    입력 = {'사업명': 사업, '비목': '여비', '품목': '출장', '용도': '업무'}
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}), \
         patch.object(질문.db, 'connect', side_effect=lambda **k: Connection(Cursor(None))), \
         patch.object(lookup.ontology_graph, '준비'), \
         patch.object(질문, '금지목록_일치', side_effect=AssertionError('옛 경로 호출')):
        r = 질문.질문생성(입력)
        assert r['상태'] == '실패' and r['메타']['사유'].startswith('준비실패:')
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}), \
         patch.object(질문.db, 'connect', side_effect=lambda **k: Connection(Cursor((1,)))), \
         patch.object(lookup.ontology_graph, '준비', side_effect=RuntimeError('AGE 장애')), \
         patch.object(질문, '금지목록_일치', side_effect=AssertionError('옛 경로 호출')):
        assert 질문.질문생성(입력)['상태'] == '실패'
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}), \
         patch.object(질문.db, 'connect', side_effect=lambda **k: Connection(Cursor((1,)))), \
         patch.object(lookup.ontology_graph, '준비'), \
         patch.object(질문, '_새경로_질문', return_value={'상태': '있음'}) as 새, \
         patch.object(질문, '금지목록_일치', side_effect=AssertionError('옛 경로 호출')):
        assert 질문.질문생성(입력)['상태'] == '있음' and 새.called
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': ''}), \
         patch.object(질문.db, 'connect', side_effect=lambda **k: Connection(Cursor(None))), \
         patch.object(질문, '금지목록_일치', return_value=True) as 옛:
        assert 질문.질문생성(입력)['상태'] == '바로판정' and 옛.called


def 판정_검사():
    for row, age_error in ((None, False), ((1,), True)):
        with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}), \
             patch.object(orchestrate.db, 'connect', side_effect=lambda **k: Connection(Cursor(row))), \
             patch.object(lookup.ontology_graph, '준비', side_effect=RuntimeError('AGE 장애') if age_error else None), \
             patch.object(orchestrate, '_실패응답', side_effect=lambda *a, **k: {'판정': '판단불가'}), \
             patch.object(orchestrate, '_룰조회', side_effect=AssertionError('옛 경로 호출')) as 옛:
            assert orchestrate.판정('출장', 사업명=사업, 기록=False)['판정'] == '판단불가'
            assert not 옛.called
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}), \
         patch.object(orchestrate.db, 'connect', side_effect=lambda **k: Connection(Cursor((1,)))), \
         patch.object(lookup.ontology_graph, '준비'), \
         patch.object(orchestrate, '_정규화단계', return_value={'경로': '온톨로지'}) as 첫단계, \
         patch.object(orchestrate, '_룰조회', side_effect=AssertionError('옛 경로 호출')):
        assert orchestrate.판정('출장', 사업명=사업, 기록=False)['경로'] == '온톨로지' and 첫단계.called
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': ''}), \
         patch.object(orchestrate.db, 'connect', side_effect=lambda **k: Connection(Cursor(None))), \
         patch.object(lookup.ontology_graph, '준비', side_effect=AssertionError('불필요한 AGE 조회')), \
         patch.object(orchestrate, '_정규화단계', return_value=None), \
         patch.object(orchestrate, '_룰조회', return_value={'경로': '옛'}) as 옛:
        assert orchestrate.판정('출장', 사업명=사업, 기록=False)['경로'] == '옛' and 옛.called


def _events(response):
    events = {}
    for block in response.text.split('\n\n'):
        name, value = None, None
        for line in block.splitlines():
            if line.startswith('event: '):
                name = line[7:]
            elif line.startswith('data: '):
                value = json.loads(line[6:])
        if name and value is not None:
            events.setdefault(name, []).append(value)
    return events


def 캐시_API_검사():
    norm = {'사업명': 사업, '품목': '출장', '금액': 1000, '용도': '업무'}
    question = {'사업명': 사업, '확정비목': '여비', '품목': '출장', '금액': 1000, '용도': '업무'}
    judge = {'사업명': 사업, '확정비목': '여비', '정규화': {'품목': '출장', '금액': 1000, '용도': '업무'}}
    for error in (lookup.온톨로지_미준비('없음'), RuntimeError('AGE 장애')):
        with TestClient(app) as client, \
             patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}), \
             patch.object(routes_normalize, 'MOCK', False), \
             patch.object(routes_questions, 'MOCK', False), \
             patch.object(routes_judge, 'MOCK', False), \
             patch.object(routes_normalize.ontology_readiness, '확인', side_effect=error), \
             patch.object(routes_normalize.response_cache, '조회', return_value={'판정': '가능'}) as 캐시, \
             patch.object(routes_normalize, '_live_정규화', side_effect=AssertionError('옛 정규화 호출')):
            결과 = _events(client.post('/api/normalize', json=norm))
            assert 결과['완료'][0]['실패'] and '오류' in 결과 and '결과' not in 결과
            캐시.reset_mock()
            결과 = client.post('/api/questions', json=question)
            assert 결과.status_code == 200 and 결과.json()['상태'] == '실패'
            assert not 캐시.called
            결과 = _events(client.post('/api/judge', json=judge))
            assert 결과['결과'][0]['판정'] == '판단불가' and 결과['완료'][0]['실패']
            assert not 캐시.called
            token = research_sessions.생성(None, None)
            session = research_sessions.조회(token, None, None)
            session.질문결과 = {'상태': '있음', '질문': []}
            session.판정결과 = ({'판정': '가능'}, None)
            sse_question = client.post('/api/questions', json={**question, '조사ID': token},
                                       headers={'Accept': 'text/event-stream'})
            assert sse_question.status_code == 503
            결과 = _events(client.post('/api/judge', json={**judge, '조사ID': token}))
            assert 결과['결과'][0]['판정'] == '판단불가' and 결과['완료'][0]['실패']
    with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}), \
         patch.object(routes_normalize, '_온톨로지_정규화', return_value=None), \
         patch.object(routes_normalize, '_사업_비목', side_effect=AssertionError('옛 정규화 호출')):
        assert_raises(lambda: routes_normalize._live_정규화(정규화요청(**norm)), RuntimeError)


def 설정_전환_캐시_검사():
    args = (None, None, 사업, '여비', None, None, None, '출장', 1000, '업무', None)
    with patch.object(routes_questions.response_cache, 'l3_해시', return_value='동일'):
        with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': ''}):
            old = routes_questions.질문세트_키(*args)
        with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}):
            new = routes_questions.질문세트_키(*args)
        assert old != new
    question = {'사업명': 사업, '확정비목': '여비', '품목': '출장', '금액': 1000, '용도': '업무'}
    judge = {'사업명': 사업, '확정비목': '여비', '정규화': {'품목': '출장', '금액': 1000, '용도': '업무'}}
    with TestClient(app) as client, patch.object(routes_judge.response_cache, '조회', return_value=None) as 조회:
        with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': ''}):
            q = _events(client.post('/api/questions', json=question, headers={'Accept': 'text/event-stream'}))['완료'][0]
            assert client.post('/api/judge', json=judge).status_code == 200
        with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}):
            assert client.post('/api/questions', json={**question, '조사ID': q['조사ID']},
                               headers={'Accept': 'text/event-stream'}).status_code == 409
            assert client.post('/api/judge', json=judge).status_code == 200
        assert 조회.call_args_list[0].args[0] != 조회.call_args_list[1].args[0]


def 질문세트_경로_검사():
    body = 판정요청(사업명=사업, 확정비목='여비', 질문세트='이전키',
                정규화={'품목': '출장', '금액': 1000, '용도': '업무'})
    old = {'상태': '있음', '질문': [], '노드결정': {'id': '이전노드'}, '_온톨로지경로': False}
    with patch.object(routes_judge.response_cache, '질문세트_조회', return_value=old), \
         patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}):
        assert routes_judge._사용자답변_읽기(body, None) == (None, True, None)
        assert routes_judge._질문저장값_읽기(body, None) is None
        assert routes_judge._조건답_읽기(body, None) is None
    # 이전 저장 형식은 기존 경로에서 그대로 허용한다.
    for stored in (old, {k: v for k, v in old.items() if k != '_온톨로지경로'}):
        with patch.object(routes_judge.response_cache, '질문세트_조회', return_value=stored), \
             patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': ''}):
            assert routes_judge._질문저장값_읽기(body, None) == stored
            assert routes_judge._사용자답변_읽기(body, None)[1] is False
    with patch.object(routes_judge.response_cache, '질문세트_조회', return_value={**old, '_온톨로지경로': True}), \
         patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}):
        assert routes_judge._질문저장값_읽기(body, None)['_온톨로지경로'] is True


if __name__ == '__main__':
    대상_검사()
    질문_검사()
    판정_검사()
    캐시_API_검사()
    설정_전환_캐시_검사()
    질문세트_경로_검사()
    print('설정 사업 데이터 누락·AGE 장애·캐시·정상/미설정 경로 검사 통과')
    import sys
    if '--db' in sys.argv:
        import psycopg
        with psycopg.connect('postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5',
                             options='-c default_transaction_read_only=on') as conn, conn.cursor() as cur:
            with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': 사업}):
                assert lookup.새경로_대상(cur, 사업) is True
            with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': '존재하지않는사업'}):
                assert_raises(lambda: lookup.새경로_대상(cur, '존재하지않는사업'), lookup.온톨로지_미준비)
                assert lookup.새경로_대상(cur, 사업) is False
        print('5462 읽기 전용 AGE의 정상·데이터 0·미설정 경로 검사 통과')
