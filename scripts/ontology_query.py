# -*- coding: utf-8 -*-
"""LLM이 제안한 제한 Cypher 구문을 구조로 파싱한다.

질의 원문을 실행하지 않고 파싱한 구문에서 범위 조건을 추가해 재구성한다.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    name: str
    label: str


@dataclass(frozen=True)
class Edge:
    name: str
    label: str
    direction: str


@dataclass(frozen=True)
class Predicate:
    name: str
    field: str
    op: str
    param: str


@dataclass(frozen=True)
class Query:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    predicates: tuple[Predicate, ...]
    result: str
    limit: int


_TOKEN = re.compile(r'\s*(?:(->|<-)|([\w]+)|(\$[A-Za-z_][A-Za-z_0-9]*)|([():.\[\]=\-]))', re.UNICODE)
_EDGES = {
    'RELATION': {('Entity', 'Entity')},
    'TARGETS': {('Norm', 'Entity')},
    'EXCEPTION_TO': {('Norm', 'Norm')},
    'HAS_SOURCE': {('Norm', 'Source')},
    'IN_ARTICLE': {('Source', 'DocArticle')},
}
_FIELDS = {'Entity': {'id'}, 'Norm': {'id', 'data.종류'}, 'Source': {'id', 'data.doc_id'},
           'DocArticle': {'id', 'data.조번호'}, 'Relation': {'id'}, 'Alias': {'id'},
           'RELATION': {'type'}}


def parse(query: str) -> Query:
    """한 연결 체인만 파싱한다. 허용된 나머지 문자열도 실행하지 않는다."""
    if not isinstance(query, str) or len(query) > 4000:
        raise QueryError('질의 길이 오류')
    tokens = []
    offset = 0
    while offset < len(query):
        match = _TOKEN.match(query, offset)
        if not match:
            if not query[offset:].strip():
                break
            raise QueryError('허용되지 않은 문자')
        tokens.append(next(x for x in match.groups() if x is not None))
        offset = match.end()
    pos = 0
    def take(expected=None):
        nonlocal pos
        if pos >= len(tokens):
            raise QueryError('질의가 끝나지 않음')
        token = tokens[pos]
        if expected is not None and token.upper() != expected.upper():
            raise QueryError('질의 구문 오류')
        pos += 1
        return token
    def name():
        token = take()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', token) or len(token) > 32:
            raise QueryError('변수 이름 오류')
        return token
    def node():
        take('('); n = name(); take(':'); label = name(); take(')')
        if label not in {'Entity', 'Norm', 'Source', 'DocArticle'}:
            raise QueryError('노드 종류 오류')
        return Node(n, label)
    take('MATCH')
    nodes = [node()]
    edges = []
    while pos < len(tokens) and tokens[pos] in ('-', '<-'):
        left = take(); take('['); ename = name(); take(':'); label = name(); take(']')
        right = take()
        if (left, right) not in {('-', '->'), ('<-', '-')}:
            raise QueryError('관계 방향 오류')
        following = node()
        pair = (nodes[-1].label, following.label) if left == '-' else (following.label, nodes[-1].label)
        if pair not in _EDGES.get(label, set()):
            raise QueryError('허용되지 않은 관계')
        edges.append(Edge(ename, label, 'out' if left == '-' else 'in'))
        nodes.append(following)
        if len(edges) > 4:
            raise QueryError('관계 깊이 초과')
    if not edges:
        raise QueryError('관계가 필요함')
    variables = {x.name: x.label for x in (*nodes, *edges)}
    if len(variables) != len(nodes) + len(edges):
        raise QueryError('변수 중복')
    predicates = []
    if pos < len(tokens) and tokens[pos].upper() == 'WHERE':
        take('WHERE')
        while True:
            var = name(); take('.'); field = take()
            if field == 'data':
                take('.'); field += '.' + take()
            op = take().upper()
            param = take()
            if (var not in variables or field not in _FIELDS.get(variables[var], set())
                    or op not in {'=', 'IN'} or not re.fullmatch(r'\$[A-Za-z_][A-Za-z_0-9]*', param)):
                raise QueryError('조건 오류')
            predicates.append(Predicate(var, field, op, param[1:]))
            if len(predicates) > 8:
                raise QueryError('조건 초과')
            if pos >= len(tokens) or tokens[pos].upper() != 'AND':
                break
            take('AND')
    take('RETURN'); take('DISTINCT'); result = name(); take('.'); take('id'); take('AS'); take('norm_id'); take('LIMIT')
    limit = take()
    if variables.get(result) != 'Norm' or not limit.isdecimal() or not 1 <= int(limit) <= 50 or pos != len(tokens):
        raise QueryError('반환 오류')
    return Query(tuple(nodes), tuple(edges), tuple(predicates), result, int(limit))


def execute(cur, *, query: str, params: dict, 지도, budget) -> dict:
    """서버가 만든 범위와 읽기 전용 트랜잭션에서만 규범 ID를 반환한다."""
    import json
    import ontology_age
    import ontology_graph
    from ontology_query_map import Scope, readonly, decode_id
    ast = parse(query)
    if not isinstance(지도, Scope) or not isinstance(params, dict) or budget is None:
        raise QueryError('조회 범위 없음')
    allowed_nodes = {'Entity': 지도.entity_ids, 'Norm': 지도.norm_ids,
                     'Source': 지도.source_ids, 'DocArticle': 지도.article_ids}
    if any(node.label not in allowed_nodes for node in ast.nodes):
        raise QueryError('범위를 확인할 수 없는 노드')
    if any(edge.label not in _EDGES for edge in ast.edges):
        raise QueryError('범위를 확인할 수 없는 관계')
    if set(params) != {p.param for p in ast.predicates}:
        raise QueryError('매개변수 오류')
    if any(p.param.startswith('__') for p in ast.predicates):
        raise QueryError('예약된 매개변수')
    labels = {x.name: x.label for x in (*ast.nodes, *ast.edges)}
    visible = {x['id'] for x in 지도.view['후보']} | {지도.view['비목_id']}
    for pred in ast.predicates:
        value = params[pred.param]
        vals = value if pred.op == 'IN' else [value]
        if not isinstance(vals, list) or not 1 <= len(vals) <= 80 or any(not isinstance(v, str) for v in vals):
            raise QueryError('매개변수 형식 오류')
        if pred.field == 'id':
            # 질의에서 지정할 수 있는 식별자는 공개된 후보뿐이다.
            if labels[pred.name] != 'Entity' or not set(vals) <= visible:
                raise QueryError('지도에 없는 식별자')
        elif pred.field == 'type':
            if not set(vals) <= {'상위', '준용', '위임', '사용'}:
                raise QueryError('관계 종류 오류')
        elif pred.field == 'data.종류':
            if not set(vals) <= set(지도.view['규범종류']):
                raise QueryError('지도에 없는 규범 종류')
        elif pred.field == 'data.doc_id':
            if not set(vals) <= set(지도.view['문서ID']):
                raise QueryError('지도에 없는 문서')
        elif pred.field == 'data.조번호':
            if not set(vals) <= set(지도.view['조번호']):
                raise QueryError('지도에 없는 조번호')
    if not budget.reserve_tool():
        raise QueryError('도구 예산 소진')
    parts = [f'({ast.nodes[0].name}:{ast.nodes[0].label})']
    for edge, node in zip(ast.edges, ast.nodes[1:]):
        e = f'[{edge.name}:{edge.label}]'
        parts.append((f'-{e}->' if edge.direction == 'out' else f'<-{e}-') + f'({node.name}:{node.label})')
    conditions = [f'{n.name}.id IN $__{n.label}' for n in ast.nodes]
    conditions += [f'({e.name}.program IS NULL OR {e.name}.program = $__program)' for e in ast.edges if e.label == 'RELATION']
    def field_expr(field):
        return '.'.join('`' + x + '`' if not x.isascii() else x for x in field.split('.'))
    conditions += [f'{p.name}.{field_expr(p.field)} {p.op} ${p.param}' for p in ast.predicates]
    compiled = ('MATCH ' + ''.join(parts) + ' WHERE ' + ' AND '.join(conditions)
                + f' RETURN DISTINCT {ast.result}.id LIMIT {ast.limit}')
    bound = {**params, **{'__' + k: list(v) for k, v in allowed_nodes.items()}, '__program': 지도.program}
    with readonly(cur, budget):
        if ontology_age.준비_버전(cur) != 지도.version:
            raise QueryError('온톨로지 판본 변경')
        ontology_graph.준비(cur)
        rows = cur.execute("SELECT n::text FROM ag_catalog.cypher('suddoe_ontology', $$ " + compiled +
                           " $$, %s) AS (n ag_catalog.agtype)",
                           (json.dumps(bound, ensure_ascii=False),), prepare=True).fetchall()
        if ontology_age.준비_버전(cur) != 지도.version or budget.remaining_seconds() <= 0:
            raise QueryError('온톨로지 판본 또는 시간 변경')
    output = [decode_id(row[0]) for row in rows]
    if len(output) > ast.limit or any(not isinstance(x, str) or x not in 지도.norm_ids for x in output):
        raise QueryError('결과 범위 오류')
    return {'규범ID': output, '상한도달': len(output) == ast.limit, '버전': 지도.version}
