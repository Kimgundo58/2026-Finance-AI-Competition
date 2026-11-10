# -*- coding: utf-8 -*-
"""허용 Cypher 문법의 독립 테스트. 네트워크와 DB를 사용하지 않는다."""
from ontology_query import QueryError, parse


def main():
    query = ('MATCH (e:Entity)<-[t:TARGETS]-(n:Norm)-[x:EXCEPTION_TO]->(other:Norm) '
             'WHERE e.id IN $ids AND t.id = $x RETURN DISTINCT other.id AS norm_id LIMIT 5')
    # TARGETS는 id 조건을 허용하지 않는다.
    try:
        parse(query)
        raise AssertionError('관계 필드 허용')
    except QueryError:
        pass
    valid = ('MATCH (e:Entity)<-[t:TARGETS]-(n:Norm)-[x:EXCEPTION_TO]->(other:Norm) '
             'WHERE e.id IN $ids RETURN DISTINCT other.id AS norm_id LIMIT 5')
    ast = parse(valid)
    assert len(ast.edges) == 2 and ast.result == 'other' and ast.predicates[0].param == 'ids'
    assert parse('MATCH (n:Norm)-[s:HAS_SOURCE]->(p:Source)-[a:IN_ARTICLE]->(d:DocArticle) WHERE n.data.종류 = $kind AND p.data.doc_id = $doc RETURN DISTINCT n.id AS norm_id LIMIT 10').predicates[0].field == 'data.종류'
    for suffix in ('; MATCH (a) DELETE a', ' UNION MATCH (x) RETURN x', ' // comment'):
        try:
            parse(valid + suffix)
            raise AssertionError('금지 구문 허용')
        except QueryError:
            pass
    for bad in (
        'MATCH (a:Entity),(n:Norm) RETURN DISTINCT n.id AS norm_id LIMIT 1',
        'MATCH (a:Entity)-[r:RELATION*]->(n:Norm) RETURN DISTINCT n.id AS norm_id LIMIT 1',
        'MATCH (a:Entity)-[r:TARGETS]->(n:Norm) RETURN DISTINCT n.id AS norm_id LIMIT 1',
        'MATCH (a:Entity)<-[r:TARGETS]-(n:Norm) WHERE a.id = "x" RETURN DISTINCT n.id AS norm_id LIMIT 1',
        'MATCH (a:Entity)<-[r:TARGETS]-(n:Norm) RETURN DISTINCT n.id AS norm_id LIMIT 999',
    ):
        try:
            parse(bad)
            raise AssertionError('금지 구문 허용')
        except QueryError:
            pass
    import sys
    if '--db' in sys.argv:
        import psycopg
        import ontology_fund as fund
        import ontology_query_map as maps
        from ontology_query import execute
        from research_runtime import Budget, activate
        conn = psycopg.connect('postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5',
                               autocommit=True, options='-c default_transaction_read_only=on')
        budget = Budget(seconds=120)
        with activate(budget), conn.cursor() as cur:
            scope = maps.map_for(cur, 사업=fund._사업, 비목_id=fund._창업활동자금_비목,
                                 자금=fund.창업활동자금, budget=budget, 품목='API 이용료')
            assert len(scope.view_json) <= 12000
            result = execute(cur, query=scope.view['문법'], params={'ids': [fund._창업활동자금_비목]}, 지도=scope, budget=budget)
            assert result['규범ID'] and set(result['규범ID']) <= set(scope.norm_ids)
            assert result['상한도달'] == (len(result['규범ID']) == 20)
            for query, params in [
                ('MATCH (n:Norm)-[s:HAS_SOURCE]->(p:Source)-[a:IN_ARTICLE]->(d:DocArticle) WHERE n.data.종류 = $kind AND p.data.doc_id = $doc RETURN DISTINCT n.id AS norm_id LIMIT 20',
                 {'kind': scope.view['규범종류'][0], 'doc': scope.view['문서ID'][0]}),
                ('MATCH (n:Norm)-[s:HAS_SOURCE]->(p:Source)-[a:IN_ARTICLE]->(d:DocArticle) WHERE d.data.조번호 = $article RETURN DISTINCT n.id AS norm_id LIMIT 20',
                 {'article': scope.view['조번호'][0]}),
            ]:
                result = execute(cur, query=query, params=params, 지도=scope, budget=budget)
                assert set(result['규범ID']) <= set(scope.norm_ids)
            exception_query = scope.view['예시'][0]
            result = execute(cur, query=exception_query, params={'id': fund._창업활동자금_비목}, 지도=scope, budget=budget)
            assert set(result['규범ID']) <= set(scope.norm_ids)
            try:
                execute(cur, query=scope.view['문법'], params={'ids': ['not-in-map']}, 지도=scope, budget=budget)
                raise AssertionError('지도 밖 식별자 허용')
            except QueryError:
                pass
            with maps.readonly(cur, budget):
                assert cur.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
            try:
                maps.map_for(cur, 사업=fund._사업, 비목_id=fund._창업활동자금_비목,
                             자금=fund.시제품제작비, budget=budget)
                raise AssertionError('비목과 자금 불일치 허용')
            except ValueError:
                pass
            expired = Budget(seconds=0)
            try:
                execute(cur, query=scope.view['문법'], params={'ids': [fund._창업활동자금_비목]}, 지도=scope, budget=expired)
                raise AssertionError('시간 초과 조회 허용')
            except QueryError:
                pass
            timed = Budget(seconds=.05)
            try:
                with activate(timed), maps.readonly(cur, timed):
                    cur.execute('SELECT pg_sleep(0.2)')
                raise AssertionError('statement_timeout이 작동하지 않음')
            except psycopg.errors.QueryCanceled:
                pass
            assert int(conn.info.transaction_status) == 0
        conn.close()
        print('5462 읽기 전용 AGE 지도·질의 검사 통과')
    if '--all-programs' in sys.argv:
        import psycopg
        import ontology_fund as fund
        import ontology_lookup as lookup
        import ontology_query_map as maps
        from ontology_query import execute
        from research_runtime import Budget, activate
        conn = psycopg.connect('postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5',
                               autocommit=True, options='-c default_transaction_read_only=on')
        programs = list(lookup._스키마()['사업키'].values())
        hits = {'TARGETS': 0, 'RELATION': 0, 'EXCEPTION_TO': 0, 'HAS_SOURCE/IN_ARTICLE': 0}
        with conn.cursor() as cur:
            for program in programs:
                candidates = [x['노드'] for x in lookup.레이블_목록(cur, program) if x['세목'] is None]
                assert candidates
                category = candidates[0]
                budget = Budget(seconds=120)
                with activate(budget):
                    scope = maps.map_for(cur, 사업=program, 비목_id=category,
                                         자금=fund.경로_자금(program, category), budget=budget, 품목='API 이용료')
                    result = execute(cur, query=scope.view['문법'], params={'ids': [category]}, 지도=scope, budget=budget)
                    assert set(result['규범ID']) <= set(scope.norm_ids)
                    assert scope.program == program and scope.category == category
                    hits['TARGETS'] += len(result['규범ID'])
                    for label, query in (
                        ('RELATION', 'MATCH (a:Entity)-[r:RELATION]->(b:Entity)<-[t:TARGETS]-(n:Norm) RETURN DISTINCT n.id AS norm_id LIMIT 5'),
                        ('EXCEPTION_TO', 'MATCH (n:Norm)-[e:EXCEPTION_TO]->(o:Norm) RETURN DISTINCT o.id AS norm_id LIMIT 5'),
                        ('HAS_SOURCE/IN_ARTICLE', 'MATCH (n:Norm)-[s:HAS_SOURCE]->(p:Source)-[a:IN_ARTICLE]->(d:DocArticle) RETURN DISTINCT n.id AS norm_id LIMIT 5'),
                    ):
                        result = execute(cur, query=query, params={}, 지도=scope, budget=budget)
                        hits[label] += len(result['규범ID'])
        assert all(hits.values()), hits
        conn.close()
        print('5462 읽기 전용 7사업 지도 범위·지원 간선 비어 있지 않은 결과 검사 통과', hits)
    print('제한 Cypher 파서 모의 검사 통과')


if __name__ == '__main__':
    main()
