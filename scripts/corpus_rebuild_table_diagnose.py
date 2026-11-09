# -*- coding: utf-8 -*-
"""권위 스냅샷 내부의 표 셀과 기존 청크 대응 가능성만 집계한다."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import corpus_rebuild_prepare as prepare
from ontology_norm import 발췌_정규화


def compact(text):
    return ''.join(발췌_정규화(text).split())


def 진단(value):
    source = prepare.원천_검증(value)
    counts = Counter()
    articles = []
    for doc in source['문서']:
        for article in doc['조문']:
            chunks = article.get('기존청크') or []
            try:
                prepare.정책_구간(article['본문'], chunks)
                continue
            except ValueError:
                pass
            tags = Counter((c.get('retrieval_scope'), c.get('적용대상')) for c in chunks)
            articles.append({'문서': doc['doc_id'], '조번호': article['조번호'], '제목': article.get('조제목'),
                             '고정메타종류수': len({prepare.export.해시({k: c.get(k) for k in
                                 ('layer', '기관id', 'version', '사업명', 'parse_quality')}) for c in chunks}),
                             '태그': [{'검색범위': k[0], '적용대상': k[1], '수': n} for k, n in tags.items()]})
            indexed = [compact(c.get('text') or '') for c in chunks]
            for line in article['본문'].splitlines():
                if line.count('|') < 2:
                    continue
                cells = [compact(x) for x in line.split('|')]
                cells = [x for x in cells if len(x) >= 8 and not re.fullmatch(r'[-:]+', x)]
                if not cells:
                    counts['셀없는표행'] += 1
                    continue
                candidates = [{i for i, text in enumerate(indexed) if cell in text} for cell in cells]
                if any(not matches for matches in candidates):
                    counts['셀누락표행'] += 1
                elif len(set.intersection(*candidates)) == 1:
                    counts['청크1개와일치'] += 1
                elif len(set.intersection(*candidates)) > 1:
                    counts['복수청크와일치'] += 1
                    matches = set.intersection(*candidates)
                    tags = {prepare.export.해시({k: chunks[i].get(k) for k in prepare._정책칸}) for i in matches}
                    counts['복수중동일태그' if len(tags) == 1 else '복수중상이태그'] += 1
                    scope = {(chunks[i].get('retrieval_scope'), chunks[i].get('적용대상')) for i in matches}
                    counts['복수중검색범위동일' if len(scope) == 1 else '복수중검색범위상이'] += 1
                else:
                    counts['셀별청크불일치'] += 1
    return {'집계': dict(counts), '조문별': articles}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, type=Path)
    args = ap.parse_args()
    print(json.dumps(진단(json.loads(args.source.read_text(encoding='utf8'))), ensure_ascii=False))
