# -*- coding: utf-8 -*-
import sys, json, re
sys.path.insert(0, 'scripts/_lib')
import db

PAT_NUM = re.compile(r'(\d+)\s*(일|개월|주)\s*(전까지|전|이내|이전)')
PAT_EXTRA = re.compile(r'(익월|영업일|이주일|즉시|지체\s*없이|한\s*달)')

with db.connect() as c, c.cursor() as cur:
    cur.execute('''select code,사업명,비목,구분,항목,설명,기본_오프셋일,근거 from corpus.check_items
                   where 기본_오프셋일 <> 0 order by code''')
    items = cur.fetchall()

    body_cache = {}
    def get_body(doc_id, jo):
        key = (doc_id, jo)
        if key not in body_cache:
            cur.execute('select 본문 from corpus.doc_articles where doc_id=%s and 조번호=%s', key)
            r = cur.fetchone()
            body_cache[key] = r[0] if r else None
        return body_cache[key]

    for code, biz, item, gubun, hangmok, seolmyeong, offset, geun in items:
        print('='*80)
        print(code, biz, item, gubun, '| offset=', offset)
        print(' 설명:', seolmyeong)
        for g in geun:
            body = get_body(g['doc_id'], g['조번호'])
            if body is None:
                print('  [', g['doc_id'], g['조번호'], '] 본문 없음')
                continue
            hits = list(PAT_NUM.finditer(body)) + list(PAT_EXTRA.finditer(body))
            if not hits:
                print('  [', g['doc_id'], g['조번호'], '] 기한 표현 0건')
            for m in hits:
                i, j = m.start(), m.end()
                ctx = body[max(0, i-70):j+70].replace('\n', ' ')
                print('  [', g['doc_id'], g['조번호'], ']', repr(m.group(0)), '|', ctx)
