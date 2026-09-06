# -*- coding: utf-8 -*-
import sys, json, re
sys.path.insert(0, 'scripts/_lib')
import db

DOCS = [
    'L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223',
    '예비창업패키지 세부관리기준(2025년)',
    '초기창업패키지 세부관리기준(2025년)',
    '창업중심대학 세부관리기준2025년 개정',
    '초격차 스타트업 프로젝트 세부관리기준(제10차)',
    '창업도약패키지 세부관리기준(2025년)',
    '2026년 재도전성공패키지 세부관리기준(11차 개정)',
    '모두의 창업 프로젝트 세부관리기준(개정본)',
    '붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문',
]

# 중앙 기본 정규식 + 보조 신호(익월/영업일/즉시/지체없이/이주일 등은 별도 카운트)
PAT_NUM = re.compile(r'(\d+)\s*(일|개월|주)\s*(전까지|전|이내|이전)')
PAT_EXTRA = re.compile(r'(익월|영업일|이주일|즉시|지체\s*없이|한\s*달)')

with db.connect() as c, c.cursor() as cur:
    matches = []
    for doc_id in DOCS:
        cur.execute('''select article_id, 조번호, 본문 from corpus.doc_articles
                       where doc_id=%s and coalesce(삭제,false)=false''', (doc_id,))
        for aid, jo, body in cur.fetchall():
            for m in PAT_NUM.finditer(body):
                i, j = m.start(), m.end()
                ctx = body[max(0, i-120):j+120]
                matches.append({
                    'doc_id': doc_id, '조번호': jo, 'article_id': aid,
                    '매치': m.group(0), '컨텍스트': ctx, '유형': 'NUM'
                })
            for m in PAT_EXTRA.finditer(body):
                i, j = m.start(), m.end()
                ctx = body[max(0, i-120):j+120]
                matches.append({
                    'doc_id': doc_id, '조번호': jo, 'article_id': aid,
                    '매치': m.group(0), '컨텍스트': ctx, '유형': 'EXTRA'
                })

    # L3
    cur.execute('select org_id, 기관명 from tenant.orgs')
    org_names = dict(cur.fetchall())
    cur.execute('select org_id, article_id, 조번호, 본문 from tenant.l3_articles')
    l3rows = cur.fetchall()
    l3_matches = []
    for org_id, aid, jo, body in l3rows:
        if not body:
            continue
        for m in PAT_NUM.finditer(body):
            i, j = m.start(), m.end()
            ctx = body[max(0, i-120):j+120]
            l3_matches.append({
                'org_id': str(org_id), 'org_name': org_names.get(org_id),
                '조번호': jo, 'article_id': aid,
                '매치': m.group(0), '컨텍스트': ctx, '유형': 'NUM'
            })
        for m in PAT_EXTRA.finditer(body):
            i, j = m.start(), m.end()
            ctx = body[max(0, i-120):j+120]
            l3_matches.append({
                'org_id': str(org_id), 'org_name': org_names.get(org_id),
                '조번호': jo, 'article_id': aid,
                '매치': m.group(0), '컨텍스트': ctx, '유형': 'EXTRA'
            })

print('L1/L2 매치 수(NUM):', sum(1 for m in matches if m['유형']=='NUM'))
print('L1/L2 매치 수(EXTRA):', sum(1 for m in matches if m['유형']=='EXTRA'))
print('L3 매치 수(NUM):', sum(1 for m in l3_matches if m['유형']=='NUM'))
print('L3 매치 수(EXTRA):', sum(1 for m in l3_matches if m['유형']=='EXTRA'))

with open(r'C:\Users\dogun\AppData\Local\Temp\claude\gihan\matches.json', 'w', encoding='utf-8') as f:
    json.dump({'l1l2': matches, 'l3': l3_matches}, f, ensure_ascii=False, indent=1)
