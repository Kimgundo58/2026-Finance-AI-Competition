-- 필수: Apache AGE가 포함된 PostgreSQL 17 이미지에서 postgres로 실행한다.
-- 관계형 corpus는 유지한다. 그래프 적재는 ontology_age.동기화가 별도 트랜잭션이 아닌
-- ontology_load의 쓰기 트랜잭션 안에서 수행한다. 운영 실행 전 별도 리허설 필수.
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET LOCAL search_path = ag_catalog, public;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'suddoe_ontology') THEN
        PERFORM ag_catalog.create_graph('suddoe_ontology');
    END IF;
END
$$;

DO $$
DECLARE label_name text;
BEGIN
    FOREACH label_name IN ARRAY ARRAY['Entity', 'Norm', 'Alias', 'Source', 'Relation', 'DocArticle'] LOOP
        IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
                       WHERE g.name = 'suddoe_ontology' AND l.name = label_name) THEN
            PERFORM ag_catalog.create_vlabel('suddoe_ontology', label_name::cstring);
        END IF;
    END LOOP;
    FOREACH label_name IN ARRAY ARRAY['RELATION', 'FROM_NODE', 'TO_NODE', 'TARGETS', 'ADDRESSEE', 'APPROVER', 'EXCEPTION_TO', 'EXCEPTION_TEXT', 'HAS_ALIAS', 'HAS_SOURCE', 'IN_ARTICLE'] LOOP
        IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
                       WHERE g.name = 'suddoe_ontology' AND l.name = label_name) THEN
            PERFORM ag_catalog.create_elabel('suddoe_ontology', label_name::cstring);
        END IF;
    END LOOP;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS age_entity_id ON suddoe_ontology."Entity" ((properties -> '"id"'::agtype));
CREATE UNIQUE INDEX IF NOT EXISTS age_norm_id ON suddoe_ontology."Norm" ((properties -> '"id"'::agtype));
CREATE UNIQUE INDEX IF NOT EXISTS age_alias_id ON suddoe_ontology."Alias" ((properties -> '"id"'::agtype));
CREATE UNIQUE INDEX IF NOT EXISTS age_source_id ON suddoe_ontology."Source" ((properties -> '"id"'::agtype));
CREATE UNIQUE INDEX IF NOT EXISTS age_relation_id ON suddoe_ontology."Relation" ((properties -> '"id"'::agtype));
CREATE UNIQUE INDEX IF NOT EXISTS age_docarticle_id ON suddoe_ontology."DocArticle" ((properties -> '"id"'::agtype));
CREATE INDEX IF NOT EXISTS age_entity_properties ON suddoe_ontology."Entity" USING gin (properties);
CREATE INDEX IF NOT EXISTS age_norm_properties ON suddoe_ontology."Norm" USING gin (properties);
CREATE INDEX IF NOT EXISTS age_alias_properties ON suddoe_ontology."Alias" USING gin (properties);
CREATE INDEX IF NOT EXISTS age_relation_properties ON suddoe_ontology."Relation" USING gin (properties);
CREATE INDEX IF NOT EXISTS age_source_properties ON suddoe_ontology."Source" USING gin (properties);
CREATE INDEX IF NOT EXISTS age_docarticle_properties ON suddoe_ontology."DocArticle" USING gin (properties);

REVOKE ALL ON SCHEMA suddoe_ontology FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA suddoe_ontology FROM PUBLIC;
GRANT USAGE ON SCHEMA ag_catalog, suddoe_ontology TO suddoe_app, suddoe_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA suddoe_ontology TO suddoe_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA suddoe_ontology TO suddoe_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA suddoe_ontology TO suddoe_ingest;

-- 보안 주체는 뷰 소유자가 아닌 호출자다. 내용은 AGE 정점의 data에서 읽는다.
-- 테이블을 바꾸거나 사용자가 임의 Cypher를 뷰에 전달할 수 없다.
CREATE OR REPLACE VIEW corpus.age_concepts WITH (security_invoker = true) AS
SELECT (jsonb_populate_record(NULL::corpus.concepts, (properties -> '"data"'::agtype)::text::jsonb)).*
  FROM suddoe_ontology."Entity" WHERE properties @> '{"kind":"concept"}'::agtype;
CREATE OR REPLACE VIEW corpus.age_concept_labels WITH (security_invoker = true) AS
SELECT (jsonb_populate_record(NULL::corpus.concept_labels, (properties -> '"data"'::agtype)::text::jsonb)).*
  FROM suddoe_ontology."Alias";
CREATE OR REPLACE VIEW corpus.age_concept_relations WITH (security_invoker = true) AS
SELECT (jsonb_populate_record(NULL::corpus.concept_relations, (properties -> '"data"'::agtype)::text::jsonb)).*
  FROM suddoe_ontology."Relation";
CREATE OR REPLACE VIEW corpus.age_norms WITH (security_invoker = true) AS
SELECT (jsonb_populate_record(NULL::corpus.norms, (properties -> '"data"'::agtype)::text::jsonb)).*
  FROM suddoe_ontology."Norm";
CREATE OR REPLACE VIEW corpus.age_provenance WITH (security_invoker = true) AS
SELECT (jsonb_populate_record(NULL::corpus.provenance, (properties -> '"data"'::agtype)::text::jsonb)).*
  FROM suddoe_ontology."Source";
GRANT SELECT ON corpus.age_concepts, corpus.age_concept_labels, corpus.age_concept_relations,
    corpus.age_norms, corpus.age_provenance TO suddoe_app, suddoe_ingest;

-- corpus 변경과 graph 교체를 같은 트랜잭션 잠금으로 직렬화한다.
CREATE TABLE IF NOT EXISTS corpus.ontology_age_state (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    dirty boolean NOT NULL DEFAULT true,
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO corpus.ontology_age_state(id) VALUES (true) ON CONFLICT (id) DO NOTHING;
REVOKE ALL ON corpus.ontology_age_state FROM PUBLIC;
GRANT SELECT ON corpus.ontology_age_state TO suddoe_app, suddoe_ingest;
GRANT UPDATE (dirty, counts, updated_at) ON corpus.ontology_age_state TO suddoe_ingest;

CREATE OR REPLACE FUNCTION corpus.ontology_age_mark_dirty() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, corpus AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('suddoe_ontology_age'));
    UPDATE corpus.ontology_age_state SET dirty = true, updated_at = now() WHERE id = true AND NOT dirty;
    IF NOT FOUND AND NOT EXISTS (SELECT 1 FROM corpus.ontology_age_state WHERE id = true) THEN
        RAISE EXCEPTION 'ontology AGE state missing';
    END IF;
    RETURN NULL;
END
$$;
REVOKE ALL ON FUNCTION corpus.ontology_age_mark_dirty() FROM PUBLIC;
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['concepts', 'concept_labels', 'concept_relations', 'norms', 'provenance'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS ontology_age_dirty ON corpus.%I', table_name);
        EXECUTE format('CREATE TRIGGER ontology_age_dirty AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON corpus.%I FOR EACH STATEMENT EXECUTE FUNCTION corpus.ontology_age_mark_dirty()', table_name);
    END LOOP;
END
$$;
