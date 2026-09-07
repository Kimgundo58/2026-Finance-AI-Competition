# -*- coding: utf-8 -*-
"""L3(기관 자체규정) 조문에서 집행일 기준으로 환산 가능한 기한을 찾는다.

`check_items.기본_오프셋일` 은 대부분 규정 근거가 없는 운영 기본값이라 그대로 날짜로
띄우지 않는다. `기한근거='규정근거'` 면 그 값을 쓰고, 아니면 이 모듈이 L3 에서 찾는다.
못 찾으면 `None` — 체크리스트에는 남고 캘린더에는 안 뜬다.

기준점은 `expense_plans.집행예정일` 하나뿐이라 「사업 종료 후 30일」처럼 다른 기준점을
쓰는 기한은 환산하지 않는다.
"""
from __future__ import annotations

import re

from ._common import _질의

# 집행예정일로 환산해도 되는 기준 표현. 허용 목록으로 둔다 — 금지 목록이면 새 표현이 조용히 통과한다.
_환산가능_기준 = ("취득일", "구입일", "구매일", "집행일", "지출일", "결제일", "사용일")

# "취득일부터 1개월 이내" · "집행일로부터 30일 이내" · "지출일 기준 15일 이내"
_기한_RE = re.compile(
    r"(?P<기준>" + "|".join(_환산가능_기준) + r")"
    r"\s*(?:부터|로부터|기준|에서)?\s*"
    r"(?P<수>\d{1,3})\s*(?P<단위>일|개월|주)\s*(?:이내|안에|까지)"
)

_단위일수 = {"일": 1, "주": 7, "개월": 30}

# 항목 대 조문 매칭 임계. 조문이 훨씬 길어 순수 바이그램 포함률만 본다.
_임계 = 0.5


def _바이그램(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _포함률(작은: set[str], 큰: set[str]) -> float:
    """작은 쪽(항목)이 큰 쪽(조문)에 얼마나 담겼나."""
    if not 작은:
        return 0.0
    return len(작은 & 큰) / len(작은)


def _조문_기한(본문: str) -> tuple[int, str] | None:
    """조문 본문에서 환산 가능한 기한 하나를 뽑는다. (오프셋일, 발췌) 또는 None."""
    본문1 = " ".join((본문 or "").split())
    m = _기한_RE.search(본문1)
    if not m:
        return None
    일수 = int(m.group("수")) * _단위일수[m.group("단위")]
    시작 = max(0, m.start() - 40)
    return 일수, 본문1[시작:m.end() + 20].strip()


def 기한_해석(org_id: str | None, 항목: str, 설명: str | None = None
              ) -> tuple[int, str] | None:
    """이 기관의 L3 조문에서 이 할일에 걸리는 기한을 찾는다.

    반환 `(오프셋일, 근거)` — 오프셋은 집행예정일에 더할 일수. 못 찾으면 `None`.
    DB 장애도 「근거 없음」과 같이 안 띄우는 쪽으로 떨어진다.
    """
    if not org_id:
        return None
    행 = _질의(
        'SELECT article_id, "조번호", "조제목", "본문" '
        'FROM tenant.l3_articles WHERE org_id = %s',
        (org_id,),
    )
    if not 행:
        return None

    항목bg = _바이그램(f"{항목 or ''}{설명 or ''}")
    최고: tuple[float, int, str] | None = None
    for article_id, 조번호, 조제목, 본문 in 행:
        딴 = _조문_기한(본문)
        if not 딴:                                   # 기한 표현이 없으면 볼 것도 없다
            continue
        점수 = _포함률(항목bg, _바이그램(f"{조제목 or ''}{본문 or ''}"))
        if 점수 < _임계:
            continue
        if 최고 is None or 점수 > 최고[0]:
            일수, 발췌 = 딴
            최고 = (점수, 일수, f"L3 {조번호}({조제목 or ''}) · {발췌}")
    if 최고 is None:
        return None
    return 최고[1], 최고[2]
