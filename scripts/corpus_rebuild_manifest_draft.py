# -*- coding: utf-8 -*-
"""검토된 스냅샷의 표 8개에 대한 정책 초안을 별도 파일로 만든다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import corpus_rebuild_export as export
import corpus_rebuild_prepare as prepare
import corpus_rebuild_manifest as validator


SOURCE_SHA = 'e852938c3568bb72fb4196263177fea57b5b42050ee5db2e71aa8735c4148287'
POLICIES = {
    ('L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223', '제36조'): ('창업기업', '검색대상', '제목: 창업기업등 사업비 비목; 기존 검색대상'),
    ('모두의 창업 프로젝트 세부관리기준(개정본)', '별지1'): ('주관기관', '참조확장전용', '본문: 라운드 운영비와 주관기관 역할; 기존 참조확장전용'),
    ('모두의 창업 프로젝트 세부관리기준(개정본)', '별지2'): ('창업기업', '검색대상', '제목: 진출자 사업비 집행기준; 기존 검색대상'),
    ('예비창업패키지 세부관리기준(2025년)', '붙임2'): ('창업기업', '검색대상', '제목: 창업기업등 사업비 비목 해설표; 기존 검색대상'),
    ('창업중심대학 세부관리기준2025년 개정', '참고4'): ('창업기업', '참조확장전용', '제목: 창업기업 사업비 집행 증빙서류; 기존 참조확장전용'),
    ('초격차 스타트업 프로젝트 세부관리기준(제10차)', '참고2'): ('창업기업', '검색대상', '제목: 초격차 창업기업 사업비 집행기준; 기존 검색대상'),
    ('초기창업패키지 세부관리기준(2025년)', '붙임2'): ('창업기업', '검색대상', '제목: 창업기업등 사업비 비목 해설표; 기존 검색대상'),
}
SPECIAL = ('초격차 스타트업 프로젝트 세부관리기준(제10차)', '참고5')
HEADER = '| 비목 | 세목 | 보조세목 | 정의 |'


def 만들기(source):
    if source.get('sha256') != SOURCE_SHA:
        raise ValueError('검토한 원천과 다른 해시')
    data = prepare.원천_검증(source)
    result = []
    for doc in data['문서']:
        for a in doc['조문']:
            key = (a['doc_id'], a['조번호'])
            if key not in validator.대상:
                continue
            body = a['본문']
            if key == SPECIAL:
                boundary = body.find(HEADER)
                if (boundary <= 0 or body.count(HEADER) != 1 or '프로그램 운영비' not in body[:boundary]
                        or '재료비' not in body[boundary:] or '창업기업 사업비 보조세목 정의' not in body[boundary:]):
                    raise ValueError('초격차 두 표의 구조와 주체 경계 불일치')
                spans = [
                    {'시작': 0, '끝': boundary, '검색범위': '참조확장전용', '적용대상': '주관기관',
                     '근거': '첫 표 프로그램 운영비 및 기관 한도; 기존 참조확장전용'},
                    {'시작': boundary, '끝': len(body), '검색범위': '참조확장전용', '적용대상': '창업기업',
                     '근거': '두 번째 표 재료비 및 뒤의 창업기업 사업비 보조세목 제목; 기존 참조확장전용'},
                ]
            else:
                대상, 범위, 근거 = POLICIES[key]
                spans = [{'시작': 0, '끝': len(body), '검색범위': 범위, '적용대상': 대상, '근거': 근거}]
            result.append({'doc_id': key[0], '조번호': key[1], '본문해시': a['원문해시'], '구간': spans})
    manifest = {'형식': 1, '원천해시': SOURCE_SHA, '표': result}
    validator.검증(source, manifest)
    return manifest


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    args = ap.parse_args()
    result = 만들기(json.loads(args.source.read_text(encoding='utf8')))
    export.새_파일_쓰기(args.output, result)
    print(json.dumps({'표수': len(result['표']), 'sha256': export.해시(result)}, ensure_ascii=False))
