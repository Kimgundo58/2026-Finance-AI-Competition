# -*- coding: utf-8 -*-
"""범위 제외: 사업별 세부관리기준에서 이 서비스 범위 밖인 편에 속한 조번호를 찾는다.

`build_refs.py` 가 호출해, 이 조들에서는 참조(corpus.refs)를 수집하지 않는다.
공개 함수는 `범위밖_조(doc_id, articles)`. import 시점 부수 효과는 없다.
현재 대상은 모두의 창업 프로젝트 세부관리기준의 제3편 로컬트랙이다.
"""
from __future__ import annotations

import re

# doc_id 에 키 문자열이 들어 있으면 해당 패턴 이후의 조를 범위 밖으로 본다.
# 목차에도 같은 문자열이 있으므로 첫 매치가 아니라 마지막 매치를 경계로 쓴다.
범위밖_시작: dict[str, re.Pattern[str]] = {
    "모두의 창업 프로젝트": re.compile(r"제\s*3\s*편\s*로컬트랙"),
}


def 범위밖_조(doc_id: str, articles: list[dict]) -> set[str]:
    """범위 밖 구간에 속하는 조번호 집합. 해당 없으면 빈 집합."""
    pat = next((v for k, v in 범위밖_시작.items() if k in doc_id), None)
    if pat is None:
        return set()
    일치 = [i for i, a in enumerate(articles) if pat.search(a.get("본문") or "")]
    if not 일치:
        return set()
    경계 = 일치[-1]        # 편 표제는 앞 조 본문 끝에 붙어 있으므로 다음 조부터 범위 밖이다
    return {a["조번호"] for a in articles[경계 + 1:]}
