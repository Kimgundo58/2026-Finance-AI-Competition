# -*- coding: utf-8 -*-
"""권위 원문 해시에 결박된 8개 표의 명시적 검색 범위를 검증한다."""
from __future__ import annotations

import hashlib


대상 = {
    ('L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223', '제36조'),
    ('모두의 창업 프로젝트 세부관리기준(개정본)', '별지1'),
    ('모두의 창업 프로젝트 세부관리기준(개정본)', '별지2'),
    ('예비창업패키지 세부관리기준(2025년)', '붙임2'),
    ('창업중심대학 세부관리기준2025년 개정', '참고4'),
    ('초격차 스타트업 프로젝트 세부관리기준(제10차)', '참고2'),
    ('초격차 스타트업 프로젝트 세부관리기준(제10차)', '참고5'),
    ('초기창업패키지 세부관리기준(2025년)', '붙임2'),
}


def 원문_분할(body, 시작, 끝, 토큰수, 한도=900):
    """표 정책 경계를 넘지 않는 정확한 원문 조각을 순서대로 반환한다."""
    if not (0 <= 시작 < 끝 <= len(body)) or 한도 < 1:
        raise ValueError('표 분할 범위 오류')
    result = []
    pos = 시작
    while pos < 끝:
        low, high = pos + 1, 끝
        best = pos
        while low <= high:
            mid = (low + high) // 2
            if 토큰수(body[pos:mid]) <= 한도:
                best, low = mid, mid + 1
            else:
                high = mid - 1
        if best == pos:
            raise ValueError('표 문자 하나가 토큰 한도를 초과함')
        # 긴 행도 빠짐없이 보존하며 가능한 경우 줄 경계에서 자른다.
        if best < 끝:
            newline = body.rfind('\n', pos, best)
            if newline > pos and 토큰수(body[pos:newline + 1]) <= 한도:
                best = newline + 1
        result.append((pos, best, body[pos:best]))
        pos = best
    if ''.join(x[2] for x in result) != body[시작:끝]:
        raise ValueError('표 분할 누락')
    return result


def 검증(source, manifest):
    if manifest.get('형식') != 1 or manifest.get('원천해시') != source.get('sha256'):
        raise ValueError('표 범위 원천 해시 또는 형식 불일치')
    articles = {(a['doc_id'], a['조번호']): a for d in source['내용']['문서'] for a in d['조문']}
    rows = manifest.get('표')
    if not isinstance(rows, list) or len(rows) != len(대상):
        raise ValueError('표 범위 목록 누락')
    seen = set()
    for row in rows:
        key = (row.get('doc_id'), row.get('조번호'))
        if key not in 대상 or key in seen or key not in articles:
            raise ValueError('표 범위 논리키 오류')
        seen.add(key)
        body = articles[key]['본문']
        if row.get('본문해시') != hashlib.sha256(body.encode('utf8')).hexdigest():
            raise ValueError('표 본문 해시 불일치')
        spans = row.get('구간')
        if not isinstance(spans, list) or not spans:
            raise ValueError('표 범위 없음')
        end = 0
        for span in spans:
            start, stop = span.get('시작'), span.get('끝')
            if (type(start) is not int or type(stop) is not int or start != end or stop <= start or stop > len(body)
                    or span.get('검색범위') not in ('검색대상', '참조확장전용')
                    or span.get('적용대상') not in ('공통', '창업기업', '주관기관')
                    or not isinstance(span.get('근거'), str) or not span['근거'].strip()):
                raise ValueError('표 범위 누락·겹침 또는 태그 근거 오류')
            end = stop
        if end != len(body):
            raise ValueError('표 본문 일부 누락')
    return {(r['doc_id'], r['조번호']): r for r in rows}
