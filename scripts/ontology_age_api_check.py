#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""버리는 DB에서 실제 /api/judge·AGE·캐시 연결을 점검한다.

실제 LLM과 검색은 호출하지 않고 고정 출력을 주입한다. 판정 기록은 끄고,
게스트 메모리 캐시만 사용하며 DB에 쓰지 않는다. 장애는 커서 결과로 주입한다.
실제 모델 정확도나 검색 품질에 대한 시험은 아니다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--버리는-DB', action='store_true', required=True)
    parser.add_argument('--dsn', required=True, help='격리된 로컬 DB의 명시적 앱 역할 DSN')
    args = parser.parse_args()
    parsed = urlsplit(args.dsn)
    name = unquote(parsed.path.lstrip('/'))
    if (parsed.scheme not in ('postgres', 'postgresql') or parsed.username != 'suddoe_app' or parsed.hostname not in ('localhost', '127.0.0.1', '::1')
            or parsed.query or parsed.fragment or not name or name == 'suddoe_ontology' or (parsed.port in (None, 5432) and name != 'suddoe_ci')):
        parser.error('명시적인 격리 로컬 DB DSN이 필요합니다')
    root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(root), str(root / 'scripts')]
    os.environ['SUDDOE_DSN'] = args.dsn
    os.environ['SUDDOE_MOCK'] = '0'
    os.environ['SUDDOE_ONTOLOGY_PROGRAMS'] = '재도전성공패키지'
    os.environ['SUDDOE_WARMUP_ON_START'] = '0'
    os.environ['SUDDOE_IP_HOURLY_LIMIT'] = '1000000'
    import orchestrate as O
    import retrieve
    import ontology_graph as G
    import ontology_age as A
    import scripts.ontology_age as SA
    from server import response_cache
    from fastapi.testclient import TestClient
    from server.main import app

    # 실제 DENSE SQL을 같은 연결에서 실행한다. 임베딩 모델 대신 1024차원 고정 벡터를 쓴다.
    import psycopg
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute("SET LOCAL search_path = corpus, tenant, extensions, public")
            G.준비(cur)
            first_path = cur.execute('SHOW search_path').fetchone()[0]
            G.준비(cur)
            assert cur.execute('SHOW search_path').fetchone()[0] == first_path
            assert all(schema in first_path for schema in ('ag_catalog', 'corpus', 'tenant', 'extensions', 'public'))
            vector = '[' + ','.join(['1'] + ['0'] * 1023) + ']'
            cur.execute(retrieve.DENSE.format(사업=''), {'v': vector, 'k': 1}).fetchall()
    print('통과 AGE 준비 후 기존 search_path·고정 벡터 DENSE SQL')

    calls = {'cypher': 0, 'llm': 0, '보조': 0}
    original_query = G._조회

    def cypher(*a, **kw):
        calls['cypher'] += 1
        return original_query(*a, **kw)

    G._조회 = cypher
    original_judge = O.판정
    O.판정 = lambda q, **kw: original_judge(q, **{**kw, '기록': False})

    def fixed_llm(prompt, schema, **kw):
        # 판정과 따로 도는 짧은 호출(재설계 v043: 할 일 다듬기 · 금지 재확인)은 준비 · 판정 호출 수와 따로 센다
        if '할일다듬기' in schema.get('properties', {}):
            calls['보조'] += 1
            return {'할일다듬기': []}, {'모델': '고정', '입력토큰': 0, '출력토큰': 0}
        if schema is O._금지재확인_스키마:
            calls['보조'] += 1
            return {'이유': '', '결과': '해당없음'}, {'모델': '고정', '입력토큰': 0, '출력토큰': 0}
        calls['llm'] += 1
        if '행동' in schema.get('properties', {}):
            return {'행동': '판정', '요청': ''}, {'모델': '고정', '입력토큰': 0, '출력토큰': 0}
        ids = (schema['properties']['인용']['items'].get('enum') or [])[:1]
        return {'판정': '확인 필요', '요약': '고정 출력 시험', '해야할일': [], '인용': ids, '전제': []}, {'모델': '고정', '입력토큰': 0, '출력토큰': 0}

    O.llm_호출 = fixed_llm
    retrieve.검색 = lambda *a, **kw: O._빈_검색결과()
    client = TestClient(app, raise_server_exceptions=False)
    raw = {'품목': '외부 전문가 자문 세션', '금액': 250000, '금액_추정여부': False, '용도': '사업 자문',
           '질문원문': '외부 전문가 자문 세션 25만원', '질의문': '외부 전문가 자문 세션',
           '분류후보': [{'비목': '지급수수료', '세목': '멘토링비', '표시문': '지급수수료'}],
           '비목후보': [{'비목': '지급수수료', '신뢰도': 1.0}], '수치': {'부가세구분': '포함'}, '누락필드': []}
    payload = {'사업명': '재도전성공패키지', '확정비목': '지급수수료', '확정세목': '멘토링비',
               '정규화': raw, '답변': [], '추가입력값': {'인원': 1, '일수': 1}, '부가세구분': '포함'}

    def event(response, name):
        for part in response.text.split('\n\n'):
            lines = part.splitlines()
            if 'event: ' + name in lines:
                return json.loads('\n'.join(x[6:] for x in lines if x.startswith('data: ')))
        raise AssertionError(f'{name} 이벤트가 없습니다')

    def request():
        response = client.post('/api/judge', json=payload)
        if response.status_code != 200:
            raise AssertionError(f'API HTTP {response.status_code}')
        return event(response, '결과'), event(response, '완료')

    response_cache._메모리.clear()
    response_cache.데이터버전_해시_무효화()
    outcome, done = request()
    assert outcome.get('판정') == '확인 필요' and done.get('캐시') is False
    assert calls['cypher'] > 0 and calls['llm'] == 2
    before = dict(calls)
    outcome, done = request()
    assert outcome.get('판정') == '확인 필요' and done.get('캐시') is True and calls == before

    original_version = SA.준비_버전

    class Row:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class Proxy:
        def __init__(self, cur, mode):
            self.cur, self.mode = cur, mode

        def execute(self, sql, *a, **kw):
            if 'SELECT dirty, counts, updated_at' in sql:
                if self.mode == 'dirty':
                    return Row((True, {}, None))
                if self.mode == 'missing-state':
                    return Row(None)
            if self.mode == 'missing-graph' and 'corpus.age_concepts' in sql:
                raise RuntimeError('injected missing graph relation')
            return self.cur.execute(sql, *a, **kw)

    for mode in ('dirty', 'missing-state', 'missing-graph'):
        SA.준비_버전 = lambda cur, m=mode: original_version(Proxy(cur, m))
        A.준비_확인 = lambda cur, m=mode: original_version(Proxy(cur, m))
        outcome, done = request()
        # 사전 준비 확인에서 중단하면 완료 이벤트에는 실패만 있고 캐시 키가 없다.
        assert outcome.get('판정') == '확인 필요' and done.get('캐시') is not True and calls == before, mode
        cache_key = next(iter(response_cache._메모리), 'not-found')
        assert response_cache.조회(cache_key, None) is None, mode
        print(f'통과 {mode}: 캐시 우회·확인 필요')
    print('통과 정상 AGE 조회·캐시 적중; 실제 LLM·검색·DB 기록 없음')
    return 0


if __name__ == '__main__':
    sys.exit(main())
