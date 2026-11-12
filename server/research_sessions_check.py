# -*- coding: utf-8 -*-
"""DB·외부 호출 없이 조사 세션과 실제 HTTP/SSE 경로를 확인한다."""
import json
import os
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
    # MOCK 의 ② 품목 조사는 web_research 대신 mock_flow.품목조사 를 부른다(스펙 8-5). 그 자리를 가짜로 바꿔 세션 경로만 본다
    import research_runtime
    from server import mock_flow
    def research(품목, 거래처):
        calls.append(품목)
        research_runtime.emit('판매처', '확인 중', id='e')
        return {'상태': '확인필요', '후보': [candidate], '출처': ['https://example.com']}
    original = mock_flow.품목조사
    mock_flow.품목조사 = research
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
            changed = {**judge, '조사답변': [{'id': 'a', '답': '직접입력', '정정': '사용자 설명'}]}
            assert client.post('/api/judge', json=changed).status_code == 409
            newq = client.post('/api/questions', json={**request, '조사답변': changed['조사답변']}).json()
            assert calls == ['API']
            changed['질문세트'] = newq['질문세트']
            revised = sse(client.post('/api/judge', json=changed))['결과'][0]['조사']
            assert revised['확인한정보'][0]['답'] == '직접입력' and revised['확인한정보'][0]['정정'] == '사용자 설명'
            forged = {**judge, '조사답변': [{'id': 'wrong', '답': '맞아요'}]}
            assert client.post('/api/judge', json=forged).status_code == 400
            assert client.post('/api/judge', json={**judge, '질문세트': 'wrong'}).status_code == 400
            assert client.post('/api/judge', json={**judge, '정규화': {'품목': '다른 품목'}}).status_code == 400
            assert client.post('/api/questions', json={**request, '조사ID': token + 'x'}).status_code == 400
            assert research_sessions.조회(token, 'other', None) is None
            token_empty = sse(client.post('/api/normalize', json=norm))['결과'][0]['조사ID']
            # MOCK 은 공개 검색어가 없어도 품목 조사를 한다(새 흐름에는 검색어 칸이 없다, 스펙 8-2 · 8-5)
            empty_req = {**request, '조사ID': token_empty, '공개검색어': None}
            empty_first = client.post('/api/questions', json=empty_req).json()
            assert empty_first['상태'] == '조사확인' and empty_first['조사']['후보'][0]['id'] == 'a'
            empty_final = client.post('/api/questions', json={**empty_req, '조사답변': [{'id': 'a', '답': '맞아요'}]}).json()
            assert empty_final['질문세트'] and calls == ['API', 'API']
            session = research_sessions.조회(token, None, None)
            session.expires = 0
            assert client.post('/api/questions', json=request).status_code == 400
            분류실패_검사(client, norm)
            판정조사_검사(client, norm)
            실제_품목조사_검사(client)
            판정조사_항상_검사(client, norm)
            설명정리_검사(client, norm)
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
        mock_flow.품목조사 = original
    print('조사 세션 HTTP/SSE 검사 통과')


def 판정조사_검사(client, norm):
    """④-2 판정용 조사 결과는 세션에만 남고(질문 응답 · 판정 응답에는 없다) 판정의 조사 값(답변_검증)으로 넘어간다. 답키는 바뀌지 않는다."""
    from server import routes_questions
    token = sse(client.post('/api/normalize', json=norm))['결과'][0]['조사ID']
    request = {'조사ID': token, '사업명': '예비창업패키지', '확정비목': '여비', '품목': 'API', '금액': 100, '용도': '개발',
               '웹조사': True, '공개검색어': 'API'}
    client.post('/api/questions', json=request)
    결과 = {'상태': '일부', '요건결과': {'k1': {'번호': 'C1', '결과': '확인됨', '값': '원화', '발췌': '원화 결제', 'URL': 'https://example.com', '이유': ''}},
          '출처': ['https://example.com']}
    받은 = {}
    def 가짜(req, body, 조사확인=None, 답키=None, 준비확인=False, 노드결정=None, 판정조사설정=None):
        받은['설정'] = 판정조사설정
        return {'상태': '없음', '질문세트': None, '질문': [], '추가입력': [], '용어풀이': [], '_판정조사': 결과}
    with patch.object(routes_questions, '_기존_질문', 가짜):
        q = client.post('/api/questions', json={**request, '조사답변': [{'id': 'a', '답': '맞아요'}]}).json()
    s = research_sessions.조회(token, None, None)
    assert '_판정조사' not in q and '판정조사' not in json.dumps(q, ensure_ascii=False), q
    assert s.판정조사 == 결과 and 받은['설정'] is None      # MOCK 은 판정용 조사를 하지 않는다(설정 없음)
    조사 = research_sessions.답변_검증(s, [{'id': 'a', '답': '맞아요'}], s.질문세트, s.입력, s.버전)
    assert 조사['판정조사'] == 결과 and research_sessions.답변키({k: v for k, v in 조사.items() if k != '판정조사'}) == s.확인답키
    assert '판정조사' not in research_sessions.화면_조사(조사)
    judge = {'조사ID': token, '사업명': '예비창업패키지', '확정비목': '여비',
             '정규화': {'품목': 'API', '금액': 100, '용도': '개발'}, '질문세트': s.질문세트, '조사답변': [{'id': 'a', '답': '맞아요'}]}
    answer = client.post('/api/judge', json=judge)
    assert answer.status_code == 200 and '판정조사' not in answer.text, answer.text[:300]


def 실제_품목조사_검사(client):
    """실제(비 MOCK) 경로의 ② 품목 조사 호출부(routes_questions 의 web_research.research)를 합성으로 지난다.

    web_research 를 가짜 모듈로 바꿔 불렸는지 · 받은 인자만 본다. 외부 호출 · DB 없음. v046: 공개 검색어는 넘기지 않고(None),
    검색어 · 주소가 둘 다 없어도 지출 항목 · 사용 목적으로 조사한다.
    """
    import sys
    from server import routes_questions
    불림 = []
    def research(공개검색어, **k):
        불림.append((공개검색어, k))
        return {'상태': '확인필요', '후보': [{'id': 'w', '유형': '제품성격', '내용': '합성', '출처': [], '상태': '확인필요'}], '출처': []}
    가짜 = types.ModuleType('web_research')
    가짜.research = research
    본문 = {'사업명': '예비창업패키지', '품목': 'API', '금액': 100, '용도': '개발', '웹조사': True,
          '공개검색어': 'API 크레딧', '판매URL': 'https://example.com/p', '거래처': '판매사'}
    with patch.dict(sys.modules, {'web_research': 가짜}), patch.object(routes_questions, 'MOCK', False),          patch.object(routes_questions.ontology_readiness, '확인', lambda *a: None),          patch.object(routes_questions.response_cache, '요청_해시', lambda: 'v-합성'),          patch.object(routes_questions.제한, '통과', lambda ip: (True, None)),          patch.object(routes_questions.mock_flow, '품목조사', lambda *a: (_ for _ in ()).throw(AssertionError('mock 경로'))):
        r = sse(client.post('/api/questions', json=본문, headers={'Accept': 'text/event-stream'}))['완료'][0]
        assert r['상태'] == '조사확인' and r['조사']['후보'][0]['id'] == 'w', r
        assert len(불림) == 1 and 불림[0][0] is None, 불림
        k = 불림[0][1]
        assert k['public_url'] == 'https://example.com/p' and k['vendor'] == '판매사' and k['budget'] is not None, k
        assert k['context'] == {'품목': 'API', '용도': '개발', '금액': 100} and k['requirements'] == [], k
        빈 = sse(client.post('/api/questions', json={**본문, '공개검색어': None, '판매URL': None},
                            headers={'Accept': 'text/event-stream'}))['완료'][0]
        assert 빈['상태'] == '조사확인' and len(불림) == 2 and 불림[1][0] is None, (빈, 불림)


def 판정조사_항상_검사(client, norm):
    """v046: ④-2 판정용 조사는 웹조사 값과 상관없이 돈다 — 별칭 일치(웹조사 false · 비목 없음 → 서버 분류)와 재점검(확정비목 ·
    웹조사 false)에서도 질문 생성에 설정 {판매URL, 거래처} 가 넘어가고, 공개 검색어는 넘기지 않는다. live 경로를 MOCK 끄고
    DB · LLM 자리를 가짜로 바꿔 본다. 조사 확인 없이 온 요청의 질문세트 키에는 거래처 · 판매URL 이 들어간다."""
    from server import routes_questions
    import question_generate
    받은 = []

    def 가짜질문(req, body, 조사확인=None, 답키=None, 준비확인=False, 노드결정=None, 판정조사설정=None):
        받은.append(판정조사설정)
        return {'상태': '없음', '질문세트': None, '질문': [], '추가입력': [], '용어풀이': []}
    분류 = {'비목': '지급수수료', '세목': '소프트웨어 구독료', '표시문': '지급수수료 > 소프트웨어 구독료', '품목': '디자인 소프트웨어'}
    공통 = {'사업명': '예비창업패키지', '품목': '피그마', '금액': 100, '용도': '디자인', '웹조사': False,
          '거래처': '피그마코리아', '판매URL': 'https://example.com/figma', '공개검색어': '피그마 무시'}
    설정 = {'판매URL': 'https://example.com/figma', '거래처': '피그마코리아'}
    with patch.object(routes_questions, 'MOCK', False), patch.object(routes_questions, '_기존_질문', 가짜질문), \
         patch.object(routes_questions, '_분류', lambda body, 조사확인, *a, **k: {'분류': 분류, '노드결정': None, '안내': '안내'}), \
         patch.object(routes_questions.ontology_readiness, '확인', lambda *a: None), \
         patch.object(routes_questions.response_cache, '요청_해시', lambda: 'v1'):
        for 이름, 추가 in (('별칭 일치', {}), ('재점검', {'확정비목': '여비', '확정세목': None})):
            token = sse(client.post('/api/normalize', json=norm))['결과'][0]['조사ID']
            q = client.post('/api/questions', json={**공통, **추가, '조사ID': token}).json()
            assert q['상태'] == '없음' and 받은[-1] == 설정, (이름, q, 받은)
            assert ('분류' in q) is (이름 == '별칭 일치'), (이름, q)
        # 효과: 설정 함수가 None 이면(옛 동작: 공개 조사를 켠 흐름에서만) 같은 요청에 설정이 넘어가지 않는다
        with patch.object(routes_questions, '_판정조사_설정', lambda body: None):
            token = sse(client.post('/api/normalize', json=norm))['결과'][0]['조사ID']
            client.post('/api/questions', json={**공통, '조사ID': token})
            assert 받은[-1] is None, 받은
    assert routes_questions._판정조사_설정(types.SimpleNamespace(판매URL=None, 거래처=None)) is None   # MOCK 은 고정 진행 줄만

    # _live_질문: 조사 확인 없이 온 요청에도 판정조사설정을 질문 생성에 넘기고, 키에 거래처 · 판매URL 을 더한다
    from server.models import 질문요청
    body = 질문요청(사업명='예비창업패키지', 확정비목='여비', 품목='API', 금액=100, 용도='개발')
    본키 = routes_questions.질문세트_키(None, None, body.사업명, body.확정비목, None, None, None, body.품목, body.금액, body.용도, None)
    키들, 입력들 = [], []

    def 조회(k, org, account):
        키들.append(k)
        return None

    def 생성(입력, **kw):
        입력들.append(입력)
        return {'상태': '없음', '질문': [], '메타': {}}
    with patch.object(routes_questions.response_cache, '질문세트_조회', 조회), \
         patch.object(routes_questions.response_cache, '저장', lambda *a, **k: True), \
         patch.object(routes_questions.제한, '통과', lambda ip: (True, None)), \
         patch.object(question_generate, '질문생성', 생성):
        for 설정값 in ({'판매URL': None, '거래처': 'A'}, {'판매URL': None, '거래처': 'B'}, None):
            routes_questions._live_질문(body, None, None, '127.0.0.1', 0.0, None, None, None, 설정값)
    assert 키들[2] == 본키 and len({*키들}) == 3, 키들
    assert 입력들[0]['_판정조사설정'] == {'판매URL': None, '거래처': 'A'} and '_판정조사설정' not in 입력들[2], 입력들


def 분류실패_검사(client, norm):
    """③ 분류가 장애(DB · LLM)로 끝나면 화면 안내는 「확인하지 못했어요 · 다시 시도」이고, 같은 답으로 다시 부르면 저장된 결과를 내지 않고
    분류를 다시 한다. 맞는 분류가 없다는 결과는 예전처럼 안내문(None)이고 저장해 다시 쓴다(2026-11-12 실행기록 20261112_225525)."""
    from server import routes_questions
    import item_classify
    token = sse(client.post('/api/normalize', json=norm))['결과'][0]['조사ID']
    req = {'조사ID': token, '사업명': '예비창업패키지', '품목': 'API', '금액': 100, '용도': '개발',
           '웹조사': True, '공개검색어': 'API'}
    client.post('/api/questions', json=req)
    확인 = {**req, '조사답변': [{'id': 'a', '답': '맞아요'}]}
    결과들 = [{'분류': None, '노드결정': None, '안내': routes_questions._분류실패_안내, '실패': True},
            {'분류': None, '노드결정': None, '안내': item_classify.안내문(None)}]
    불림 = []
    def 가짜분류(body, 조사확인, 조사ID=None, 재조사함=False, **k):
        불림.append(1)
        return 결과들[min(len(불림), 2) - 1]
    with patch.object(routes_questions, '_분류', 가짜분류):
        첫 = client.post('/api/questions', json=확인).json()
        assert 첫['상태'] == '분류없음' and 첫['분류안내'] == routes_questions._분류실패_안내 and len(불림) == 1, 첫
        assert set(첫) >= {'상태', '질문세트', '질문', '추가입력', '용어풀이', '조사ID', '조사', '분류', '분류안내'}, 첫
        둘 = client.post('/api/questions', json=확인).json()      # 장애 결과는 다시 쓰지 않고 분류를 다시 한다
        assert len(불림) == 2 and 둘['분류안내'] == item_classify.안내문(None), 둘
        셋 = client.post('/api/questions', json=확인).json()      # 맞는 분류가 없다는 결과는 저장해 다시 쓴다
        assert len(불림) == 2 and 셋 == 둘, 셋
    assert routes_questions._분류실패_안내 != item_classify.안내문(None)
    # _분류 자체: DB 예외 · 분류 LLM 실패는 실패=True, 맞는 분류 없음은 실패 없이 안내문(None)
    body = types.SimpleNamespace(사업명='예비창업패키지', 품목='API', 용도='개발', 금액=100, 거래처=None, 품목설명=None)
    from _lib import db
    class 연결:
        def __enter__(self):
            return types.SimpleNamespace(cursor=lambda: None)
        def __exit__(self, *a):
            return False
    def 끊김(**k):
        raise ConnectionError('synthetic')
    import ontology_lookup
    with patch.object(routes_questions, 'MOCK', False):
        with patch.object(db, 'connect', 끊김):
            r = routes_questions._분류(body, None)
            assert r['실패'] and r['분류'] is None and r['안내'] == routes_questions._분류실패_안내, r
        with patch.object(db, 'connect', lambda **k: 연결()), patch.object(ontology_lookup, '레이블_목록', lambda *a: []):
            for 디버그, 실패 in (({'LLM실패': 'LLM실패'}, True), ({}, False)):
                with patch.object(item_classify, '분류', lambda *a, **k: {'분류': None, '노드결정': None, '방법': '없음', '디버그': 디버그}):
                    r = routes_questions._분류(body, None)
                assert bool(r.get('실패')) is 실패 and r['분류'] is None, r
                assert r['안내'] == (routes_questions._분류실패_안내 if 실패 else item_classify.안내문(None)), r


def 설명정리_검사(client, norm):
    """5-2 · 8-4 (v047): 사용자가 쓴 한 문장(직접입력 정정 · 품목설명)을 LLM 1회로 {품목, 성격, 설명, 용도} 로 바꿔 분류 LLM 의
    <확인한정보> 자료로 넘긴다. ① 성공 → 분류 프롬프트에 JSON 칸 ② 실패 · 형식 오류 → 글자 그대로 ③ 질문 · 판정 문맥의 사용자 진술은 원문.
    같은 문장은 세션 캐시로 다시 바꾸지 않는다. MOCK · 별칭 일치는 바꾸지 않는다. 가짜 LLM · 가짜 온톨로지 조회만 쓴다."""
    import functools
    import item_classify
    import ontology_lookup
    import ontology_research_plan
    from llm_qwen import LLM실패
    from server import routes_questions
    from _lib import db
    정정 = '시제품 외형을 만드는 3D 프린터용 필라멘트를 삽니다'
    설명 = '센서 부품을 사서 시제품에 넣어요'
    조사확인 = {'상태': '확인필요', '확인한정보': [
        {'id': 'a', '유형': '제품성격', '내용': '공개 발췌', '확인문장': '공개 확인문장', '답': '맞아요'},
        {'id': 'b', '유형': '제품성격', '내용': '다른 발췌', '답': '직접입력', '정정': 정정}],
        '미확인': [], '업체이용조건': [], '출처': [], '품목설명': 설명}
    원본 = json.loads(json.dumps(조사확인, ensure_ascii=False))
    변환표 = {정정: {'품목': '3D 프린터용 필라멘트', '성격': '구입', '설명': '3D 프린터용 필라멘트', '용도': '시제품 외형 제작'},
           설명: {'품목': '센서 부품', '성격': '구입', '설명': '시제품에 넣는 센서 부품', '용도': ''}}
    기록 = {'변환': [], '분류': []}
    상태 = {'모드': '성공'}
    def 가짜llm(prompt, schema, **k):
        if schema is item_classify._설명_스키마:
            문장 = next(x for x in 변환표 if x in prompt)
            기록['변환'].append(문장)
            if 상태['모드'] == '실패':
                raise LLM실패('synthetic')
            if 상태['모드'] == '형식오류':
                return {**변환표[문장], '성격': '대여'}, {}
            return dict(변환표[문장]), {}
        기록['분류'].append(prompt)
        return {'성격': '구입', '성격이같은후보': ['N1'], '번호': 'N1', '확신': '유사', '이유': 'x'}, {'지연ms': 1}
    후보 = [{'id': 'n1', '정식명': '시제품 재료', '별칭': [], '비목': ['재료비']}]
    경로 = {'비목': '재료비', '세목': None, '표시문': '재료비', '다른경로': []}
    class 연결:
        def __enter__(self):
            return types.SimpleNamespace(cursor=lambda: None)
        def __exit__(self, *a):
            return False
    body = types.SimpleNamespace(사업명='예비창업패키지', 품목='필라멘트', 용도='시제품', 금액=100, 거래처=None, 품목설명=None)
    원래분류 = item_classify.분류
    def 분류자료():
        return 기록['분류'][-1].split('<확인한정보>\n')[1].split('</확인한정보>')[0]
    with (patch.object(routes_questions, 'MOCK', False), patch.object(db, 'connect', lambda **k: 연결()),
          patch.object(ontology_lookup, '레이블_목록', lambda *a: []),
          patch.object(ontology_lookup, '후보_정리', lambda *a: (후보, [])),
          patch.object(ontology_lookup, '노드_결정', lambda *a: []),
          patch.object(ontology_lookup, '분류_경로', lambda *a: 경로),
          patch.object(item_classify, '분류', functools.partial(원래분류, llm=가짜llm))):
        # ① 성공: 분류 자료에 JSON 칸, 사용자 문장 원문은 없다. [맞아요] 한 공개 확인문장은 그대로
        캐시 = {}
        r = routes_questions._분류(body, 조사확인, 설명캐시=캐시)
        자료 = 분류자료()
        assert r['분류'] and r['분류']['비목'] == '재료비' and len(기록['변환']) == 2, (r, 기록['변환'])
        assert '사용자 설명(정리): {"품목": "3D 프린터용 필라멘트", "성격": "구입"' in 자료 and 정정 not in 자료, 자료
        assert '"설명": "시제품에 넣는 센서 부품"' in 자료 and 설명 not in 자료 and '"용도"' not in 자료.split('센서 부품", ')[-1], 자료
        assert '공개 확인문장' in 자료 and set(캐시) == {정정, 설명}, 캐시
        # 같은 문장은 세션 캐시로 다시 바꾸지 않는다(변환 LLM 0회, 분류 LLM 은 다시 돈다)
        routes_questions._분류(body, 조사확인, 설명캐시=캐시)
        assert len(기록['변환']) == 2 and len(기록['분류']) == 2 and 분류자료() == 자료
        # ② 실패 · 형식 오류: 문장 글자 그대로, 캐시에 남기지 않는다. 분류는 계속한다(분류 실패가 아니다)
        for 모드 in ('실패', '형식오류'):
            상태['모드'], 캐시 = 모드, {}
            r = routes_questions._분류(body, 조사확인, 설명캐시=캐시)
            자료 = 분류자료()
            assert r['분류'] and not r.get('실패') and 정정 in 자료 and 설명 in 자료 and '사용자 설명(정리)' not in 자료, (모드, 자료)
            assert 캐시 == {}, 캐시
        상태['모드'] = '성공'
        # 별칭 일치면 분류 LLM 도 변환 LLM 도 부르지 않는다
        n = len(기록['변환'])
        with patch.object(ontology_lookup, '노드_결정', lambda *a: [{'concept_id': 'n1'}]):
            r = routes_questions._분류(body, 조사확인, 설명캐시={})
        assert r['분류'] and len(기록['변환']) == n, 기록['변환']
    # ③ 질문 · 판정 문맥(확인정보_문맥)은 사용자가 쓴 문장 그대로이고 조사확인 자체는 바뀌지 않았다(8-4)
    assert 조사확인 == 원본
    문맥 = ontology_research_plan.확인정보_문맥(조사확인)
    assert 정정 in 문맥 and 설명 in 문맥 and '사용자 설명(정리)' not in 문맥 and '"성격"' not in 문맥, 문맥
    # MOCK: 외부 호출 없이 mock 분류(변환 LLM 0회)
    n = len(기록['변환'])
    with patch.object(item_classify, '분류', functools.partial(원래분류, llm=가짜llm)):
        캐시 = {}
        routes_questions._분류(body, 조사확인, 설명캐시=캐시)
    assert len(기록['변환']) == n and 캐시 == {}
    # 화면 경로: /api/questions 가 조사 세션의 설명정리 를 분류에 넘긴다(같은 세션 두 요청이 같은 캐시를 쓴다)
    token = sse(client.post('/api/normalize', json=norm))['결과'][0]['조사ID']
    req = {'조사ID': token, '사업명': '예비창업패키지', '품목': 'API', '금액': 100, '용도': '개발', '웹조사': True}
    client.post('/api/questions', json=req)
    받은 = []
    def 가짜분류(body, 조사확인, 조사ID=None, 재조사함=False, 설명캐시=None):
        받은.append(설명캐시)
        return {'분류': None, '노드결정': None, '안내': routes_questions._분류실패_안내, '실패': True}
    with patch.object(routes_questions, '_분류', 가짜분류):
        for 답 in ('맞아요', '직접입력'):
            client.post('/api/questions', json={**req, '조사답변': [{'id': 'a', '답': 답, **({'정정': 정정} if 답 == '직접입력' else {})}]})
    세션 = research_sessions.조회(token, None, None)
    assert len(받은) == 2 and all(x is 세션.설명정리 for x in 받은), 받은


if __name__ == '__main__':
    main()
