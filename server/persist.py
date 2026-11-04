# -*- coding: utf-8 -*-
"""판정 결과를 지출계획(tenant.expense_plans)에 연결한다.

live 판정은 orchestrate.decisions_저장() 이 판정 시점에 tenant.decisions 행을 이미 만든다.
여기서는 그 행을 계획에 연결만 한다. 아래 세 단계는 한 트랜잭션이다(하나라도 실패하면 모두 롤백).
  1. decisions 행의 plan_id 를 UPDATE 한다 (캐시 적중이면 처리단계='캐시' 행을 새로 INSERT 한다)
  2. expense_plans.latest_decision_id 와 상태='judged' 를 UPDATE 한다
  3. routes_tasks.동기화_실행() 으로 plan_tasks(할일)를 갱신한다
"""
from __future__ import annotations

import json
import logging

from ._common import MOCK, _계정조건, 트랜잭션

_log = logging.getLogger(__name__)


def 판정_저장(plan_id: int | None, body, out: dict,
             org_id: str | None = None, decision_id: int | None = None, *,
             account_id: str | None = None, 캐시적중: bool = False, 실패: bool = False) -> dict:
    """판정을 계획에 연결한다. 반환 모양은 고정이다(routes_judge 가 SSE 저장 이벤트로 그대로 보낸다).

        {"저장": true,  "decision_id": 123, "plan_id": 45,
         "할일": {"생성":.., "갱신":.., "보존_사용자":.., "보존_날짜수정":.., "코드매칭":.., "코드미매칭":..}}
        {"저장": false, "사유": "..."}

    할일 값은 할일동기화응답 모델을 그대로 덤프하므로 이름을 바꾼 집계 필드의 이전 이름도 함께 나간다.
    실패=True 면 아무것도 쓰지 않는다. 장애로 나온 판단불가를 계획의 판정으로 남기지 않는다.
    캐시적중=True 면 이 org·계정·계획으로 처리단계='캐시' 판정 행을 새로 INSERT 해 연결한다.
    org_id·account_id 는 토큰으로 검증된 값이다. 계획은 같은 org·계정의 것만 연결한다.
    """
    if plan_id is None:
        return {"저장": False, "사유": "plan_id 없음"}
    if 실패:
        return {"저장": False, "사유": "판정에 실패해 계획에 연결하지 않았습니다"}
    if MOCK:
        from .models import 할일동기화응답
        return {
            "저장": True, "decision_id": 9001, "plan_id": plan_id,
            "할일": 할일동기화응답(생성=2, 코드매칭=2).model_dump(),
        }
    if decision_id is None and not 캐시적중:
        return {"저장": False, "사유": "판정 기록이 없어 계획에 연결하지 않았습니다"}
    if 캐시적중 and org_id is None:
        # 게스트(org_id NULL) 판정 행은 RLS 정책에 막혀 INSERT 되지 않는다. DB 오류로 보고하지 않고 미리 거른다.
        return {"저장": False, "사유": "로그인한 기관만 판정을 계획에 저장할 수 있습니다"}

    try:
        연결 = _계획에_연결(plan_id, body, out, org_id, account_id, decision_id, 캐시적중)
    except Exception:                                          # noqa: BLE001
        _log.exception("판정-계획 연결 실패 (plan_id=%s)", plan_id)
        return {"저장": False, "사유": "저장 실패 (DB)"}
    if 연결 is None:
        return {"저장": False, "사유": f"plan_id {plan_id} 을(를) 찾지 못했습니다 (기관·계정 불일치 포함)"}
    decision_id, 동기화결과 = 연결
    return {
        "저장": True, "decision_id": decision_id, "plan_id": plan_id,
        "할일": 동기화결과.model_dump(),
    }


def _할일_목록(out: dict) -> list[dict]:
    """판정의 해야할일 + 증빙목록. 증빙(결제 후 제출 서류)은 check_items code 가 없어 구분='결제후',
    코드추정_제외=True 를 붙여 같은 plan_tasks 에 넣는다."""
    증빙 = [
        {"항목": e["증빙명"], "구분": "결제후", "코드추정_제외": True,
         "설명": (f'{e["발급처"]}에서 발급받습니다.' if e.get("발급처")
                else "지출 후 제출해야 하는 자료입니다.")}
        for e in (out.get("증빙목록") or []) if e.get("증빙명")
    ]
    return list(out.get("해야할일", []) or []) + 증빙


def _계획에_연결(plan_id: int, body, out: dict, org_id: str | None, account_id: str | None,
               decision_id: int | None, 캐시적중: bool):
    """한 트랜잭션에서 계획 소유 확인 → (캐시 판정 행 INSERT) → 판정 행·계획 UPDATE → 할일 동기화를 한다.

    (decision_id, 할일동기화응답) 을 돌려준다. 계획이 없거나 이 org 의 것이 아니면 아무것도 쓰지 않고
    None 을 돌려준다(같은 기관의 다른 계정 계획도 None). plan_tasks.decision_id 가 decisions 를 참조하지만
    같은 트랜잭션이라 FK 를 만족한다.
    """
    from .models import 할일동기화
    from .routes_tasks import 동기화_실행

    조건, org인자 = _계정조건(org_id, "p", account_id)
    with 트랜잭션(org_id, account_id) as cur:
        cur.execute(f"SELECT 1 FROM tenant.expense_plans p WHERE p.plan_id = %s AND {조건} "
                    f"FOR UPDATE", (plan_id, *org인자))
        if cur.fetchone() is None:
            return None
        if 캐시적중:
            # 캐시의 decision_id 는 다른 계획·다른 org 것일 수 있어 그대로 붙이지 않는다.
            from orchestrate import decisions_저장
            decision_id = decisions_저장(cur, _캐시판정_행(body, out, org_id, account_id, plan_id))
        else:
            cur.execute("UPDATE tenant.decisions SET plan_id = %s WHERE decision_id = %s",
                        (plan_id, decision_id))
        # 비목 없이 임시저장한 계획은 판정 때 고른 비목을 남긴다. 이미 있는 비목은 바꾸지 않는다.
        cur.execute("UPDATE tenant.expense_plans "
                    "SET latest_decision_id = %s, 상태 = 'judged', updated_at = now(), "
                    "    확정비목 = COALESCE(확정비목, %s) "
                    "WHERE plan_id = %s", (decision_id, getattr(body, "확정비목", None) or None, plan_id))
        동기화결과 = 동기화_실행(cur, plan_id,
                           할일동기화(decision_id=decision_id, 해야할일=_할일_목록(out)), org_id,
                           account_id)
    return decision_id, 동기화결과


def _캐시판정_행(body, out: dict, org_id: str | None, account_id: str | None, plan_id: int) -> dict:
    """캐시 응답을 tenant.decisions 한 행으로 만든다. 처리단계='캐시' 로 LLM 을 호출한 판정과 구분한다."""
    정규화 = getattr(body, "정규화", None) or {}
    # 저장된 계획의 정규화 값에는 이전 키로 원문이 들어 있을 수 있어 그 키도 읽는다.
    질문원문 = (정규화.get("질문원문") or 정규화.get("질문")
             or 정규화.get("_원문") or "(캐시 적중)")  # 구 이름 호환(제거 예정: 다음 릴리스)
    return dict(
        org_id=org_id, account_id=account_id, 사업명=getattr(body, "사업명", None), plan_id=plan_id,
        질문원문=질문원문,
        정규화=json.dumps(정규화, ensure_ascii=False),
        비목=getattr(body, "확정비목", None), 금액=정규화.get("금액"),
        판정=out.get("판정"), 신뢰등급=out.get("신뢰등급"), 요약=out.get("요약"),
        규정버전=out.get("규정버전"),
        인용=json.dumps(out.get("인용") or [], ensure_ascii=False, default=str),
        해야할일=json.dumps(out.get("해야할일") or [], ensure_ascii=False),
        전제=json.dumps(out.get("전제") or [], ensure_ascii=False, default=str),
        참조관계=json.dumps(out.get("참조관계") or [], ensure_ascii=False, default=str),
        검증코드=[], 처리단계="캐시",
    )
