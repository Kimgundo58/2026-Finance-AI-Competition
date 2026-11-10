# -*- coding: utf-8 -*-
"""AGE 그래프 간선과 관계형 원천의 읽기 전용 동등성 검사."""
from __future__ import annotations

def 간선_동등성(cur):
    """관계 방향·끝점·중복까지 원본과 EXCEPT ALL로 양방향 비교한다."""
    cur.execute("LOAD '$libdir/plugins/age'")
    cur.execute("SET LOCAL search_path=ag_catalog,extensions,public")
    nid = "(n.properties -> '\"id\"'::agtype)::text"
    eid = "(v.properties -> '\"id\"'::agtype)::text"
    pairs = []
    expected = "SELECT src_id, 관계, dst_id, 적용사업, 검수단계, 연결종류 FROM corpus.concept_relations"
    actual = '''SELECT (a.properties -> '\"id\"'::agtype)::text,
        (e.properties -> '\"type\"'::agtype)::text,
        (b.properties -> '\"id\"'::agtype)::text,
        nullif((e.properties -> '\"program\"'::agtype)::text, 'null'),
        (e.properties -> '\"review\"'::agtype)::text,
        nullif((e.properties -> '\"link_type\"'::agtype)::text, 'null')
        FROM suddoe_ontology."RELATION" e JOIN suddoe_ontology."Entity" a ON a.id=e.start_id
        JOIN suddoe_ontology."Entity" b ON b.id=e.end_id'''
    pairs.append(("RELATION", expected, actual))
    for edge, column in (("TARGETS", "대상"), ("ADDRESSEE", "수범자"), ("APPROVER", "승인주체")):
        expected = f"SELECT n.norm_id, x.id FROM corpus.norms n CROSS JOIN LATERAL unnest(n.{column}) AS x(id)"
        actual = f'''SELECT {nid}, {eid} FROM suddoe_ontology."{edge}" e
            JOIN suddoe_ontology."Norm" n ON n.id=e.start_id JOIN suddoe_ontology."Entity" v ON v.id=e.end_id'''
        pairs.append((edge, expected, actual))
    for edge, exists in (("EXCEPTION_TO", True), ("EXCEPTION_TEXT", False)):
        test = "EXISTS" if exists else "NOT EXISTS"
        expected = f'''SELECT n.norm_id, x.id FROM corpus.norms n CROSS JOIN LATERAL unnest(n.예외) AS x(id)
            WHERE {test} (SELECT 1 FROM corpus.norms n2 WHERE n2.norm_id=x.id)'''
        if exists:
            actual = f'''SELECT {nid}, (v.properties -> '\"id\"'::agtype)::text
                FROM suddoe_ontology."{edge}" e JOIN suddoe_ontology."Norm" n ON n.id=e.start_id
                JOIN suddoe_ontology."Norm" v ON v.id=e.end_id'''
        else:
            actual = f'''SELECT {nid}, ((v.properties -> '\"data\"'::agtype)::text::jsonb)->>'문장'
                FROM suddoe_ontology."{edge}" e JOIN suddoe_ontology."Norm" n ON n.id=e.start_id
                JOIN suddoe_ontology."Entity" v ON v.id=e.end_id'''
        pairs.append((edge, expected, actual))

    rdata = "(r.properties -> '\"data\"'::agtype)::text::jsonb"
    sdata = "(s.properties -> '\"data\"'::agtype)::text::jsonb"
    aid = "(a.properties -> '\"id\"'::agtype)::text"
    for edge, column in (("FROM_NODE", "src_id"), ("TO_NODE", "dst_id")):
        expected = f"SELECT to_jsonb(r), r.{column} FROM corpus.concept_relations r"
        actual = f'''SELECT {rdata}, {eid} FROM suddoe_ontology."{edge}" e
            JOIN suddoe_ontology."Relation" r ON r.id=e.start_id
            JOIN suddoe_ontology."Entity" v ON v.id=e.end_id'''
        pairs.append((edge, expected, actual))
    expected = "SELECT a.concept_id, to_jsonb(a) FROM corpus.concept_labels a"
    actual = f'''SELECT {aid}, (b.properties -> '\"data\"'::agtype)::text::jsonb
        FROM suddoe_ontology."HAS_ALIAS" e JOIN suddoe_ontology."Entity" a ON a.id=e.start_id
        JOIN suddoe_ontology."Alias" b ON b.id=e.end_id'''
    pairs.append(("HAS_ALIAS", expected, actual))

    expected = "SELECT p.owner_kind, p.owner_id, to_jsonb(p) FROM corpus.provenance p WHERE p.owner_kind <> '용어'"
    relation_owner = f"({rdata}->>'src_id') || '|' || ({rdata}->>'관계') || '|' || ({rdata}->>'dst_id') || '|' || coalesce({rdata}->>'적용사업','공통')"
    actual = f'''SELECT '개념'::text, {aid}, {sdata} FROM suddoe_ontology."HAS_SOURCE" e
        JOIN suddoe_ontology."Entity" a ON a.id=e.start_id JOIN suddoe_ontology."Source" s ON s.id=e.end_id
        UNION ALL SELECT '규범'::text, {nid}, {sdata} FROM suddoe_ontology."HAS_SOURCE" e
        JOIN suddoe_ontology."Norm" n ON n.id=e.start_id JOIN suddoe_ontology."Source" s ON s.id=e.end_id
        UNION ALL SELECT '관계'::text, {relation_owner}, {sdata} FROM suddoe_ontology."HAS_SOURCE" e
        JOIN suddoe_ontology."Relation" r ON r.id=e.start_id JOIN suddoe_ontology."Source" s ON s.id=e.end_id'''
    pairs.append(("HAS_SOURCE", expected, actual))
    expected = """SELECT to_jsonb(p), jsonb_build_object('doc_id',p.doc_id,'조번호',p.조번호,'항호',p.항호)
        FROM corpus.provenance p WHERE p.owner_kind <> '용어'"""
    actual = f'''SELECT {sdata}, (a.properties -> '\"data\"'::agtype)::text::jsonb
        FROM suddoe_ontology."IN_ARTICLE" e JOIN suddoe_ontology."Source" s ON s.id=e.start_id
        JOIN suddoe_ontology."DocArticle" a ON a.id=e.end_id'''
    pairs.append(("IN_ARTICLE", expected, actual))
    for name, expected, actual in pairs:
        # SQL은 위 정해진 목록만 조립하며 사용자 입력을 받지 않는다.
        diff = cur.execute(f"""WITH expected AS ({expected}), actual AS ({actual})
            SELECT EXISTS (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual),
                   EXISTS (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected),
                   (SELECT count(*) FROM suddoe_ontology."{name}") = (SELECT count(*) FROM actual)""").fetchone()
        if diff != (False, False, True):
            raise AssertionError(f"AGE 간선 방향·끝점·중복 불일치: {name}")
    return len(pairs)
