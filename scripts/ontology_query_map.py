# -*- coding: utf-8 -*-
"""AGE 조회 지도의 공개 부분과 서버 전용 범위. 사용자·모델에서 범위를 받지 않는다."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import json
import re

import ontology_age
import ontology_fund
import ontology_graph
import ontology_lookup
import ontology_research
from ontology_norm import 모든비목

지도_버전 = '2'

@dataclass(frozen=True)
class Scope:
    version: str
    program: str
    category: str
    fund: str | None
    entity_ids: tuple[str, ...]
    norm_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    article_ids: tuple[str, ...]
    view_json: str

    @property
    def view(self):
        return json.loads(self.view_json)


@contextmanager
def readonly(cur, budget):
    """독립 트랜잭션 안에서만 작동한다. 이미 진행 중인 트랜잭션은 거부한다."""
    conn = cur.connection
    if not conn.autocommit or int(conn.info.transaction_status) != 0:
        raise RuntimeError('독립 읽기 전용 트랜잭션 필요')
    remaining = budget.remaining_seconds()
    if remaining <= 0:
        raise RuntimeError('조사 시간 소진')
    with conn.transaction():
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, min(10000, int(remaining * 1000)))),))
        yield


def map_for(cur, *, 사업, 비목_id, 자금, budget, 질문='', 품목='', 용도=''):
    if not 사업 or not 비목_id or budget is None or not budget.reserve_tool():
        raise ValueError('지도 범위 또는 예산 오류')
    if ontology_fund.경로_자금(사업, 비목_id) != 자금:
        raise ValueError('비목과 자금 범위 불일치')
    with readonly(cur, budget):
        version = ontology_age.준비_버전(cur)
        labels = ontology_lookup.레이블_목록(cur, 사업)
        # 판정 경로(ontology_lookup.대상_노드)와 같은 범위: 사업이 쓰는 비목이거나, 모든 사업에 적용되는
        # 공통 비목(적용사업 NULL, 예: 재도전의 재료비는 통합관리지침으로 적용)만 받는다.
        if 비목_id not in {x['노드'] for x in labels if x.get('세목') is None} and not cur.execute(
                "SELECT 1 FROM corpus.age_concepts WHERE concept_id = %s AND 종류 = '비목' AND 적용사업 IS NULL",
                (비목_id,)).fetchone():
            raise ValueError('사업의 선택 비목이 아님')
        rows = ontology_research.후보_목록(cur, 사업=사업, 비목_id=비목_id, 자금=자금)
        # 지도에 등장하지 않는 값을 모델이 파라미터로 새로 만들 수 없다.
        terms = set(re.findall(r'[\w가-힣]+', ' '.join(str(x or '')[:300] for x in (질문, 품목, 용도)).casefold()))
        def relevance(row):
            label = ' '.join([row['정식명'], *row['별칭']]).casefold()
            return (-sum(len(term) for term in terms if len(term) > 1 and term in label), row['id'])
        rows = sorted(rows, key=relevance)
        display = [{**x, '정식명': x['정식명'][:100], '별칭': [a[:100] for a in x['별칭'][:4]]} for x in rows[:40]]
        ids = {x['id'] for x in display}
        ids.add(비목_id)
        if 자금 != ontology_fund.창업활동자금:
            ids.add(모든비목)
        valid = cur.execute('''SELECT concept_id FROM corpus.age_concepts
            WHERE concept_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = %s)''', (list(ids), 사업)).fetchall()
        entities = {x[0] for x in valid}
        if 비목_id not in entities:
            raise ValueError('선택 비목 범위 오류')
        norms = cur.execute('''SELECT norm_id, 종류, 대상, 예외 FROM corpus.age_norms
            WHERE (적용사업 IS NULL OR 적용사업 = %s) ORDER BY norm_id''', (사업,)).fetchall()
        if len(norms) > 10000:
            raise ValueError('규범 범위 초과')
        prov = cur.execute('''SELECT owner_id, doc_id, "조번호", "항호" FROM corpus.age_provenance
            WHERE owner_kind = '규범' AND owner_id = ANY(%s)''', ([r[0] for r in norms],)).fetchall()
        schema = ontology_lookup._스키마()
        keys = {v: k for k, v in (schema.get('문서키') or {}).items()}
        excluded = set(ontology_lookup._다른사업_문서([사업]))
        sources = {}
        for owner, doc, article, paragraph in prov:
            sources.setdefault(owner, []).append((doc, article, paragraph))
        eligible = {}
        for nid, kind, targets, exceptions in norms:
            srcs = sources.get(nid, [])
            if not srcs or any(doc not in keys or doc in excluded or
                    ontology_fund.다른_자금(자금, keys.get(doc), article, paragraph)
                    for doc, article, paragraph in srcs):
                continue
            # 기존 창업활동자금 공통규범 적용 미결을 모델 질의로 해제하지 않는다.
            if (자금 == ontology_fund.창업활동자금 and not ontology_fund.공통규범_창업활동자금_적용
                    and not any('창업활동자금' in x for x in (targets or []) if isinstance(x, str))):
                continue
            eligible[nid] = (kind, targets or [], exceptions or [])
        # 선택 비목/부분지도/공통 대상에 닿는 규범과 그 명시적 예외만 허용한다.
        selected = {nid for nid, (_, targets, _) in eligible.items() if entities.intersection(targets)}
        current = set(selected)
        for _ in range(3):
            current = {x for nid in current for x in eligible[nid][2] if x in eligible} - selected
            if not current:
                break
            selected.update(current)
        permitted = [(nid, eligible[nid][0]) for nid in sorted(selected)]
        if len(permitted) > 10000:
            raise ValueError('규범 범위 초과')
        norm_ids = [x[0] for x in permitted]
        ontology_graph.준비(cur)
        source_rows = cur.execute('''SELECT s::text, d::text FROM ag_catalog.cypher('suddoe_ontology',
          $$ MATCH (n:Norm)-[:HAS_SOURCE]->(s:Source)-[:IN_ARTICLE]->(d:DocArticle)
             WHERE n.id IN $ids RETURN s.id, d.id $$, %s)
          AS (s ag_catalog.agtype, d ag_catalog.agtype) LIMIT 40001''',
          (json.dumps({'ids': norm_ids}, ensure_ascii=False),), prepare=True).fetchall()
        if len(source_rows) > 40000:
            raise ValueError('근거 범위 초과')
        source_ids = {decode_id(x[0]) for x in source_rows}
        article_ids = {decode_id(x[1]) for x in source_rows}
        if ontology_age.준비_버전(cur) != version or budget.remaining_seconds() <= 0:
            raise RuntimeError('온톨로지 판본 또는 시간 변경')
    from ontology_query import _EDGES
    view = {'지도형식': 지도_버전, '버전': version, '사업': 사업, '자금': 자금, '비목_id': 비목_id,
            '후보': display, '후보잘림': len(rows) > len(display),
            '문법': 'MATCH (e:Entity)<-[t:TARGETS]-(n:Norm) WHERE e.id IN $ids RETURN DISTINCT n.id AS norm_id LIMIT 20',
            '예시': ['MATCH (e:Entity)<-[t:TARGETS]-(n:Norm)-[x:EXCEPTION_TO]->(o:Norm) WHERE e.id = $id RETURN DISTINCT o.id AS norm_id LIMIT 20',
                   'MATCH (n:Norm)-[s:HAS_SOURCE]->(p:Source)-[a:IN_ARTICLE]->(d:DocArticle) WHERE n.data.종류 = $kind AND p.data.doc_id = $doc RETURN DISTINCT n.id AS norm_id LIMIT 20'],
            '구조': [{'간선': edge, '방향': '출발→도착', '출발': left, '도착': right}
                   for edge, pairs in _EDGES.items() for left, right in sorted(pairs)],
            '조건': 'Entity.id, RELATION.type, Norm.data.종류, Source.data.doc_id, DocArticle.data.조번호만 = 또는 IN, AND만 사용',
            '규범종류': sorted({k for _, k in permitted if isinstance(k, str)}),
            '문서ID': sorted({doc for nid, _ in permitted for doc, _, _ in sources[nid]})[:40],
            '조번호': sorted({article for nid, _ in permitted for _, article, _ in sources[nid] if article})[:40],
            '부분지도안내': '이 지도는 안전 범위가 확인된 지원 간선만 표시한다. 범위와 크기 제한으로 생략된 항목이 있을 수 있으며 미표시는 부재를 뜻하지 않는다.'}
    view['문서목록잘림'] = len({doc for nid, _ in permitted for doc, _, _ in sources[nid]}) > len(view['문서ID'])
    view['조번호목록잘림'] = len({article for nid, _ in permitted for _, article, _ in sources[nid] if article}) > len(view['조번호'])
    while len(json.dumps(view, ensure_ascii=False)) > 12000 and view['후보']:
        view['후보'].pop()
        view['후보잘림'] = True
    if len(json.dumps(view, ensure_ascii=False)) > 12000:
        raise ValueError('지도 크기 초과')
    return Scope(version, 사업, 비목_id, 자금, tuple(sorted(entities)), tuple(norm_ids),
                 tuple(sorted(source_ids)), tuple(sorted(article_ids)), json.dumps(view, ensure_ascii=False))


def decode_id(value):
    """AGE·드라이버의 text 표현이 일반 문자열 또는 JSON 문자열이어도 타입을 확인한다."""
    if not isinstance(value, str):
        raise ValueError('AGE 식별자 타입 오류')
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError('AGE 식별자 형식 오류') from None
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError('AGE 식별자 형식 오류')
    return value
