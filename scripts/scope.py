# -*- coding: utf-8 -*-
"""사업 스코프 컷 — 세부관리기준에서 범위 밖 편(모두의 창업 프로젝트 제3편 로컬트랙)의 조를 가려낸다.

import 시점에 stdout 을 건드리지 않는다.
"""
from __future__ import annotations

import re

# 목차에도 같은 문자열이 있으므로 첫 매치가 아니라 마지막 매치를 컷으로 쓴다.
범위밖_시작: dict[str, re.Pattern[str]] = {
    "모두의 창업 프로젝트": re.compile(r"제\s*3\s*편\s*로컬트랙"),
}


def 범위밖_조(doc_id: str, articles: list[dict]) -> set[str]:
    """범위 밖 구간에 속하는 조번호 집합. 해당 없으면 빈 집합."""
    pat = next((v for k, v in 범위밖_시작.items() if k in doc_id), None)
    if pat is None:
        return set()
    히트 = [i for i, a in enumerate(articles) if pat.search(a.get("본문") or "")]
    if not 히트:
        return set()
    컷 = 히트[-1]          # 헤딩은 앞 조 꼬리에 붙으므로 다음 조부터 범위 밖
    return {a["조번호"] for a in articles[컷 + 1:]}
