# -*- coding: utf-8 -*-
"""화면 라벨 전부 × 금액 스윕과 규범 도달 분석. LLM 없음 · 쓰기 없음."""
import sys, os, json, collections
sys.stdout.reconfigure(encoding='utf-8')
R = os.environ['REPO']
sys.path[:0] = [R, R + '/scripts']
os.environ['SUDDOE_ONTOLOGY_PROGRAMS'] = '모두의 창업 프로젝트'
from _lib import db
import ontology_lookup as L, ontology_gate as G, orchestrate as O
from server._common import 비목_ENUM
from server.catalog import _사업_비목
사업 = '모두의 창업 프로젝트'
OUT = os.environ['OUT']
with db.connect(autocommit=True) as c:
    cur = c.cursor()
    print('비목_ENUM', 비목_ENUM)
    print('사업_비목', _사업_비목(사업))
    lab = L.레이블_목록(cur, 사업)
    화면 = [x for x in lab if x['비목'] in 비목_ENUM]
    print('레이블', len(lab), '화면', len(화면))
    표 = []
    for x in 화면:
        비목, 세목 = x['비목'], x['세목']
        for 금액 in (10000, 1000000):
            입력 = O.게이트_입력(금액, '포함', {})
            q = O.온톨로지_조회(cur, 사업, 비목, 세목, 입력, 확정=True, 품목=None)
            g = q['게이트']
            구분 = collections.Counter(c_['구분'] for c_ in g['조건'])
            문장 = [c_['문장'] for c_ in g['조건']]
            표.append({'표시문': x['표시문'], '금액': 금액, '규범': len(q['적용']['규범']), '종류': g['종류'], '상한': g['상한'], '조건': len(g['조건']),
                      '구분': dict(구분), '세목미정있음': any(c_['구분'] == '세목미정' for c_ in g['조건']), '문장': 문장})
    json.dump(표, open(f'{OUT}/sweep_라벨.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    print('표시문 | 1만원: 규범/조건/종류/상한 | 100만원')
    모음 = collections.defaultdict(dict)
    for t in 표:
        모음[t['표시문']][t['금액']] = t
    for k, v in 모음.items():
        a, b = v[10000], v[1000000]
        print(f"{k} | {a['규범']}/{a['조건']}/{a['종류']}/{a['상한']} | {b['규범']}/{b['조건']}/{b['종류']}/{b['상한']} | 세목미정={a['세목미정있음']}")
    무상한 = [t for t in 표 if t['상한'] is None and not t['종류']]
    print('상한 없음(가능이 나올 수 있는) 행:', len(무상한), '/', len(표), [t['표시문'] for t in 무상한])
    조건수 = sorted(t['조건'] for t in 표 if not t['종류'])
    print('LLM 경로 조건 줄: 최소', 조건수[0], '중앙', 조건수[len(조건수) // 2], '최대', 조건수[-1])
    세목있는 = {x['비목'] for x in 화면 if x['세목']}
    print('세목미정 안내가 뜨는데 고를 세목이 없는 비목:', sorted({t['표시문'] for t in 표 if t['세목미정있음'] and t['표시문'] not in 세목있는}))
    # 모든 라벨에 공통으로 뜨는 문장
    세기 = collections.Counter(s for t in 표 if t['금액'] == 1000000 and not t['종류'] for s in t['문장'])
    전체행 = sum(1 for t in 표 if t['금액'] == 1000000 and not t['종류'])
    print('100만원 LLM 경로 행 수', 전체행, '문장 빈도 상위:')
    for s, n in 세기.most_common(16):
        print(f'  {n}/{전체행} {s[:90]}')
    # 도달 분석
    노드표 = {r[0]: (r[1], r[2]) for r in cur.execute("select concept_id,종류,pref_label from corpus.concepts").fetchall()}
    시작들 = set(x['노드'] for x in 화면)
    품목들 = [k for k, v in 노드표.items() if v[0] == '품목']
    품목도달 = {}
    for p in 품목들:
        상위 = {b for _, b in L._상위_경로(cur, p, [사업])}
        품목도달[p] = sorted(상위 & 시작들)
    가림 = []
    for p in 품목들:
        rs = L.노드_결정(cur, 노드표[p][1], 사업)
        if p not in {r['concept_id'] for r in rs}:
            가림.append((p, 노드표[p][1], [r['concept_id'] for r in rs]))
    print('정식명으로 찾아도 노드_결정이 돌려주지 않는 품목(가림):', len(가림))
    for g_ in 가림[:40]:
        print('  ', g_)
    미도달품목 = [p for p, v in 품목도달.items() if not v]
    print('품목', len(품목들), '화면 라벨 아래 없는 품목', len(미도달품목), [노드표[p][1] for p in 미도달품목][:40])
    가림표 = {g_[0] for g_ in 가림}
    도달품목 = [p for p, v in 품목도달.items() if v and p not in 가림표]
    도달규범 = set()
    for s in list(시작들) + 도달품목:
        for n in L.적용_규범(cur, 사업, s)['규범']:
            도달규범.add(n['norm_id'])
    cols = ['id', '종류', 'verified', '미결', '대상', '수범자', '결과', '조건', '수치', '승인주체', '예외']
    전체 = [dict(zip(cols, r)) for r in cur.execute("select norm_id,종류,verified,해석미결,대상,수범자,결과,조건,수치,승인주체,예외 from corpus.norms").fetchall()]
    print('규범 전체', len(전체), '화면 경로 도달', len(도달규범))
    미도달 = [n for n in 전체 if n['id'] not in 도달규범]
    print('미도달 종류별', collections.Counter(n['종류'] for n in 미도달))
    print('미도달 대상 종류', collections.Counter(v.split(':')[0] for n in 미도달 for v in n['대상']))
    print('미도달 verified', collections.Counter(n['verified'] for n in 미도달))
    print('미도달 수범자', collections.Counter(s for n in 미도달 for s in (n['수범자'] or ['(없음)'])).most_common(14))
    진출자 = {'주체:진출자', '주체:창업기업등', '주체:창업기업', '주체:팀원'}
    직접 = [n for n in 미도달 if set(n['수범자'] or []) & 진출자 and n['종류'] in ('의무', '금지', '한도')]
    print('미도달 중 진출자·창업기업 수범자의 의무·금지·한도', len(직접), collections.Counter(n['종류'] for n in 직접), 'verified', sum(1 for n in 직접 if n['verified']))
    json.dump([{k: n[k] for k in ('id', '종류', 'verified', '대상', '수범자')} for n in 미도달], open(f'{OUT}/sweep_미도달규범.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    json.dump([{k: n[k] for k in ('id', '종류', 'verified', '대상', '수범자')} | {'문장': (n['결과'] or {}).get('문장')} for n in 직접], open(f'{OUT}/sweep_미도달_진출자.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    print('결과 키', collections.Counter(k for n in 전체 for k in (n['결과'] or {})))
    print('수치 분모주체 있음', sum(1 for n in 전체 if (n['수치'] or {}).get('분모주체')), '승인주체 있음', sum(1 for n in 전체 if n['승인주체']), '수범자 있음', sum(1 for n in 전체 if n['수범자']), '제재 있음', sum(1 for n in 전체 if (n['결과'] or {}).get('제재')))
    print('구조화 조건 항목', collections.Counter(c_.get('항목') for n in 전체 for c_ in ((n['조건'] or {}).get('구조화') or [])).most_common(30))
    print('구조화 조건 연산', collections.Counter(c_.get('연산') for n in 전체 for c_ in ((n['조건'] or {}).get('구조화') or [])).most_common(20))
    print('구조화 조건 항목 중 입력키에 있는 것', sorted({c_.get('항목') for n in 전체 for c_ in ((n['조건'] or {}).get('구조화') or [])} & set(G.입력키)))
    print('관계', cur.execute("select 관계,count(*) from corpus.concept_relations group by 1 order by 2 desc").fetchall())
    전체대상 = [n for n in 전체 if '비목:창업/*' in n['대상']]
    print('비목:창업/* 대상', len(전체대상), collections.Counter((n['종류'], n['verified']) for n in 전체대상))
    print('도달 의무 중 자연어 조건 있는 것', sum(1 for n in 전체 if n['id'] in 도달규범 and n['종류'] == '의무' and ((n['조건'] or {}).get('자연어'))), '/', sum(1 for n in 전체 if n['id'] in 도달규범 and n['종류'] == '의무'))
    타 = [n for n in 전체 if n['id'] in 도달규범 and n['종류'] == '의무' and n['수범자'] and not (set(n['수범자']) & 진출자)]
    print('도달 의무 중 수범자에 진출자·창업기업 없음', len(타), [(n['id'], n['수범자']) for n in 타][:30])
    print('도달 의무 중 수범자 칸 비어 있음', sum(1 for n in 전체 if n['id'] in 도달규범 and n['종류'] == '의무' and not n['수범자']))
    예외의무 = [n['id'] for n in 전체 if n['id'] in 도달규범 and n['종류'] == '의무' and n['예외']]
    print('도달 의무 중 예외 있음', len(예외의무), 예외의무[:15])
