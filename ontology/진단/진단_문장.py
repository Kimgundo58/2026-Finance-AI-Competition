# -*- coding: utf-8 -*-
"""조건 문장 품질: 조각 · 중복 · 시점 칸. 스윕 결과와 원천 JSON 만 읽는다."""
import sys, os, json, re, collections, difflib
sys.stdout.reconfigure(encoding='utf-8')
OUT = os.environ['OUT']
표 = json.load(open(f'{OUT}/sweep_라벨.json', encoding='utf-8'))
문장들 = collections.Counter(s for t in 표 if t['금액'] == 1000000 for s in t['문장'])
끝 = re.compile(r'(다|요|니다|세요|해요|어요)[.!?]?\s*$')
조각 = [s for s in 문장들 if not 끝.search(s)]
print('서로 다른 조건 문장', len(문장들), '서술어로 안 끝나는 문장', len(조각))
for s in 조각:
    print(' 조각', 문장들[s], s[:120])
print('--- 라벨별 뜻 겹침(문자 유사도 0.72 이상 쌍)')
겹침표 = {}
for t in 표:
    if t['금액'] != 1000000 or t['종류']:
        continue
    쌍 = []
    ss = t['문장']
    for i in range(len(ss)):
        for j in range(i + 1, len(ss)):
            if difflib.SequenceMatcher(None, ss[i], ss[j]).ratio() >= 0.72:
                쌍.append((ss[i][:60], ss[j][:60]))
    겹침표[t['표시문']] = 쌍
for k in ('지급수수료 > 멘토링비', '재료비', '외주용역비', '기계장치'):
    print(k, len(겹침표.get(k, [])))
    for a, b in 겹침표.get(k, [])[:5]:
        print('   ', a, '//', b)
print('겹침 쌍 있는 라벨', sum(1 for v in 겹침표.values() if v), '/', len(겹침표))
