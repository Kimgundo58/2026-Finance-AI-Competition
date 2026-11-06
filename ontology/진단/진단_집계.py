# -*- coding: utf-8 -*-
import sys, json, os, glob, collections, statistics as st
sys.stdout.reconfigure(encoding='utf-8')
OUT=os.environ['OUT']
모델=collections.Counter(); llm전체=collections.Counter(); 판=collections.Counter(); llm횟수=collections.Counter(); 판정초=[]; 실패=[]; 줄=[]; 전체=[]; 경로세트=collections.Counter(); 문구=collections.Counter()
for p in sorted(glob.glob(f'{OUT}/real_*.json')):
    i=os.path.basename(p)[5:-5]
    d=json.load(open(p,encoding='utf-8')); r=d.get('판정응답') or {}
    판[r.get('판정')]+=1
    l=d.get('llm') or []
    실제=[x for x in l if x['성공']]
    for x in l: 모델[x.get('모델')]+=1
    llm전체[len(l)]+=1
    llm횟수[len(실제)]+=1
    for x in l:
        if x['단계']=='판정' and x['성공']: 판정초.append(x['초'])
        if not x['성공']: 실패.append((i,x['단계'],x['초']))
    조=len(r.get('조건') or []); 할=len(r.get('해야할일') or [])
    if 조: 줄.append(조)
    전체.append((i,r.get('판정'),조,할,(r.get('코드판정') or {}).get('종류'),len(r.get('인용') or []),(r.get('요약') or '')[:150]))
    경로세트[tuple(r.get('근거경로') or [])]+=1
print('판정',판); print('시도 수 분포',llm전체,'모델',모델); print('LLM 호출 수(성공) 분포',llm횟수); print('호출 실패',실패)
print('판정 LLM 초: 중앙',st.median(판정초),'최대',max(판정초),'건',len(판정초))
print('조건 줄 있는 응답',len(줄),'최소',min(줄),'중앙',st.median(줄),'최대',max(줄))
print('근거경로 세트 빈도');[print(' ',n,list(k)[:3]) for k,n in 경로세트.most_common(6)]
for t in 전체: print(*t,sep=' | ')
