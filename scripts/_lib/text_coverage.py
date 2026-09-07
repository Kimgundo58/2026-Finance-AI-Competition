# -*- coding: utf-8 -*-
"""원문과 재구성본(표) 사이의 내용 손실을 어절 다중집합으로 검사한다.

`extract_tables.py`(빠진 어절 복구) 와 `table_splice.py`(손실이 있으면 갈아끼우지 않음) 가 쓴다.
표는 프로즈 순서를 컬럼으로 재배치하므로 순서는 보지 않는다.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

# 표 구조 기호(파이프·구분선)만으로 된 토큰은 내용이 아니므로 양쪽에서 똑같이 걷어낸다.
_구두점만 = re.compile(r"^[\-–—:·.\s]+$")


def _토큰화(text: str) -> list[str]:
    text = (text or "").replace("|", " ")
    return [t for t in text.split() if t and not _구두점만.match(t)]


def 부족_어절(원본: str, 재구성본: str) -> list[str]:
    """원본에는 있는데 재구성본엔 없는 어절. 원본 등장 순, 부족한 개수만큼."""
    부족 = Counter(_토큰화(원본)) - Counter(_토큰화(재구성본))
    if not 부족:
        return []
    남은 = dict(부족)
    out = []
    for t in _토큰화(원본):
        if 남은.get(t, 0) > 0:
            out.append(t)
            남은[t] -= 1
    return out


def 손실률(원본: str, 재구성본: str) -> float:
    """부족 어절 수 / 원본 전체 어절 수. 원본이 비어 있으면 0.0."""
    원_tok = _토큰화(원본)
    if not 원_tok:
        return 0.0
    return len(부족_어절(원본, 재구성본)) / len(원_tok)


def 누락_구간(원본: str, 재구성본: str, 최소길이: int = 20, 다리길이: int = 8) -> list[str]:
    """원본에서 재구성본이 통째로 놓친 연속 구간을 원문 슬라이스 그대로 돌려준다.

    문자 단위 SequenceMatcher 로 안 덮인 구간을 구하고, `다리길이` 이하로 떨어진 구간은 하나로 이은 뒤
    `최소길이` 미만은 버린다.
    """
    if not 원본 or not 재구성본:
        return []
    sm = difflib.SequenceMatcher(a=원본, b=재구성본, autojunk=False)
    covered = bytearray(len(원본))
    for block in sm.get_matching_blocks():
        for i in range(block.a, block.a + block.size):
            covered[i] = 1

    n = len(원본)
    구간들: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if covered[i]:
            i += 1
            continue
        j = i
        while j < n and not covered[j]:
            j += 1
        구간들.append((i, j))
        i = j

    이어붙임: list[tuple[int, int]] = []
    for start, end in 구간들:
        if 이어붙임 and start - 이어붙임[-1][1] <= 다리길이:
            이어붙임[-1] = (이어붙임[-1][0], end)
        else:
            이어붙임.append((start, end))

    return [원본[s:e] for s, e in 이어붙임
            if len(re.sub(r"\s", "", 원본[s:e])) >= 최소길이]
