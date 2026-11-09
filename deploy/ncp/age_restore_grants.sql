-- pg_restore --no-privileges 뒤 수집 역할의 기존 최소 권한을 복구한다.
-- db/init/18_corpus_ingest.sql, 19_ontology.sql에 대응한다. 비밀번호는 변경하지 않는다.
GRANT USAGE ON SCHEMA corpus, tenant, extensions TO suddoe_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA corpus TO suddoe_ingest;
GRANT INSERT, UPDATE, DELETE ON corpus.documents, corpus.doc_articles, corpus.chunks,
    corpus.chunk_terms, corpus.chunk_len, corpus.refs, corpus.ingest_jobs,
    corpus.concepts, corpus.concept_labels, corpus.concept_relations, corpus.norms,
    corpus.provenance, corpus.terminology TO suddoe_ingest;
GRANT USAGE ON SEQUENCE corpus.doc_articles_article_id_seq, corpus.chunks_chunk_id_seq,
    corpus.refs_ref_id_seq TO suddoe_ingest;
GRANT MAINTAIN ON corpus.term_df TO suddoe_ingest;
GRANT DELETE ON tenant.judge_cache TO suddoe_ingest;
GRANT SELECT (article_id, concept_ids), UPDATE (concept_ids) ON tenant.l3_articles TO suddoe_ingest;
