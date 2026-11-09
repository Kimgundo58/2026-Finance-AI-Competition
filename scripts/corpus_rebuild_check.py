# -*- coding: utf-8 -*-
"""검색 범위 태그를 임의로 넓히지 않는 순수 함수 검사. DB·모델 호출 없음."""
from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import corpus_rebuild_apply as apply
import corpus_rebuild_export as export
import corpus_rebuild_manifest as manifest
import corpus_rebuild_prepare as p


def main():
    body = '첫째 내용\n둘째 내용'
    분할 = manifest.원문_분할(body, 0, len(body), len, 한도=4)
    assert ''.join(x[2] for x in 분할) == body and all(len(x[2]) <= 4 for x in 분할)
    def chunk(text, scope='검색대상', 대상='창업기업'):
        return {'text': text, 'layer': 'L2', '사업명': ['사업'], '적용대상': 대상,
                'retrieval_scope': scope, 'parse_quality': 'high', '페이지': 1}
    원래 = [chunk('첫째 내용'), chunk('둘째 내용', '참조확장전용')]
    spans = p.정책_구간(body, 원래)
    assert p._고유_구간('앞\n  "자료" 뒤', '앞 “자료” 뒤') == (0, 10)
    assert p._증명_구간('가 나 다', '가나다') == [(0, 1), (2, 3), (4, 5)]
    assert p._증명_구간('가 나', '가나') == [(0, 2), (3, 4)]
    for 본문, 글 in [('가 나 가  나', '가나'), ('가 X 나', '가나'), ('가 가', '가가 가가')]:
        try:
            p._증명_구간(본문, 글)
        except ValueError:
            pass
        else:
            raise AssertionError('공백 외 문자 변경 또는 중복 좌표 허용')
    페이지본문 = '앞 - 1 - 뒤'
    페이지구간 = p.정책_구간(페이지본문, [chunk('앞 뒤')])
    전체정책 = p.정책_구간(페이지본문, [chunk(페이지본문)])
    try:
        p.기존_정책_보존(페이지본문, 전체정책, [('앞 뒤', 전체정책[0][2])])
    except ValueError:
        pass
    else:
        raise AssertionError('새 청크에서 누락된 페이지 번호를 보존으로 판정함')
    try:
        p.새_청크_정책(페이지본문, 페이지본문, 페이지구간)
    except ValueError:
        pass
    else:
        raise AssertionError('정규화에서 지운 페이지 번호에 정책을 부여함')
    assert p.새_청크_정책(body, '첫째 내용', spans)['retrieval_scope'] == '검색대상'
    assert p.새_청크_정책(body, '둘째 내용', spans)['retrieval_scope'] == '참조확장전용'
    p.기존_정책_보존(body, spans, [('첫째 내용', spans[0][2]), ('둘째 내용', spans[1][2])])
    for fn in (
        lambda: p.새_청크_정책(body, body, spans),
        lambda: p.정책_구간(body, [chunk('없는 내용')]),
        lambda: p.정책_구간('반복 반복', [chunk('반복')]),
        lambda: p.정책_구간(body, [chunk('첫째 내용'), chunk('첫째 내용', 대상='주관기관')]),
        lambda: p.새_청크_정책('첫째 내용 새문자', '첫째 내용 새문자', p.정책_구간('첫째 내용 새문자', [chunk('첫째 내용')])),
        lambda: p.정책_구간(body, [chunk('첫째 내용', 대상=None)]),
        lambda: p.기존_정책_보존(body, spans, [('첫째 내용', spans[0][2])]),
    ):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError('모호하거나 범위가 섞인 청크가 통과됨')
    article = {'doc_id': '문서', '조번호': '제1조', '조제목': '제목', '본문': body,
               '원문해시': hashlib.sha256(body.encode('utf-8')).hexdigest(), '기존청크': 원래}
    payload = {'형식': export.FORMAT, '문서키': ['문서'],
              '문서': [{'doc_id': '문서', 'status': 'active', '조문': [article]}]}
    with patch.object(export, '대상_문서', return_value=['문서']):
        value = {'내용': payload, 'sha256': export.해시(payload)}
        m = {'형식': 1, '원천해시': value['sha256'], '표': [{'doc_id': '문서', '조번호': '제1조',
             '본문해시': article['원문해시'], '구간': [{'시작': 0, '끝': len(body), '검색범위': '검색대상',
             '적용대상': '창업기업', '근거': '조문 주체'}]}]}
        with patch.object(manifest, '대상', {('문서', '제1조')}):
            assert manifest.검증(value, m)
            for start, end in ((1, len(body)), (0, len(body) - 1)):
                bad = {**m, '표': [{**m['표'][0], '구간': [{**m['표'][0]['구간'][0], '시작': start, '끝': end}]}]}
                try:
                    manifest.검증(value, bad)
                except ValueError:
                    pass
                else:
                    raise AssertionError('표 본문 누락 허용')
            split = len(body) // 2
            table_chunks = [{'doc_id': '문서', '조번호': '제1조', 'text': body[a:b],
                '표시작': a, '표끝': b, '페이지': None, '적용대상': '창업기업', 'retrieval_scope': '검색대상',
                'layer': 'L2', '사업명': None,
                'embedding': json.dumps([0.0] * 1024), '길이': 1, '용어': {'단어': 1}}
                for a, b in ((0, split), (split, len(body)))]
            table_data = {'형식': 1, '원천해시': value['sha256'], '표매니페스트해시': export.해시(m),
                          '문서키': ['문서'], '청크': table_chunks}
            apply.계획_검증(value, {'내용': table_data, 'sha256': export.해시(table_data)},
                         value['sha256'], export.해시(table_data), m)
            import bm25_index
            with patch.object(bm25_index, '_장_매핑', return_value={}), \
                 patch.object(bm25_index, '_헤더', return_value='제목'), \
                 patch.object(bm25_index, '토큰화', side_effect=lambda texts: [['단어'] for _ in texts]):
                assert apply.BM25_표본_확인(value, {'내용': table_data}) == 2
            missing = {**table_data, '청크': table_chunks[1:]}
            try:
                apply.계획_검증(value, {'내용': missing, 'sha256': export.해시(missing)},
                             value['sha256'], export.해시(missing), m)
            except ValueError:
                pass
            else:
                raise AssertionError('표 청크 누락 허용')
        정보 = {'모델': 'nlpai-lab/KURE-v1', '장치': 'cpu', '코드해시': {k: 'hash' for k in
               ('corpus_ingest.py', 'bm25_index.py', 'retrieve.py', 'corpus_rebuild_manifest.py',
                'corpus_rebuild_export.py', 'corpus_rebuild_prepare.py')},
                '모델파일해시': {'weights': 'modelhash'}}
        sample = {'내용': {'생성정보': 정보}, 'sha256': 'planhash'}
        receipt = {'생성종료코드': 0, '원천해시': value['sha256'], '표매니페스트해시': export.해시(m),
                   '계획해시': 'planhash', '계획파일해시': 'hash', '생성정보': 정보}
        with patch.object(apply, '파일_해시', return_value='hash'), \
             patch.object(p, '모델_파일해시', return_value={'weights': 'modelhash'}):
            apply.생성물_확인(value, m, sample, receipt, receipt_sha=export.해시(receipt), plan_path=None)
            try:
                apply.생성물_확인(value, m, sample, receipt, receipt_sha='0' * 64, plan_path=None)
            except ValueError:
                pass
            else:
                raise AssertionError('다른 생성 확인서 해시 허용')
        assert p.원천_검증(value) is payload
        try:
            p.원천_검증({**value, 'sha256': '0' * 64})
        except ValueError:
            pass
        else:
            raise AssertionError('원천 파일 변조 허용')
        새 = []
        for 글, 정책 in [('첫째 내용', spans[0][2]), ('둘째 내용', spans[1][2])]:
            새.append({'doc_id': '문서', '조번호': '제1조', 'text': 글, **정책,
                     'embedding': json.dumps([0.0] * 1024), '길이': 1, '용어': {'단어': 1}})
        계획내용 = {'형식': 1, '원천해시': value['sha256'], '문서키': ['문서'], '청크': 새}
        계획 = {'내용': 계획내용, 'sha256': export.해시(계획내용)}
        apply.계획_검증(value, 계획, value['sha256'], 계획['sha256'])
        누락 = {**계획내용, '청크': 새[:1]}
        try:
            apply.계획_검증(value, {'내용': 누락, 'sha256': export.해시(누락)}, value['sha256'], export.해시(누락))
        except ValueError:
            pass
        else:
            raise AssertionError('기존 청크 누락 허용')
    for dsn in ('postgresql://postgres@127.0.0.1:5432/suddoe_ontology',
                'postgresql://postgres@127.0.0.1:5462/suddoe_rebuild_test',
                'postgresql://postgres@example.com:5470/suddoe_rebuild_test',
                'postgresql://postgres@127.0.0.1:5470/suddoe'):
        with patch.dict(os.environ, {'SUDDOE_REBUILD_TARGET_DSN': dsn}):
            try:
                apply.대상_DSN()
            except ValueError:
                continue
            raise AssertionError('금지된 DB 대상 허용')
    with patch.dict(os.environ, {'SUDDOE_REBUILD_TARGET_DSN': 'postgresql://postgres@127.0.0.1:5470/suddoe_rebuild_test'}):
        assert apply.대상_DSN()
    try:
        apply.반영(SimpleNamespace(info=SimpleNamespace(host='example.com', port=5470,
                  dbname='suddoe_rebuild_test', transaction_status=0)), {}, {})
    except ValueError:
        pass
    else:
        raise AssertionError('직접 호출의 원격 DB 연결 허용')
    identity = {'db': 'suddoe_rebuild_test', 'system_id': '12345', 'nonce': 'a' * 32, 'count': 1}
    info = SimpleNamespace(port=5470, dbname=identity['db'])
    class Cursor:
        def execute(self, query):
            self.value = (True, False) if 'row_security_active' in query else (identity,)
            return self
        def fetchone(self):
            return self.value
    inspected = 'b' * 64 + '|true|[{"HostIp":"127.0.0.1","HostPort":"5470"}]'
    env = {'SUDDOE_REBUILD_CONTAINER_NAME': 'suddoe-rebuild-test',
           'SUDDOE_REBUILD_CONTAINER_ID': 'b' * 64, 'SUDDOE_REBUILD_NONCE': 'a' * 32,
           'SUDDOE_REBUILD_SYSTEM_ID': '12345'}
    with patch.dict(os.environ, env), patch.object(apply.subprocess, 'run', side_effect=[
        SimpleNamespace(stdout=inspected), SimpleNamespace(stdout=json.dumps(identity))]):
        apply.격리_확인(SimpleNamespace(info=info), Cursor())
    with patch.dict(os.environ, env), patch.object(apply.subprocess, 'run', side_effect=[
        SimpleNamespace(stdout=inspected), SimpleNamespace(stdout=json.dumps({**identity, 'nonce': 'c' * 32}))]):
        try:
            apply.격리_확인(SimpleNamespace(info=info), Cursor())
        except ValueError:
            pass
        else:
            raise AssertionError('다른 DB 식별값 허용')
    print('권위 원문 청크 정책 보존·누락·중복·충돌 검사 통과')


if __name__ == '__main__':
    main()
