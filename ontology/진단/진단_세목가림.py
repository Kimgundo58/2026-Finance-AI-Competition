# -*- coding: utf-8 -*-
import sys, os, collections
sys.stdout.reconfigure(encoding='utf-8')
R=os.environ['REPO']; sys.path[:0]=[R,R+'/scripts']
os.environ['SUDDOE_ONTOLOGY_PROGRAMS']='모두의 창업 프로젝트'
from _lib import db
import ontology_lookup as L
from server._common import 비목_ENUM
사업='모두의 창업 프로젝트'
with db.connect(autocommit=True) as c:
    cur=c.cursor()
    lab=[x for x in L.레이블_목록(cur,사업) if x['비목'] in 비목_ENUM]
    print(len(lab))
    세목라벨=[x for x in lab if x['세목']]
    품목=cur.execute("select concept_id,pref_label from corpus.concepts where 종류='품목'").fetchall()
    금지표=collections.defaultdict(list)
    for nid,종,v,대상,문 in cur.execute("select norm_id,종류,verified,대상,결과->>'문장' from corpus.norms where 종류 in ('금지','한도','요건','의무')").fetchall():
        for t in 대상:
            if t.startswith('품목:'): 금지표[t].append((nid,종,v,문))
    행=[]
    for p,name in 품목:
        상위={b for _,b in L._상위_경로(cur,p,[사업])}
        for x in 세목라벨:
            b=L.대상_노드(cur,사업,x['비목'],None)['대상_id']
            s=x['노드']
            if b in 상위 and s not in 상위:
                행.append((name,x['표시문']))
    print('세목을 확정하면 품목 노드로 못 가는 (품목,세목) 쌍',len(행))
    p표=collections.Counter(a for a,_ in 행); print('품목 수',len(p표))
    # 금지 규범이 직접 걸린 품목
    직접=[(n,p) for p,n in 품목 for _ in [0] if any(k=='금지' for _,k,_,_ in 금지표.get(p,[]))]
    print('금지 규범 직접 대상 품목',len(직접))
    print('그중 세목 확정 때 못 가는 품목', [n for n,p in 직접 if n in p표][:60])
    for 비, 세, 품 in [('창업활동비','재료구입비','키보드'),('창업활동비','재료구입비','노트북'),('창업활동비','재료구입비','미니PC'),('창업활동비','재료구입비','귀금속'),('창업활동비','재료구입비','상품권'),('창업활동비','재료구입비','마우스'),('창업활동비',None,'키보드'),('재료비',None,'귀금속'),('기계장치',None,'노트북')]:
        대=L.대상_노드(cur,사업,비,세)
        print(비,세,품,'→',L.품목_노드(cur,사업,품,대['대상_id']),'| 노드_결정',[r['concept_id'] for r in L.노드_결정(cur,품,사업)][:3])
