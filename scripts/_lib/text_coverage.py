# -*- coding: utf-8 -*-
"""원문 대 재구성본 사이 «내용 손실» 을 어절 다중집합으로 검사한다.

2026-09-05 사고(레인 C, ai-35 배정) 재발 방지용. `table_splice.py` 가 오염된
조를 `_tables.json` 표로 갈아끼우며 「기계장치」행의 유의사항 셀 한 칸을
통째로 떨어뜨렸다 — 원인은 `extract_tables.py` 의 pdfplumber 셀 인식 실패
(창업중심대학 세부관리기준2025년 개정.pdf, 0-idx 페이지 12, 표0, 마지막 행
3열)였다. 그 회귀를 막는 두 지점에서 이 모듈을 쓴다:

  1. `extract_tables.py` — 표 bbox 전체 텍스트 대비 추출된 셀 텍스트가 어절을
     빠뜨리면, 빠진 어절을 복구용 행으로 붙인다 (원인 쪽 봉합).
  2. `table_splice.py`   — 표로 갈아끼우기 **직전** 원문과 표본문을 대조해,
     그래도 어절이 빠지면 갈아끼우지 않는다 (재발 시 안전망 — «모르면 손
     안 댄다» 원칙 그대로).

**순서는 안 본다.** 표는 프로즈 순서를 컬럼으로 재배치하므로 순서 비교는
항상 오탐이다 — 어절이 «있는지 없는지» 그 다중집합(Counter) 만 본다.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

# 표 구조 기호(파이프·구분선) 만으로 된 토큰은 내용이 아니다. 실측: 마크다운
# 표의 `| --- | --- |` 구분행이 `-` 토큰 다발을 만들어 순진한 비교의 오탐 원인이
# 됐다(중앙 ai-35 실측 — 259줄 중 59줄 오탐). 원문 쪽 글머리 기호(`- 사무용…`)도
# 같은 모양이라, 양쪽에 동일하게 걷어내면 비대칭 편향이 없다.
_구두점만 = re.compile(r"^[\-–—:·.\s]+$")


def _토큰화(text: str) -> list[str]:
    text = (text or "").replace("|", " ")
    return [t for t in text.split() if t and not _구두점만.match(t)]


def 부족_어절(원본: str, 재구성본: str) -> list[str]:
    """원본에는 있는데 재구성본엔 없는 어절 — 원본 등장 순, 부족한 개수만큼."""
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
    """원본에서 재구성본이 통째로 놓친 **연속 구간**을 원문 그대로(축자) 돌려준다.

    `부족_어절()` 은 어절 다중집합이라 검출은 잘하지만, 복구문을 그걸로
    이어붙이면 어순이 뭉개진다 — 이미 다른 자리에서 «소비된» 흔한 어절(「등」·
    「비품」 등)이 정확히 필요한 자리에서 빠져, 정답셋의 축자 인용(`_대조방식:
    verbatim`)과 문자 단위로 안 맞는다(2026-09-05 실측 — gold_id 561 「…사무용
    비품 등 구입비로는…」의 「비품 등」이 이 방식으로 빠졌다). 문자 단위
    `difflib.SequenceMatcher` 로 원본을 덮는 매칭 블록을 구하고, 안 덮인
    구간만 **원문 슬라이스 그대로** 뽑는다 — 순서·구두점·공백이 원문과
    한 글자도 다르지 않으니 부분일치 매칭이 그대로 산다.

    🔴 안 덮인 구간 사이에 낀 **짧은** 덮인 조각(`다리길이` 이하)은 다리를
    놓아 하나로 잇는다 — 안 그러면 마지막 문장이 우연히 앞쪽 다른 셀과 한두
    글자(「및」·「등」) 가 겹쳐 두 조각으로 쪼개지고, 짧은 뒤쪽 조각이 `최소길이`
    미만이라 버려지면서 문장이 중간에서 잘린다(2026-09-05 2차 실측 — 참고3
    「…단순저장장치(USB, 외장하드 」에서 끊기고 「등) 및 소모성 부품(USB증폭기
    등) 집행 불가」가 통째로 사라졌었다). 다리를 놓은 뒤에야 `최소길이` 로
    거른다 — 표 재배치로 생기는 라벨 한두 어절짜리 파편(「비목」열 헤더 등)은
    그래도 걸러진다.
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
