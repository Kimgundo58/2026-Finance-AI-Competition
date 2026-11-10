# -*- coding: utf-8 -*-
"""검토된 검색 자료를 지정된 로컬 격리 복원 DB에만 원자적으로 반영한다.

운영·5432/5462 및 기본 DSN은 허용하지 않는다. 기존 결정의 인용 JSON과
기존 청크 행·ID는 남기지만, 교체된 조문 ID로 과거 본문을 재현할 수는 없다.
실행 전 최신 운영 DB와 첨부의 별도 복구용 백업이 필요하다.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import math
import os
import re
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import corpus_rebuild_export as export
import corpus_rebuild_prepare as prepare
import corpus_rebuild_manifest as 표범위


def 대상_DSN() -> str:
    dsn = os.environ.get('SUDDOE_REBUILD_TARGET_DSN', '')
    설정 = conninfo_to_dict(dsn)
    if (설정.get('host') != '127.0.0.1'
            or not re.fullmatch(r'suddoe_rebuild_[a-z0-9_]+', 설정.get('dbname', ''))
            or not 설정.get('port', '').isdigit() or not 5470 <= int(설정['port']) <= 5499
            or any(k in 설정 for k in ('service', 'hostaddr', 'options'))):
        raise ValueError('허용되지 않은 격리 대상 DB')
    return dsn


def 격리_확인(conn, cur):
    """Docker 직접 조회와 연결된 DB의 복원 식별값을 독립 비교한다."""
    name = os.environ.get('SUDDOE_REBUILD_CONTAINER_NAME', '')
    container_id = os.environ.get('SUDDOE_REBUILD_CONTAINER_ID', '')
    nonce = os.environ.get('SUDDOE_REBUILD_NONCE', '')
    system_id = os.environ.get('SUDDOE_REBUILD_SYSTEM_ID', '')
    if (not re.fullmatch(r'suddoe-rebuild-[a-zA-Z0-9_-]+', name)
            or not re.fullmatch(r'[0-9a-f]{64}', container_id)
            or not re.fullmatch(r'[0-9a-f]{32}', nonce)
            or not re.fullmatch(r'[0-9]+', system_id)):
        raise ValueError('격리 식별값 누락 또는 형식 오류')
    inspected = subprocess.run(
        ['docker', 'inspect', '--format', '{{.Id}}|{{.State.Running}}|{{json (index .NetworkSettings.Ports "5432/tcp")}}', name],
        check=True, capture_output=True, text=True, timeout=10).stdout.strip().split('|', 2)
    if len(inspected) != 3 or inspected[0] != container_id or inspected[1] != 'true':
        raise ValueError('격리 컨테이너 불일치')
    ports = json.loads(inspected[2])
    if ports != [{'HostIp': '127.0.0.1', 'HostPort': str(conn.info.port)}]:
        raise ValueError('격리 포트 바인딩 불일치')
    query = "SELECT json_build_object('db',current_database(),'system_id',(pg_control_system()).system_identifier::text,'nonce',(SELECT min(nonce) FROM public.suddoe_rebuild_identity),'count',(SELECT count(*) FROM public.suddoe_rebuild_identity))::text"
    direct = json.loads(subprocess.run(
        ['docker', 'exec', container_id, 'psql', '-X', '-U', 'postgres', '-d', conn.info.dbname,
         '-v', 'ON_ERROR_STOP=1', '-A', '-t', '-c', query],
        check=True, capture_output=True, text=True, timeout=10).stdout.strip())
    linked = cur.execute(query).fetchone()[0]
    if isinstance(linked, str):
        linked = json.loads(linked)
    expected = {'db': conn.info.dbname, 'system_id': system_id, 'nonce': nonce, 'count': 1}
    if direct != expected or linked != expected:
        raise ValueError('격리 DB 식별값 불일치')
    superuser, active_rls = cur.execute("SELECT r.rolsuper, row_security_active('tenant.judge_cache') FROM pg_roles r WHERE r.rolname=current_user").fetchone()
    if not superuser or active_rls:
        raise ValueError('캐시 전체 확인을 위한 관리자 권한 부족')


def 계획_검증(원천, 계획, 원천해시, 계획해시, 매니페스트=None):
    data = prepare.원천_검증(원천)
    if 원천.get('sha256') != 원천해시 or 계획.get('sha256') != 계획해시:
        raise ValueError('검토된 원천 또는 계획 해시 불일치')
    p = 계획.get('내용')
    if (not isinstance(p, dict) or export.해시(p) != 계획해시 or p.get('형식') != 1
            or p.get('원천해시') != 원천해시 or p.get('문서키') != data['문서키']):
        raise ValueError('검색 자료 계획 형식 또는 해시 오류')
    조표 = {(a['doc_id'], a['조번호']): a for d in data['문서'] for a in d['조문']}
    표들 = 표범위.검증(원천, 매니페스트) if 매니페스트 is not None else {}
    if 표들 and p.get('표매니페스트해시') != export.해시(매니페스트):
        raise ValueError('표 범위 매니페스트 해시 불일치')
    표청크 = {}
    새범위: dict[tuple, list] = {}
    for chunk in p.get('청크') or []:
        key = (chunk.get('doc_id'), chunk.get('조번호'))
        if key not in 조표 or not isinstance(chunk.get('text'), str):
            raise ValueError('검색 자료 조문 키 오류')
        원본문 = 조표[key]['본문']
        if key in 표들:
            left, right = chunk.get('표시작'), chunk.get('표끝')
            if (type(left) is not int or type(right) is not int or not 0 <= left < right <= len(원본문)
                    or 원본문[left:right] != chunk['text'] or chunk.get('페이지') is not None):
                raise ValueError('표 청크 원문 좌표 오류')
            spans = [s for s in 표들[key]['구간'] if s['시작'] <= left and right <= s['끝']]
            if (len(spans) != 1 or chunk.get('적용대상') != spans[0]['적용대상']
                    or chunk.get('retrieval_scope') != spans[0]['검색범위']):
                raise ValueError('표 청크 정책 경계 오류')
            표청크.setdefault(key, []).append((left, right))
        else:
            구간 = prepare.정책_구간(원본문, 조표[key].get('기존청크') or [])
            정책 = prepare.새_청크_정책(원본문, chunk['text'], 구간)
            if any(chunk.get(k) != v for k, v in 정책.items()):
                raise ValueError('검색 범위 정책 변경')
            새범위.setdefault(key, []).append((chunk['text'], 정책))
        벡터 = json.loads(chunk.get('embedding') or 'null')
        if (not isinstance(벡터, list) or len(벡터) != 1024
                or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in 벡터)):
            raise ValueError('임베딩 형식 오류')
        용어 = chunk.get('용어')
        if (not isinstance(용어, dict) or any(not isinstance(k, str) or type(v) is not int or v <= 0 for k, v in 용어.items())
                or type(chunk.get('길이')) is not int or chunk['길이'] != sum(용어.values())):
            raise ValueError('BM25 색인 형식 오류')
    if not p.get('청크'):
        raise ValueError('검색 자료 없음')
    for key, a in 조표.items():
        if key in 표들:
            end = 0
            for left, right in sorted(표청크.get(key, [])):
                if left != end:
                    raise ValueError('표 청크 누락 또는 중복')
                end = right
            if end != len(a['본문']):
                raise ValueError('표 본문 전체가 청킹되지 않음')
            continue
        old = prepare.정책_구간(a['본문'], a.get('기존청크') or [])
        prepare.기존_정책_보존(a['본문'], old, 새범위.get(key, []))
    return data, p


def 파일_해시(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def 생성물_확인(source, manifest, plan, receipt, *, receipt_sha, plan_path):
    if export.해시(receipt) != receipt_sha:
        raise ValueError('검토한 생성 확인서 해시 불일치')
    if (receipt.get('생성종료코드') != 0 or receipt.get('원천해시') != source.get('sha256')
            or receipt.get('표매니페스트해시') != export.해시(manifest)
            or receipt.get('계획해시') != plan.get('sha256')
            or receipt.get('계획파일해시') != 파일_해시(plan_path)
            or receipt.get('생성정보') != (plan.get('내용') or {}).get('생성정보')):
        raise ValueError('생성 확인서와 자료 불일치')
    정보 = receipt.get('생성정보') or {}
    if 정보.get('모델') != 'nlpai-lab/KURE-v1' or 정보.get('장치') != 'cpu':
        raise ValueError('승인된 오프라인 모델 정보 불일치')
    코드해시 = 정보.get('코드해시') or {}
    if set(코드해시) != {'corpus_ingest.py', 'bm25_index.py', 'retrieve.py',
                        'corpus_rebuild_manifest.py', 'corpus_rebuild_export.py', 'corpus_rebuild_prepare.py'}:
        raise ValueError('생성 코드 해시 목록 불일치')
    for filename, expected in 코드해시.items():
        if not re.fullmatch(r'[a-zA-Z0-9_]+\.py', filename):
            raise ValueError('생성 코드 파일 이름 오류')
        if 파일_해시(Path(__file__).parent / filename) != expected:
            raise ValueError('생성 이후 코드 변경')
    if not 정보.get('코드해시') or not 정보.get('모델파일해시'):
        raise ValueError('생성 코드 또는 모델 해시 누락')
    if prepare.모델_파일해시() != 정보['모델파일해시']:
        raise ValueError('생성 이후 모델 파일 변경')


def BM25_표본_확인(source, plan):
    from collections import Counter
    import bm25_index
    docs = {d['doc_id']: d for d in source['내용']['문서']}
    headings = {k: bm25_index._장_매핑(d['조문']) for k, d in docs.items()}
    articles = {(a['doc_id'], a['조번호']): a for d in docs.values() for a in d['조문']}
    chunks = plan['내용']['청크']
    indices = {0, len(chunks)//2, len(chunks)-1}
    for key in 표범위.대상:
        idx = next((i for i, c in enumerate(chunks) if (c['doc_id'], c['조번호']) == key), None)
        if idx is None:
            raise ValueError('표 검색 자료 표본 누락')
        indices.add(idx)
    inputs = []
    for i in sorted(indices):
        c = chunks[i]
        a = articles[(c['doc_id'], c['조번호'])]
        header = bm25_index._헤더(c['layer'], c['사업명'], c['doc_id'],
                                headings[c['doc_id']].get(c['조번호'], ''), c['조번호'], a['조제목'])
        inputs.append(f"{header}\n{c['text']}")
    tokens = bm25_index.토큰화(inputs)
    for i, actual in zip(sorted(indices), tokens):
        if chunks[i]['길이'] != len(actual) or chunks[i]['용어'] != dict(Counter(actual)):
            raise ValueError('BM25 표본 재계산 불일치')
    return len(indices)


def _refs_지문(cur):
    return cur.execute('''SELECT count(*), md5(coalesce(string_agg(md5(row_to_json(r)::text), '' ORDER BY ref_id), ''))
        FROM corpus.refs r''').fetchone()


def _참조_표현_진단(cur, 조표, ids):
    """기존 참조를 바꾸지 않고 권위 원문 출현 여부만 집계한다."""
    counts = {'출현': 0, '미출현': 0, '비교불가': 0}
    rows = cur.execute('SELECT src_doc_id, src_조번호, 참조문자열 FROM corpus.refs WHERE src_doc_id=ANY(%s)', (ids,)).fetchall()
    for doc_id, number, expression in rows:
        article = 조표.get((doc_id, number))
        if not article or not expression or not article.get('본문'):
            counts['비교불가'] += 1
            continue
        # build_refs.scan 과 같이 공백 차이를 제거한 비교는 진단일 뿐 유효성 판정이 아니다.
        ref = re.sub(r'\s+', '', expression)
        body = re.sub(r'\s+', '', article['본문'])
        counts['출현' if ref in body else '미출현'] += 1
    return counts


def _참조_고아수(cur, ids):
    return cur.execute('''SELECT count(*) FROM corpus.refs r
        LEFT JOIN corpus.doc_articles a ON a.doc_id=r.src_doc_id AND a."조번호"=r.src_조번호
        LEFT JOIN corpus.documents d ON d.doc_id=r.dst_doc_id
        LEFT JOIN corpus.doc_articles b ON b.doc_id=r.dst_doc_id AND b."조번호"=r.dst_조번호
        WHERE r."해결상태" IN ('resolved','remapped')
          AND (r.src_doc_id = ANY(%s) OR r.dst_doc_id = ANY(%s))
          AND (a.article_id IS NULL OR d.doc_id IS NULL OR (r.dst_조번호 IS NOT NULL AND b.article_id IS NULL))''',
        (ids, ids)).fetchone()[0]


def _시퀀스_확인(cur):
    seq = cur.execute("SELECT pg_get_serial_sequence('corpus.chunks','chunk_id')").fetchone()[0]
    if not seq or len(seq.split('.')) != 2:
        raise ValueError('청크 시퀀스 식별 실패')
    schema, name = (s.strip('"') for s in seq.split('.'))
    last, called = cur.execute(sql.SQL('SELECT last_value, is_called FROM {}').format(sql.Identifier(schema, name))).fetchone()
    최대 = cur.execute('SELECT coalesce(max(chunk_id), 0) FROM corpus.chunks').fetchone()[0]
    if (last + (1 if called else 0)) <= 최대:
        raise ValueError('청크 시퀀스 충돌 위험')


def 계획_반영_코어(conn, 원천, 계획, *, 대상확인):
    """검증된 자료만 받으며 활성 트랜잭션에서 호출자의 대상 확인을 수행한다."""
    if not callable(대상확인) or int(conn.info.transaction_status) != 2:
        raise ValueError('대상 확인과 활성 트랜잭션 필요')
    ids = 원천['문서키']
    조표 = {(a['doc_id'], a['조번호']): a for d in 원천['문서'] for a in d['조문']}
    with conn.cursor() as cur:
        대상확인(conn, cur)
        cur.execute('''LOCK TABLE corpus.documents, corpus.doc_articles, corpus.chunks,
            corpus.chunk_terms, corpus.chunk_len, corpus.refs, tenant.judge_cache IN SHARE ROW EXCLUSIVE MODE''')
        캐시수 = cur.execute('SELECT count(*) FROM tenant.judge_cache').fetchone()[0]
        cur.execute('DELETE FROM tenant.judge_cache')
        if cur.execute('SELECT count(*) FROM tenant.judge_cache').fetchone()[0] != 0:
            raise ValueError('판정 캐시 전체 삭제 검증 실패')
        docs = cur.execute('''SELECT doc_id, layer, version, 시행일, status, parse_quality,
            extraction, retrieval_scope, index_target FROM corpus.documents
            WHERE doc_id = ANY(%s) ORDER BY doc_id''', (ids,)).fetchall()
        srcdocs = 원천['문서']
        if len(docs) != len(srcdocs):
            raise ValueError('대상 문서 누락')
        keys = ('doc_id', 'layer', 'version', '시행일', 'status', 'parse_quality', 'extraction', 'retrieval_scope', 'index_target')
        for row, source in zip(docs, srcdocs):
            if any((str(a) if k == '시행일' and a is not None else a) != source.get(k) for k, a in zip(keys, row)):
                raise ValueError('대상 문서 상태 또는 메타 불일치')
        rows = cur.execute('''SELECT article_id, doc_id, "조번호", "조제목", "조순번", "페이지", "삭제"
            FROM corpus.doc_articles WHERE doc_id=ANY(%s)''', (ids,)).fetchall()
        대상조 = {(r[1], r[2]): r for r in rows}
        if set(대상조) != set(조표):
            raise ValueError('대상 조문의 논리키 집합 불일치')
        for key, a in 조표.items():
            r = 대상조[key]
            if (r[3], r[4], r[5], r[6]) != (a['조제목'], a['조순번'], a['페이지'], a['삭제']):
                raise ValueError('대상 조문 메타 불일치')
        # 기존 원천에도 있는 미해결 목적 조문은 보존한다. 이 작업은 참조를
        # 수정하지 않으며, 논리키·전체 참조 지문과 미해결 수가 바뀌면 실패한다.
        참조고아전 = _참조_고아수(cur, ids)
        참조진단 = _참조_표현_진단(cur, 조표, ids)
        참조전 = _refs_지문(cur)
        _시퀀스_확인(cur)
        # 기존 article_id와 chunk_id는 변경하거나 재사용하지 않는다.
        for key, a in 조표.items():
            cur.execute('UPDATE corpus.doc_articles SET "본문"=%s WHERE article_id=%s', (a['본문'], 대상조[key][0]))
        cur.execute('DELETE FROM corpus.chunk_terms WHERE chunk_id IN (SELECT chunk_id FROM corpus.chunks WHERE doc_id=ANY(%s))', (ids,))
        cur.execute('DELETE FROM corpus.chunk_len WHERE chunk_id IN (SELECT chunk_id FROM corpus.chunks WHERE doc_id=ANY(%s))', (ids,))
        cur.execute("UPDATE corpus.chunks SET status='superseded' WHERE doc_id=ANY(%s) AND status='active'", (ids,))
        for c in 계획['청크']:
            article_id = 대상조[(c['doc_id'], c['조번호'])][0]
            chunk_id = cur.execute('''INSERT INTO corpus.chunks
                (doc_id,article_id,layer,기관id,parse_quality,version,status,retrieval_scope,"조번호","조제목","항호","페이지","사업명","적용대상",text,embedding)
                VALUES (%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,%s,%s,%s::extensions.vector)
                RETURNING chunk_id''', (c['doc_id'], article_id, c['layer'], c['기관id'], c['parse_quality'],
                c['version'], c['retrieval_scope'],
                c['조번호'], c['조제목'], c['항호'], c['페이지'], c['사업명'], c['적용대상'], c['text'], c['embedding'])).fetchone()[0]
            cur.execute('INSERT INTO corpus.chunk_len (chunk_id,dl) VALUES (%s,%s)', (chunk_id, c['길이']))
            cur.executemany('INSERT INTO corpus.chunk_terms (chunk_id,term,tf) VALUES (%s,%s,%s)',
                            [(chunk_id, term, tf) for term, tf in c['용어'].items()])
        cur.execute('REFRESH MATERIALIZED VIEW corpus.term_df')
        if 참조전 != _refs_지문(cur) or _참조_고아수(cur, ids) != 참조고아전:
            raise ValueError('참조 보존 검증 실패')
        새수 = cur.execute("SELECT count(*) FROM corpus.chunks WHERE doc_id=ANY(%s) AND status='active'", (ids,)).fetchone()[0]
        if 새수 != len(계획['청크']):
            raise ValueError('활성 청크 수 불일치')
    return {'문서수': len(ids), '조문수': len(조표), '활성청크수': 새수, '삭제한임시캐시수': 캐시수,
            '참조표현진단': 참조진단, '보존한기존참조미해결수': 참조고아전}


def 반영(conn, 원천, 계획):
    """로컬 격리 대상만 허용하며, 사용자 데이터 전후 검사는 호출자 책임이다."""
    if (conn.info.host != '127.0.0.1'
            or not 5470 <= conn.info.port <= 5499
            or not re.fullmatch(r'suddoe_rebuild_[a-z0-9_]+', conn.info.dbname or '')
            or int(conn.info.transaction_status) != 0):
        raise ValueError('격리 DB 연결이 아님')
    with conn.transaction():
        return 계획_반영_코어(conn, 원천, 계획, 대상확인=격리_확인)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, type=Path)
    ap.add_argument('--source-sha256', required=True)
    ap.add_argument('--manifest', required=True, type=Path)
    ap.add_argument('--manifest-sha256', required=True)
    ap.add_argument('--plan', required=True, type=Path)
    ap.add_argument('--receipt', required=True, type=Path)
    ap.add_argument('--receipt-sha256', required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if not args.apply:
        raise SystemExit('DB 반영은 별도 검토 후 --apply가 필요합니다')
    source = json.loads(args.source.read_text(encoding='utf-8'))
    if source.get('sha256') != args.source_sha256:
        raise ValueError('검토된 원천 해시 불일치')
    # 검토된 생성 기록과 원천·계획을 확인하고 임베딩을 다시 계산하지 않는다.
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    if export.해시(manifest) != args.manifest_sha256:
        raise ValueError('검토된 표 범위 해시 불일치')
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    receipt = json.loads(args.receipt.read_text(encoding='utf-8'))
    생성물_확인(source, manifest, plan, receipt, receipt_sha=args.receipt_sha256, plan_path=args.plan)
    원천, 준비 = 계획_검증(source, plan, args.source_sha256, plan['sha256'], manifest)
    표본수 = BM25_표본_확인(source, plan)
    대상_DSN()  # 직접 실행도 로컬 DSN 제한을 우회하지 않는다.
    raise SystemExit('사용자 데이터 보존 감사가 필요한 작업입니다. 검증된 전용 runner를 사용하세요.')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # 원문, 연결 정보, 데이터베이스 오류 문자열은 출력하지 않는다.
        raise SystemExit(f'재구축 중단: {type(e).__name__}') from None
