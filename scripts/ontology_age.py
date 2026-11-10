# -*- coding: utf-8 -*-
"""관계형 온톨로지를 같은 DB 트랜잭션에서 Apache AGE 그래프로 복제한다.

호출자는 JSON 검증과 corpus 쓰기를 마친 동일 커서로 동기화()를 호출한다.
이 함수는 커밋하지 않는다. 한 건이라도 빠지면 예외를 내어 전체 쓰기를 되돌린다.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

GRAPH = "suddoe_ontology"
_CHUNK = 100


def 준비_버전(cur):
    """동기화 상태와 행 수를 확인하고 캐시용 버전을 반환한다. 실패하면 예외다."""
    row = cur.execute("SELECT dirty, counts, updated_at FROM corpus.ontology_age_state WHERE id = true").fetchone()
    if row is None or row[0] or not isinstance(row[1], dict):
        raise RuntimeError("온톨로지 AGE 그래프가 준비되지 않았다")
    names = ("개념", "관계", "규범", "별칭", "근거", "간선")
    expected = row[1]
    if any(type(expected.get(key)) is not int or expected[key] < 0 for key in names) or expected["개념"] == 0 or expected["규범"] == 0:
        raise RuntimeError("온톨로지 AGE 행 수가 기록되지 않았다")
    actual = cur.execute("""SELECT (SELECT count(*) FROM corpus.age_concepts),
        (SELECT count(*) FROM corpus.age_concept_relations), (SELECT count(*) FROM corpus.age_norms),
        (SELECT count(*) FROM corpus.age_concept_labels), (SELECT count(*) FROM corpus.age_provenance),
        (SELECT count(*) FROM suddoe_ontology."_ag_label_edge")""").fetchone()
    if actual is None or tuple(expected[key] for key in names) != tuple(actual):
        raise RuntimeError("온톨로지 AGE 그래프 행 수가 일치하지 않는다")
    if row[2] is None:
        raise RuntimeError("온톨로지 AGE 갱신 시각이 없다")
    return _key(expected, row[2].isoformat())


def 준비_확인(cur):
    """기존 조회용 이름. 준비_버전과 같은 검사를 실행한다."""
    return 준비_버전(cur)


def _key(*values):
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rows(cur, table):
    # 호출 장소의 상수만 받는다. 입력 데이터는 SQL로 조립하지 않는다.
    if table not in {"concepts", "concept_labels", "concept_relations", "norms", "provenance"}:
        raise ValueError("허용되지 않은 테이블")
    cur.execute(f"SELECT row_to_json(t)::text FROM corpus.{table} AS t")
    return [json.loads(row[0]) for row in cur.fetchall()]


def _cypher(cur, query, rows):
    """AGE 매개변수는 서버 prepared statement의 agtype map으로만 전달한다."""
    if not rows:
        return 0
    total = 0
    for start in range(0, len(rows), _CHUNK):
        chunk = rows[start:start + _CHUNK]
        cur.execute(
            "SELECT * FROM ag_catalog.cypher('suddoe_ontology', $$ " + query + " $$, %s) AS (n ag_catalog.agtype)",
            (json.dumps({"rows": chunk}, ensure_ascii=False),), prepare=True,
        )
        value = cur.fetchone()
        if value is None:
            raise RuntimeError("AGE 결과 건수를 읽지 못했다")
        total += int(str(value[0]))
    if total != len(rows):
        raise RuntimeError(f"AGE 적재 건수 불일치: 기대 {len(rows)}, 실제 {total}")
    return total


def _vertices(cur, label, rows):
    if label not in {"Entity", "Norm", "Alias", "Source", "Relation", "DocArticle"}:
        raise ValueError("허용되지 않은 노드")
    # label은 상수 화이트리스트, 데이터는 prepared parameter이다.
    query = f"UNWIND $rows AS row CREATE (n:{label} {{id: row.id, kind: row.kind, data: row.data}}) RETURN count(n)"
    return _cypher(cur, query, rows)


def _edges(cur, left, edge, right, rows, direct=False):
    if left not in {"Entity", "Norm", "Alias", "Source", "Relation", "DocArticle"} or right not in {"Entity", "Norm", "Alias", "Source", "Relation", "DocArticle"}:
        raise ValueError("허용되지 않은 노드")
    if edge not in {"RELATION", "FROM_NODE", "TO_NODE", "TARGETS", "ADDRESSEE", "APPROVER", "EXCEPTION_TO", "EXCEPTION_TEXT", "HAS_ALIAS", "HAS_SOURCE", "IN_ARTICLE"}:
        raise ValueError("허용되지 않은 관계")
    props = "type: row.type, program: row.program, review: row.review, link_type: row.link_type" if direct else "id: row.id"
    query = (f"UNWIND $rows AS row MATCH (a:{left} {{id: row.src}}), (b:{right} {{id: row.dst}}) "
             f"CREATE (a)-[e:{edge} {{{props}}}]->(b) RETURN count(e)")
    return _cypher(cur, query, rows)


def 동기화(cur):
    """현재 corpus 5표를 전부 읽어 전용 그래프를 교체한다. 예외 시 호출자가 롤백한다."""
    if cur.connection.autocommit:
        raise RuntimeError("AGE 동기화는 autocommit에서 실행할 수 없다")
    cur.execute("LOAD '$libdir/plugins/age'")
    cur.execute("SELECT set_config('search_path', 'ag_catalog,' || current_setting('search_path'), true)")
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('suddoe_ontology_age'))")
    concepts, aliases, relations, norms, sources = (_rows(cur, name) for name in
        ("concepts", "concept_labels", "concept_relations", "norms", "provenance"))
    entities = {r["concept_id"]: {"id": r["concept_id"], "kind": "concept", "data": r} for r in concepts}
    for relation in relations:
        for key in ("src_id", "dst_id"):
            ident = relation[key]
            entities.setdefault(ident, {"id": ident, "kind": "article" if ident.startswith("조:") else "unresolved", "data": {}})
    for norm in norms:
        for key in ("대상", "수범자", "승인주체"):
            for ident in norm[key] or []:
                entities.setdefault(ident, {"id": ident, "kind": "unresolved", "data": {}})
    norm_rows = [{"id": n["norm_id"], "kind": "norm", "data": n} for n in norms]
    norm_ids = {n["id"] for n in norm_rows}
    alias_rows, alias_edges = [], []
    for a in aliases:
        ident = _key(a["concept_id"], a["정규화"], a["적용사업"])
        alias_rows.append({"id": ident, "kind": "alias", "data": a})
        alias_edges.append({"id": ident, "src": a["concept_id"], "dst": ident})
    rel_rows, direct_edges, from_edges, to_edges, rel_owner = [], [], [], [], {}
    for r in relations:
        owner = f"{r['src_id']}|{r['관계']}|{r['dst_id']}|{r['적용사업'] or '공통'}"
        ident = _key(r["src_id"], r["관계"], r["dst_id"], r["적용사업"])
        rel_owner[owner] = ident
        rel_rows.append({"id": ident, "kind": "relation", "data": r})
        direct_edges.append({"src": r["src_id"], "dst": r["dst_id"], "type": r["관계"], "program": r["적용사업"], "review": r["검수단계"], "link_type": r["연결종류"]})
        from_edges.append({"id": ident, "src": ident, "dst": r["src_id"]})
        to_edges.append({"id": ident, "src": ident, "dst": r["dst_id"]})
    target_edges, addressee_edges, approver_edges, exception_edges, exception_text_edges = [], [], [], [], []
    for n in norms:
        for ident in n["대상"] or []:
            target_edges.append({"id": _key(n["norm_id"], ident), "src": n["norm_id"], "dst": ident})
        for key, out in (("수범자", addressee_edges), ("승인주체", approver_edges)):
            for ident in n[key] or []:
                out.append({"id": _key(n["norm_id"], ident), "src": n["norm_id"], "dst": ident})
        for ident in n["예외"] or []:
            if ident in norm_ids:
                exception_edges.append({"id": _key(n["norm_id"], ident), "src": n["norm_id"], "dst": ident})
            else:
                text_id = "예외:" + _key(ident)
                entities.setdefault(text_id, {"id": text_id, "kind": "exception_text", "data": {"문장": ident}})
                exception_text_edges.append({"id": _key(n["norm_id"], ident), "src": n["norm_id"], "dst": text_id})
    source_rows, article_rows, article_edges = [], {}, []
    owner_edges = {"Entity": [], "Norm": [], "Relation": []}
    occurrences = Counter()
    for source in sources:
        owner_kind = source["owner_kind"]
        owner = source["owner_id"]
        if owner_kind == "용어":
            continue  # 용어 근거는 별도 terminology 경로로 유지한다.
        label = {"개념": "Entity", "규범": "Norm", "관계": "Relation"}.get(owner_kind)
        if label is None:
            raise RuntimeError("알 수 없는 근거 소유자")
        if label == "Relation":
            owner = rel_owner.get(owner)
        if not owner or (label == "Entity" and owner not in entities) or (label == "Norm" and owner not in norm_ids):
            raise RuntimeError("근거 소유자를 AGE에 연결할 수 없다")
        raw = _key(source)
        occurrences[raw] += 1
        ident = _key(raw, occurrences[raw])
        article = _key(source["doc_id"], source["조번호"], source["항호"])
        article_rows[article] = {"id": article, "kind": "article", "data": {"doc_id": source["doc_id"], "조번호": source["조번호"], "항호": source["항호"]}}
        source_rows.append({"id": ident, "kind": "source", "data": source})
        owner_edges[label].append({"id": ident, "src": owner, "dst": ident})
        article_edges.append({"id": ident, "src": ident, "dst": article})
    cur.execute("SELECT * FROM ag_catalog.cypher('suddoe_ontology', $$ MATCH (n) DETACH DELETE n $$) AS (n ag_catalog.agtype)")
    for label, rows in (("Entity", list(entities.values())), ("Norm", norm_rows), ("Alias", alias_rows), ("Relation", rel_rows), ("Source", source_rows), ("DocArticle", list(article_rows.values()))):
        _vertices(cur, label, rows)
    for left, edge, right, rows, direct in (("Entity", "RELATION", "Entity", direct_edges, True), ("Relation", "FROM_NODE", "Entity", from_edges, False), ("Relation", "TO_NODE", "Entity", to_edges, False), ("Norm", "TARGETS", "Entity", target_edges, False), ("Norm", "ADDRESSEE", "Entity", addressee_edges, False), ("Norm", "APPROVER", "Entity", approver_edges, False), ("Norm", "EXCEPTION_TO", "Norm", exception_edges, False), ("Norm", "EXCEPTION_TEXT", "Entity", exception_text_edges, False), ("Entity", "HAS_ALIAS", "Alias", alias_edges, False), ("Source", "IN_ARTICLE", "DocArticle", article_edges, False)):
        _edges(cur, left, edge, right, rows, direct)
    for label, rows in owner_edges.items():
        _edges(cur, label, "HAS_SOURCE", "Source", rows)
    edge_count = sum(len(rows) for rows in (direct_edges, from_edges, to_edges, target_edges, addressee_edges,
                     approver_edges, exception_edges, exception_text_edges, alias_edges, article_edges, *owner_edges.values()))
    counts = {"개념": len(concepts), "관계": len(relations), "규범": len(norms), "별칭": len(aliases), "근거": len(source_rows), "간선": edge_count}
    cur.execute("UPDATE corpus.ontology_age_state SET dirty = false, counts = %s::jsonb, updated_at = now() WHERE id = true", (json.dumps(counts, ensure_ascii=False),))
    if cur.rowcount != 1:
        raise RuntimeError("온톨로지 AGE 상태 기록 실패")
    return counts
