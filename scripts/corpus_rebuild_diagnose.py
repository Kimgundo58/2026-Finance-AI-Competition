# -*- coding: utf-8 -*-
"""같은 권위 스냅샷의 청크 위치 실패를 원문 출력 없이 집계한다. DB·모델 없음."""
from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import json
from pathlib import Path

import corpus_rebuild_prepare as prepare
from ontology_norm import 발췌_정규화


def 진단(value, 사례=False):
    원천 = prepare.원천_검증(value)
    수 = Counter()
    문서별 = Counter()
    원인 = Counter()
    예시 = []
    미해결조문 = Counter()
    범위 = {}
    for doc in 원천['문서']:
        for article in doc['조문']:
            body = article['본문']
            normalized = 발췌_정규화(body)
            compact = ''.join(normalized.split())
            for chunk in article.get('기존청크') or []:
                글 = chunk.get('text') or ''
                if 글 and body.find(글) >= 0:
                    continue
                수['정확위치없음'] += 1
                문서별[doc['doc_id']] += 1
                text = 발췌_정규화(글)
                if text and normalized.count(text) == 1:
                    수['발췌정규화로유일'] += 1
                elif text and normalized.count(text) > 1:
                    수['발췌정규화로중복'] += 1
                elif text and compact.count(''.join(text.split())) == 1:
                    수['공백제거시유일'] += 1
                else:
                    수['그외불일치'] += 1
                if text and normalized.count(text) != 1:
                    if not (text and compact.count(''.join(text.split())) == 1):
                        미해결조문[(doc['doc_id'], article['조번호'])] += 1
                        key = (doc['doc_id'], article['조번호'])
                        범위.setdefault(key, Counter())[str(chunk.get('retrieval_scope'))] += 1
                    정책 = {prepare.export.해시({k: c.get(k) for k in prepare._정책칸})
                          for c in article.get('기존청크') or []}
                    원인['단일태그조문' if len(정책) == 1 else '혼합태그조문'] += 1
                    비교 = SequenceMatcher(None, normalized, text, autojunk=False)
                    blocks = 비교.get_matching_blocks()
                    matched = sum(b.size for b in blocks)
                    pct = round(100 * matched / max(len(text), 1))
                    원인[f'문자대응률_{pct // 10 * 10}대'] += 1
                    unmatched = ''.join(text[j1:j2] for op, _, _, j1, j2 in 비교.get_opcodes() if op in ('insert', 'replace'))
                    if not unmatched.strip():
                        원인['누락문자_공백만'] += 1
                    elif all(not c.isalnum() for c in unmatched):
                        원인['누락문자_기호만'] += 1
                    else:
                        원인['누락문자_글자포함'] += 1
                    if 사례 and pct < 80:
                        다른조 = [a['조번호'] for a in doc['조문'] if a is not article
                               and text and text in 발췌_정규화(a['본문'])]
                        예시.append({'문서': doc['doc_id'], '조번호': article['조번호'],
                                   '청크앞부분': text[:100], '본문앞부분': normalized[:100], '대응률': pct,
                                   '동일문서내타조문': 다른조})
                    elif 사례 and unmatched.strip() and len(예시) < 10:
                        예시.append({'문서': doc['doc_id'], '조번호': article['조번호'], '대응률': pct,
                                   '대응안된청크일부': unmatched[:100]})
    return {'집계': dict(수), '원인집계': dict(원인), '문서별건수': dict(sorted(문서별.items())),
            '미해결조문': [{'문서': d, '조번호': a, '청크수': n, '검색범위': dict(범위[(d, a)])}
                         for (d, a), n in sorted(미해결조문.items())],
            '낮은대응률사례': 예시}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--examples', action='store_true')
    args = parser.parse_args()
    print(json.dumps(진단(json.loads(args.source.read_text(encoding='utf-8')), args.examples), ensure_ascii=False))


if __name__ == '__main__':
    main()
