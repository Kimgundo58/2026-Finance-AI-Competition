# -*- coding: utf-8 -*-
"""예외 순환·누락·범위·원문 검사. DB 없이 실행한다."""
from datetime import date
from unittest.mock import patch

import ontology_evidence as 검증


def main():
    cols = 검증.ontology_lookup._규범_컬럼
    def norm(i, 예외=(), 발췌=None):
        return {'norm_id': i, '종류': '허용', '적용사업': None, '대상': ['품목:x'],
                '예외': list(예외), 'verified': False,
                '근거': [{'doc_id': 'doc', '조번호': i, '항호': '①', '발췌': 발췌 or i}]}

    a = norm('규범:a', ['규범:b'])
    b = norm('규범:b', ['규범:c'])
    c = norm('규범:c', ['규범:a'])
    db = {x['norm_id']: x for x in (a, b, c)}
    class Cursor:
        def __init__(self):
            self.status = 'active'
            self.date = date(2026, 1, 1)
            self.bad_text = False
        def execute(self, sql, params):
            self.sql, self.params = sql, params
            return self
        def fetchall(self):
            if 'age_norms' in self.sql:
                return [tuple(db[i].get(k) for k in cols) for i in self.params[0] if i in db]
            if 'doc_articles' in self.sql:
                return [(i, 'doc', i, '① ' + ('오류' if self.bad_text else i), self.status, self.date)
                        for i in self.params[0]]
            raise AssertionError(self.sql)

    def 근거(cur, norms):
        for n in norms:
            n['근거'] = db[n['norm_id']]['근거']

    def 조문(cur, norms, depth, 대상, *, 사업):
        assert depth == 3 and 대상 is None
        return [{'article_id': n['norm_id'], 'doc_id': 'doc', '조번호': n['norm_id'],
                 '항호': '①', '출처': '규범근거', '경로': ['조:doc#' + n['norm_id']]}
                for n in norms]

    def 실패(fn):
        try:
            fn()
        except ValueError:
            return
        raise AssertionError('검증 실패가 통과됨')

    기존행 = {'article_id': '규범:a', 'doc_id': 'doc', '조번호': '규범:a',
           '항호': '①', '출처': '규범근거', '경로': ['조:doc#규범:a']}
    조회 = {'적용': {'규범': [a]}, '근거행': [기존행], '자금': None}
    cur = Cursor()
    with patch.object(검증.ontology_age, '준비_버전', return_value='v'), \
         patch.object(검증.ontology_lookup, '_스키마', return_value={'문서키': {'키': 'doc'}}), \
         patch.object(검증.ontology_lookup, '_다른사업_문서', return_value=[]), \
         patch.object(검증.ontology_lookup, '_근거_붙이기', side_effect=근거), \
         patch.object(검증.ontology_lookup, '근거_조문', side_effect=조문):
        결과 = 검증.예외_근거(cur, 조회, '사업', 기준일=date(2026, 11, 9))
        assert {n['norm_id'] for n in 결과['규범']} == {'규범:b', '규범:c'}
        assert len(결과['근거행']) == 2 and len(결과['발췌']) == 3
        assert 조회['적용']['규범'] == [a] and a['verified'] is False
        cur.status = 'inactive'
        실패(lambda: 검증.예외_근거(cur, 조회, '사업'))
        cur.status = 'active'
        cur.date = None
        assert 검증.예외_근거(cur, 조회, '사업')['시행일미확인'] == ['doc']
        cur.date = date(2027, 1, 1)
        실패(lambda: 검증.예외_근거(cur, 조회, '사업', 기준일=date(2026, 11, 9)))
        cur.date = date(2026, 1, 1)
        cur.bad_text = True
        assert 검증.예외_근거(cur, 조회, '사업')['근거행']  # 저장 발췌가 달라도 실제 위치가 있으면 원문을 사용
        cur.bad_text = False
        실패(lambda: 검증.예외_근거(cur, {**조회, '근거행': []}, '사업'))
        bad = norm('규범:a', ['규범:없는예외'])
        실패(lambda: 검증.예외_근거(cur, {**조회, '적용': {'규범': [bad]}}, '사업'))
        many = norm('규범:a', [f'규범:{i}' for i in range(65)])
        실패(lambda: 검증.예외_근거(cur, {**조회, '적용': {'규범': [many]}}, '사업'))
        missing = {'건너뜀': '조문 미특정', '경로': ['미특정:규정 제3조']}
        실패(lambda: 검증.예외_근거(cur, {**조회, '근거행': [기존행, missing]}, '사업'))
        generic = {'건너뜀': '조문 미특정', '경로': ['미특정:관련 법령']}
        assert 검증.예외_근거(cur, {**조회, '근거행': [기존행, generic]}, '사업')['미해결관계'] == ['미특정:관련 법령']
        actor = {'건너뜀': '조문 아님', '경로': ['조:doc#제1조', '기관:담당자']}
        assert 검증.예외_근거(cur, {**조회, '근거행': [기존행, actor]}, '사업')['발췌']
        다른 = {'doc_id': 'other', '조번호': '제1조', '항호': None, '발췌': '다른 자금'}
        mixed = {**a, '근거': [*a['근거'], 다른]}
        same_scope = {**a, '근거': [*a['근거'], {**다른, 'doc_id': 'doc'}]}
        with patch.object(검증.ontology_lookup, '_스키마', return_value={'문서키': {'키': 'doc', '다른': 'other'}}), \
             patch.object(검증.ontology_fund, '다른_자금', side_effect=lambda 자금, 키, 조, 항: 키 == '다른'):
            r = 검증.예외_근거(cur, {**조회, '적용': {'규범': [mixed]}, '자금': '자금'}, '사업')
            assert r['제외']['다른자금'] == 1
            실패(lambda: 검증.예외_근거(cur, {**조회, '적용': {'규범': [same_scope]}, '자금': '자금'}, '사업'))
            미정 = {**norm('규범:미정'), '근거': [다른]}
            우선 = {**미정, 'norm_id': '규범:우선', '종류': '우선순위',
                  '조건': {'범위': '상충시', '하위': 'L3'}}
            기본 = {**조회, '적용': {'규범': [a, 우선], '공통미정': [미정]}, '자금': '자금'}
            assert 검증.예외_근거(cur, 기본, '사업')['발췌']
            실패(lambda: 검증.예외_근거(cur, 기본, '사업', l3있음=True))
            # 일반 규범이나 선택된 규범의 예외면 자금 밖이라는 이유만으로 빠지지 않는다.
            실패(lambda: 검증.예외_근거(cur, {**기본, '적용': {'규범': [미정]}}, '사업'))
            연결 = {**a, '예외': ['규범:미정']}
            실패(lambda: 검증.예외_근거(cur, {**기본, '적용': {'규범': [연결], '공통미정': [미정]}}, '사업'))
    print('ontology_evidence_check 통과')


def 금액_검사():
    import orchestrate as O
    for 값, 기대 in [('가능', '확인 필요'), ('확인 필요', '확인 필요'), ('불가', '불가')]:
        응답, 사유 = {'판정': 값, '요약': '시험'}, []
        O._시행일_미확인_반영(응답, 사유)
        assert 응답['판정'] == 기대 and 응답['시행일미확인'] and '시행일' in 응답['요약'] and 사유
    assert '시행일미확인' in O.온톨로지결과_칸
    from server.judge_response import 파이프라인결과_매핑
    from server.models import 판정요청
    mapped = 파이프라인결과_매핑({'판정': '확인 필요', '요약': '시행일 확인', '시행일미확인': True}, 판정요청(정규화={}))
    assert mapped['시행일미확인'] is True
    def run(제시):
        s = O._판정상태(질문='이용료 집행', 사업명='사업', org_id=None, plan_id=None,
                      기록=False, dry=False, 주입=None, 참조확장_사용=True)
        s.온톨로지 = {'적용': {'규범': []}, '근거행': [], '대상': None, '자금': None}
        s.게이트입력, s.게이트결과 = {'금액': 1000}, {}
        s.정규화결과 = {'품목': '이용료', '용도': '개발'}
        s.출력 = {'판정': '가능', '요약': '5만원까지 가능합니다.'}
        s.문장맵 = {'S01': ('article', 1, None)}
        s.제시원문 = {'S01': 제시}
        검증결과 = {'판정': '가능', '요약': '5만원까지 가능합니다.', '전제목록': [],
                '인용목록': [{'문장ID': 'S01', '원문': '최대 5만원까지 지원합니다.'}]}
        with patch.object(O, '검증', return_value=(검증결과, [])), \
             patch.object(O, '_할일_설명', return_value={}), \
             patch.object(O.ontology_context, '근거경로들', return_value=[]), \
             patch.object(O.ontology_lookup, '_스키마', return_value={}), \
             patch.object(O, '_종료', side_effect=lambda 상태, 응답, 기록: 응답):
            return O._새_검증단계(s, None)
    assert run('관련 요건을 만족해야 합니다.')['판정'] == '확인 필요'
    assert run('최대 5만원까지 지원합니다.')['판정'] == '가능'
    print('제시하지 않은 원문 금액 하향 검사 통과')


def 예외_처리_검사():
    import orchestrate as O
    assert O._미해결_합치기([{'대상': '기존'}, '미특정:법'], ['미특정:법', '미특정:기준']) == [
        {'대상': '기존'}, '미특정:법', '미특정:기준']
    s = O._판정상태(질문='지출', 사업명='사업', org_id=None, plan_id=None,
                  기록=False, dry=False, 주입='empty', 참조확장_사용=True)
    s.온톨로지 = {'근거행': [], '노드들': set(), '대상': None, '자금': None}
    with patch.object(O.ontology_lookup, '_스키마', return_value={}), \
         patch.object(O.ontology_context, '문맥_구성', return_value={'L3_초과': True, '근거초과': True, '빠진수': 1}), \
         patch.object(O, '_종료', side_effect=lambda 상태, 응답, 기록: 응답):
        결과 = O._새_검색단계(s)
    assert 결과['판정'] == '확인 필요' and 결과['게이트'] == 'B' and 'L3 미연결' in 결과['검증사유'][0]
    print('L3 안내 우선·미해결 관계 병합 검사 통과')


def 항없는_원문_검사():
    import assemble_context
    import llm_validate
    import ontology_context
    본문 = '제출 자료는 다음과 같습니다. ① 거래명세서와 ② 결제확인서를 제출하세요. 이후 담당자가 확인합니다.'
    발췌 = '다음과 같습니다. ① 거래명세서와 ② 결제확인서를 제출하세요.'
    cur = ontology_context._가짜커서([(7, '문서', '제1조', '자료', 본문)], [])
    행 = {'article_id': 7, 'doc_id': '문서', '조번호': '제1조', '항호': None,
          '출처': '규범근거', '경로': ['조:문서#제1조']}
    맵 = {('문서', '제1조', None): [발췌]}
    문맥 = ontology_context.문맥_구성(cur, 근거행=[행], schema={}, 근거한정=True, 근거발췌=맵)
    제시 = {}
    프롬프트, 문장맵, _ = assemble_context.프롬프트_구성(cur, '자료를 확인하나요?', {}, 문맥=문맥, 제시원문=제시)
    assert len(문장맵) == 1
    sid = next(iter(문장맵))
    assert 문장맵[sid] == ('article', 7, None)
    assert 발췌 in 제시[sid] and 발췌 in 프롬프트
    assert llm_validate._항_추출(본문, 문장맵[sid][2]) == (본문, '조전체')
    다른발췌 = ontology_context.문맥_구성(cur, 근거행=[행], schema={}, 근거한정=True,
                                   근거발췌={('문서', '제1조', None): ['원문에 없는 저장 발췌']})
    제시2 = {}
    assemble_context.프롬프트_구성(cur, '자료를 확인하나요?', {}, 문맥=다른발췌, 제시원문=제시2)
    assert 본문 in 제시2.values() and all('원문에 없는 저장 발췌' not in x for x in 제시2.values())
    print('항호 없는 DB 원문·SID 검사 통과')


def 표_인용_검사():
    import assemble_context
    import llm_validate
    import ontology_context
    assert ontology_context._표_항호('②표-4')
    내부표 = ('제35조(용역) ① 일반.\n② <표-4>에 따라 심의한다.\n< 표-4 > 심의 기준\n'
           '① 첫 항목\n② 두 항목\n③ 세 항목\n- 13 -\n③ <표-4>에 따라 검토한다.\n④ 다음 항.')
    assert 검증._위치_확인(내부표, '②표-4', '제35조')
    assert not 검증._위치_확인('제35조(용역) ① 일반. ③ 다음 항.', '②표-4', '제35조')
    assert not 검증._위치_확인(내부표.replace('- 13 -\n', ''), '②표-4', '제35조')
    assert not 검증._위치_확인(내부표.replace('③ 세 항목', '- 12 -\n③ 표 안 항목\n표의 나머지\n③ 세 항목'), '②표-4', '제35조')
    호표 = ('제22조(비목) ① 일반.\n② 사항은 다음과 같다.\n1. <표-3>을 따른다.\n'
          '< 표-3 > 심의 항목\n① 첫 항목\n② 두 항목\n③ 세 항목\n'
          '2. 국외 출장 기준\n3. 자산 관리 기준')
    assert 검증._위치_확인(호표, '②표-3', '제22조')
    assert not 검증._위치_확인(호표.replace('3. 자산 관리 기준', '자산 관리 기준'), '②표-3', '제22조')
    선택표 = ontology_context._근거_조각(호표, '②표-3', '제22조')[0][1]
    assert '③ 세 항목' in 선택표 and '2. 국외 출장 기준' not in 선택표
    선택 = ontology_context._근거_조각(내부표, '②표-4', '제35조')
    assert any('① 첫 항목' in 글 for _, 글 in 선택)
    assert all('③ <표-4>에 따라 검토한다' not in 글 for _, 글 in 선택)
    두칸 = '| 비목 | 집행 증빙서류 |\n| --- | --- |\n| 지급수수료 | 선택한 증빙 |\n| | 이어지는 증빙 |\n| 재료비 | 다른 증빙 |'
    assert 검증._위치_확인(두칸, '/지급수수료', '참고4')
    assert not 검증._위치_확인(두칸, '/없는비목', '참고4')
    assert ontology_context._근거_조각(두칸, '/지급수수료', '참고4') == [('/지급수수료', '선택한 증빙 이어지는 증빙')]
    assert llm_validate._항_추출(두칸, '/지급수수료') == ('선택한 증빙 이어지는 증빙', '표행')
    반복머리 = '| 비 목 | 기 준 |\n| --- | --- |\n| 지급 수수료 | 첫 내용 |\n| 비 목 | 기 준 |\n| | 둘째 내용 |\n| 여비 | 다른 내용 |'
    assert ontology_context._근거_조각(반복머리, '/지급수수료', '참고2') == [('/지급수수료', '첫 내용 둘째 내용')]
    세칸반복 = ('| 비 목 | 주요 내용 |  |\n| --- | --- | --- |\n'
            '| 외주 용역비 | 기 준 | 기준 내용 |\n| | 유 의 사 항 | |\n'
            '| 비 목 | 주요 내용 | |\n| | | 유의 내용 |\n| 여비 | 기 준 | 다른 내용 |')
    assert ontology_context._근거_조각(세칸반복, '/외주용역비/유의사항', '참고3') == [('/외주용역비/유의사항', '유의 내용')]
    assert ontology_context._근거_조각(세칸반복, '/외주용역비/기준', '참고3') == [('/외주용역비/기준', '기준 내용')]
    교육표 = ('창업중심대학 창업기업 사업비 집행기준\n| 교육 훈련비 | 기 준 | 같은 비목 내용 |\n'
          '| | 유 의 사 항 | |\n| 광고 선전비 | 기 준 | 다른 비목 내용 |')
    assert ontology_context._근거_조각(교육표, '/교육훈련비/유의사항', '참고3') == [('/교육훈련비/유의사항', '같은 비목 내용')]
    assert not 검증._위치_확인(교육표.replace('광고 선전비', '다른 비목'), '/교육훈련비/유의사항', '참고3')
    assert not 검증._위치_확인(교육표 + '\n' + 교육표, '/교육훈련비/유의사항', '참고3')
    초격차표 = (반복머리 + '\n[참고2] 초격차 창업기업 사업비 집행기준\n'
             '· (기술이전비) 선택한 세부 기준\n· (학회·세미나 참가비) 다음 기준')
    assert '선택한 세부 기준' in ontology_context._근거_조각(초격차표, '/지급수수료', '참고2')[0][1]
    assert '다음 기준' not in ontology_context._근거_조각(초격차표, '/지급수수료', '참고2')[0][1]
    assert '선택한 세부 기준' not in ontology_context._근거_조각(초격차표 + '\n· (기술이전비) 중복', '/지급수수료', '참고2')[0][1]
    세칸 = ('| 구분 |  | 내용 |\n| --- | --- | --- |\n| 결 제 수 단 별 | 사업비 카드 | 사용 원칙 |\n'
          '|  |  | 이어지는 내용 |\n| 금 액 별 | 기준 | 다른 내용 |\n\n□ 사업비 집행·청구\n자료')
    선택행 = ontology_context._근거_조각(세칸, '/사업비집행·청구/결제수단별', '참고5')
    assert 선택행 == [('/사업비집행·청구/결제수단별', '사업비 카드 | 사용 원칙 이어지는 내용')]
    assert not 검증._위치_확인(세칸.replace('□ 사업비 집행·청구', ''), '/사업비집행·청구/결제수단별', '참고5')
    assert llm_validate._항_추출(세칸, '/사업비집행·청구/결제수단별')[0] == 선택행[0][1]
    제한표 = '| 구분 | 내용 |\n| --- | --- |\n| 지급불가 대상 | 금지 내용 |\n| 사업비 환수 | 환수 내용 |\n'
    assert 검증._위치_확인(제한표, '/사업비지급제한및환수', '참고5')
    assert not 검증._위치_확인(제한표 + '\n' + 제한표, '/사업비지급제한및환수', '참고5')
    assert not 검증._위치_확인(제한표.replace('사업비 환수', '다른 항목'), '/사업비지급제한및환수', '참고5')
    반복 = '첫 문맥: 같은 발췌. 둘째 문맥: 같은 발췌.'
    구간 = ontology_context._발췌_구간([(None, 반복)], ['같은 발췌'], 중복모두=True)
    assert len(구간[None]) == 2 and all(반복[a:b] == '같은 발췌' for a, b in 구간[None])
    try:
        ontology_context._발췌_구간([('②', 반복)], ['같은 발췌'])
    except ValueError:
        pass
    else:
        raise AssertionError('특정 항의 모호한 발췌가 통과됨')
    표 = '| 비목 | 정의 |\n| --- | --- |\n| 이용료 | 선택한 표 발췌 |\n| 기타 | 무관한 표 행 |\n'
    본문 = 표 + '\n제36조(비목) ① 일반 근거 내용은 표-10을 따릅니다.\n< 표-10 비목 >\n② 다른 항목입니다.'
    cur = ontology_context._가짜커서([(7, '문서', '제36조', '비목', 본문)], [])
    기본 = {'article_id': 7, 'doc_id': '문서', '조번호': '제36조', '출처': '규범근거'}
    표행 = {**기본, '항호': '①표-10', '경로': ['조:문서#제36조①표-10']}
    조행 = {**기본, '항호': None, '경로': ['조:문서#제36조']}
    항행 = {**기본, '항호': '①', '경로': ['조:문서#제36조①']}
    발췌 = {('문서', '제36조', '①표-10'): ['선택한 표 발췌'],
          ('문서', '제36조', None): ['일반 근거 내용은'],
          ('문서', '제36조', '①'): ['일반 근거 내용은']}
    문맥 = ontology_context.문맥_구성(cur, 근거행=[표행, 조행, 항행], schema={}, 근거한정=True, 근거발췌=발췌)
    제시 = {}
    prompt, 맵, _ = assemble_context.프롬프트_구성(cur, '사용 가능한가요?', {}, 문맥=문맥, 제시원문=제시)
    assert '선택한 표 발췌' in prompt and '일반 근거 내용은' in prompt and '무관한 표 행' in prompt
    assert ('article', 7, None) in 맵.values() and ('article', 7, '①') in 맵.values()
    assert llm_validate._항_추출(본문, None) == (본문, '조전체')
    bare = {**기본, '항호': '표-10', '출처': '개념근거', '경로': ['조:문서#제36조표-10']}
    문맥2 = ontology_context.문맥_구성(cur, 근거행=[표행, bare, 조행, 항행], schema={}, 근거한정=True, 근거발췌=발췌)
    제시2 = {}
    prompt2, 맵2, _ = assemble_context.프롬프트_구성(cur, '사용 가능한가요?', {}, 문맥=문맥2, 제시원문=제시2)
    assert '선택한 표 발췌' in prompt2 and '무관한 표 행' in prompt2  # 개념 근거가 표 전체를 지정했다
    assert '다른 항목입니다' in prompt2 and list(맵2.values()).count(('article', 7, None)) == 1
    표만 = ontology_context.문맥_구성(cur, 근거행=[표행], schema={}, 근거한정=True,
                               근거발췌={('문서', '제36조', '①표-10'): ['원문과 다른 저장 발췌']})
    표프롬프트, _, _ = assemble_context.프롬프트_구성(cur, '질문', {}, 문맥=표만)
    assert '무관한 표 행' in 표프롬프트 and '다른 항목입니다' not in 표프롬프트
    try:
        ontology_context.문맥_구성(cur, 근거행=[{**표행, '항호': '①표-99'}], schema={}, 근거한정=True,
                              근거발췌={('문서', '제36조', '①표-99'): []})
    except ValueError:
        pass
    else:
        raise AssertionError('없는 표 위치가 통과됨')
    print('표·일반항·조 범위와 없는 표 위치 검사 통과')


def 실제_항없는_검사():
    import psycopg
    import ontology_fund
    import assemble_context
    from llm_validate import 근거_인용
    with psycopg.connect('postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5',
                         options='-c default_transaction_read_only=on') as conn, conn.cursor() as cur:
        rows = cur.execute('''SELECT a.article_id, a.doc_id, a."조번호", a."본문", p.owner_id, p.발췌
            FROM corpus.doc_articles a JOIN corpus.age_provenance p
              ON p.doc_id=a.doc_id AND p.조번호=a."조번호"
            WHERE p.owner_kind='규범' AND p.항호 IS NULL AND a."조번호"=%s''', ('p15-2',)).fetchall()
        대상 = next(r for r in rows if 검증.발췌_포함(r[5], r[3]) and
                  not any(검증.발췌_포함(r[5], 조각) for _, 조각 in 검증.ontology_context._조_조각(r[3])))
        aid, doc, 조, 본문, nid, 발췌 = 대상
        src = {'doc_id': doc, '조번호': 조, '항호': None, '발췌': 발췌}
        행 = {'article_id': aid, 'doc_id': doc, '조번호': 조, '항호': None,
             '출처': '규범근거', '경로': [f'조:안내책자#{조}']}
        조회 = {'적용': {'규범': [{'norm_id': nid, '예외': [], '근거': [src]}]}, '근거행': [행]}
        근거 = 검증.예외_근거(cur, 조회, ontology_fund._사업, 기준일=date(2026, 11, 9))
        문맥 = 검증.ontology_context.문맥_구성(cur, 근거행=[행], schema=검증.ontology_lookup._스키마(),
                                         근거한정=True, 근거발췌=근거['발췌'])
        제시 = {}
        _, 문장맵, _ = assemble_context.프롬프트_구성(cur, '자료를 확인하나요?', {}, 문맥=문맥, 제시원문=제시)
        assert len(문장맵) == 1 and next(iter(문장맵.values())) == ('article', aid, None)
        인용 = 근거_인용(문장맵, conn=conn)
        assert len(인용) == 1 and 인용[0]['원문'] == 본문 and 검증.발췌_포함(발췌, next(iter(제시.values())))
        print('5462 읽기 전용 실제 책자 연속 발췌·SID·원문 인용 통과')


if __name__ == '__main__':
    main()
    금액_검사()
    예외_처리_검사()
    항없는_원문_검사()
    표_인용_검사()
    import sys
    if '--db-cross' in sys.argv:
        실제_항없는_검사()
    if '--db' in sys.argv:
        import psycopg
        import orchestrate
        import ontology_fund
        with psycopg.connect('postgresql://postgres:devpw@127.0.0.1:5462/suddoe?connect_timeout=5',
                             options='-c default_transaction_read_only=on') as conn:
            with conn.cursor() as cur:
                입력 = orchestrate.게이트_입력(100000, '포함', None)
                조회 = orchestrate.온톨로지_조회(cur, ontology_fund._사업, '창업활동비', '지급수수료',
                                            입력, 확정=True, 품목='API 이용료')
                print('미확인 근거:', [(r.get('건너뜀'), (r.get('경로') or [])[-1:]) for r in 조회['근거행'] if r.get('건너뜀')])
                기존 = {(r.get('doc_id'), r.get('조번호'), r.get('항호')) for r in 조회['근거행']}
                누락 = [(n['norm_id'], g.get('doc_id'), g.get('조번호'), g.get('항호'))
                      for n in 검증._규범_모으기(조회).values() for g in n.get('근거') or []
                      if (g.get('doc_id'), g.get('조번호'), g.get('항호')) not in 기존]
                print('기존 규범 근거 누락:', len(누락), 누락[:3])
                키표 = {v: k for k, v in (검증.ontology_lookup._스키마().get('문서키') or {}).items()}
                다른자금 = [x for x in 누락 if 검증.ontology_fund.다른_자금(조회.get('자금'), 키표.get(x[1]), x[2], x[3])]
                print('누락 중 다른 자금:', len(다른자금), '그 외:', len(누락)-len(다른자금))
                모두다른 = [n['norm_id'] for n in 검증._규범_모으기(조회).values()
                           if n.get('근거') and all((g.get('doc_id'), g.get('조번호'), g.get('항호')) not in 기존
                           and 검증.ontology_fund.다른_자금(조회.get('자금'), 키표.get(g.get('doc_id')), g.get('조번호'), g.get('항호')) for g in n['근거'])]
                print('전체 근거가 다른 자금인 규범:', len(모두다른), 모두다른[:3])
                b4 = 검증.ontology_context.규범_요약(조회['적용'], 조회['게이트'], 분류표시='지급수수료', 확정=True)
                표시 = [(n['종류'], n['norm_id']) for n in 검증._규범_모으기(조회).values()
                      if n['norm_id'] in 모두다른 and any((g.get('발췌') or '') in (b4 or '')
                      for g in n.get('근거') or [] if g.get('발췌'))]
                print('다른 자금만 근거인 B4 노출:', len(표시), 표시[:8])
                전부 = [(n['종류'], n['norm_id']) for n in 검증._규범_모으기(조회).values()
                     if any(g.get('발췌') and g['발췌'] in (b4 or '') and
                     검증.ontology_fund.다른_자금(조회.get('자금'), 키표.get(g.get('doc_id')), g.get('조번호'), g.get('항호'))
                     for g in n.get('근거') or [])]
                print('다른 자금 provenance의 B4 노출:', len(전부), 전부[:8])
                범위밖만 = [(n['종류'], n['norm_id']) for n in 검증._규범_모으기(조회).values()
                    if any(g.get('발췌') and g['발췌'] in (b4 or '') and
                    검증.ontology_fund.다른_자금(조회.get('자금'), 키표.get(g.get('doc_id')), g.get('조번호'), g.get('항호')) and
                    not any(h.get('발췌') == g['발췌'] and not 검증.ontology_fund.다른_자금(조회.get('자금'), 키표.get(h.get('doc_id')), h.get('조번호'), h.get('항호')) for h in n.get('근거') or [])
                    for g in n.get('근거') or [])]
                print('범위 내 동일 발췌 없는 B4:', len(범위밖만), 범위밖만[:8])
                결과 = 검증.예외_근거(cur, 조회, ontology_fund._사업, 기준일=date(2026, 11, 9))
                print('읽기 전용 AGE 예외·근거 확인:', len(결과['규범']), len(결과['발췌']))
