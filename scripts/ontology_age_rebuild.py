#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""빈 AGE 그래프를 복원된 관계형 원천에서 재구축하고 같은 트랜잭션에서 검증한다.

SUDDOE_INGEST_DSN을 명시해야 한다. 복원 스크립트 전용이며 자격 정보를 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
import ontology_age
from ontology_age_validate import 간선_동등성


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore-token', required=True)
    args = parser.parse_args()
    dsn = os.environ.get('SUDDOE_INGEST_DSN', '')
    if not dsn:
        raise RuntimeError('SUDDOE_INGEST_DSN 필요')
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            if cur.execute('SELECT current_user').fetchone()[0] != 'suddoe_ingest':
                raise RuntimeError('수집 역할이 아님')
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('suddoe_ontology_age'))")
            state = cur.execute("SELECT dirty, counts->>'restore_nonce' FROM corpus.ontology_age_state WHERE id=true").fetchone()
            if state != (True, args.restore_token):
                raise RuntimeError('복원 대상 또는 대기 상태가 일치하지 않음')
            if cur.execute('SELECT count(*) FROM suddoe_ontology."_ag_label_vertex"').fetchone()[0] or cur.execute('SELECT count(*) FROM suddoe_ontology."_ag_label_edge"').fetchone()[0]:
                raise RuntimeError('복원 대상 그래프가 비어 있지 않음')
            counts = ontology_age.동기화(cur)
            for source, view in (('concepts', 'age_concepts'), ('concept_labels', 'age_concept_labels'),
                                 ('concept_relations', 'age_concept_relations'), ('norms', 'age_norms'),
                                 ('provenance', 'age_provenance')):
                where = " WHERE owner_kind <> '용어'" if source == 'provenance' else ''
                row = cur.execute(f'''SELECT EXISTS (SELECT * FROM corpus.{source}{where} EXCEPT ALL SELECT * FROM corpus.{view}),
                    EXISTS (SELECT * FROM corpus.{view} EXCEPT ALL SELECT * FROM corpus.{source}{where})''').fetchone()
                if row != (False, False):
                    raise RuntimeError(f'AGE 원천 대조 실패: {source}')
            간선수 = 간선_동등성(cur)
            ontology_age.준비_확인(cur)
    print(f'AGE 원천 5표·{간선수}종 간선 재구축 및 검증 완료:', counts)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'AGE 재구축 실패: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
