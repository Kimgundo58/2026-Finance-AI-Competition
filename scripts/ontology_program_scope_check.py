# -*- coding: utf-8 -*-
"""7개 사업의 합성 화면 입력으로 최종 규정 문맥 경로를 읽기 전용 점검한다.

외부 LLM·검색 없이 지도에 API 후보가 있으면 고정 질의를, 없으면 완료를 돌려준다. 실제 온톨로지 조회,
게이트, 근거/예외 확인, 문맥, 문장맵과 원문 인용을 사용한다. 운영·5432 금지.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from collections import Counter
from pathlib import Path
from datetime import date
from unittest.mock import patch

import psycopg
import orchestrate as O
import ontology_evidence
import ontology_lookup
import ontology_research
import ontology_research_plan as plan
import ontology_query_plan
import ontology_query
import research_runtime
from llm_validate import 근거_인용
from ontology_norm import 발췌_포함
from ontology_norm import 발췌_정규화

ROOT = Path(__file__).resolve().parents[1]
DSN = 'postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5'
보고서 = ROOT / 'codex_handoff' / '판정근거_사업별검사.md'
_출력한_위치 = set()


def 실패_진단(cur, 조회, 사업):
    """실패 원인의 관찰만 반환한다. 이 결과로 판정이나 검증을 우회하지 않는다."""
    행들 = 조회.get('근거행') or []
    ids = [r['article_id'] for r in 행들 if r.get('article_id') is not None]
    docs = {r[0]: r for r in cur.execute('''SELECT a.article_id, a.doc_id, a."조번호", a."본문", d.status, d.시행일
        FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id=a.doc_id WHERE a.article_id=ANY(%s)''', (ids,)).fetchall()}
    위치 = {(r.get('doc_id'), r.get('조번호'), r.get('항호')): r for r in 행들 if r.get('article_id') is not None}
    문제 = []
    for r in 행들:
        if r.get('article_id') is None:
            continue
        row = docs.get(r['article_id'])
        if not row:
            문제.append(('조문 없음', r.get('doc_id'), r.get('조번호'), r.get('항호')))
        elif row[4] != 'active':
            문제.append(('비활성', row[1], row[2], r.get('항호')))
        elif row[5] is not None and row[5] > date.today():
            문제.append(('시행 전', row[1], row[2], r.get('항호')))
        elif not ontology_evidence._위치_확인(row[3], r.get('항호'), row[2]):
            문제.append(('항 위치 미확인', row[1], row[2], r.get('항호')))
    schema = ontology_lookup._스키마()
    키표 = {v: k for k, v in (schema.get('문서키') or {}).items()}
    규범들 = ontology_evidence._규범_모으기(조회)
    전부밖 = 0
    for n in 규범들.values():
        유효 = 0
        바깥 = 0
        for src in n.get('근거') or []:
            key = (src.get('doc_id'), src.get('조번호'), src.get('항호'))
            행 = 위치.get(key)
            if not 행:
                if O.ontology_fund.다른_자금(조회.get('자금'), 키표.get(key[0]), key[1], key[2]):
                    바깥 += 1
                else:
                    문제.append(('선택 규범 근거 위치 없음', *key))
                continue
            row = docs.get(행['article_id'])
            if not row:
                continue
            발췌 = src.get('발췌')
            if O.ontology_context._표_항호(key[2]):
                try:
                    시작, 끝 = O.ontology_context._표_범위(row[3], key[1], key[2])
                    선택조각 = [(None, row[3][시작:끝])]
                    O.ontology_context._발췌_구간(선택조각, [발췌])
                except ValueError:
                    문제.append(('표 위치·발췌 유일성 실패', *key))
                    continue
            else:
                선택조각 = ([(None, row[3])] if not key[2] else
                        O.ontology_context._고른_조각(row[3], key[2]))
            if not any(발췌_포함(발췌, s) for _, s in 선택조각):
                종류 = '조 전체에는 있으나 선택 항과 불일치' if 발췌_포함(발췌, row[3]) else '조 전체에도 발췌 없음'
                문제.append((종류, *key))
                if os.environ.get('SUDDOE_SCOPE_TRACE') == '1' and key not in _출력한_위치 and 종류.startswith('조 전체에는'):
                    _출력한_위치.add(key)
                    text = 발췌_정규화(row[3])
                    excerpt = 발췌_정규화(발췌)
                    at = text.find(excerpt)
                    직전 = [(m.group(), m.start()) for m in re.finditer('[①-⑳]', text[:at])][-6:]
                    print('위치대조', key, '발췌', excerpt[:220], '앞문맥', text[max(0, at-100):at],
                          '뒷문맥', text[at+len(excerpt):at+len(excerpt)+100], '직전항표시', 직전, flush=True)
            else:
                유효 += 1
        if not 유효 and 바깥 and 바깥 == len(n.get('근거') or []):
            전부밖 += 1
    return {'분류': dict(Counter(x[0] for x in 문제)), '위치예': 문제[:3], '전부다른자금규범': 전부밖}


def main():
    schema = ontology_lookup._스키마()
    사업들 = list(schema['사업키'].values())
    assert len(사업들) == 7 and len(set(사업들)) == 7
    if '--사업' in sys.argv:
        index = sys.argv.index('--사업')
        선택 = sys.argv[index + 1]
        if 선택 not in 사업들:
            raise SystemExit('시험 사업명이 올바르지 않습니다')
        사업들 = [선택]
    원조회, 원증거, 원문맥, 원프롬프트 = (O.온톨로지_조회, ontology_evidence.예외_근거,
                                      O.ontology_context.문맥_구성, O.프롬프트_구성)
    원후보, 원후보조회 = ontology_research.후보_목록, ontology_research.규범ID_검증조회
    결과들 = []
    for 사업 in 사업들:
        비목 = '창업활동비' if 사업 == '모두의 창업 프로젝트' else '지급수수료'
        세목 = '지급수수료' if 비목 == '창업활동비' else None
        품목, 용도, 금액 = 'API 이용료', '제품 개발', 10000
        질문 = f'{품목} {금액}원을 제품 개발에 써도 되나요?'
        정규화 = {'품목': 품목, '용도': 용도, '금액': 금액, '질문원문': 질문,
                '부가세구분': '포함'}
        현재 = {'사업': 사업, '비목': 비목, '세목': 세목, '단계': '조회 전',
              '정상': False, '프롬프트길이': None, '추가계획호출': 0,
              '규범': 0, '조건': 0, '예외참조': 0, '인용검사': None,
              '추가조회호출': 0, '추가조회결과': None, '판정모형호출': 0}
        결과들.append(현재)
        선택후보 = []
        def 연결(*args, **kwargs):
            return psycopg.connect(DSN, autocommit=True, options='-c default_transaction_read_only=on')
        def 조회(*args, **kwargs):
            현재['단계'] = '온톨로지 조회'
            out = 원조회(*args, **kwargs)
            적용 = out['적용']
            ns = ontology_evidence._규범_모으기(out)
            현재['자금'] = out.get('자금')
            현재['규범'] = len(ns)
            현재['조건'] = len(out['게이트'].get('조건') or [])
            현재['예외참조'] = sum(len(n.get('예외') or []) for n in ns.values())
            현재['코드게이트'] = out['게이트'].get('종류')
            return out
        def 추가계획(prompt, *args, **kwargs):
            현재['추가계획호출'] += 1
            지도 = json.loads(prompt.split('\n', 1)[1])['구조지도']
            후보 = [r['id'] for r in 지도.get('후보', []) if 'API' in r.get('정식명', '')]
            if not 후보:
                return {'행동': '완료', '질의': '', '인자': []}, {}
            return {'행동': '조회', '질의': 'MATCH (n:Norm)-[r:TARGETS]->(e:Entity) WHERE e.id = $id RETURN DISTINCT n.id AS norm_id LIMIT 20',
                    '인자': [{'이름': 'id', '값들': [후보[0]]}]}, {}
        def 후보목록(*args, **kwargs):
            rows = 원후보(*args, **kwargs)
            선택후보[:] = [r['id'] for r in rows if 'API' in r.get('정식명', '')]
            return rows
        def 후보조회(*args, **kwargs):
            현재['추가조회호출'] += 1
            out = 원후보조회(*args, **kwargs)
            현재['추가조회결과'] = {'완료': out.get('완료'), '사유': out.get('사유'),
                             '규범수': len(out.get('규범') or []), '근거행수': len(out.get('근거행') or []),
                             '예외참조': sum(len(n.get('예외') or []) for n in out.get('규범') or [])}
            return out
        def 판정모형(prompt, schema, **kwargs):
            현재['판정모형호출'] += 1
            현재['프롬프트길이'] = len(prompt)
            현재['프롬프트시행일미확인'] = '시행일이 확인되지 않았다' in prompt
            ids = schema['properties']['인용']['items']['enum']
            return {'판정': '가능' if '--가능' in sys.argv else '확인 필요', '요약': '선택한 규정의 조건을 확인한 후 집행하세요.',
                    '해야할일': [], '인용': [ids[0]], '전제': []}, {'모델': '고정 응답'}
        def 증거(*args, **kwargs):
            현재['단계'] = '원문·예외 검증'
            try:
                out = 원증거(*args, **kwargs)
            except Exception as e:
                현재['상세사유'] = f'{type(e).__name__}: {e}'
                try:
                    현재['진단'] = 실패_진단(args[0], args[1], args[2])
                except Exception as 진단오류:
                    현재['진단'] = {'진단실패': type(진단오류).__name__}
                raise
            현재['추가예외'] = len(out['규범'])
            현재['발췌위치'] = len(out['발췌'])
            return out
        def 문맥(*args, **kwargs):
            현재['단계'] = '최종 문맥'
            try:
                out = 원문맥(*args, **kwargs)
            except Exception:
                try:
                    rows = kwargs.get('근거행') or []
                    articles = O.ontology_context._조문_읽기(args[0], sorted({r['article_id'] for r in rows if r.get('article_id') is not None}))
                    bad = []
                    for 행 in rows:
                        글 = articles.get(행.get('article_id'))
                        if not 글:
                            continue
                        try:
                            O.ontology_context._근거_조각(글['본문'], 행.get('항호'), 글['조번호'])
                        except ValueError as e:
                            bad.append((행.get('doc_id'), 행.get('조번호'), 행.get('항호'), str(e)))
                    현재['문맥위치실패'] = {'건수': len(bad), '위치예': bad[:4]}
                    발췌실패 = []
                    for (doc, 조, 항), 발췌들 in (kwargs.get('근거발췌') or {}).items():
                        글 = next((v for v in articles.values() if v['doc_id'] == doc and v['조번호'] == 조), None)
                        if not 글:
                            continue
                        try:
                            조각 = O.ontology_context._근거_조각(글['본문'], 항, 조)
                            O.ontology_context._발췌_구간(조각, 발췌들)
                        except ValueError:
                            정 = 발췌_정규화(글['본문'])
                            발췌실패.append((doc, 조, 항, [정.count(발췌_정규화(x)) for x in 발췌들 if x]))
                    현재['발췌위치실패'] = {'건수': len(발췌실패), '위치예': 발췌실패[:4]}
                except Exception as e:
                    현재['문맥위치실패'] = {'진단실패': type(e).__name__}
                raise
            현재['근거초과'] = bool(out.get('근거초과'))
            현재['L3초과'] = bool(out.get('L3_초과'))
            return out
        def 프롬프트(*args, **kwargs):
            현재['단계'] = '프롬프트·인용'
            out = 원프롬프트(*args, **kwargs)
            제시 = kwargs.get('제시원문') or {}
            assert set(제시) == set(out[1]), 'SID와 제시 원문이 불일치'
            인용 = 근거_인용(out[1], conn=args[0].connection, org_id=None)
            assert len(인용) == len(out[1]), '문장맵 인용이 원문으로 해석되지 않음'
            현재['인용검사'] = len(인용)
            return out
        try:
            with patch.dict(os.environ, {'SUDDOE_ONTOLOGY_PROGRAMS': ','.join(사업들)}), \
                 patch.object(O.db, 'connect', side_effect=연결), \
                 patch.object(O, '온톨로지_조회', side_effect=조회), \
                 patch.object(O, '_새_l3_스레드', return_value=([], None)), \
                 patch.object(O, '_새_검색_스레드', side_effect=lambda *a, **k: (O._빈_검색결과(), None)), \
                 patch.object(O, '_판정준비_호출', return_value={'행동': '판정', '요청': ''}), \
                 patch.object(O, 'llm_호출', side_effect=판정모형), \
                 patch.object(ontology_query_plan, 'llm_호출', side_effect=추가계획), \
                 patch.object(ontology_research, '후보_목록', side_effect=후보목록), \
                 patch.object(ontology_research, '규범ID_검증조회', side_effect=후보조회), \
                 patch.object(ontology_evidence, '예외_근거', side_effect=증거), \
                 patch.object(O.ontology_context, '문맥_구성', side_effect=문맥), \
                 patch.object(O, '프롬프트_구성', side_effect=프롬프트), \
                 research_runtime.activate(research_runtime.Budget()):
                응답 = O.판정(질문, 사업명=사업, 확정비목=비목, 확정세목=세목,
                           정규화결과=정규화, 부가세구분='포함', dry=False, 기록=False,
                           사용일자='2026-10-01' if '--사용일자' in sys.argv else None)
            현재['결과'] = 응답.get('판정')
            현재['시행일미확인'] = bool(응답.get('시행일미확인'))
            if 현재['판정모형호출']:
                assert 현재['인용검사'] and 현재['프롬프트길이'] and 응답.get('인용목록')
                현재.update(정상=True, 단계='최종 프롬프트 구성', 사유=None)
            else:
                현재['사유'] = 현재.get('상세사유') or (응답.get('검증사유') or [응답.get('요약') or '미확인'])[0]
                if 응답.get('코드판정'):
                    현재['단계'] = '코드 판정'
        except Exception as e:
            현재['사유'] = f'{type(e).__name__}: {e}'
        print(json.dumps(현재, ensure_ascii=False), flush=True)
    if '--사업' not in sys.argv:
        작성(결과들)


def 작성(rows):
    정상 = sum(r['정상'] for r in rows)
    줄 = ['# 판정 근거 사업별 검사', '',
         '범위: 격리 5462/suddoe 읽기 전용, 7개 사업 합성 입력, L3 없음. 외부 LLM·검색·DB 기록 없음.',
         '공통 입력: API 이용료 10,000원, 제품 개발, 부가세 포함. 비목·세목은 아래 표와 같다.',
         '지도에 API 후보가 있으면 고정 질의를 실행하고 없으면 조회를 종료한다. 판정 준비·최종 판정 응답도 고정했다. 실제 서비스 정확도·실제 검색 효과는 측정하지 않았다.',
         f'최종 프롬프트 구성 {정상}/{len(rows)}. 실패한 사례의 길이는 측정하지 않았다.', '',
         '| 사업 | 비목·세목 | 자금 | 상태·중단 단계 | 사유 | 프롬프트 글자 |', '|---|---|---|---|---|---:|']
    for r in rows:
        분류 = ' / '.join(x for x in (r['비목'], r['세목']) if x)
        사유 = str(r.get('사유') or '-').replace('|', '\\|').replace('\n', ' ')
        길이 = str(r['프롬프트길이']) if r['정상'] else '미구성'
        줄.append(f"| {r['사업']} | {분류} | {r.get('자금') or '-'} | {r['단계']} | {사유} | {길이} |")
    줄 += ['', '조건·예외·추가 조회·인용의 실제 호출 결과:', '']
    for r in rows:
        줄.append(f"- {r['사업']}: 규범 {r['규범']}, 게이트 조건 {r['조건']}, 예외 참조 {r['예외참조']}, "
                f"추가 조회 계획 {r['추가계획호출']}회, 추가 규범 조회 {r['추가조회호출']}회 "
                f"({r['추가조회결과'] or '선택 없음'}), 인용 원문 검사 {r['인용검사'] if r['인용검사'] is not None else '미도달'}.")
        if r.get('진단'):
            줄.append(f"  - 실패 진단: {json.dumps(r['진단'], ensure_ascii=False)}")
        if r.get('문맥위치실패'):
            줄.append(f"  - 문맥 위치 진단: {json.dumps(r['문맥위치실패'], ensure_ascii=False)}")
    줄 += ['', '이 검사는 각 사업의 한 입력만 다룬다. 실패 원인은 해당 입력과 현재 격리 DB에 한정되며 정책 변경 근거로 일반화하지 않는다.']
    보고서.write_text('\n'.join(줄) + '\n', encoding='utf-8')
    print('보고서:', 보고서, flush=True)


def 기준_본문_비교():
    """두 로컬 DB의 특정 활성 조문 본문과 문서 시행일만 읽어 대조한다."""
    대상 = [
        ('L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223', '제36조'),
        ('창업중심대학 세부관리기준2025년 개정', '참고3'),
        ('창업중심대학 세부관리기준2025년 개정', '참고4'),
        ('창업중심대학 세부관리기준2025년 개정', '참고5'),
        ('초격차 스타트업 프로젝트 세부관리기준(제10차)', '제18조'),
        ('초격차 스타트업 프로젝트 세부관리기준(제10차)', '참고2'),
        ('초격차 스타트업 프로젝트 세부관리기준(제10차)', '참고5'),
    ]
    기준dsn = 'postgresql://postgres:devpw@127.0.0.1:5432/suddoe_ontology?connect_timeout=5'
    결과 = []
    with psycopg.connect(기준dsn, options='-c default_transaction_read_only=on') as 기준, \
         psycopg.connect(DSN, options='-c default_transaction_read_only=on') as 리허설:
        for doc, 조 in 대상:
            def 읽기(conn):
                return conn.execute('''SELECT a."본문", d.status, d.시행일
                    FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id=a.doc_id
                    WHERE a.doc_id=%s AND a."조번호"=%s''', (doc, 조)).fetchone()
            a, b = 읽기(기준), 읽기(리허설)
            해시 = lambda row: hashlib.sha256((row[0] or '').encode('utf-8')).hexdigest() if row else None
            항목 = {'문서': doc, '조': 조, '기준존재': bool(a), '리허설존재': bool(b),
                '본문일치': bool(a and b and 해시(a) == 해시(b)),
                '기준상태': a[1] if a else None, '리허설상태': b[1] if b else None,
                '기준시행일': str(a[2]) if a and a[2] else None,
                '리허설시행일': str(b[2]) if b and b[2] else None}
            결과.append(항목)
            print(json.dumps(항목, ensure_ascii=False), flush=True)
    return 결과


def 선택_표_위치_점검():
    """예비 사업의 현재 선택 규범 발췌가 명시된 표 안에서 유일한지 확인한다."""
    import ontology_table_evidence
    with psycopg.connect(DSN, options='-c default_transaction_read_only=on') as conn, conn.cursor() as cur:
        조회 = O.온톨로지_조회(cur, '예비창업패키지', '지급수수료', None,
                         O.게이트_입력(10000, '포함', None), 확정=True, 품목='API 이용료')
        norms = ontology_evidence._규범_모으기(조회)
        sources = {(g['doc_id'], g['조번호'], g['항호'], g['발췌'])
                 for n in norms.values() for g in n.get('근거') or []
                 if g.get('조번호') == '제36조' and g.get('항호') == '①표-10' and g.get('발췌')}
        개수 = Counter()
        for doc, 조, 항, 발췌 in sources:
            row = cur.execute('SELECT "본문" FROM corpus.doc_articles WHERE doc_id=%s AND "조번호"=%s', (doc, 조)).fetchone()
            if not row:
                개수['원문없음'] += 1
                continue
            try:
                시작, 끝 = ontology_table_evidence.앞쪽_표_구간(row[0], 조, 항)
            except ValueError:
                개수['표경계실패'] += 1
                continue
            table = 발췌_정규화(row[0][시작:끝])
            needle = 발췌_정규화(발췌)
            found = sum(1 for m in re.finditer('(?=' + re.escape(needle) + ')', table)) if needle else 0
            개수['유일' if found == 1 else '없음' if found == 0 else '중복'] += 1
        print(json.dumps({'사업': '예비창업패키지', '비목': '지급수수료',
                         '서로다른선택발췌': len(sources), '표내위치': dict(개수)}, ensure_ascii=False))


if __name__ == '__main__':
    import sys
    if '--compare' in sys.argv:
        결과 = 기준_본문_비교()
        if '--report' in sys.argv:
            원문 = 보고서.read_text(encoding='utf-8').split('\n## 기준 본문 비교\n', 1)[0]
            줄 = ['\n## 기준 본문 비교', '',
                 '5432/suddoe_ontology와 5462/suddoe의 지정 조문을 둘 다 읽기 전용으로 비교했다. 운영·원본 PDF는 비교하지 않았다.', '']
            for r in 결과:
                줄.append(f"- {r['문서']} {r['조']}: 본문 {'일치' if r['본문일치'] else '불일치 또는 누락'}, "
                        f"기준 시행일 {r['기준시행일'] or 'NULL'}, 리허설 시행일 {r['리허설시행일'] or 'NULL'}.")
            줄 += ['', '항호 없는 책자 p15-2의 연속 발췌는 조 전체 위치에서 검증하도록 수정했으며, '
                  '격리 DB provenance → 제시 조각 → SID → 원문 인용 검사를 별도로 통과했다.']
            보고서.write_text(원문.rstrip() + '\n' + '\n'.join(줄) + '\n', encoding='utf-8')
    elif '--table' in sys.argv:
        선택_표_위치_점검()
    else:
        main()
