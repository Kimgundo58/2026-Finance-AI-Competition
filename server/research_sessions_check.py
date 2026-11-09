# -*- coding: utf-8 -*-
"""DB·외부 호출 없이 조사 세션과 실제 HTTP/SSE 경로를 확인한다."""
import json
import os
import sys
import types
from unittest.mock import patch

os.environ['SUDDOE_MOCK'] = '1'
from fastapi.testclient import TestClient
from server.main import app
from server import research_sessions


def sse(response):
    result = {}
    for block in response.text.split('\n\n'):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith('event: '):
                event = line[7:]
            elif line.startswith('data: '):
                data = json.loads(line[6:])
        if event and data is not None:
            result.setdefault(event, []).append(data)
    return result


def main():
    candidate = {'id': 'a', '유형': '공개정보', '내용': '공개 정보',
                 '출처': [{'제목': '안내', 'url': 'https://example.com', '발췌': '공개 정보'}], '상태': '확인필요'}
    calls = []
    def research(product_name, *, public_url, budget, emit, requirements=None, context=None):
        calls.append(product_name)
        if emit:
            emit({'id': 'e', '단계': '판매처', '상태': '진행중', '문장': '확인 중'})
        return {'상태': '확인필요', '후보': [candidate], '출처': ['https://example.com']}
    original = sys.modules.get('web_research')
    sys.modules['web_research'] = types.SimpleNamespace(research=research)
    try:
        with TestClient(app) as client:
            norm = {'품목': 'API', '금액': 100, '용도': '개발', '사업명': '예비창업패키지'}
            n = sse(client.post('/api/normalize', json=norm))['결과'][0]
            token = n['조사ID']
            request = {'조사ID': token, '사업명': '예비창업패키지', '확정비목': '여비',
                       '품목': 'API', '금액': 100, '용도': '개발', '웹조사': True, '공개검색어': 'API'}
            response = client.post('/api/questions', json=request, headers={'Accept': 'text/event-stream'})
            events = sse(response)
            assert response.status_code == 200 and events['진행'][0]['단계'] == '판매처'
            first = events['완료'][0]
            assert first['상태'] == '조사확인' and first['질문'] == [] and first['질문세트'] is None
            assert first['조사']['후보'][0]['id'] == 'a' and first['조사ID'] == token
            confirmed = {**request, '조사답변': [{'id': 'a', '답': '맞아요'}]}
            q = sse(client.post('/api/questions', json=confirmed,
                                headers={'Accept': 'text/event-stream'}))['완료'][0]
            assert q['상태'] == '있음' and q['질문세트']
            repeat = client.post('/api/questions', json=confirmed).json()
            assert repeat == q
            judge = {'조사ID': token, '사업명': '예비창업패키지', '확정비목': '여비',
                     '정규화': {'품목': 'API', '금액': 100, '용도': '개발'}, '질문세트': q['질문세트'],
                     '조사답변': [{'id': 'a', '답': '맞아요'}]}
            answer = client.post('/api/judge', json=judge)
            assert answer.status_code == 200 and sse(answer)['결과'][0]['조사']['확인한정보'][0]['내용'] == '공개 정보'
            again = sse(client.post('/api/judge', json=judge))
            assert again['진행'][0]['단계'] == '판정'
            changed = {**judge, '조사답변': [{'id': 'a', '답': '아니에요'}]}
            assert client.post('/api/judge', json=changed).status_code == 409
            newq = client.post('/api/questions', json={**request, '조사답변': changed['조사답변']}).json()
            assert calls == ['API']
            changed['질문세트'] = newq['질문세트']
            revised = sse(client.post('/api/judge', json=changed))['결과'][0]['조사']
            assert not revised['확인한정보'] and revised['미확인'][0]['답'] == '아니에요'
            forged = {**judge, '조사답변': [{'id': 'wrong', '답': '맞아요'}]}
            assert client.post('/api/judge', json=forged).status_code == 400
            assert client.post('/api/judge', json={**judge, '질문세트': 'wrong'}).status_code == 400
            assert client.post('/api/judge', json={**judge, '정규화': {'품목': '다른 품목'}}).status_code == 400
            assert client.post('/api/questions', json={**request, '조사ID': token + 'x'}).status_code == 400
            assert research_sessions.조회(token, 'other', None) is None
            token_empty = sse(client.post('/api/normalize', json=norm))['결과'][0]['조사ID']
            empty_req = {**request, '조사ID': token_empty, '공개검색어': None}
            empty_first = client.post('/api/questions', json=empty_req).json()
            assert empty_first['상태'] == '조사확인' and not empty_first['조사']['후보']
            empty_final = client.post('/api/questions', json={**empty_req, '조사답변': []}).json()
            assert empty_final['질문세트'] and calls == ['API']
            session = research_sessions.조회(token, None, None)
            session.expires = 0
            assert client.post('/api/questions', json=request).status_code == 400
        # 응답 객체만 만들고 스트림을 전혀 읽지 않는 요청도 세션 잠금을 남기지 않는다.
        from server import routes_judge, routes_questions
        from server.models import 질문요청, 판정요청
        fake_req = types.SimpleNamespace(headers={'accept': 'text/event-stream'}, client=None)
        token2 = research_sessions.생성(None, None)
        s = research_sessions.조회(token2, None, None)
        with patch.object(routes_questions.auth, '검증된_org', return_value=None), \
             patch.object(routes_questions.auth, '검증된_account', return_value=None), \
             patch.object(routes_questions, '클라이언트_ip', return_value='127.0.0.1'), \
             patch.object(routes_judge.제한, '통과', return_value=(True, None)):
            qb = 질문요청(조사ID=token2, 사업명='예비창업패키지', 확정비목='여비', 품목='API', 금액=100, 용도='개발')
            routes_questions.questions(fake_req, qb)
            assert not s.lock.locked()
            s.입력 = research_sessions.입력키('예비창업패키지', '여비', None, 'API', 100, '개발')
            s.버전 = json.dumps(['', '', False], ensure_ascii=False)
            s.질문세트, s.질문결과 = None, {'상태': '없음'}
            s.확인답키 = research_sessions.답변키(research_sessions.후보_답변(s, []))
            jb = 판정요청(조사ID=token2, 사업명='예비창업패키지', 확정비목='여비',
                        정규화={'품목': 'API', '금액': 100, '용도': '개발'})
            routes_judge.judge(fake_req, jb, mock=None, 목=None)
            assert not s.lock.locked()
    finally:
        if original is None:
            del sys.modules['web_research']
        else:
            sys.modules['web_research'] = original
    print('조사 세션 HTTP/SSE 검사 통과')


if __name__ == '__main__':
    main()
