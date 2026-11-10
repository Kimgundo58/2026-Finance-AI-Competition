# -*- coding: utf-8 -*-
"""L3(기관 자체규정) 조문에서 집행예정일 기준으로 환산할 수 있는 기한을 찾는다.

routes_tasks 의 할일 동기화(POST /api/plans/{plan_id}/tasks:sync)가 호출한다.
공개 함수: 조문_조회(cur, org_id) -> 조문 목록, 기한_해석(조문, 항목, 설명) -> (오프셋일, 근거) | None,
바이그램(s). 읽는 테이블은 tenant.l3_articles 이고, RLS 정책이 적용되므로 GUC app.org_id 가
설정된 커서를 받는다. 기준점은 expense_plans.집행예정일 하나뿐이라 '사업 종료 후 30일'처럼
다른 기준점을 쓰는 기한은 환산하지 않는다. 못 찾으면 None 이고 날짜를 만들지 않는다.
"""
from __future__ import annotations

import re

# 집행예정일로 환산해도 되는 기준 표현. 금지 목록이면 새 표현이 검사 없이 통과하므로 허용 목록으로 둔다.
_환산가능_기준 = ("취득일", "구입일", "구매일", "집행일", "지출일", "결제일", "사용일")

# "취득일부터 1개월 이내" · "집행일로부터 30일 이내" · "지출일 기준 15일 이내"
_기한_RE = re.compile(
    r"(?P<기준>" + "|".join(_환산가능_기준) + r")"
    r"\s*(?:부터|로부터|기준|에서)?\s*"
    r"(?P<수>\d{1,3})\s*(?P<단위>일|개월|주)\s*(?:이내|안에|까지)"
)

_단위일수 = {"일": 1, "주": 7, "개월": 30}

# 항목 대 조문 매칭 임계. 조문이 항목보다 훨씬 길어서 항목 쪽 바이그램 포함률만 본다.
_임계 = 0.5


def 바이그램(s: str) -> set[str]:
    """공백을 모두 지운 뒤의 글자 바이그램 집합. 한 글자면 그 글자 하나, 빈 문자열이면 빈 집합."""
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _포함률(작은: set[str], 큰: set[str]) -> float:
    """작은 쪽(항목) 바이그램 중 큰 쪽(조문)에도 있는 비율."""
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


def 조문_조회(cur, org_id: str | None) -> list[tuple]:
    """이 기관의 L3 조문 중 기한 표현이 있는 것만 읽어 매칭 재료를 미리 계산한다.

    반환 원소는 (조번호, 조제목, (오프셋일, 발췌), 조문 바이그램). org_id 가 없으면 빈 목록.
    DB 오류는 예외로 전파한다(호출자의 트랜잭션이 롤백된다).
    """
    if not org_id:
        return []
    # 판정에 쓰는 문서만 본다. 교체된 문서나 확인을 기다리는 문서가 일정 날짜를 바꾸면 안 된다
    cur.execute('SELECT a."조번호", a."조제목", a."본문" FROM tenant.l3_articles a '
                'JOIN tenant.l3_documents d ON d.doc_id = a.doc_id '
                'WHERE a.org_id = %s AND d.status = \'active\' AND d."파싱품질" IN (\'pass\', \'warn\')',
                (org_id,))
    조문 = []
    for 조번호, 조제목, 본문 in cur.fetchall():
        기한 = _조문_기한(본문)
        if 기한 is None:
            continue
        조문.append((조번호, 조제목, 기한, 바이그램(f"{조제목 or ''}{본문 or ''}")))
    return 조문


def 기한_해석(조문: list[tuple], 항목: str, 설명: str | None = None
              ) -> tuple[int, str] | None:
    """`조문_조회()` 결과에서 이 할일에 해당하는 기한을 찾는다.

    반환 (오프셋일, 근거). 오프셋은 집행예정일에 더할 일수다. 포함률이 임계 미만이면 None.
    """
    if not 조문:
        return None
    항목_바이그램 = 바이그램(f"{항목 or ''}{설명 or ''}")
    최고: tuple[float, int, str] | None = None
    for 조번호, 조제목, (일수, 발췌), 조문_바이그램 in 조문:
        점수 = _포함률(항목_바이그램, 조문_바이그램)
        if 점수 < _임계:
            continue
        if 최고 is None or 점수 > 최고[0]:
            최고 = (점수, 일수, f"L3 {조번호}({조제목 or ''}) · {발췌}")
    if 최고 is None:
        return None
    return 최고[1], 최고[2]
