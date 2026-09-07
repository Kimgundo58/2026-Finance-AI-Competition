# -*- coding: utf-8 -*-
"""판정 결과 → DB 영속화.

`tenant.decisions` 에 INSERT 하지 않는다 — `orchestrate.decisions_적재()` 가 판정 시점에
이미 그 행을 만든다. 여기서는 잇기만 한다:
  1. 이미 있는 `decisions` 행에 `plan_id` 를 UPDATE
  2. `expense_plans.latest_decision_id`·`상태='judged'` UPDATE
  3. `routes_tasks.동기화()` 를 함수로 호출해 `plan_tasks` 를 잇는다
"""
from __future__ import annotations

import logging

from ._common import MOCK, _실행, _질의

_log = logging.getLogger(__name__)


def 판정_저장(plan_id: int | None, body, out: dict,
             org_id: str | None = None, decision_id: int | None = None) -> dict:
    """반환 모양은 고정이다 (`main.py` 가 SSE `저장` 이벤트로 그대로 흘린다).

        {"저장": true,  "decision_id": 123, "plan_id": 45,
         "할일": {"생성":.., "갱신":.., "보존_user":.., "보존_날짜수정":.., "코드매칭":.., "코드미상":..}}
        {"저장": false, "사유": "..."}
    """
    if MOCK:
        if plan_id is None:
            return {"저장": False, "사유": "plan_id 없음"}
        return {
            "저장": True, "decision_id": 9001, "plan_id": plan_id,
            "할일": {"생성": 2, "갱신": 0, "보존_user": 0, "보존_날짜수정": 0,
                    "코드매칭": 2, "코드미상": 0},
        }
    return _실_저장(plan_id, body, out, org_id, decision_id)


def _캐시판정_적재(body, out: dict, org_id: str | None) -> int | None:
    """캐시가 답한 판정을 이 org 이름으로 `tenant.decisions` 에 새로 넣고 id 를 준다.

    캐시의 decision_id 는 다른 계획·다른 org 것일 수 있어 그대로 붙이지 않는다.
    `경로='캐시'` 로 남겨 LLM 호출분과 가른다.
    """
    import json as _json
    try:
        _실행("SELECT set_config('app.org_id', %s, true)", (org_id,)) if org_id else None
        행 = _질의(
            'INSERT INTO tenant.decisions '
            '  (org_id, "사업명", 질문원문, "정규화", "비목", "금액", "판정", "신뢰등급", '
            '   "요약", "인용", "해야할일", "전제", "버전스탬프", "참조사슬", "강등코드", "경로") '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING decision_id',
            (org_id, getattr(body, "사업명", None),
             (getattr(body, "정규화", None) or {}).get("_원문") or "(캐시 적중)",
             _json.dumps(getattr(body, "정규화", None) or {}, ensure_ascii=False),
             getattr(body, "확정비목", None),
             (getattr(body, "정규화", None) or {}).get("금액"),
             out.get("판정"), out.get("신뢰등급"), out.get("요약"),
             _json.dumps(out.get("인용") or [], ensure_ascii=False),
             _json.dumps(out.get("해야할일") or [], ensure_ascii=False),
             _json.dumps(out.get("전제") or [], ensure_ascii=False),
             out.get("버전스탬프"),
             _json.dumps(out.get("참조사슬") or [], ensure_ascii=False),
             [], "캐시"))
        return 행[0][0] if 행 else None
    except Exception:                                          # noqa: BLE001
        _log.exception("캐시 판정 적재 실패 — 저장을 건너뛴다(판정은 이미 사용자에게 갔다)")
        return None


def _실_저장(plan_id: int | None, body, out: dict,
            org_id: str | None, decision_id: int | None) -> dict:
    if plan_id is None:
        return {"저장": False, "사유": "plan_id 없음"}
    # decision_id 가 없는 경우가 둘이다: 판정 실패(저장할 게 없다) · 캐시 적중(응답은 완전한데
    # 새 decisions 행이 없다). 후자는 이 plan·이 org 이름으로 새 행을 만든다.
    if decision_id is None:
        if not (out or {}).get("판정"):
            return {"저장": False, "사유": "decision_id 없음 — 판정이 기록되지 않았다"}
        decision_id = _캐시판정_적재(body, out, org_id)
        if decision_id is None:
            return {"저장": False, "사유": "캐시 판정을 기록하지 못했다"}

    # 지연 import — 순환참조 회피 + MOCK 모드에서 이 경로가 아예 안 불리게.
    from .routes_plans import _org조건
    from .routes_tasks import 동기화 as _할일동기화
    from .models import 할일동기화

    조건, org인자 = _org조건(org_id, "p")

    # 이 plan_id 가 이 org 것인지 확인한다 — expense_plans UPDATE 의 rowcount 로 판별.
    소유확인 = _실행(
        f"UPDATE tenant.expense_plans p "
        f"SET latest_decision_id = %s, 상태 = 'judged', updated_at = now() "
        f"WHERE p.plan_id = %s AND {조건}",
        (decision_id, plan_id, *org인자),
    )
    if 소유확인 != 1:
        return {"저장": False, "사유": f"plan_id {plan_id} 을(를) 찾지 못했습니다 (기관 불일치 포함)"}

    # decisions 행은 이미 있다 — plan_id 만 잇는다.
    _실행("UPDATE tenant.decisions SET plan_id = %s WHERE decision_id = %s",
         (plan_id, decision_id))

    # 할일 동기화. `증빙목록`(결제 후 제출 서류)은 check_items 코드가 없으므로
    # `구분='결제후'` 힌트와 `매칭금지` 를 실어 같은 표에 심는다.
    _증빙 = [
        {"항목": e["증빙명"], "구분": "결제후", "매칭금지": True,
         "설명": (f'{e["발급처"]}에서 발급받습니다.' if e.get("발급처")
                else "지출 후 제출해야 하는 자료입니다.")}
        for e in (out.get("증빙목록") or []) if e.get("증빙명")
    ]
    동기화결과 = _할일동기화(
        plan_id,
        할일동기화(decision_id=decision_id,
                해야할일=list(out.get("해야할일", []) or []) + _증빙),
        org_id,
    )

    return {
        "저장": True, "decision_id": decision_id, "plan_id": plan_id,
        "할일": 동기화결과.model_dump(),
    }
