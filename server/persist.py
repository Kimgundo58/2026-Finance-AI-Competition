# -*- coding: utf-8 -*-
"""판정 결과 → DB 영속화.   **[지출계획 계통 · 시그니처 동결]**

🔴 **`tenant.decisions` 에 INSERT 하지 않는다.** `scripts/orchestrate.py::decisions_적재()`
   가 판정 시점에 이미 그 행을 만든다 (decision_id 도 거기서 나온다). 여기서 또 넣으면
   판정 1건당 행이 둘 생기고, 정답셋 평가가 그 테이블을 분모로 쓰기 때문에 지표까지
   오염된다 (ai-14 정정, 2026-09-01 — 최초 지시서 ②-1 은 폐기됨).

이 함수가 하는 일은 **잇기** 세 가지뿐이다:
  1. 이미 있는 `decisions` 행에 `plan_id` 를 UPDATE 로 붙인다 (INSERT 아니다)
  2. `expense_plans.latest_decision_id`·`상태='judged'` 를 UPDATE 한다
  3. `routes_tasks.동기화()` 를 **함수로 호출**해 `plan_tasks` 를 잇는다
     (할일 계통이라 편집하지 않고 import 만 한다 — 재판정 규칙이 그 파일에 몰려 있다)

`decision_id` 가 None 이면 (orchestrate 가 DB 없이 돌았거나 적재가 실패한 경우) 없는
행을 만들지 않고 저장 실패를 알린다. 판정 결과 자체는 이미 사용자 화면에 나갔으므로
저장 실패로 SSE 스트림을 죽이지 않는다 — 호출부(`main.py::_실_판정`)가
`저장` 이벤트로 이 반환값을 그대로 흘린다.

과도기 주석: 근본 해법은 `orchestrate.판정()` 이 처음부터 `plan_id` 를 받아 decisions
행을 한 번에 쓰는 것이다 (ai-14 → ai-25 제안 중). 그게 들어오면 아래 1번 UPDATE 는
사라지고 이 함수는 2번·3번만 남는다.
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
    """캐시가 답한 판정을 «이 org 이름으로» `tenant.decisions` 에 새로 넣고 id 를 준다.

    🔴 왜 새 행인가 — 캐시에는 «다른 계획» 의 decision_id 가 들어 있을 수 있다. 그걸
       이 plan 에 붙이면 남의 판정 행을 남의 org 가 가리키게 된다(TENANT_LEAK 류).
       내용을 복사해 이 org 소유로 새로 적는 것이 안전하다.
    🔴 `경로='캐시'` 로 남긴다 — 나중에 「이 판정은 LLM 을 부른 것인가」를 가릴 수
       있어야 한다. 지표에서 캐시분을 빼거나 넣는 판단이 그 값에 달렸다.
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
    # 🔴 2026-09-07 — `decision_id` 가 없는 경우가 «둘» 이다.
    #    (a) 판정이 정말 실패했다      → 저장할 게 없다
    #    (b) 판정 캐시가 적중했다      → 응답은 «완전한데» 새 decisions 행이 없다
    #    (b) 를 예전처럼 그냥 되돌리면 계획이 «영원히 draft» 로 남는다. 사용자는
    #    화면에서 온전한 판정을 받았는데 목록에는 「미판정」으로 보인다(ai-3f 실측
    #    재현: plan 23567 · SSE 는 인용 5건·해야할일 7건인데 상태=draft).
    #    남의 `decision_id` 를 이 plan 에 붙이는 건 여전히 금지다(TENANT_LEAK) —
    #    대신 «이 plan·이 org 이름으로 새 행을 만든다». 내용은 캐시가 준 그대로다.
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

    # 먼저 이 plan_id 가 이 org 것인지 확인한다 — 남의 org 면 decisions 도 안 건드린다.
    소유확인 = _실행(  # UPDATE 없이 존재확인만 하고 싶지만 _질의 를 또 끌어오는 대신
                       # expense_plans UPDATE 를 먼저 걸어 rowcount 로 판별한다.
        f"UPDATE tenant.expense_plans p "
        f"SET latest_decision_id = %s, 상태 = 'judged', updated_at = now() "
        f"WHERE p.plan_id = %s AND {조건}",
        (decision_id, plan_id, *org인자),
    )
    if 소유확인 != 1:
        return {"저장": False, "사유": f"plan_id {plan_id} 을(를) 찾지 못했습니다 (기관 불일치 포함)"}

    # ① decisions 행은 이미 있다 (orchestrate.decisions_적재) — plan_id 만 잇는다. INSERT 아니다.
    _실행("UPDATE tenant.decisions SET plan_id = %s WHERE decision_id = %s",
         (plan_id, decision_id))

    # ③ 할일 동기화 — routes_tasks.py 를 편집하지 않고 함수만 부른다.
    # 🔴 2026-09-07 — `증빙목록` 을 «같이» 넘긴다. 이 값은 `orchestrate.증빙_발급처()` 가
    #    `corpus.rules.증빙`(79/82 룰에 채워져 있다 — 인건비만 10종) 을
    #    `corpus.evidence_sources.발급처` 와 조인해 만든 것인데, 지금까지 판정 응답에
    #    실려 SSE 로 «흘러가기만» 하고 저장이 안 됐다. 그래서 새로고침하면 사라지고,
    #    화면의 「3. 결제 후 필요 증빙」(`plan.evidence`)이 늘 비어 있었다.
    #    ⇒ 「서버가 만든다」 ≠ 「화면이 쓴다」. 할일과 같은 표에 `구분='결제후'` 로 심어
    #      기존 체크·완료·일정 UI 를 그대로 쓴다(새 테이블·새 계약을 만들지 않는다).
    #    code 가 없으므로 `구분` 힌트를 명시한다(`routes_tasks` 네 번째 자리).
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
