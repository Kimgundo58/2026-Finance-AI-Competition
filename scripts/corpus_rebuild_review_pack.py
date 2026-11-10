# -*- coding: utf-8 -*-
"""권위 스냅샷의 표 쟁점만 외부 독립 검토용 공개자료로 구성한다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import corpus_rebuild_prepare as prepare


def 보고서(value, 분류):
    source = prepare.원천_검증(value)
    parts = ['# 표 검색 범위 독립 검토', '',
             '동일 권위 DB에서 추출한 공개 규정이며 해당 조문 본문은 전체를 제공한다.',
             '질문: 표의 검색대상·참조확장전용 및 적용대상을 실제 원문과 기존 태그로 안전하게 재구성할 경계는?',
             '선택지: (A) 명확한 행·셀에만 태그 귀속, 나머지 미확정 (B) 조문 전체 일괄 귀속 (C) 추가 원문 필요.',
             '제안: A. 상충 태그·제목·구분선은 근거 없이 인접 행에 귀속하지 않는다. 법적 판단은 하지 않는다.', '']
    for doc in source['문서']:
        for article in doc['조문']:
            chunks = article.get('기존청크') or []
            try:
                prepare.정책_구간(article['본문'], chunks)
                continue
            except ValueError:
                pass
            기관표 = doc['doc_id'] == '모두의 창업 프로젝트 세부관리기준(개정본)' and article['조번호'] == '별지1'
            if 기관표 != (분류 == '기관'):
                continue
            parts.extend([f"## {doc['doc_id']} / {article['조번호']}", '',
                          '권위 본문 전체:', '', article['본문'], '',
                          f'기존 청크 {len(chunks)}개의 정책 태그 (기존 청크 본문은 생략):', ''])
            for c in chunks:
                tag = {k: c.get(k) for k in prepare._정책칸}
                parts.extend([json.dumps(tag, ensure_ascii=False), ''])
    return '\n'.join(parts)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--group', choices=('기관', '기업'), required=True)
    args = ap.parse_args()
    report = 보고서(json.loads(args.source.read_text(encoding='utf8')), args.group)
    with args.output.open('x', encoding='utf8', newline='\n') as f:
        f.write(report)
    print(json.dumps({'글자수': len(report)}, ensure_ascii=False))
