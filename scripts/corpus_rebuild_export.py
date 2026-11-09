# -*- coding: utf-8 -*-
"""권위 DB의 공개 규정 원문과 청크 정책을 논리키로만 내보낸다.

원천은 로컬 5432/suddoe_ontology 읽기 전용이다. 사용자·기관 정보는 읽지
않는다. 출력은 새 개인 폴더의 새 파일로만 쓰고, 본문은 화면에 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'postgresql://postgres:devpw@127.0.0.1:5432/suddoe_ontology'
FORMAT = 1


def 직렬화(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')


def 해시(value) -> str:
    return hashlib.sha256(직렬화(value)).hexdigest()


def 대상_문서() -> list[str]:
    스키마 = json.loads((ROOT / 'ontology' / 'schema.json').read_text(encoding='utf-8'))
    매핑 = 스키마.get('문서키')
    if not isinstance(매핑, dict) or any(not isinstance(v, str) or not v for v in 매핑.values()):
        raise ValueError('문서키 목록 오류')
    ids = sorted(set(매핑.values()))
    if len(ids) != 27:
        raise ValueError('기준 문서 목록 변경: 재검토 필요')
    return ids


def 추출(conn) -> dict:
    """하나의 읽기 전용 스냅샷에서 27개 활성 문서만 읽는다."""
    ids = 대상_문서()
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            위치 = cur.execute("SELECT current_database() AS db, current_setting('transaction_read_only') AS ro").fetchone()
            if 위치['db'] != 'suddoe_ontology' or 위치['ro'] != 'on':
                raise ValueError('권위 DB 또는 읽기 전용 상태 오류')
            문서들 = cur.execute('''SELECT doc_id, layer, version, 시행일, status, parse_quality,
                extraction, retrieval_scope, index_target FROM corpus.documents
                WHERE doc_id = ANY(%s) ORDER BY doc_id''', (ids,)).fetchall()
            if {x['doc_id'] for x in 문서들} != set(ids) or any(x['status'] != 'active' for x in 문서들):
                raise ValueError('기준 문서 누락 또는 비활성')
            조들 = cur.execute('''SELECT doc_id, "조번호", "조제목", "조순번", "본문", "페이지", "삭제"
                FROM corpus.doc_articles WHERE doc_id = ANY(%s)
                ORDER BY doc_id, "조순번" NULLS LAST, "조번호"''', (ids,)).fetchall()
            청크들 = cur.execute('''SELECT c.doc_id, c."조번호", c."조제목", c."항호", c."페이지",
                c.layer, c.기관id, c.version, c."사업명", c."적용대상", c.retrieval_scope, c.parse_quality, c.text,
                a.doc_id AS article_doc, a."조번호" AS article_no
                FROM corpus.chunks c LEFT JOIN corpus.doc_articles a ON a.article_id=c.article_id
                WHERE c.doc_id = ANY(%s) AND c.status='active'
                ORDER BY c.doc_id, c."조번호", c.chunk_id''', (ids,)).fetchall()
    문서표 = {d['doc_id']: {**d, '조문': []} for d in 문서들}
    조표 = {}
    for a in 조들:
        키 = (a['doc_id'], a['조번호'])
        if 키 in 조표 or a['doc_id'] not in 문서표:
            raise ValueError('조문 논리키 중복 또는 문서 누락')
        row = {**a, '원문해시': hashlib.sha256(a['본문'].encode('utf-8')).hexdigest(), '기존청크': []}
        조표[키] = row
        문서표[a['doc_id']]['조문'].append(row)
    if any(not d['조문'] for d in 문서표.values()):
        raise ValueError('조문 없는 기준 문서')
    for c in 청크들:
        키 = (c['doc_id'], c['조번호'])
        if 키 not in 조표 or c['article_doc'] != 키[0] or c['article_no'] != 키[1]:
            raise ValueError('청크의 조문 논리키 불일치')
        조표[키]['기존청크'].append({k: v for k, v in c.items() if k not in ('doc_id', 'article_doc', 'article_no')})
    payload = {'형식': FORMAT, '기준': '로컬 suddoe_ontology', '문서키': ids,
             '문서': [문서표[i] for i in ids]}
    return {'내용': payload, 'sha256': 해시(payload)}


def 새_파일_쓰기(경로: Path, value: dict) -> None:
    경로 = 경로.resolve()
    if 경로 == ROOT or ROOT in 경로.parents or not 경로.parent.is_dir():
        raise ValueError('저장소 밖에 이미 존재하는 개인 폴더가 필요하다')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    fd = os.open(경로, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(직렬화(value))
    except BaseException:
        경로.unlink(missing_ok=True)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True, type=Path)
    args = ap.parse_args()
    with psycopg.connect(SOURCE, options='-c default_transaction_read_only=on') as conn:
        out = 추출(conn)
    새_파일_쓰기(args.output, out)
    print(json.dumps({'문서수': len(out['내용']['문서']), '조문수': sum(len(x['조문']) for x in out['내용']['문서']),
                      'sha256': out['sha256']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
