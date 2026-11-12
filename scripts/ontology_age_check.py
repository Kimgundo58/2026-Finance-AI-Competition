# -*- coding: utf-8 -*-
"""AGE 적재 동등성·권한·롤백 점검. 폐기 DB에서만 실행.

기본 대상 127.0.0.1:5462/suddoe. --버리는-DB가 있으면 로컬 suddoe_ci도 허용.
AGE_TEST_APP_DSN, AGE_TEST_INGEST_DSN을 명시한다. 모든 변경 시험은 롤백한다.
"""
from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ontology_age  # noqa: E402
from ontology_age_validate import 간선_동등성 as _간선_동등성  # noqa: E402
import ontology_lookup  # noqa: E402
import ontology_norm  # noqa: E402


def _dsn(name, user, disposable):
    value = os.environ.get(name, "")
    try:
        u = urlsplit(value)
        target = (u.hostname, u.port, u.path)
        allowed = target == ("127.0.0.1", 5462, "/suddoe") or (
            disposable and u.hostname in ("localhost", "127.0.0.1", "::1") and
            u.port is not None and u.path == "/suddoe_ci")
        if (u.scheme not in ("postgres", "postgresql") or not allowed or
                u.username != user or u.query):
            raise ValueError
    except ValueError:
        raise SystemExit(f"{name}: 폐기 DB 대상만 허용한다") from None
    return value, target


def _ready_failure(cur):
    try:
        ontology_age.준비_확인(cur)
    except RuntimeError:
        return
    raise AssertionError("준비되지 않은 그래프를 허용했다")


def _상황_후보_동등성(cur):
    """원천 SQL과 AGE 경로의 순서·상한 결과를 사업별로 비교한다. 판정이 쓰는 규범은 AI대조 이상(ontology_norm.확정_검수단계, 2026-11-13)이라
    생성 칼럼 verified(AI검증 이상)가 아니라 검수단계로 고른다."""
    reference = """SELECT c.concept_id, c.pref_label,
                  count(*) FILTER (WHERE n.종류 = ANY(%s)) AS 먼저수
             FROM corpus.norms n
             CROSS JOIN LATERAL unnest(n.대상) AS d(id)
             JOIN corpus.concepts c ON c.concept_id = d.id
            WHERE n.검수단계 = ANY(%s) AND NOT n.해석미결 AND n.종류 <> '우선순위'
              AND (n.적용사업 IS NULL OR n.적용사업 = ANY(%s))
              AND c.종류 = ANY(%s) AND (c.적용사업 IS NULL OR c.적용사업 = ANY(%s))
            GROUP BY c.concept_id, c.pref_label
            ORDER BY (count(*) FILTER (WHERE n.종류 = ANY(%s)) = 0), 3 DESC, c.concept_id"""
    programs = list(ontology_lookup._스키마()["사업키"].values())
    for program in [None, *programs]:
        businesses = [program] if program else []
        kinds = list(ontology_lookup.상황_먼저_종류)
        params = (kinds, list(ontology_norm.확정_검수단계), businesses, list(ontology_lookup.상황_종류), businesses, kinds)
        rows = cur.execute(reference, params).fetchall()
        expected = [{"id": row[0], "정식명": row[1]} for row in rows]
        assert ontology_lookup.상황_후보(cur, program, None) == expected, f"상황 후보 차이: {program}"
        assert ontology_lookup.상황_후보(cur, program) == expected[:ontology_lookup.상황후보_상한]



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--버리는-DB", action="store_true", dest="disposable")
    parser.add_argument("--읽기만", action="store_true", dest="read_only")
    args = parser.parse_args()
    app, app_target = _dsn("AGE_TEST_APP_DSN", "suddoe_app", args.disposable)
    ingest, ingest_target = _dsn("AGE_TEST_INGEST_DSN", "suddoe_ingest", args.disposable)
    if app_target != ingest_target:
        raise SystemExit("앱과 적재 계정의 대상이 다르다")
    tables = (("concepts", "age_concepts"), ("concept_labels", "age_concept_labels"),
              ("concept_relations", "age_concept_relations"), ("norms", "age_norms"),
              ("provenance", "age_provenance"))
    with psycopg.connect(app) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout='60s'")
            baseline = ontology_age.준비_버전(cur)
            assert baseline and baseline == ontology_age.준비_확인(cur)
            for table, view in tables:
                where = " WHERE owner_kind <> '용어'" if table == "provenance" else ""
                # 식별자는 위의 고정 목록에서만 온다.
                diff = cur.execute(f"""SELECT
                    EXISTS (SELECT * FROM corpus.{table}{where} EXCEPT ALL SELECT * FROM corpus.{view}),
                    EXISTS (SELECT * FROM corpus.{view} EXCEPT ALL SELECT * FROM corpus.{table}{where})""").fetchone()
                assert diff == (False, False), f"AGE 내용 차이: {table}"
            edge_count = _간선_동등성(cur)
            cur.execute("SET LOCAL statement_timeout='20s'")
            _상황_후보_동등성(cur)
    print(f"정상 상태·5표·{edge_count}종 간선 동등성 통과", flush=True)
    if args.read_only:
        return 0

    with psycopg.connect(ingest) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='30s'")
            cur.execute("UPDATE corpus.norms SET 검수단계=검수단계 WHERE false")
            _ready_failure(cur)
        conn.rollback()
        with conn.cursor() as cur:
            assert ontology_age.준비_버전(cur) == baseline
            old = cur.execute("SELECT norm_id, 검수단계 FROM corpus.norms WHERE 검수단계 <> '재검토' ORDER BY norm_id LIMIT 1").fetchone()
            assert old is not None
            cur.execute("UPDATE corpus.norms SET 검수단계='재검토' WHERE norm_id=%s", (old[0],))
            cur.execute("LOAD '$libdir/plugins/age'")
            cur.execute("SET LOCAL search_path=ag_catalog,public")
            cur.execute("SELECT * FROM ag_catalog.cypher('suddoe_ontology', $$ CREATE (:Norm {id:'__age_check_atomic__',kind:'norm',data:{}}) $$) AS (n agtype)")
            _ready_failure(cur)
        conn.rollback()
        with conn.cursor() as cur:
            assert cur.execute("SELECT 검수단계 FROM corpus.norms WHERE norm_id=%s", (old[0],)).fetchone()[0] == old[1]
            assert ontology_age.준비_버전(cur) == baseline
            cur.execute("UPDATE corpus.ontology_age_state SET counts='{}'::jsonb WHERE id=true")
            _ready_failure(cur)
        conn.rollback()
        with conn.cursor() as cur:
            assert ontology_age.준비_버전(cur) == baseline
            cur.execute("LOAD '$libdir/plugins/age'")
            cur.execute("SET LOCAL search_path=ag_catalog,public")
            cur.execute("SELECT * FROM ag_catalog.cypher('suddoe_ontology', $$ CREATE (:Norm {id:'__age_check_rollback__',kind:'norm',data:{}}) $$) AS (n agtype)")
            _ready_failure(cur)
        conn.rollback()
        with conn.cursor() as cur:
            assert ontology_age.준비_버전(cur) == baseline
            # 간선 수는 유지하면서 방향만 뒤집어, 단순 count가 아닌 끝점 비교를 시험한다.
            cur.execute("LOAD '$libdir/plugins/age'")
            cur.execute("SET LOCAL search_path=ag_catalog,extensions,public")
            cur.execute('''UPDATE suddoe_ontology."TARGETS" SET start_id=end_id, end_id=start_id
                           WHERE id=(SELECT id FROM suddoe_ontology."TARGETS" LIMIT 1)''')
            assert cur.rowcount == 1
            try:
                _간선_동등성(cur)
            except AssertionError as exc:
                if "TARGETS" not in str(exc):
                    raise
            else:
                raise AssertionError("방향이 뒤집힌 TARGETS를 탐지하지 못했다")
        conn.rollback()
        with conn.cursor() as cur:
            assert ontology_age.준비_버전(cur) == baseline
    print("dirty·상태 누락·간선 방향 변조·원천과 그래프 변경의 동시 롤백 통과", flush=True)

    with psycopg.connect(app) as conn:
        with conn.cursor() as cur:
            cur.execute("LOAD '$libdir/plugins/age'")
            cur.execute("SET LOCAL search_path=ag_catalog,public")
            try:
                cur.execute("SELECT * FROM ag_catalog.cypher('suddoe_ontology', $$ CREATE (:Norm {id:'__age_check_denied__'}) $$) AS (n agtype)")
            except psycopg.errors.InsufficientPrivilege:
                pass
            else:
                raise AssertionError("앱 역할이 그래프를 변경했다")
        conn.rollback()
    print("앱 쓰기 거부 통과", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
