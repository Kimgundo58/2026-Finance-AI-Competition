# -*- coding: utf-8 -*-
"""AGE 추가 조사. 결과는 판정 문맥에만 쓰며 코드 게이트에는 넣지 않는다."""
from __future__ import annotations

import ontology_age
import ontology_context
import ontology_fund
import ontology_graph
import ontology_lookup
from ontology_norm import 모든비목, 발췌_포함


def 규범ID_검증조회(cur, *, 사업, 규범_ids, 자금, 상태):
    """독립 읽기 전용 트랜잭션에서 제한 시간 안에 원문과 예외를 확인한다."""
    import ontology_query_map
    budget = 상태.get('budget')
    if budget is None:
        return {'규범': [], '근거행': [], '완료': False, '사유': '조회 예산 누락'}
    with ontology_query_map.readonly(cur, budget):
        return _규범ID_검증조회(cur, 사업=사업, 규범_ids=규범_ids, 자금=자금, 상태=상태)


def _규범ID_검증조회(cur, *, 사업, 규범_ids, 자금, 상태):
    """검증된 그래프 조회의 규범 ID를 원문·예외까지 확인한다.

    호출 예산은 그래프 실행기가 예약한다. 이 함수는 게이트를 변경하지 않고,
    하나라도 확인하지 못하면 부분 결과를 성공으로 돌려주지 않는다.
    """
    import ontology_evidence

    def 실패(사유):
        return {'규범': [], '근거행': [], '완료': False, '사유': 사유}

    if (not 사업 or not isinstance(규범_ids, list) or not 규범_ids or len(규범_ids) > 64
            or any(not isinstance(i, str) or not i.startswith('규범:') for i in 규범_ids)
            or len(set(규범_ids)) != len(규범_ids)):
        return 실패('규범 식별 또는 조회 범위 오류')
    budget, 버전 = 상태.get('budget'), 상태.get('버전')
    if not budget or not 버전 or not 상태.get('기준일') or budget.remaining_seconds() <= 0:
        return 실패('조회 예산 또는 기준 누락')
    if ontology_age.준비_버전(cur) != 버전 or budget.remaining_seconds() <= 0:
        return 실패('온톨로지 판본 변경')
    cols = ontology_lookup._규범_컬럼
    rows = cur.execute(f"""SELECT {', '.join(cols)} FROM corpus.age_norms
        WHERE norm_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = %s)
        ORDER BY norm_id""", (규범_ids, 사업)).fetchall()
    규범 = [dict(zip(cols, row)) for row in rows]
    if {n['norm_id'] for n in 규범} != set(규범_ids):
        return 실패('규범 누락 또는 사업 불일치')
    ontology_lookup._근거_붙이기(cur, 규범)
    schema = ontology_lookup._스키마()
    키표 = {v: k for k, v in (schema.get('문서키') or {}).items()}
    행 = ontology_fund.근거행_거르기(
        ontology_lookup.근거_조문(cur, 규범, 3, None, 사업=사업), 자금, 키표)
    try:
        근거 = ontology_evidence.예외_근거(cur, {'적용': {}, '추가규범': 규범,
                                          '근거행': 행, '자금': 자금}, 사업,
                                     기준일=상태['기준일'])
    except (ValueError, RuntimeError):
        return 실패('선택 규범 또는 연결 예외의 원문 확인 실패')
    if ontology_age.준비_버전(cur) != 버전 or budget.remaining_seconds() <= 0:
        return 실패('온톨로지 판본 변경')
    발췌 = '\n'.join(x for xs in 근거['발췌'].values() for x in xs)
    if not budget.add_evidence(발췌):
        return 실패('근거 예산 소진')
    미해결행 = [{'건너뜀': '조문 미특정', '경로': [관계]} for 관계 in 근거['미해결관계']]
    return {'규범': [*규범, *근거['규범']], '근거행': [*행, *근거['근거행'], *미해결행],
            '미해결관계': 근거['미해결관계'], '완료': True, '사유': None, '버전': 버전}


def 후보_목록(cur, *, 사업, 비목_id, 자금=None):
    """기존 품목 후보 범위에서 최대 3단 하위만 반환한다. 원문은 읽지 않는다."""
    if not 사업 or not 비목_id:
        raise ValueError('사업과 비목이 필요하다')
    뿌리 = [비목_id]
    if 자금 != '창업활동자금' and 비목_id != 모든비목:
        뿌리.append(모든비목)
    ids = {r[1] for r in ontology_graph.경로(cur, 뿌리, ['상위'], 역방향=True,
                                            사업들=[사업], 최대깊이=3)}
    rows = cur.execute("""SELECT concept_id, pref_label FROM corpus.age_concepts
        WHERE concept_id = ANY(%s) AND 종류 IN ('세목', '품목')
          AND (적용사업 IS NULL OR 적용사업 = %s) ORDER BY concept_id""", (list(ids), 사업)).fetchall()
    labels = {}
    for cid, label in cur.execute("""SELECT concept_id, alt_label FROM corpus.age_concept_labels
        WHERE concept_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = %s)
        ORDER BY concept_id, alt_label""", ([r[0] for r in rows], 사업)).fetchall():
        if label not in labels.setdefault(cid, []):
            labels[cid].append(label)
    return [{'id': cid, '정식명': name, '별칭': [x for x in labels.get(cid, []) if x != name]}
            for cid, name in rows]


def 후보_조회(cur, *, 사업, 비목_id, 후보_ids, 자금, 상태):
    """선택된 최대 3개 품목의 규범과 활성 원문을 검증한다.

    상태는 budget(reserve_tool/add_evidence/remaining_seconds), 호출수, 버전,
    기준일(date)을 담는다. 실패한 호출도 예산을 쓴다. 반환은 규범, 근거행,
    노드들, 완료, 사유, 버전이다. 완료는 법적 의미의 충분성이나 허용이 아니다.
    """
    def 실패(사유, 버전=None):
        return {'규범': [], '근거행': [], '노드들': [], '완료': False, '사유': 사유, '버전': 버전}
    if not isinstance(후보_ids, list) or not 1 <= len(후보_ids) <= 3 or any(not isinstance(x, str) for x in 후보_ids) or len(set(후보_ids)) != len(후보_ids):
        return 실패('후보 선택 오류')
    if not 사업 or not 비목_id or not 상태.get('기준일') or not 상태.get('버전'):
        return 실패('조회 범위 또는 기준일 누락')
    budget = 상태.get('budget')
    if budget is None or 상태.get('호출수', 0) >= 2 or budget.remaining_seconds() <= 0:
        return 실패('조회 예산 소진')
    if not budget.reserve_tool():
        return 실패('조회 예산 소진')
    상태['호출수'] = 상태.get('호출수', 0) + 1
    버전 = ontology_age.준비_버전(cur)
    if 버전 != 상태['버전']:
        return 실패('온톨로지 판본 변경', 버전)
    허용 = {x['id'] for x in 후보_목록(cur, 사업=사업, 비목_id=비목_id, 자금=자금)}
    if not set(후보_ids) <= 허용:
        return 실패('조회 범위 밖 후보', 버전)
    cols = ontology_lookup._규범_컬럼
    rows = cur.execute(f"""SELECT {', '.join(cols)} FROM corpus.age_norms
        WHERE 대상 && %s::text[] AND (적용사업 IS NULL OR 적용사업 = %s)
        ORDER BY norm_id""", (후보_ids, 사업)).fetchall()
    규범 = [dict(zip(cols, row)) for row in rows]
    if not 규범:
        return 실패('선택한 후보의 규범 없음', 버전)
    # 명시적으로 연결된 예외도 같은 사업 안에서 읽는다. 예외를 빼고 원문 검증을
    # 통과시키거나 LLM이 선택하지 않았다는 이유로 허용 단서를 버리지 않는다.
    for _ in range(3):
        규범_ids = {n['norm_id'] for n in 규범}
        누락 = {e for n in 규범 for e in (n.get('예외') or [])
                if isinstance(e, str) and e.startswith('규범:') and e not in 규범_ids}
        if not 누락:
            break
        if len(규범) + len(누락) > 64:
            return 실패('연결된 예외 범위 초과', 버전)
        rows = cur.execute(f"""SELECT {', '.join(cols)} FROM corpus.age_norms
            WHERE norm_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = %s)
            ORDER BY norm_id""", (list(누락), 사업)).fetchall()
        if not rows:
            break
        규범.extend(dict(zip(cols, row)) for row in rows)
    규범_ids = {n['norm_id'] for n in 규범}
    if any(isinstance(e, str) and e.startswith('규범:') and e not in 규범_ids
           for n in 규범 for e in (n.get('예외') or [])):
        return 실패('연결된 예외 근거 누락', 버전)
    ontology_lookup._근거_붙이기(cur, 규범)
    schema = ontology_lookup._스키마()
    키표 = {v: k for k, v in (schema.get('문서키') or {}).items()}
    제외문서 = set(ontology_lookup._다른사업_문서([사업]))
    근거행, 근거글 = [], []
    for norm in 규범:
        if not norm['근거']:
            return 실패('규범 근거 없음', 버전)
        for src in norm['근거']:
            doc, 조, 항, 발췌 = (src.get(k) for k in ('doc_id', '조번호', '항호', '발췌'))
            if doc not in 키표 or doc in 제외문서 or ontology_fund.다른_자금(자금, 키표.get(doc), 조, 항):
                return 실패('근거의 사업 또는 자금 불일치', 버전)
            row = cur.execute('''SELECT a.article_id, a."본문", d.status, d.시행일
                FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id = a.doc_id
                WHERE a.doc_id = %s AND a."조번호" = %s''', (doc, 조)).fetchone()
            if not row or row[2] != 'active' or row[3] is None or row[3] > 상태['기준일'] or not 발췌_포함(발췌, row[1]):
                return 실패('활성 원문 또는 시행일 검증 실패', 버전)
            if 항 and not any(발췌_포함(발췌, 조각) for _, 조각 in ontology_context._고른_조각(row[1], 항)):
                return 실패('근거 위치 검증 실패', 버전)
            근거글.append(row[1])
            근거행.append({'doc_id': doc, '조번호': 조, '항호': 항, 'article_id': row[0],
                         '깊이': 0, '출처': '규범근거', '경로': [f'조:{키표[doc]}#{조}{항 or ""}']})
    if ontology_age.준비_버전(cur) != 버전:
        return 실패('온톨로지 판본 변경', 버전)
    if not budget.add_evidence('\n'.join(근거글)):
        return 실패('근거 예산 소진', 버전)
    return {'규범': 규범, '근거행': 근거행, '노드들': list(후보_ids), '완료': True, '사유': None, '버전': 버전}
