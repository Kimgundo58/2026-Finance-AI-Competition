# -*- coding: utf-8 -*-
"""판정 문맥의 기존 근거와 연결된 예외 원문을 확인한다. DB에는 쓰지 않는다.

예외_근거(cur, 조회, 사업, 기준일=...)는 기존 조회를 변경하지 않고
{'규범': 새 예외 규범, '근거행': 새 근거행, '발췌': 조·항별 발췌}를 반환한다. 반환 규범은 문맥
보강용이며 코드 게이트에 합치면 안 된다. 근거 또는 예외를 완전히 확인하지
못하면 예외를 발생시킨다. 규범 ID가 아닌 자연어 예외는 원 규범에 그대로 둔다.
"""
from __future__ import annotations

from datetime import date
import re

import ontology_age
import ontology_context
import ontology_fund
import ontology_lookup
import ontology_table_evidence
from ontology_norm import 발췌_포함

_목록 = ('규범', '경로밖_품목규범', '상황규범', '공통미정', '품목표')
_예외_상한 = 64
_특정_위치 = re.compile(r'제\s*\d+\s*(?:조|항|호|장|편)|부칙|별지|별표|붙임|(?i:p)\s*\d+')


def _규범_모으기(조회):
    적용 = 조회.get('적용') or {}
    묶음 = [n for k in _목록 for n in (적용.get(k) or [])]
    묶음.extend(조회.get('추가규범') or [])
    표 = {}
    for n in 묶음:
        if not isinstance(n, dict) or not isinstance(n.get('norm_id'), str):
            raise ValueError('규범 식별 불가')
        표.setdefault(n['norm_id'], n)
    return 표


def _위치_확인(본문, 항호, 조번호=None):
    if ontology_context._표_항호(항호):
        try:
            ontology_context._표_범위(본문, 조번호, 항호)
            return True
        except ValueError:
            return False
    키 = ontology_context.항호_키(항호)
    if not 키:
        return True
    if 키.startswith('/'):
        return bool(ontology_context.별지_행_내용(본문, 키))
    return any((k or '').split('#')[0] == 키 for k, _ in ontology_context._조_조각(본문))


def _행_검증(cur, 행들, *, 사업, 자금, 기준일, 키표, 시행일미확인=None):
    조문, 미해결 = [], []
    for 행 in 행들:
        if (행.get('건너뜀') == '조문 아님' and 행.get('경로')
                and not str(행['경로'][-1]).startswith(('조:', '미특정'))):
            continue  # 위임받는 행위자는 문서 조문이 아니다.
        경로 = 행.get('경로') or []
        if (행.get('건너뜀') == '조문 미특정' and 경로 and
                str(경로[-1]).startswith('미특정:') and not _특정_위치.search(str(경로[-1]))):
            미해결.append(str(경로[-1]))
            continue
        if 행.get('건너뜀') or 행.get('article_id') is None:
            raise ValueError('필수 조문 미확인')
        조문.append(행)
    if not 조문:
        return {}, 미해결
    rows = cur.execute('''SELECT a.article_id, a.doc_id, a."조번호", a."본문", d.status, d.시행일
        FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id = a.doc_id
        WHERE a.article_id = ANY(%s)''', (list({r['article_id'] for r in 조문}),)).fetchall()
    표 = {r[0]: r for r in rows}
    제외문서 = set(ontology_lookup._다른사업_문서([사업]))
    for 행 in 조문:
        row = 표.get(행['article_id'])
        if (not row or row[1] != 행.get('doc_id') or row[2] != 행.get('조번호') or not row[3]
                or row[4] != 'active' or (row[5] is not None and row[5] > 기준일)):
            raise ValueError('활성 원문 또는 시행일 확인 실패')
        if row[5] is None and 시행일미확인 is not None:
            시행일미확인.add(row[1])
        문서키 = 키표.get(row[1])
        if not 문서키 or row[1] in 제외문서:
            raise ValueError('다른 사업의 원문')
        if not 행.get('공통미정') and ontology_fund.다른_자금(자금, 문서키, row[2], 행.get('항호')):
            raise ValueError('다른 자금의 원문')
        if not _위치_확인(row[3], 행.get('항호'), row[2]):
            raise ValueError('원문 항 위치 확인 실패')
    return 표, 미해결


def 예외_근거(cur, 조회, 사업, *, 기준일=None, l3있음=False):
    """기존 근거와 연결 예외를 검사하고 문맥에 더할 규범·근거행만 돌려준다.

    모든 원래 조문 행의 활성·사업·자금·시행일·항 위치를 검사한다. 기존 자금
    필터로 명백히 다른 자금인 미선택 provenance는 제외하고 건수를 기록한다.
    새 예외는
    동일 사업의 AGE 규범에서 재귀 조회하고 각 발췌가 활성 원문에 있는지 확인한다.
    새 예외의 준용·위임을 기존 조회와 같이 3단까지 확장한다. 고리 자체는 정상이며
    새 규범이 64건을 넘거나 누락되면 실패한다. 조문이 아닌 위임 행위자는 허용한다.
    """
    if not isinstance(조회, dict) or not 사업:
        raise ValueError('사업 또는 조회 결과 없음')
    기준일 = 기준일 or date.today()
    버전 = ontology_age.준비_버전(cur)
    schema = ontology_lookup._스키마()
    키표 = {v: k for k, v in (schema.get('문서키') or {}).items()}
    자금 = 조회.get('자금')
    기존행 = 조회.get('근거행') or []
    기존위치 = {(r.get('doc_id'), r.get('조번호'), r.get('항호')) for r in 기존행}
    시행일미확인 = set()
    문서, 미해결 = _행_검증(cur, 기존행, 사업=사업, 자금=자금, 기준일=기준일, 키표=키표, 시행일미확인=시행일미확인)

    표 = _규범_모으기(조회)
    적용 = 조회.get('적용') or {}
    공통ids = {n.get('norm_id') for n in 적용.get('공통미정') or []}
    일반ids = {n.get('norm_id') for k in ('경로밖_품목규범', '상황규범', '품목표') for n in 적용.get(k) or []}
    일반ids.update(n.get('norm_id') for n in 조회.get('추가규범') or [])
    일반ids.update(n.get('norm_id') for n in 적용.get('규범') or [] if n.get('종류') != '우선순위')
    # B4가 L3를 위해 실제 출력하는 우선순위 발췌는 제외하지 않는다.
    l3순서 = next((n.get('norm_id') for n in 적용.get('규범') or []
                 if n.get('종류') == '우선순위' and (n.get('조건') or {}).get('범위') == '상충시'
                 and (n.get('조건') or {}).get('하위') == 'L3'), None) if l3있음 else None
    제외후보 = set()
    for i, n in 표.items():
        src = n.get('근거') or []
        if (i not in 일반ids and i != l3순서 and (i in 공통ids or n.get('종류') == '우선순위')
                and src and all((s.get('doc_id'), s.get('조번호'), s.get('항호')) not in 기존위치
                    and ontology_fund.다른_자금(자금, 키표.get(s.get('doc_id')), s.get('조번호'), s.get('항호'))
                    for s in src)):
            제외후보.add(i)
    # 선택된 규범의 예외로 참조된 규범은 빠뜨리지 않는다(연쇄 참조 포함).
    바뀜 = True
    while 바뀜:
        필요 = {e for i, n in 표.items() if i not in 제외후보 for e in (n.get('예외') or [])}
        남김 = 제외후보 - 필요
        바뀜 = 남김 != 제외후보
        제외후보 = 남김
    표 = {i: n for i, n in 표.items() if i not in 제외후보}
    시작_ids = set(표)
    대기 = list(표.values())
    신규 = []
    cols = ontology_lookup._규범_컬럼
    while 대기:
        ids = set()
        for 규범 in 대기:
            예외 = 규범.get('예외') or []
            if not isinstance(예외, list) or any(not isinstance(e, str) for e in 예외):
                raise ValueError('예외 형식 오류')
            ids.update(e for e in 예외 if e.startswith('규범:') and e not in 표)
        if not ids:
            break
        if len(신규) + len(ids) > _예외_상한:
            raise ValueError('예외 규범 수 초과')
        rows = cur.execute(f'''SELECT {', '.join(cols)} FROM corpus.age_norms
            WHERE norm_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = %s)
            ORDER BY norm_id''', (list(ids), 사업)).fetchall()
        찾음 = [dict(zip(cols, row)) for row in rows]
        if {n['norm_id'] for n in 찾음} != ids:
            raise ValueError('예외 규범 누락 또는 사업 불일치')
        for n in 찾음:
            표[n['norm_id']] = n
        신규.extend(찾음)
        대기 = 찾음
    if 신규:
        ontology_lookup._근거_붙이기(cur, 신규)
        if any(not n.get('근거') for n in 신규):
            raise ValueError('예외 규범 근거 없음')
        # 공통 미정·품목표에서만 닿는 단서는 원래 문맥 구분을 유지한다.
        태그 = {i: set() for i in 표}
        적용 = 조회.get('적용') or {}
        for k in _목록:
            for n in 적용.get(k) or []:
                if n['norm_id'] in 태그:
                    태그[n['norm_id']].add(k if k in ('공통미정', '품목표') else '일반')
        for n in 조회.get('추가규범') or []:
            태그[n['norm_id']].add('일반')
        바뀜 = True
        while 바뀜:
            바뀜 = False
            for n in 표.values():
                for e in n.get('예외') or []:
                    if e in 태그:
                        크기 = len(태그[e])
                        태그[e].update(태그[n['norm_id']])
                        바뀜 |= len(태그[e]) != 크기
        추가행 = []
        for 종류 in ('일반', '공통미정', '품목표'):
            def 분류(n):
                값 = 태그[n['norm_id']]
                return '일반' if '일반' in 값 or not 값 else ('공통미정' if '공통미정' in 값 else '품목표')
            선택 = [n for n in 신규 if 분류(n) == 종류]
            if 선택:
                rows = ontology_lookup.근거_조문(cur, 선택, 3, None, 사업=사업)
                for r in rows:
                    위치키 = (r.get('doc_id'), r.get('조번호'), r.get('항호'))
                    if (위치키 not in 기존위치 and r.get('doc_id') and
                            ontology_fund.다른_자금(자금, 키표.get(r['doc_id']), r.get('조번호'), r.get('항호'))):
                        continue
                    추가행.append({**r, **({종류: True} if 종류 != '일반' else {})})
        새문서, 새미해결 = _행_검증(cur, 추가행, 사업=사업, 자금=자금, 기준일=기준일, 키표=키표, 시행일미확인=시행일미확인)
        문서.update(새문서)
        미해결.extend(새미해결)
    else:
        추가행 = []
    위치 = {(r.get('doc_id'), r.get('조번호'), r.get('항호')): r
          for r in [*기존행, *추가행] if r.get('article_id') is not None}
    발췌, 제외수 = {}, 0
    for n in 표.values():
        if not n.get('근거'):
            raise ValueError('규범 근거 없음')
        유효수 = 0
        for src in n['근거']:
            키 = (src.get('doc_id'), src.get('조번호'), src.get('항호'))
            행 = 위치.get(키)
            if not 행:
                if ontology_fund.다른_자금(자금, 키표.get(키[0]), 키[1], 키[2]):
                    제외수 += 1
                    continue
                raise ValueError('규범 근거 조문 누락')
            본문 = 문서[행['article_id']][3]
            # 원문은 확인된 조·항·표 위치를 문맥에 사용한다. 저장 발췌는 적재 검사의 대상이다.
            발췌.setdefault(키, []).append(src.get('발췌') or '')
            유효수 += 1
        if not 유효수:
            raise ValueError('선택 범위 규범 근거 없음')
    if ontology_age.준비_버전(cur) != 버전:
        raise ValueError('온톨로지 판본 변경')
    return {'규범': [n for n in 신규 if n['norm_id'] not in 시작_ids],
            '근거행': [r for r in 추가행 if r.get('건너뜀') != '조문 미특정'
                     and (r.get('doc_id'), r.get('조번호'), r.get('항호')) not in 기존위치],
            '발췌': 발췌, '미해결관계': list(dict.fromkeys(미해결)),
            '제외': {'다른자금': 제외수}, '시행일미확인': sorted(시행일미확인)}
