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
    재조사: dict | None = None         # ③-R 재조사 결과 {후보: [...]}. 세션.조사 와 따로 둔다(조사답변 id 는 세션.조사 후보와 맞아야 한다).
                                       # None 이 아니면 재조사를 이미 했다(한 번만 한다, 스펙 8-2 서버 순서)
    재조사확인: list | None = None     # 재조사 후보 + 사용자 답. 한 번 받으면 남기고 다음 요청 · 판정(답변_검증)이 여기서 읽는다(품목설명 과 같다)
    설명정리: dict = field(default_factory=dict)  # 5-2 사용자 한 문장 → 분류 자료 JSON 글(item_classify.설명_반영). 같은 문장은 세션에서 한 번만 바꾼다


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


def _답_결합(후보, 답변):
    """후보마다 받은 답을 붙인 행 목록. 답이 빠졌거나 형식이 틀리면 ValueError."""
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
    확인 = []
    for c in 후보:
        a = 답표[c['id']]
        row = {**copy.deepcopy(c), '답': a['답']}
        if a.get('정정'):
            row['정정'] = a['정정'].strip()
        확인.append(row)
    return 확인


def 후보_답변(s, 답변):
    """서버 저장 후보 ID에만 사용자의 답을 결합한다. 답은 맞아요 | 직접입력(+정정=사용자 설명) 두 가지(재설계 11-11 ②).

    후보마다 답이 있어야 다음 단계로 간다(답하지 않은 후보가 있으면 ValueError). 직접입력 설명은 ③ 분류 LLM 의 자료다.
    """
    확인 = _답_결합(s.조사.get('후보') or [], 답변)
    업체 = [row for row in 확인 if row.get('유형') == '업체이용조건']
    return {'상태': s.조사.get('상태', '없음'), '확인한정보': 확인,
            '업체이용조건': 업체, '미확인': [], '출처': copy.deepcopy(s.조사.get('출처') or []),
            **{k: copy.deepcopy(s.조사[k]) for k in ('부족정보',) if k in s.조사},
            **({'사유': s.조사['사유']} if s.조사.get('사유') else {})}


def 재조사_답변(s, 답변):
    """③-R 재조사 후보(세션.재조사)에 사용자의 답을 붙인다(스펙 8-2 ③). 재조사 후보가 없거나 답이 없으면 빈 목록.

    재조사를 하지 않았거나 찾은 후보가 없는 세션에 온 답은 쓰지 않는다(재조사 로직: routes_questions._재조사).
    """
    후보 = (s.재조사 or {}).get('후보') or []
    return _답_결합(후보, 답변) if 후보 and 답변 else []


def 재조사_붙이기(조사, 재조사확인):
    """재조사 확인 결과를 확인한정보 에 같은 모양({내용, 답, 정정?, …})으로 더한다(별도 칸 없음).

    그래서 답변키 · 분류 자료(item_classify.확인정보_글) · 질문 · 판정 프롬프트(확인정보_문맥) · 결과 화면의 「검색해 확인한 공개 정보」 가
    ② 확인 답과 같이 읽는다. 재조사 답이 바뀌면 답변키가 바뀌어 ③ 분류를 다시 한다. 같은 id 는 두 번 넣지 않는다.
    """
    있는 = {x.get('id') for x in 조사.get('확인한정보') or []}
    새 = [copy.deepcopy(x) for x in 재조사확인 or [] if x.get('id') not in 있는]
    if 새:
        조사['확인한정보'] = [*(조사.get('확인한정보') or []), *새]
        조사['업체이용조건'] = [*(조사.get('업체이용조건') or []), *(copy.deepcopy(x) for x in 새 if x.get('유형') == '업체이용조건')]
        출처 = list(조사.get('출처') or [])
        for x in 새:
            출처 += [c for c in x.get('출처') or [] if c not in 출처]
        조사['출처'] = 출처
    return 조사


def 설명_받음(조사확인) -> bool:
    """「설명을 받았다」(스펙 8-2 ②): 품목설명이 있거나 조사 답 · 재조사 답(확인한정보 에 합쳐 있다)에 직접입력 정정이 하나라도 있다.
    조사확인 은 세션 값(품목설명 · 재조사확인)을 붙인 확인 결과라 요청에 다시 안 와도 세션 기준이다. 소비처: routes_questions._분류없음_다음 · mock_flow.분류."""
    조사확인 = 조사확인 or {}
    return bool(조사확인.get('품목설명')) or any((x.get('정정') or '').strip() for x in 조사확인.get('확인한정보') or [])


def 재조사_답_대기(s) -> bool:
    """재조사 후보가 있는데 아직 답(세션.재조사확인)을 받지 않았다. 이때 재조사답변 없이 품목설명만 온 요청은 받지 않는다(조율 결정 v048)."""
    return bool((s.재조사 or {}).get('후보')) and not s.재조사확인


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
    조사 = 재조사_붙이기(설명_붙이기(후보_답변(s, 답변), s.품목설명), s.재조사확인)
    if s.확인답키 != 답변키(조사):
        raise 재질문('확인 내용이 변경되어 질문을 다시 만들어야 합니다')
    # ④-2 판정용 조사 결과는 답키 계산 뒤에 붙인다(답키는 사용자가 확인한 내용만으로 만든다). 판정이 조사["판정조사"] 로 읽는다
    return {**조사, '판정조사': s.판정조사} if isinstance(조사, dict) and s.판정조사 else 조사
