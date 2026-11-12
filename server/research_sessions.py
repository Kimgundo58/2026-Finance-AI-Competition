# -*- coding: utf-8 -*-
"""단일 서버 프로세스의 서명된 조사 세션. 재시작 시 만료하며 실패는 재확인으로 처리한다."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field

from scripts._lib.paths import ensure_on_path

ensure_on_path()
import research_runtime

_키 = secrets.token_bytes(32)
_잠금 = threading.RLock()
_세션 = {}
_TTL = 24 * 3600
_최대 = 2000


class 재질문(ValueError):
    pass


@dataclass
class Session:
    owner: tuple
    expires: float
    budget: object = field(default_factory=research_runtime.Budget)
    lock: object = field(default_factory=threading.Lock)
    입력: str | None = None
    버전: str | None = None
    질문세트: str | None = None
    질문결과: dict | None = None
    조사: dict = field(default_factory=lambda: {'상태': '없음', '후보': [], '출처': []})
    조사완료: bool = False
    확인답키: str | None = None
    판정요청키: str | None = None
    판정결과: tuple | None = None
    판정키: str | None = None          # 판정결과를 만든 요청의 메인 캐시 키(routes_judge). 다르면 다시 판정한다
    분류: dict | None = None           # ③ 서버가 정한 분류 {비목, 세목, 표시문, 품목}. 판정 요청의 확정비목 · 세목이 이 값이면 입력키는 분류 전 값으로 맞춘다
    노드결정: dict | None = None       # ③ 이 고른 품목 노드 {id, 확신, 품목}. 질문 LLM 실패와 상관없이 판정이 읽는다(routes_judge)
    품목설명: str | None = None        # ③ 조사 결과 없이 [직접 입력하기] 한 설명. 판정 요청에는 없어 답변_검증 이 여기서 읽는다
    판정조사: dict | None = None       # ④-2 판정용 조사 {상태, 요건결과: {조건키: …}, 출처}. 질문 요청이 저장하고 답변_검증 이 판정에 넘긴다
    분류실패: bool = False            # ③ 분류가 장애(DB · LLM)로 끝났다. 질문결과를 같은 답에 다시 내주지 않고 분류를 다시 한다(routes_questions)


def _서명(token):
    return hmac.new(_키, token.encode('ascii'), hashlib.sha256).hexdigest()


def 생성(org, account):
    now = time.monotonic()
    with _잠금:
        for k in [k for k, s in _세션.items() if s.expires <= now]:
            del _세션[k]
        # 가득 차면 가장 먼저 끝날 세션부터 지운다. 오류로 정규화 · 질문을 막지 않는다(리뷰 28-7).
        # 지운 세션으로 다시 오면 「만료」(400)라 화면이 새 조사를 시작한다
        if len(_세션) >= _최대:
            for k in sorted(_세션, key=lambda k: _세션[k].expires)[:len(_세션) - _최대 + 1]:
                del _세션[k]
        token = secrets.token_urlsafe(32)
        _세션[token] = Session((org, account), now + _TTL)
    return token + '.' + _서명(token)


def 조회(조사ID, org, account):
    if not isinstance(조사ID, str) or len(조사ID) > 200:
        return None
    token, sep, 서명 = 조사ID.partition('.')
    if not sep or not token.isascii() or not 서명.isascii() or not hmac.compare_digest(_서명(token), 서명):
        return None
    with _잠금:
        s = _세션.get(token)
        if s is None or s.expires <= time.monotonic() or s.owner != (org, account):
            return None
        return s


def 입력키(사업, 비목, 세목, 품목, 금액, 용도, 부가세구분=None, 질문원문=None):
    값 = [x.strip() if isinstance(x, str) else x for x in (사업, 비목, 세목, 품목, 금액, 용도, 부가세구분, 질문원문)]
    값 = [None if x == '' else x for x in 값]
    if 값[6] == '모름':
        값[6] = None
    if isinstance(값[4], (int, float)) and not isinstance(값[4], bool):
        값[4] = float(값[4])
    return hashlib.sha256(json.dumps(값, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def 후보_답변(s, 답변):
    """서버 저장 후보 ID에만 사용자의 답을 결합한다. 답은 맞아요 | 직접입력(+정정=사용자 설명) 두 가지(재설계 11-11 ②).

    후보마다 답이 있어야 다음 단계로 간다(답하지 않은 후보가 있으면 ValueError). 직접입력 설명은 ③ 분류 LLM 의 자료다.
    """
    후보 = s.조사.get('후보') or []
    표 = {x['id']: x for x in 후보}
    if len(표) != len(후보):
        raise ValueError('조사 후보 오류')
    답표 = {}
    for a in 답변:
        if a['id'] not in 표 or a['id'] in 답표 or a.get('답') not in ('맞아요', '직접입력'):
            raise ValueError('조사 답변 오류')
        if a['답'] == '직접입력' and not (a.get('정정') or '').strip():
            raise ValueError('직접 입력 설명 없음')
        if a.get('정정') and a['답'] != '직접입력':
            raise ValueError('조사 정정 오류')
        답표[a['id']] = a
    if set(답표) != set(표):
        raise ValueError('답하지 않은 조사 후보')
    확인, 업체 = [], []
    for c in 후보:
        a = 답표[c['id']]
        row = {**copy.deepcopy(c), '답': a['답']}
        if a.get('정정'):
            row['정정'] = a['정정'].strip()
        확인.append(row)
        if c.get('유형') == '업체이용조건':
            업체.append(row)
    return {'상태': s.조사.get('상태', '없음'), '확인한정보': 확인,
            '업체이용조건': 업체, '미확인': [], '출처': copy.deepcopy(s.조사.get('출처') or []),
            **{k: copy.deepcopy(s.조사[k]) for k in ('부족정보',) if k in s.조사},
            **({'사유': s.조사['사유']} if s.조사.get('사유') else {})}


def 설명_붙이기(조사, 품목설명):
    """[직접 입력하기] 품목 설명을 확인 결과에 넣는다. 확인정보_문맥 이 질문 · 판정 프롬프트에 사용자 진술로 넣는다."""
    if isinstance(품목설명, str) and 품목설명.strip():
        조사['품목설명'] = 품목설명.strip()
    return 조사


def 답변키(조사):
    답 = [{'id': x['id'], '답': x['답'], '정정': x.get('정정') or None}
         for x in [*조사['확인한정보'], *조사['미확인']]]
    값 = sorted(답, key=lambda x: x['id'])
    if 조사.get('품목설명'):
        값 = {'답': 값, '품목설명': 조사['품목설명']}
    return hashlib.sha256(json.dumps(값, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def 화면_조사(조사):
    """판정 응답 · 판정 캐시에 넣는 조사 값. ④-2 판정용 조사(조건키 · 조건 번호가 든 서버 전용 값)는 뺀다."""
    return {k: v for k, v in 조사.items() if k != '판정조사'} if isinstance(조사, dict) else 조사


def 답변_검증(s, 답변, 질문세트, 입력, 버전):
    if s.입력 != 입력 or s.버전 != 버전 or s.질문세트 != 질문세트 or s.질문결과 is None:
        raise ValueError('조사 입력 또는 질문이 변경되었습니다')
    조사 = 설명_붙이기(후보_답변(s, 답변), s.품목설명)
    if s.확인답키 != 답변키(조사):
        raise 재질문('확인 내용이 변경되어 질문을 다시 만들어야 합니다')
    # ④-2 판정용 조사 결과는 답키 계산 뒤에 붙인다(답키는 사용자가 확인한 내용만으로 만든다). 판정이 조사["판정조사"] 로 읽는다
    return {**조사, '판정조사': s.판정조사} if isinstance(조사, dict) and s.판정조사 else 조사
