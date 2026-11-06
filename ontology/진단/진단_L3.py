# -*- coding: utf-8 -*-
"""L3 있는 기관 × 경로: 문맥 상한 때문에 판단불가가 나오는가. LLM 없음 · 쓰기 없음."""
import sys, os, json, collections
sys.stdout.reconfigure(encoding='utf-8')
R = os.environ['REPO']
sys.path[:0] = [R, R + '/scripts']
os.environ['SUDDOE_ONTOLOGY_PROGRAMS'] = '모두의 창업 프로젝트'
from _lib import db
import ontology_lookup as L, ontology_context as C, orchestrate as O, l3_load
사업 = '모두의 창업 프로젝트'
경로 = [('재료비', None), ('외주용역비', None), ('외주용역비', '선급금'), ('지급수수료', '멘토링비'), ('여비', '숙박비'), ('창업활동비', None), ('창업활동비', '재료구입비')]
with db.connect('postgresql://postgres:devpw@localhost:5432/suddoe', autocommit=True) if False else db.connect(autocommit=True) as c:
    cur = c.cursor()
    print('경로 | 온톨로지 근거 글자(L3 없음) | 남는 칸(상한 40,000)')
    기반 = {}
    for b, s in 경로:
        q = O.온톨로지_조회(cur, 사업, b, s, O.게이트_입력(100000, '포함', {}), 확정=True)
        m = C.문맥_구성(cur, 근거행=q['근거행'], 대상노드=q['노드들'], l3=[], 검색={}, schema=L._스키마(), 상한=10**9)
        항목 = [i for 제목, 묶음 in m['블록'] for i in 묶음]
        준용2글자 = sum(len(i[3]) + len(i[2]) for 제목, 묶음 in m['블록'] if 제목.startswith('B2.7') for i in 묶음)
        앞 = m['글자수'] - 준용2글자
        기반[(b, s)] = 앞
        print(f"{b} {s or ''} | 전체 {m['글자수']} · L3 앞 순위 {앞} · 준용2~3 {준용2글자} | 남는 칸 {40000 - 앞}")
orgs = {
    'f03ce8d6-e400-541d-8da1-41360ff6a05f': 6, 'cfeba091-251a-5ae4-8cc9-88c6e6679440': 207, '74b930aa-7f2a-4fb6-bd23-edfdaa7e8efc': 12, '1d6be2e1-7296-5492-a24b-c0838b431a7f': 11}
for org, _ in orgs.items():
    with db.connect(autocommit=True) as c:
        c.execute("SELECT set_config('app.org_id', %s, false), set_config('app.account_id', '', false)", (org,))
        cur = c.cursor()
        l3 = l3_load.조문_연결조회(cur, org) or []
        글자 = sum(len(r.get('본문') or '') for r in l3)
        줄 = []
        for b, s in 경로:
            q = O.온톨로지_조회(cur, 사업, b, s, O.게이트_입력(100000, '포함', {}), 확정=True)
            m = C.문맥_구성(cur, 근거행=q['근거행'], 대상노드=q['노드들'], l3=l3, 검색={}, schema=L._스키마())
            줄.append(f"{b}{'>' + s if s else ''}:{'초과' if m['L3_초과'] else '통과'}")
        print(org[:8], '조', len(l3), '글자', 글자, ' '.join(줄))
