# -*- coding: utf-8 -*-
"""승인 후에만 합성 지출의 실제 모델 질의 연결을 집계한다. 호출·문장 원문은 출력하지 않는다."""
from __future__ import annotations

import json
import re
import sys
from contextlib import ExitStack
from functools import wraps
from unittest.mock import patch

import ontology_context
import ontology_evidence
import ontology_fund
import ontology_lookup
import ontology_query_plan
import ontology_query_map
import ontology_query
import ontology_research
import ontology_research_plan
import research_runtime
import llm_qwen
from assemble_context import 프롬프트_구성


def 실행():
    import psycopg
    budget = research_runtime.Budget()
    이벤트 = []
    크기 = []
    원본 = ontology_query_plan.llm_호출

    def 측정(prompt, schema, **kwargs):
        크기.append(len(prompt))
        return 원본(prompt, schema, **kwargs)

    조회 = {'대상': {'비목_id': ontology_fund._창업활동자금_비목}, '자금': ontology_fund.창업활동자금,
          '게이트': {}, '적용': {'규범': []}, '근거행': [], '노드들': set()}
    입력 = {'사업명': ontology_fund._사업, '품목': 'API 이용료', '용도': '제품 개발', '질문': '개발용 API 이용료'}
    결과 = {'실행': '실패', '모델호출수': 0, '도구호출수': 0, '지도자수': 0,
          '계획프롬프트자수': 0, '조회검증성공': False, '추가규범수': 0,
          '원문위치수': 0, '문장ID수': 0, '최종문맥자수': 0,
          '실패단계': '미확인', '오류분류': '미확인'}
    실패 = {}
    sdk예외 = {}
    원본_호출 = llm_qwen._호출_1회
    def sdk감시(*args, **kwargs):
        try:
            return 원본_호출(*args, **kwargs)
        except Exception as e:
            이름 = type(e).__name__
            허용 = {'AuthenticationError', 'PermissionDeniedError', 'RateLimitError', 'APIConnectionError',
                  'NotFoundError', 'InternalServerError', 'APIStatusError', 'BadRequestError',
                  'APITimeoutError', 'TypeError', 'ValueError', 'JSONDecodeError', 'LLM실패'}
            상태 = getattr(e, 'status_code', None)
            sdk예외.update(종류=이름 if 이름 in 허용 else '기타',
                         HTTP상태=상태 if type(상태) is int and 100 <= 상태 <= 599 else None)
            raise
    def 감시(원본함수, 단계):
        @wraps(원본함수)
        def 래퍼(*args, **kwargs):
            try:
                값 = 원본함수(*args, **kwargs)
                if 단계 == '원문검증' and isinstance(값, dict) and not 값.get('완료'):
                    실패.update(단계=단계, 종류='검증실패')
                return 값
            except Exception as e:
                이름 = type(e).__name__
                실패.update(단계=단계, 종류=이름 if re.fullmatch(r'[\w]{1,80}', 이름) else '예외')
                raise
        return 래퍼
    try:
        with psycopg.connect('postgresql://suddoe_app:age-test-app@127.0.0.1:5462/suddoe',
                             options='-c default_transaction_read_only=on', autocommit=True) as conn:
            cur = conn.cursor()
            with ExitStack() as stack:
                stack.enter_context(patch.object(llm_qwen, '_호출_1회', side_effect=sdk감시))
                stack.enter_context(patch.object(ontology_query_plan, 'llm_호출', side_effect=측정))
                for 모듈, 이름, 단계 in ((ontology_query_map, 'map_for', '지도'),
                                      (ontology_query_plan, '계획', '조회계획'),
                                      (ontology_query, 'execute', '검증조회'),
                                      (ontology_research, '규범ID_검증조회', '원문검증')):
                    stack.enter_context(patch.object(모듈, 이름, side_effect=감시(getattr(모듈, 이름), 단계)))
                stack.enter_context(research_runtime.activate(budget, emit=이벤트.append))
                추가 = ontology_research_plan.추가_조회(cur, 조회, 입력)
            결과['조회검증성공'] = any(x.get('단계') == '규정추가조회' and x.get('상태') == '완료' for x in 이벤트)
            지도들 = (getattr(budget, 'ontology_state', None) or {}).get('지도') or {}
            결과['지도자수'] = len(next(iter(지도들.values())).view_json) if 지도들 else 0
            결과['추가규범수'] = len(추가.get('추가규범') or [])
            if 추가.get('추가조사상태', {}).get('상태') == '완료' and 결과['추가규범수']:
                근거 = ontology_evidence.예외_근거(cur, 추가, ontology_fund._사업)
                문맥 = ontology_context.문맥_구성(cur, 근거행=추가['근거행'], 대상노드=set(),
                    schema=ontology_lookup._스키마(), 자금=조회['자금'], 근거한정=True, 근거발췌=근거['발췌'])
                prompt, sid, _ = 프롬프트_구성(cur, 입력['질문'], {'품목': 입력['품목']}, 문맥=문맥)
                결과.update(원문위치수=len(근거['발췌']), 문장ID수=len(sid), 최종문맥자수=문맥['글자수'])
                if sid and 근거['발췌'] and 문맥['항목수'] and prompt:
                    결과['실행'] = '연결 확인'
                    결과.update(실패단계=None, 오류분류=None)
            if 결과['실행'] != '연결 확인':
                결과.update(실패단계=실패.get('단계', '추가결과'), 오류분류=실패.get('종류', '결과없음'))
    except Exception as e:
        이름 = type(e).__name__
        결과.update(실패단계=실패.get('단계', '연결검사'),
                  오류분류=실패.get('종류', 이름 if re.fullmatch(r'[\w]{1,80}', 이름) else '예외'))
    결과['모델호출수'] = budget.llm_used
    결과['도구호출수'] = budget.tools_used
    결과['계획프롬프트자수'] = max(크기, default=0)
    결과['SDK예외'] = sdk예외.get('종류')
    결과['HTTP상태'] = sdk예외.get('HTTP상태')
    print(json.dumps(결과, ensure_ascii=False))
    return 결과['실행'] == '연결 확인'


if __name__ == '__main__':
    if '--run' not in sys.argv:
        print('실제 모델 호출은 별도 실행 승인 후 --run을 지정한다.')
    else:
        sys.exit(0 if 실행() else 1)
