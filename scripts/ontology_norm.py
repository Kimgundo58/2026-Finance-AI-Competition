# -*- coding: utf-8 -*-
"""온톨로지 원천의 공통 규칙: 발췌 정규화와 조문 · 규범 ID 풀이 (docs/5_룰/5-8b_원천_JSON_형식.md).

extraction_compare · ontology_input · ontology_check · ontology_load 가 같은 함수를 쓴다.
검사는 "정규화(발췌) ⊂ 정규화(본문)" 이고, 원천 JSON 의 발췌도 정규화한 형태로 저장한다.
DB · LLM 을 쓰지 않는다.
"""
from __future__ import annotations

import re
import unicodedata

_따옴표 = str.maketrans({"“": '"', "”": '"', "″": '"', "‘": "'", "’": "'"})
# 줄 전체가 페이지 표식이거나 본문 사이에 끼어든 `- 14 -`. 숫자 범위 표기(10-20)는 공백이 없어 걸리지 않는다.
_페이지표식 = re.compile(r"(?:(?<=\s)|^)-\s*\d{1,3}\s*-(?=\s|$)")
_공백 = re.compile(r"\s+")


def 발췌_정규화(글: str | None) -> str:
    """유니코드 NFC → 따옴표 통일 → 페이지 표식 제거 → 공백 · 줄바꿈 연속을 공백 하나로 → 양끝 공백 제거."""
    s = unicodedata.normalize("NFC", 글 or "").translate(_따옴표)
    s = _페이지표식.sub(" ", s)
    return _공백.sub(" ", s).strip()


def 발췌_포함(발췌: str | None, 본문: str | None) -> bool:
    """정규화한 발췌가 정규화한 본문의 부분 문자열인가. 빈 발췌는 근거가 아니므로 거짓이다."""
    e = 발췌_정규화(발췌)
    return bool(e) and e in 발췌_정규화(본문)


_RE_조문ID = re.compile(r"^조:(?P<문서키>[^#]+)#(?P<나머지>.+)$")
_RE_조번호 = re.compile(r"^(제\d+조(?:의\d+)?)")
_RE_첨부 = re.compile(r"^((?:별지|별표|붙임|부칙)\s*\d*)")
_RE_책자 = re.compile(r"^((?:참고\d+-)?p\d+-\d+)")


def 조문ID_풀이(조문ID: str) -> dict:
    """`조:모두의창업#제36조①` → {문서키, 조번호, 항호}. `#` 뒤의 조번호를 뺀 나머지가 항호다.

    형식이 맞지 않으면 ValueError.
    """
    m = _RE_조문ID.match((조문ID or "").strip())
    if not m:
        raise ValueError(f"조문 ID 형식이 아니다: {조문ID!r}")
    문서키, 나머지 = m.group("문서키"), m.group("나머지")
    for 패턴 in (_RE_조번호, _RE_첨부, _RE_책자):
        n = 패턴.match(나머지)
        if n:
            조번호 = n.group(1).strip()
            항호 = 나머지[n.end():].strip() or None
            return {"문서키": 문서키, "조번호": 조번호, "항호": 항호}
    raise ValueError(f"조번호를 찾지 못했다: {조문ID!r}")


def 규범ID_풀이(규범ID: str) -> dict:
    """`규범:모두의창업/제36조①/한도/1` → {범위, 조문키, 종류, 순번}. 조문키 안에 `/` 가 있어도 뒤에서 자른다."""
    s = (규범ID or "").strip()
    if not s.startswith("규범:"):
        raise ValueError(f"규범 ID 형식이 아니다: {규범ID!r}")
    조각 = s[3:].split("/")
    if len(조각) < 4:
        raise ValueError(f"규범 ID 조각이 모자란다: {규범ID!r}")
    return {"범위": 조각[0], "조문키": "/".join(조각[1:-2]), "종류": 조각[-2], "순번": 조각[-1]}


def 개념ID_풀이(개념ID: str) -> dict:
    """`세목:창업/지급수수료/멘토링비@모두의창업` → {종류, 경로, 이름, 사업키}."""
    s = (개념ID or "").strip()
    if ":" not in s:
        raise ValueError(f"개념 ID 형식이 아니다: {개념ID!r}")
    종류, 경로 = s.split(":", 1)
    사업키 = None
    if "@" in 경로:
        경로, 사업키 = 경로.rsplit("@", 1)
    return {"종류": 종류, "경로": 경로, "이름": 경로.rsplit("/", 1)[-1], "사업키": 사업키}
