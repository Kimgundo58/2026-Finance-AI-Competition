# -*- coding: utf-8 -*-
"""할일 API — 계획 상세의 집행 준비 체크리스트와 홈의 다가오는 일정(캘린더).

POST /api/plans/{plan_id}/tasks:sync (판정 결과 반영, persist.판정_저장 도 함수로 호출),
POST/PATCH /api/plans/{plan_id}/tasks, GET /api/tasks. 체크리스트와 캘린더는
tenant.plan_tasks 의 같은 행이고 due_date 유무로만 나뉜다. mock 모드(SUDDOE_MOCK=1)에서는
mock_data 를 쓰고, DB 모드 구현은 `_실_*` 함수에 있다. 동기화는 출처='user' 행과
사용자가 고친 due_date 를 덮지 않으며, DB 오류는 롤백 후 예외로 전파한다.
"""
from __future__ import annotations

import difflib
import logging
import math
import re
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query

from ._common import MOCK, 할일구분_ENUM, 트랜잭션, _org조건, _질의, _실행
from . import l3_deadline
from .l3_deadline import 바이그램
from .models import (할일, 할일동기화, 할일동기화응답, 할일목록응답, 할일생성, 할일수정)
from . import mock_data

router = APIRouter(tags=["할일"])
_log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 판정 결과 → plan_tasks — POST /api/plans/{plan_id}/tasks:sync
# ════════════════════════════════════════════════════════════════════

@router.post("/api/plans/{plan_id}/tasks:sync", response_model=할일동기화응답)
def 동기화(plan_id: int, body: 할일동기화, org_id: str | None = None) -> 할일동기화응답:
    """판정의 해야할일 목록을 이 계획의 plan_tasks 에 반영한다.

    재판정 규칙: (1) 출처='user' 행은 건드리지 않는다. (2) 날짜_사용자수정=true 행의 due_date 는
    덮지 않는다. (3) 같은 code 의 ai 행은 갱신하고, 이번 결과에 없는 ai 행은 지운다.
    (4) code 가 없으면 항목 텍스트로 대조한다. DB 오류는 예외로 전파한다(HTTP 500).
    """
    if MOCK:
        보존u = sum(1 for t in mock_data.목_할일
                    if t["plan_id"] == plan_id and t["출처"] == "user")
        보존d = sum(1 for t in mock_data.목_할일
                    if t["plan_id"] == plan_id and t["날짜_사용자수정"])
        맞춤 = sum(1 for h in body.해야할일 if h.get("code"))
        return 할일동기화응답(
            생성=len(body.해야할일), 갱신=0, 보존_user=보존u, 보존_날짜수정=보존d,
            코드매칭=맞춤, 코드미상=len(body.해야할일) - 맞춤,
        )
    return _실_동기화(plan_id, body, org_id)


# ════════════════════════════════════════════════════════════════════
# 항목 텍스트 → check_items code 추정
# ════════════════════════════════════════════════════════════════════

_조사_RE = re.compile(r"(으로|에서|에게|이나|만큼|까지|부터|은|는|이|가|을|를|와|과|만|도|의|나|로)$")
_어미_RULES = (
    # "...아닌지/인지/됐는지 확인(해주세요)" — 결제전 항목 대다수가 이 어미를 공유한다.
    re.compile(r"(아닌지|인지|됐는지|끝났는지|맞는지)\s*확인(해주세요|하세요|해요|해)?[.]?$"),
    re.compile(r"(받아두세요|받으세요|제출하세요|등록하세요|준비하세요|보고하세요|보관하세요|검토하세요)[.]?$"),
    re.compile(r"(하세요|해주세요|해요|주세요)[.]?$"),
)

# 코드 추정 임계. IDF 가중 바이그램 포함도(0.65)와 SequenceMatcher(0.35)를 섞은 점수 기준이다.
_매칭_임계 = 0.50


def _문장_정규화(s: str) -> str:
    """조사와 존댓말 어미를 뗀다. 떼지 않으면 공통 어미 때문에 내용이 달라도 유사도가 높게 나온다."""
    s = (s or "").strip()
    for pat in _어미_RULES:
        새 = pat.sub("", s).strip()
        if 새 != s:
            s = 새
            break
    return " ".join(t for t in (_조사_RE.sub("", w) for w in s.split()) if t)


def _IDF_바이그램(전체_항목: list[str]) -> dict[str, float]:
    """check_items 전체에 흔한 바이그램('주관기관', '확인' 등)은 낮추고 특정 항목에만 있는
    바이그램('부가세', '비교견적' 등)은 높인다. 대칭 자카드보다 오매칭이 적다."""
    문서수 = len(전체_항목)
    빈도: dict[str, int] = {}
    for t in 전체_항목:
        for bg in 바이그램(_문장_정규화(t)):
            빈도[bg] = 빈도.get(bg, 0) + 1
    return {bg: math.log((문서수 + 1) / (df + 1)) + 1.0 for bg, df in 빈도.items()}


def _매칭_점수(항목_정규: str, 항목_bg: set[str], 마정: str, 마bg: set[str],
              idf: dict[str, float]) -> float:
    seq = difflib.SequenceMatcher(None, 항목_정규, 마정).ratio()
    교집합 = 항목_bg & 마bg
    if not 교집합:
        가중포함 = 0.0
    else:
        분모집합 = 항목_bg if len(항목_bg) <= len(마bg) else 마bg
        분모 = sum(idf.get(bg, 1.0) for bg in 분모집합)
        가중포함 = sum(idf.get(bg, 1.0) for bg in 교집합) / 분모 if 분모 else 0.0
    return 0.35 * seq + 0.65 * 가중포함


class _코드매처:
    """항목 텍스트로 `corpus.check_items.code` 를 추정한다. 요청당 한 번 만들고 메모리에서 비교한다.

    판정이 code 를 주지 않았을 때만 쓰는 폴백이다. 못 찾으면 None 이고, 호출부가
    코드=NULL 로 넣는다.
    """

    def __init__(self, 체크항목: list[tuple[str, str]]):
        """체크항목: (code, 항목) 목록. DB 조회 순서를 유지해야 동점일 때 결과가 같다."""
        # 짧은 code 이름은 마스터 항목과 바이그램이 겹치지 않아 임계를 못 넘을 수 있어서,
        # 공백만 지운 code 이름 정확 일치를 유사도보다 먼저 본다. 같은 키는 먼저 나온 code 를 쓴다.
        self._코드이름: dict[str, str] = {}
        for code, _ in 체크항목:
            self._코드이름.setdefault(re.sub(r"\s+", "", code or ""), code)
        self._idf = _IDF_바이그램([마스터항목 for _, 마스터항목 in 체크항목])
        self._마스터 = []
        for code, 마스터항목 in 체크항목:
            마정 = _문장_정규화(마스터항목)
            self._마스터.append((code, 마정, 바이그램(마정)))

    def 매칭(self, 항목: str) -> str | None:
        항목 = (항목 or "").strip()
        if not 항목 or not self._마스터:
            return None
        정확 = self._코드이름.get(re.sub(r"\s+", "", 항목))
        if 정확 is not None:
            return 정확
        항목_정규 = _문장_정규화(항목)
        항목_bg = 바이그램(항목_정규)
        최적_code, 최적_점수 = None, 0.0
        for code, 마정, 마bg in self._마스터:
            점수 = _매칭_점수(항목_정규, 항목_bg, 마정, 마bg, self._idf)
            if 점수 > 최적_점수:
                최적_code, 최적_점수 = code, 점수
        return 최적_code if 최적_점수 >= _매칭_임계 else None


# ════════════════════════════════════════════════════════════════════
# 사용자 직접 추가 — POST /api/plans/{plan_id}/tasks
# ════════════════════════════════════════════════════════════════════

@router.post("/api/plans/{plan_id}/tasks", response_model=할일, status_code=201)
def 추가(plan_id: int, body: 할일생성, org_id: str | None = None) -> 할일:
    if MOCK:
        새 = max((t["task_id"] for t in mock_data.목_할일), default=0) + 1
        행 = {"task_id": 새, "plan_id": plan_id, "출처": "user", "코드": None,
              "구분": body.구분, "항목": body.항목, "설명": body.설명,
              "due_date": body.due_date, "유형": body.유형,
              "날짜_사용자수정": body.due_date is not None,
              "상태": "준비필요", "계획제목": None}
        mock_data.목_할일.append(행)
        return 할일(**행)
    return _실_추가(plan_id, body, org_id)


# ════════════════════════════════════════════════════════════════════
# 상태·날짜·유형 수정 — PATCH /api/plans/{plan_id}/tasks/{task_id}
# ════════════════════════════════════════════════════════════════════

@router.patch("/api/plans/{plan_id}/tasks/{task_id}", response_model=할일)
def 수정(plan_id: int, task_id: int, body: 할일수정, org_id: str | None = None) -> 할일:
    if MOCK:
        행 = next((t for t in mock_data.목_할일 if t["task_id"] == task_id), None)
        if not 행:
            raise HTTPException(404, f"할일 {task_id} 을(를) 찾을 수 없습니다")
        if body.상태 is not None:
            행["상태"] = body.상태
        if body.due_date is not None:
            행["due_date"] = body.due_date
            행["날짜_사용자수정"] = True      # 이후 재판정이 이 날짜를 덮지 않게 한다
        if body.유형 is not None:
            행["유형"] = body.유형
        return 할일(**행)
    return _실_수정(plan_id, task_id, body, org_id)


# ════════════════════════════════════════════════════════════════════
# 홈 집계 · 캘린더 — GET /api/tasks
# ════════════════════════════════════════════════════════════════════

@router.get("/api/tasks", response_model=할일목록응답)
def 목록(
    상태: str | None = Query(None, description="준비필요·집행예정·완료"),
    구분: str | None = Query(None, description="결제전·결제후·집행"),
    plan_id: int | None = None,
    일정만: bool = Query(False, description="true 면 due_date 가 있는 행만 (캘린더)"),
    이후: str | None = Query(None, description="YYYY-MM-DD — 이 날짜 이후만"),
    org_id: str | None = None,
) -> 할일목록응답:
    행 = mock_data.목_할일 if MOCK else _실_목록(org_id)
    out = []
    for t in 행:
        if 상태 and t["상태"] != 상태:
            continue
        if 구분 and t["구분"] != 구분:
            continue
        if plan_id is not None and t["plan_id"] != plan_id:
            continue
        if 일정만 and not t.get("due_date"):
            continue
        if 이후 and (t.get("due_date") or "") < 이후:
            continue
        out.append(t)
    out.sort(key=lambda t: t.get("due_date") or "9999-99-99")
    return 할일목록응답(건수=len(out), 항목=[할일(**t) for t in out])


# ════════════════════════════════════════════════════════════════════
# DB 모드 구현
# ════════════════════════════════════════════════════════════════════

# 목록·단건 조회가 같이 쓰는 SELECT 와 컬럼 순서. `계획제목` 은 expense_plans JOIN 으로 가져온다.
_할일_SELECT = (
    'SELECT t.task_id, t.plan_id, t.출처, t.코드, t.구분, t.항목, t.설명, '
    't.due_date, t."유형", t.날짜_사용자수정, t.상태, p.제목 '
    'FROM tenant.plan_tasks t LEFT JOIN tenant.expense_plans p ON p.plan_id = t.plan_id '
)
_할일_컬럼 = ("task_id", "plan_id", "출처", "코드", "구분", "항목", "설명",
             "due_date", "유형", "날짜_사용자수정", "상태", "계획제목")


def _행_할일(행: tuple) -> dict:
    d = dict(zip(_할일_컬럼, 행))
    if d.get("due_date") is not None:
        d["due_date"] = d["due_date"].isoformat()
    return d


def _할일_조회(task_id: int) -> 할일:
    행 = _질의(_할일_SELECT + "WHERE t.task_id = %s", (task_id,))
    if not 행:
        raise HTTPException(404, f"할일 {task_id} 을(를) 찾을 수 없습니다")
    return 할일(**_행_할일(행[0]))


def _due계산(집행예정일, l3조문: list[tuple], 항목: str, 설명: str | None,
            오프셋: int | None, 기한근거: str | None) -> str | None:
    """due_date 는 규정 근거가 있을 때만 만든다. 없으면 None(체크리스트에는 남고 캘린더에는 안 나온다).

    경우는 셋이다. (1) 기한근거='규정근거' 면 기본_오프셋일 을 그대로 쓴다.
    (2) 그 밖에는 l3_deadline 이 이 기관 L3 조문에서 찾는다. (3) L3 에도 없으면 날짜를 만들지 않는다.
    기본_오프셋일 은 부호가 방향이다(음수는 집행일 전, 양수는 집행일 후).
    """
    if 집행예정일 is None:
        return None
    if 기한근거 == "규정근거" and 오프셋 is not None:
        return (집행예정일 + timedelta(days=오프셋)).isoformat()
    찾음 = l3_deadline.기한_해석(l3조문, 항목, 설명)
    if 찾음 is not None:
        일수, _근거 = 찾음
        return (집행예정일 + timedelta(days=일수)).isoformat()
    return None


def _체크항목_표(cur) -> dict[str, dict]:
    """`corpus.check_items` 전체를 code → 행 dict 로 한 번에 읽는다. 순서는 DB 조회 순서다."""
    cur.execute('SELECT code, "항목", 구분, 기본_오프셋일, "기한근거", "유형" FROM corpus.check_items')
    return {code: {"항목": 항목, "구분": 구분, "오프셋": 오프셋, "기한근거": 기한근거, "유형": 유형}
            for code, 항목, 구분, 오프셋, 기한근거, 유형 in cur.fetchall()}


def _동기화_준비(body: 할일동기화, 체크항목: dict[str, dict], l3조문: list[tuple],
              집행예정일) -> list[dict]:
    """판정의 해야할일 목록을 plan_tasks 에 쓸 행으로 확정한다. DB 를 읽지 않는 순수 함수다.

    - code 는 판정이 준 값을 쓰고, 없으면 `_코드매처` 로 추정한다. `매칭금지` 항목(결제 후
      제출 서류)은 추정하지 않는다. 서류 이름이 결제 전 확인 항목으로 오매칭되는 것을 막는다.
    - check_items 에 없는 code 는 FK 위반이 되므로 NULL 로 바꾸고 항목 텍스트는 유지한다.
    - 구분은 check_items 값이 우선하고, 없으면 항목의 구분 힌트, 그것도 허용 목록 밖이면 '결제전'.
    - 유형은 캘린더 배지용이며 check_items 값을 따른다. 없으면 '기타'.
    """
    매처 = _코드매처([(code, 행["항목"]) for code, 행 in 체크항목.items()])
    행들 = []
    for h in body.해야할일:
        항목 = h.get("항목") or ""
        설명 = h.get("설명")
        코드 = None if h.get("매칭금지") else (h.get("code") or 매처.매칭(항목))
        마스터 = 체크항목.get(코드) if 코드 else None
        if 코드 and 마스터 is None:
            _log.warning("할일 동기화: code=%r 가 corpus.check_items 에 없어 NULL 로 저장한다", 코드)
            코드 = None
        마스터 = 마스터 or {}
        구분 = 마스터.get("구분") or h.get("구분") or "결제전"
        if 구분 not in 할일구분_ENUM:
            구분 = "결제전"
        행들.append({
            "항목": 항목, "설명": 설명, "코드": 코드, "구분": 구분,
            "유형": (마스터.get("유형") or "기타") if 코드 else "기타",
            "due_date": _due계산(집행예정일, l3조문, 항목, 설명,
                                마스터.get("오프셋"), 마스터.get("기한근거")),
        })
    return 행들


def _동기화_적용(cur, plan_id: int, 계획org, decision_id: int | None,
              행들: list[dict], 기존: list[tuple]) -> dict:
    """확정된 행을 plan_tasks 에 반영하고 건수를 돌려준다. 호출자의 트랜잭션 안에서 실행한다.

    재판정 규칙: (1) 출처='user' 행은 매칭·삭제 대상에서 제외한다. (2) 날짜_사용자수정=true 행은
    due_date 를 덮지 않는다. (3) 같은 code 의 ai 행은 갱신하고, 이번 결과에 없는 ai 행은 삭제한다.
    (4) code 가 없으면 항목 텍스트가 같은 ai 행과 대조한다.
    """
    코드맵 = {r[2]: r for r in 기존 if r[1] == "ai" and r[2] is not None}
    항목맵 = {r[3]: r for r in 기존 if r[1] == "ai" and r[2] is None}
    생성 = 갱신 = 보존날짜 = 0
    남길: set[int] = set()

    for 행 in 행들:
        기존행 = 코드맵.get(행["코드"]) if 행["코드"] else 항목맵.get(행["항목"])
        if 기존행:
            task_id, _출처, _코드, _항목, 날짜수정 = 기존행
            남길.add(task_id)
            갱신 += 1
            if 날짜수정:
                보존날짜 += 1
            cur.execute(
                'UPDATE tenant.plan_tasks '
                'SET 코드=%s, 구분=%s, 항목=%s, 설명=%s, '
                '    due_date = CASE WHEN 날짜_사용자수정 THEN due_date ELSE %s::date END, '
                '    decision_id=%s, "유형"=%s '
                'WHERE task_id=%s',
                (행["코드"], 행["구분"], 행["항목"], 행["설명"], 행["due_date"],
                 decision_id, 행["유형"], task_id),
            )
        else:
            생성 += 1
            cur.execute(
                'INSERT INTO tenant.plan_tasks '
                '(org_id, plan_id, decision_id, 출처, 코드, 구분, 항목, 설명, due_date, "유형") '
                "VALUES (%s,%s,%s,'ai',%s,%s,%s,%s,%s,%s)",
                (계획org, plan_id, decision_id, 행["코드"], 행["구분"], 행["항목"],
                 행["설명"], 행["due_date"], 행["유형"]),
            )

    사라진 = [r[0] for r in 기존 if r[1] == "ai" and r[0] not in 남길]
    if 사라진:
        # task_id 만으로 지우면 호출 경로가 바뀔 때 다른 계획의 행을 지울 수 있어 plan_id 도 조건에 넣는다
        cur.execute('DELETE FROM tenant.plan_tasks WHERE task_id = ANY(%s) AND plan_id = %s',
                    (사라진, plan_id))
    return {"생성": 생성, "갱신": 갱신, "보존날짜": 보존날짜}


def _실_동기화(plan_id: int, body: 할일동기화, org_id: str | None = None) -> 할일동기화응답:
    """판정 결과를 `tenant.plan_tasks` 에 한 트랜잭션으로 반영한다.

    커넥션 하나로 계획·기존 행·check_items·L3 조문을 읽고 쓴다. 중간에 DB 오류가 나면
    전부 롤백하고 예외를 전파한다(라우트는 500, persist 는 저장 실패로 처리한다).
    계획이 없거나 다른 기관 것이면 404.
    """
    조건, org인자 = _org조건(org_id, "p")
    with 트랜잭션() as cur:
        cur.execute(f'SELECT p.org_id, p."집행예정일" FROM tenant.expense_plans p '
                    f'WHERE p.plan_id = %s AND {조건}', (plan_id, *org인자))
        계획 = cur.fetchone()
        if not 계획:
            raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
        계획org, 집행예정일 = 계획

        cur.execute('SELECT task_id, 출처, 코드, 항목, 날짜_사용자수정 '
                    'FROM tenant.plan_tasks WHERE plan_id = %s', (plan_id,))
        기존 = cur.fetchall()
        체크항목 = _체크항목_표(cur)
        l3조문 = (l3_deadline.조문_조회(cur, 계획org)
                 if 집행예정일 is not None and 계획org else [])

        행들 = _동기화_준비(body, 체크항목, l3조문, 집행예정일)
        건수 = _동기화_적용(cur, plan_id, 계획org, body.decision_id, 행들, 기존)

    코드매칭수 = sum(1 for 행 in 행들 if 행["코드"])
    return 할일동기화응답(
        생성=건수["생성"], 갱신=건수["갱신"],
        보존_user=sum(1 for r in 기존 if r[1] == "user"), 보존_날짜수정=건수["보존날짜"],
        코드매칭=코드매칭수, 코드미상=len(행들) - 코드매칭수,
    )


def _실_추가(plan_id: int, body: 할일생성, org_id: str | None = None) -> 할일:
    조건, org인자 = _org조건(org_id, "p")
    계획 = _질의(f'SELECT p.org_id FROM tenant.expense_plans p '
                f'WHERE p.plan_id = %s AND {조건}', (plan_id, *org인자))
    if not 계획:
        raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
    계획org = 계획[0][0]
    날짜수정 = body.due_date is not None
    행 = _질의(
        'INSERT INTO tenant.plan_tasks '
        '(org_id, plan_id, 출처, 구분, 항목, 설명, due_date, "유형", 날짜_사용자수정) '
        "VALUES (%s,%s,'user',%s,%s,%s,%s,%s,%s) RETURNING task_id",
        (계획org, plan_id, body.구분, body.항목, body.설명, body.due_date, body.유형, 날짜수정),
    )
    if not 행:
        raise HTTPException(500, "할일 생성에 실패했습니다")
    return _할일_조회(행[0][0])


def _실_수정(plan_id: int, task_id: int, body: 할일수정, org_id: str | None = None) -> 할일:
    """할일 상태·날짜·유형을 고친다. 소유 확인은 할일의 org_id 가 아니라 계획(expense_plans.org_id)
    기준이다. `_실_추가`·`_실_동기화` 와 기준이 같아야 추가는 되는데 수정은 404 인 상황이 생기지 않는다.
    """
    조건, org인자 = _org조건(org_id, "p")
    존재 = _질의(f'SELECT 1 FROM tenant.plan_tasks t '
                f'JOIN tenant.expense_plans p ON p.plan_id = t.plan_id '
                f'WHERE t.task_id = %s AND t.plan_id = %s AND {조건}',
                (task_id, plan_id, *org인자))
    if not 존재:
        raise HTTPException(404, f"할일 {task_id} 을(를) 찾을 수 없습니다")

    수정절: list[str] = []
    인자: list = []
    if body.상태 is not None:
        수정절.append("상태=%s")
        인자.append(body.상태)
    if body.due_date is not None:
        수정절.append("due_date=%s")
        인자.append(body.due_date)
        수정절.append("날짜_사용자수정=true")     # 이후 재판정이 이 날짜를 덮지 않게 한다
    if body.유형 is not None:
        수정절.append('"유형"=%s')
        인자.append(body.유형)

    if 수정절:
        인자 += [task_id, plan_id]
        _실행(f'UPDATE tenant.plan_tasks SET {", ".join(수정절)} '
               'WHERE task_id=%s AND plan_id=%s', tuple(인자))
    return _할일_조회(task_id)


def _실_목록(org_id: str | None) -> list[dict]:
    if org_id is None:
        행 = _질의(_할일_SELECT + "WHERE t.org_id IS NULL")
    else:
        행 = _질의(_할일_SELECT + "WHERE t.org_id = %s", (org_id,))
    return [_행_할일(r) for r in 행]
