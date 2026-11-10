# -*- coding: utf-8 -*-
"""정해진 관계만 Apache AGE에서 읽는다. 조회 실패는 호출자에게 전파한다."""
from __future__ import annotations

import json
import threading
import weakref

import ontology_age

# 준비 확인(상태 행 + 행 수 6개)을 이미 통과한 커넥션. 커넥션은 요청마다 새로 열므로(_lib.db.connect, 풀 없음)
# 요청당 한 번만 센다(리뷰 28-20). 커넥션이 닫혀 사라지면 기록도 사라진다
_확인됨: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_확인됨_잠금 = threading.Lock()


def 준비(cur):
    conn = cur.connection
    with _확인됨_잠금:
        확인 = conn in _확인됨
    if not 확인:
        ontology_age.준비_확인(cur)
        with _확인됨_잠금:
            _확인됨[conn] = True
    cur.execute("LOAD '$libdir/plugins/age'")
    cur.execute("""SELECT set_config('search_path',
        CASE WHEN current_setting('search_path') LIKE 'ag_catalog,extensions,%%'
             THEN current_setting('search_path')
             ELSE 'ag_catalog,extensions,' || current_setting('search_path') END, %s)""",
        (not cur.connection.autocommit,))


def _조회(cur, query, params):
    준비(cur)
    rows = cur.execute("""SELECT a::text, b::text, typ::text, program::text
        FROM ag_catalog.cypher('suddoe_ontology', $$ """ + query + """ $$, %s)
          AS (a ag_catalog.agtype, b ag_catalog.agtype, typ ag_catalog.agtype, program ag_catalog.agtype)""",
        (json.dumps(params, ensure_ascii=False),), prepare=True).fetchall()
    return rows


def 간선(cur, 시작들, 관계들, *, 역방향=False, 사업들=None):
    """고정된 방향의 1단계 관계를 시작 ID 배치로 조회한다.

    사업들=None은 사업 필터 없음, 빈 목록은 공통 관계만 뜻한다.
    """
    if not 시작들:
        return []
    if not set(관계들) <= {'상위', '준용', '위임', '사용'}:
        raise ValueError('허용되지 않은 관계')
    pattern = '(a:Entity)-[r:RELATION]->(b:Entity)'
    endpoint = 'b' if 역방향 else 'a'
    filt = '' if 사업들 is None else ' AND (r.program IS NULL OR r.program IN $programs)'
    query = (f'MATCH {pattern} WHERE {endpoint}.id IN $ids '
             f'AND r.type IN $types{filt} RETURN a.id, b.id, r.type, r.program')
    return _조회(cur, query, {'ids': list(dict.fromkeys(시작들)), 'types': list(관계들), 'programs': 사업들 or []})


def 사업_사용(cur, 사업id, 사업명):
    """사업 노드에서 출발하거나 해당 사업에 지정된 사용 관계."""
    query = ('MATCH (a:Entity)-[r:RELATION]->(b:Entity) '
             'WHERE r.type = $type AND (a.id = $id OR r.program = $program) '
             'RETURN a.id, b.id, r.type, r.program')
    return _조회(cur, query, {'type': '사용', 'id': 사업id, 'program': 사업명})


def 경로(cur, 시작들, 관계들, *, 역방향=False, 사업들=None, 최대깊이=None, 노드허용=None):
    """경로별 방문을 보존해 (시작, 도착, 관계, 깊이, 경로)를 반환한다."""
    current = [(s, s, []) for s in 시작들]
    result = []
    depth = 0
    while current and (최대깊이 is None or depth < 최대깊이):
        depth += 1
        rows = 간선(cur, [node for _, node, _ in current], 관계들, 역방향=역방향, 사업들=사업들)
        by = {}
        for src, dst, kind, _ in rows:
            a, b = (dst, src) if 역방향 else (src, dst)
            if 노드허용 is None or b in 노드허용:
                by.setdefault(a, []).append((b, kind))
        nxt = []
        for start, node, path in current:
            visited = [start, *path]
            for target, kind in by.get(node, []):
                if target in visited and not (depth == 1 and target == start):
                    continue
                newpath = [*path, target]
                result.append((start, target, kind, depth, [start, *newpath]))
                nxt.append((start, target, newpath))
        current = nxt
    return result
