# -*- coding: utf-8 -*-
"""사업 스코프 컷 — 한 벌만 둔다.

`CLAUDE.md` 사업 스코프: 모두의 창업 프로젝트는 **일반·기술트랙만** 다룬다.
세부관리기준이 제1편 총칙 / 제2편 일반·기술트랙 / 제3편 로컬트랙 구조인데,
로컬트랙은 상위 규범이 통합관리지침이 아니라 「신사업창업사관학교 운영지침」이라
위임 계통 자체가 다르다. `precedence_rules` 조회 키가 `사업명` 이라 두 트랙이 같은
키를 쓰므로, 제3편을 남겨두면 일반·기술트랙 판정에 범위 밖 규범이 딸려온다.

이 컷을 태워야 하는 곳 (2026-08-31 기준):
    scripts/tag_apply_target.py   Stage 0.5 적용대상 태깅
    scripts/build_refs.py         Stage 0.7 참조 그래프
    scripts/build_precedence.py   Stage 0.8 (자체 구현 보유 — 통합 대상)
    Stage 2 청킹                   미구현

부수 효과가 없다: import 시점에 stdout 을 건드리지 않는다. 그래서 이 모듈을
`sys.stdout = TextIOWrapper(...)` 를 하는 스크립트들이 서로 물고 있어도 안전하다.
"""
from __future__ import annotations

import re

# 목차에도 같은 문자열이 있다. 목차는 문서 앞쪽 조에 통째로 들어오므로
# **마지막 매치**를 컷으로 쓴다. 첫 매치를 쓰면 모두의창업 97조 중 95조가 잘려 나간다(실측).
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
    컷 = 히트[-1]          # 헤딩은 앞 조 꼬리에 붙는다 -> 다음 조부터 범위 밖
    return {a["조번호"] for a in articles[컷 + 1:]}
