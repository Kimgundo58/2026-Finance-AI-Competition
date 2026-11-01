# -*- coding: utf-8 -*-
"""판정 결과 → DB 영속화.

live 판정은 `orchestrate.decisions_적재()` 가 판정 시점에 `tenant.decisions` 행을 이미 만든다.
여기서는 잇기만 한다:
  1. 이미 있는 `decisions` 행에 `plan_id` 를 UPDATE (캐시 적중이면 `경로='캐시'` 행을 새로 넣는다)
  2. `expense_plans.latest_decision_id`·`상태='judged'` UPDATE
  3. `routes_tasks.동기화()` 를 함수로 호출해 `plan_tasks` 를 잇는다
"""
from __future__ import annotations

import json
import logging

from ._common import MOCK, _org조건, 트랜잭션

_log = logging.getLogger(__name__)


def 판정_저장(plan_id: int | None, body, out: dict,
             org_id: str | None = None, decision_id: int | None = None, *,
             캐시적중: bool = False, 실패: bool = False) -> dict:
    """반환 모양은 고정이다 (`main.py` 가 SSE `저장` 이벤트로 그대로 흘린다).

        {"저장": true,  "decision_id": 123, "plan_id": 45,
         "할일": {"생성":.., "갱신":.., "보존_user":.., "보존_날짜수정":.., "코드매칭":.., "코드미상":..}}
        {"저장": false, "사유": "..."}

    `실패` 면 아무것도 쓰지 않는다 — 장애로 나온 판단불가를 계획의 판정으로 남기지 않는다.
    `캐시적중` 이면 이 org·이 계획 이름으로 `경로='캐시'` 판정 행을 새로 넣고 잇는다.
    """
    if plan_id is None:
        return {"저장": False, "사유": "plan_id 없음"}
    if 실패:
        return {"저장": False, "사유": "판정에 실패해 계획에 연결하지 않았습니다"}
    if MOCK:
        return {
            "저장": True, "decision_id": 9001, "plan_id": plan_id,
            "할일": {"생성": 2, "갱신": 0, "보존_user": 0, "보존_날짜수정": 0,
                    "코드매칭": 2, "코드미상": 0},
        }
    if decision_id is None and not 캐시적중:
        return {"저장": False, "사유": "판정 기록이 없어 계획에 연결하지 않았습니다"}

    try:
        decision_id = _계획에_연결(plan_id, body, out, org_id, decision_id, 캐시적중)
    except Exception:                                          # noqa: BLE001
        _log.exception("판정-계획 연결 실패 (plan_id=%s)", plan_id)
        return {"저장": False, "사유": "저장 실패 (DB)"}
    if decision_id is None:
        return {"저장": False, "사유": f"plan_id {plan_id} 을(를) 찾지 못했습니다 (기관 불일치 포함)"}

    # 할일 동기화는 routes_tasks 가 자체 트랜잭션으로 실행한다. plan_tasks.decision_id 가
    # decisions 를 참조하므로 위 트랜잭션이 커밋된 뒤에 불러야 한다.
    from .models import 할일동기화
    from .routes_tasks import 동기화 as _할일동기화
    # `증빙목록`(결제 후 제출 서류)은 check_items 코드가 없으므로
    # `구분='결제후'` 힌트와 `매칭금지` 를 실어 같은 표에 심는다.
    _증빙 = [
        {"항목": e["증빙명"], "구분": "결제후", "매칭금지": True,
         "설명": (f'{e["발급처"]}에서 발급받습니다.' if e.get("발급처")
                else "지출 후 제출해야 하는 자료입니다.")}
        for e in (out.get("증빙목록") or []) if e.get("증빙명")
    ]
    try:
        동기화결과 = _할일동기화(
            plan_id,
            할일동기화(decision_id=decision_id,
                    해야할일=list(out.get("해야할일", []) or []) + _증빙),
            org_id,
        )
    except Exception:                                          # noqa: BLE001
        _log.exception("할일 동기화 실패 (plan_id=%s, decision_id=%s)", plan_id, decision_id)
        return {"저장": False, "사유": "저장 실패 (DB)"}

    return {
        "저장": True, "decision_id": decision_id, "plan_id": plan_id,
        "할일": 동기화결과.model_dump(),
    }


def _계획에_연결(plan_id: int, body, out: dict, org_id: str | None,
               decision_id: int | None, 캐시적중: bool) -> int | None:
    """한 트랜잭션에서 계획 확인 → (캐시 판정 행 INSERT) → 판정 행·계획 UPDATE.

    연결한 decision_id 를 돌려준다. 계획이 이 org 것이 아니면 아무것도 쓰지 않고 None.
    """
    조건, org인자 = _org조건(org_id, "p")
    with 트랜잭션(org_id) as cur:
        cur.execute(f"SELECT 1 FROM tenant.expense_plans p WHERE p.plan_id = %s AND {조건} "
                    f"FOR UPDATE", (plan_id, *org인자))
        if cur.fetchone() is None:
            return None
        if 캐시적중:
            # 캐시의 decision_id 는 다른 계획·다른 org 것일 수 있어 그대로 붙이지 않는다.
            from orchestrate import decisions_적재
            decision_id = decisions_적재(cur, _캐시판정_행(body, out, org_id, plan_id))
        else:
            cur.execute("UPDATE tenant.decisions SET plan_id = %s WHERE decision_id = %s",
                        (plan_id, decision_id))
        cur.execute("UPDATE tenant.expense_plans "
                    "SET latest_decision_id = %s, 상태 = 'judged', updated_at = now() "
                    "WHERE plan_id = %s", (decision_id, plan_id))
    return decision_id


def _캐시판정_행(body, out: dict, org_id: str | None, plan_id: int) -> dict:
    """캐시가 답한 판정을 `tenant.decisions` 한 행으로. `경로='캐시'` 로 LLM 호출분과 가른다."""
    정규화 = getattr(body, "정규화", None) or {}
    return dict(
        org_id=org_id, 사업명=getattr(body, "사업명", None), plan_id=plan_id,
        질문원문=(정규화.get("_원문") or 정규화.get("질문") or 정규화.get("질문원문")
               or "(캐시 적중)"),
        정규화=json.dumps(정규화, ensure_ascii=False),
        비목=getattr(body, "확정비목", None), 금액=정규화.get("금액"),
        판정=out.get("판정"), 신뢰등급=out.get("신뢰등급"), 요약=out.get("요약"),
        버전스탬프=out.get("버전스탬프"),
        인용=json.dumps(out.get("인용") or [], ensure_ascii=False, default=str),
        해야할일=json.dumps(out.get("해야할일") or [], ensure_ascii=False),
        전제=json.dumps(out.get("전제") or [], ensure_ascii=False, default=str),
        참조사슬=json.dumps(out.get("참조사슬") or [], ensure_ascii=False, default=str),
        강등코드=[], 경로="캐시",
    )
