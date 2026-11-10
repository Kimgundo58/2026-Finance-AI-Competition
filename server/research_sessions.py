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


def _서명(token):
    return hmac.new(_키, token.encode('ascii'), hashlib.sha256).hexdigest()


def 생성(org, account):
    now = time.monotonic()
    with _잠금:
        for k in [k for k, s in _세션.items() if s.expires <= now]:
            del _세션[k]
        if len(_세션) >= _최대:
            raise RuntimeError('조사 세션 수 초과')
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
    """서버 저장 후보 ID에만 사용자의 확인·정정을 결합한다."""
    후보 = s.조사.get('후보') or []
    표 = {x['id']: x for x in 후보}
    if len(표) != len(후보):
        raise ValueError('조사 후보 오류')
    답표 = {}
    for a in 답변:
        if a['id'] not in 표 or a['id'] in 답표:
            raise ValueError('조사 답변 오류')
        if a.get('정정') and a['답'] != '아니에요':
            raise ValueError('조사 정정 오류')
        답표[a['id']] = a
    확인, 미확인, 업체 = [], [], []
    for c in 후보:
        a = 답표.get(c['id'], {'답': '모르겠어요'})
        row = {**copy.deepcopy(c), '답': a['답']}
        if a.get('정정'):
            row['정정'] = a['정정'].strip()
        if a['답'] == '맞아요' or (a['답'] == '아니에요' and row.get('정정')):
            확인.append(row)
        else:
            미확인.append(row)
        if c.get('유형') == '업체이용조건':
            업체.append(row)
    return {'상태': s.조사.get('상태', '없음'), '확인한정보': 확인,
            '업체이용조건': 업체, '미확인': 미확인, '출처': copy.deepcopy(s.조사.get('출처') or []),
            **{k: copy.deepcopy(s.조사[k]) for k in ('부족정보', '사용자확인') if k in s.조사},
            **({'사유': s.조사['사유']} if s.조사.get('사유') else {})}


def 답변키(조사):
    답 = [{'id': x['id'], '답': x['답'], '정정': x.get('정정') or None}
         for x in [*조사['확인한정보'], *조사['미확인']]]
    return hashlib.sha256(json.dumps(sorted(답, key=lambda x: x['id']), ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()


def 답변_검증(s, 답변, 질문세트, 입력, 버전):
    if s.입력 != 입력 or s.버전 != 버전 or s.질문세트 != 질문세트 or s.질문결과 is None:
        raise ValueError('조사 입력 또는 질문이 변경되었습니다')
    조사 = 후보_답변(s, 답변)
    if s.확인답키 != 답변키(조사):
        raise 재질문('확인 내용이 변경되어 질문을 다시 만들어야 합니다')
    return 조사
