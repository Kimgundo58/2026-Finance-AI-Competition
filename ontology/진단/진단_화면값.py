# -*- coding: utf-8 -*-
"""실제 응답의 화면 표시 칸에서 내부 값 · 이상한 값을 모은다."""
import sys, json, os, re, glob, collections
sys.stdout.reconfigure(encoding='utf-8')
OUT=os.environ['OUT']
패턴=re.compile(r'(규범:|비목:|세목:|품목:|절차:|주체:|증빙:|개념:|#\d|#제|S\d{2}\b|[A-Z_]{5,}|/의무|/금지|/한도|표-\d+/|p\d+-\d+|L[1-4]_|\bnull\b|None)')
모음=collections.defaultdict(list); 항호=collections.Counter(); 조번호=collections.Counter(); 경로=collections.Counter()
for p in sorted(glob.glob(f'{OUT}/real_*.json')):
    i=os.path.basename(p)[5:-5]
    d=json.load(open(p,encoding='utf-8')); r=d.get('판정응답') or {}
    화면={'요약':[r.get('요약')],'해야할일':[(t if isinstance(t,str) else t.get('문장') or t.get('내용') or json.dumps(t,ensure_ascii=False)) for t in (r.get('해야할일') or [])],
         '조건':[(t if isinstance(t,str) else t.get('문장')) for t in (r.get('조건') or [])],'근거경로':r.get('근거경로') or [],'문의초안':[r.get('문의초안')],
         '코드판정':[json.dumps(r.get('코드판정'),ensure_ascii=False)] if r.get('코드판정') else [],'용어풀이':[json.dumps(r.get('용어풀이'),ensure_ascii=False)] if r.get('용어풀이') else [],
         '질문':[q.get('질문문','')+' '+q.get('이유','') for q in ((d.get('질문단계') or {}).get('질문') or [])]}
    for k,v in 화면.items():
        for s in v:
            if not s: continue
            s=s if isinstance(s,str) else json.dumps(s,ensure_ascii=False)
            m=패턴.search(s)
            if m: 모음[k].append((i,m.group(0),s[:140]))
    for c in r.get('인용') or []:
        항호[c.get('항호')]+=1; 조번호[c.get('조번호')]+=1
    for s in r.get('근거경로') or []: 경로[s if isinstance(s,str) else json.dumps(s,ensure_ascii=False)]+=1
for k,v in 모음.items():
    print('==',k,len(v))
    seen=set()
    for i,g,s in v:
        if s in seen: continue
        seen.add(s); print('  ',i,g,'|',s)
print('== 인용 조번호',조번호.most_common(60))
print('== 인용 항호',항호.most_common(60))
print('== 경로');[print('  ',n,s[:110]) for s,n in 경로.most_common(25)]
