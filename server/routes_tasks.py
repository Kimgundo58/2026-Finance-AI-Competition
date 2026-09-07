# -*- coding: utf-8 -*-
"""할일 「확인필요」 — 화면 11 ⑧ 집행 준비 · 화면 6 ⑥ 다가오는 일정.

체크리스트와 캘린더는 같은 테이블 같은 행이다 — due_date 유무로만 갈린다.
목 경로가 기본 구현이고 실 경로는 `_실_*` 함수와 `코드_매칭()` 에 있다.
"""
from __future__ import annotations

import difflib
import logging
import math
import re
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query

from ._common import MOCK, _질의, _실행
from . import l3_deadline
from .models import (할일, 할일동기화, 할일동기화응답, 할일목록응답, 할일생성, 할일수정)
from . import mock_data
from .routes_plans import _org조건

router = APIRouter(tags=["할일"])
_log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 판정 결과 → plan_tasks — POST /api/plans/{plan_id}/tasks:sync
# ════════════════════════════════════════════════════════════════════

@router.post("/api/plans/{plan_id}/tasks:sync", response_model=할일동기화응답)
def 동기화(plan_id: int, body: 할일동기화, org_id: str | None = None) -> 할일동기화응답:
    """재판정 규칙 4개.

    ① 출처='user' 행은 건드리지 않는다
    ② 날짜_사용자수정=true 인 행의 due_date 는 덮지 않는다
    ③ 같은 코드의 ai 행은 갱신, 없어진 것은 지운다
    ④ 코드가 없으면(코드=NULL) 항목 텍스트로 대조한다
    """
    if MOCK:
        보존u = sum(1 for t in mock_data.목_할일
                    if t["plan_id"] == plan_id and t["출처"] == "user")
        보존d = sum(1 for t in mock_data.목_할일
                    if t["plan_id"] == plan_id and t["날짜_사용자수정"])
        맞춤 = sum(1 for h in body.해야할일 if h.get("code") or 코드_매칭(h.get("항목", "")))
        return 할일동기화응답(
            생성=len(body.해야할일), 갱신=0, 보존_user=보존u, 보존_날짜수정=보존d,
            코드매칭=맞춤, 코드미상=len(body.해야할일) - 맞춤,
        )
    return _실_동기화(plan_id, body, org_id)


_조사_RE = re.compile(r"(으로|에서|에게|이나|만큼|까지|부터|은|는|이|가|을|를|와|과|만|도|의|나|로)$")
_어미_RULES = (
    # "...아닌지/인지/됐는지 확인(해주세요)" — 52종 중 결제전 대다수가 이 꼬리를 공유한다.
    re.compile(r"(아닌지|인지|됐는지|끝났는지|맞는지)\s*확인(해주세요|하세요|해요|해)?[.]?$"),
    re.compile(r"(받아두세요|받으세요|제출하세요|등록하세요|준비하세요|보고하세요|보관하세요|검토하세요)[.]?$"),
    re.compile(r"(하세요|해주세요|해요|주세요)[.]?$"),
)


def _문장_정규화(s: str) -> str:
    """조사·존댓말 어미를 뗀다 — 안 떼면 공통 꼬리 때문에 내용이 달라도 유사도가 높게 뜬다."""
    s = (s or "").strip()
    for pat in _어미_RULES:
        새 = pat.sub("", s).strip()
        if 새 != s:
            s = 새
            break
    return " ".join(t for t in (_조사_RE.sub("", w) for w in s.split()) if t)


def _바이그램(s: str) -> set[str]:
    s = s.replace(" ", "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _IDF_바이그램(전체_항목: list[str]) -> dict[str, float]:
    """52종 전반에 흔한 바이그램(«주관기관»·«확인» 류)은 죽이고, 특정 항목에만
    있는 바이그램(«부가세»·«비교견적» 류)을 올린다 — 대칭 자카드보다 오답에 덜 낚인다."""
    문서수 = len(전체_항목)
    빈도: dict[str, int] = {}
    for t in 전체_항목:
        for bg in _바이그램(_문장_정규화(t)):
            빈도[bg] = 빈도.get(bg, 0) + 1
    return {bg: math.log((문서수 + 1) / (df + 1)) + 1.0 for bg, df in 빈도.items()}


def _매칭_점수(항목_정규: str, 항목_bg: set[str], 마스터항목: str, idf: dict[str, float]) -> float:
    마정 = _문장_정규화(마스터항목)
    마bg = _바이그램(마정)
    seq = difflib.SequenceMatcher(None, 항목_정규, 마정).ratio()
    교집합 = 항목_bg & 마bg
    if not 교집합:
        가중포함 = 0.0
    else:
        분모집합 = 항목_bg if len(항목_bg) <= len(마bg) else 마bg
        분모 = sum(idf.get(bg, 1.0) for bg in 분모집합)
        가중포함 = sum(idf.get(bg, 1.0) for bg in 교집합) / 분모 if 분모 else 0.0
    return 0.35 * seq + 0.65 * 가중포함


def 코드_매칭(항목: str) -> str | None:
    """항목 텍스트 → `corpus.check_items(code)` 역추정.

    판정이 이미 code 를 싣는 경우가 대부분이라 이 함수는 code 가 없을 때만 쓰는
    안전판이다. 실패하면 None — 호출부가 코드=NULL·구분='결제전' 기본값으로 넣는다.
    매칭은 조사·존댓말 어미를 뗀 뒤 IDF 가중 바이그램 포함도(0.65)와
    SequenceMatcher(0.35)를 섞어 0.50 이상만 받는다.
    """
    if MOCK:
        return None
    항목 = (항목 or "").strip()
    if not 항목:
        return None
    후보 = _질의('SELECT code, "항목" FROM corpus.check_items')
    if not 후보:
        return None
    # code 이름 자체를 먼저 본다(유사도보다 우선) — 짧은 code 이름은 마스터 항목과
    # 바이그램이 안 겹쳐 임계를 못 넘는 경우가 있다. 공백만 지운 정확 일치만 받는다
    납작 = re.sub(r"\s+", "", 항목)
    for code, _마스터항목 in 후보:
        if re.sub(r"\s+", "", code or "") == 납작:
            return code
    idf = _IDF_바이그램([마스터항목 for _, 마스터항목 in 후보])
    항목_정규 = _문장_정규화(항목)
    항목_bg = _바이그램(항목_정규)
    최적_code, 최적_점수 = None, 0.0
    for code, 마스터항목 in 후보:
        점수 = _매칭_점수(항목_정규, 항목_bg, 마스터항목, idf)
        if 점수 > 최적_점수:
            최적_code, 최적_점수 = code, 점수
    return 최적_code if 최적_점수 >= 0.50 else None


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
# 토글·날짜 — PATCH /api/plans/{plan_id}/tasks/{task_id}
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
            행["날짜_사용자수정"] = True      # 이후 재판정이 이 날짜를 덮지 않는다
        if body.유형 is not None:
            행["유형"] = body.유형
        return 할일(**행)
    return _실_수정(plan_id, task_id, body, org_id)


# ════════════════════════════════════════════════════════════════════
# 홈 「조건부」 집계 · 캘린더 — GET /api/tasks
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
# 실 경로 구역
# ════════════════════════════════════════════════════════════════════

# 목록 조회에 공용으로 쓰는 컬럼 순서. `계획제목` 은 JOIN 으로 붙인다.
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


def _체크항목_조회(코드: str) -> tuple[str | None, int | None, str | None]:
    """`corpus.check_items` 에서 `구분`·`기본_오프셋일`·`기한근거` 를 가져온다.

    기한근거를 같이 돌려주는 것이 요점이다 — 기본_오프셋일 은 대부분 규정 근거가 없는
    운영기본값이라, 근거 구분 없이 쓰면 화면에서 「규정상 기한」으로 오인될 수 있다.
    판단은 `_due계산()` 이 하고 여기서는 재료만 넘긴다. 쿼리 실패는 예외전파=True 로
    잡아 로그에 남긴다 — 조용히 실패하면 구분이 전부 '결제전' 으로 찍히는 사고가 난다.
    """
    try:
        행 = _질의('SELECT 구분, 기본_오프셋일, "기한근거" FROM corpus.check_items WHERE code = %s',
                  (코드,), 예외전파=True)
    except Exception:                                         # noqa: BLE001
        _log.exception("체크항목_조회 실패 — code=%r (구분 기본값 '결제전' 으로 대체됨)", 코드)
        return (None, None, None)
    if not 행:
        _log.warning("체크항목_조회 — code=%r 가 corpus.check_items 에 없다 "
                     "(구분 기본값 '결제전' 으로 대체됨)", 코드)
    return (행[0][0], 행[0][1], 행[0][2]) if 행 else (None, None, None)


def _due계산(집행예정일, org_id: str | None, 항목: str, 설명: str | None,
            오프셋: int | None, 기한근거: str | None) -> str | None:
    """due_date 는 규정 근거가 있을 때만 만든다. 없으면 None(체크리스트에는 남고 캘린더엔 안 뜬다).

    갈래는 셋이다: ① 기한근거='규정근거' → 기본_오프셋일 을 그대로 쓴다
    ② 그 밖 → l3_deadline 이 이 기관 L3 에서 찾는다 ③ L3 에도 없으면 → 날짜를 만들지 않는다
    """
    if 집행예정일 is None:
        return None
    if 기한근거 == "규정근거" and 오프셋 is not None:
        return (집행예정일 + timedelta(days=오프셋)).isoformat()
    찾음 = l3_deadline.기한_해석(org_id, 항목, 설명)
    if 찾음 is not None:
        일수, _근거 = 찾음
        return (집행예정일 + timedelta(days=일수)).isoformat()
    return None


def _유형_맵(코드들: set) -> dict:
    """`corpus.check_items."유형"` 을 코드→유형으로 한 번에 뜬다.

    DB 컬럼이라 마스터가 늘어도 코드 변경이 필요 없다. 코드=NULL 이거나 맵에 없으면
    호출부가 '기타' 로 채운다 — 할일유형_ENUM 밖 값은 못 나간다.
    """
    if not 코드들:
        return {}
    행 = _질의('SELECT code, "유형" FROM corpus.check_items WHERE code = ANY(%s)', (list(코드들),))
    return {code: (유형 or "기타") for code, 유형 in 행}


def _실_동기화(plan_id: int, body: 할일동기화, org_id: str | None = None) -> 할일동기화응답:
    """판정 결과 → `tenant.plan_tasks` 적재. 재판정 규칙 4개를 지킨다.

    ① 출처='user' 행은 아예 후보에 넣지 않는다 — 매칭·삭제 대상에서 원천 제외
    ② 날짜_사용자수정=true 행은 due_date 를 UPDATE 절에서 뺀다
    ③ 같은 코드의 ai 행은 갱신, 이번 회차에 없는 ai 행은 삭제
    ④ 코드가 없으면(`코드_매칭` 도 실패) 항목 텍스트로 대조
    """
    조건, org인자 = _org조건(org_id, "p")
    계획 = _질의(f'SELECT p.org_id, p."집행예정일" FROM tenant.expense_plans p '
                f'WHERE p.plan_id = %s AND {조건}', (plan_id, *org인자))
    if not 계획:
        raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
    계획org, 집행예정일 = 계획[0]

    기존 = _질의(
        'SELECT task_id, 출처, 코드, 항목, 날짜_사용자수정 '
        'FROM tenant.plan_tasks WHERE plan_id = %s', (plan_id,))
    보존u = sum(1 for r in 기존 if r[1] == "user")
    코드맵 = {r[2]: r for r in 기존 if r[1] == "ai" and r[2] is not None}
    항목맵 = {r[3]: r for r in 기존 if r[1] == "ai" and r[2] is None}

    # 코드부터 확정한다(판정이 code 를 실어주면 그쪽이 이긴다) — 유형 맵을
    # 배치 전체에 한 번만 질의하려고 루프 전에 뗀다.
    작업 = [
        # 네 번째 값 `구분` 은 code 가 없는 항목을 위한 힌트다 — code 가 있으면 DB 가 이긴다.
        # `매칭금지` 는 코드_매칭() 을 아예 안 태운다 — 서류 이름을 확인 항목으로
        # 잘못 흡수하는 것을 막는다(결제 후 서류가 결제 전 행동으로 둔갑하는 사고)
        (h.get("항목") or "", h.get("설명"),
         None if h.get("매칭금지") else (h.get("code") or 코드_매칭(h.get("항목") or "")),
         h.get("구분"))
        for h in body.해야할일
    ]
    유형맵 = _유형_맵({코드 for _, _, 코드, _ in 작업 if 코드})

    생성 = 갱신 = 보존날짜 = 코드매칭수 = 0
    살아남은: set[int] = set()

    for 항목, 설명, 코드, 구분힌트 in 작업:
        if 코드:
            코드매칭수 += 1
        구분, 오프셋, 기한근거 = _체크항목_조회(코드) if 코드 else (None, None, None)
        구분 = 구분 or 구분힌트 or "결제전"
        유형 = 유형맵.get(코드, "기타") if 코드 else "기타"   # 캘린더 배지축 — 판정 4-way 와 무관
        # 기본_오프셋일 은 부호가 방향이다 — 음수(결제전)=그만큼 전, 양수(결제후)=그만큼 후.
        # 근거 없이 쓰지 않는다 — 판단은 _due계산() 이 한다
        새_due = _due계산(집행예정일, 계획org, 항목, 설명, 오프셋, 기한근거)

        기존행 = 코드맵.get(코드) if 코드 else 항목맵.get(항목)
        if 기존행:
            task_id, _출처, _코드, _항목, 날짜수정 = 기존행
            살아남은.add(task_id)
            갱신 += 1
            if 날짜수정:                                  # ② due_date 를 덮지 않는다
                보존날짜 += 1
                _실행(
                    'UPDATE tenant.plan_tasks '
                    'SET 코드=%s, 구분=%s, 항목=%s, 설명=%s, decision_id=%s, "유형"=%s '
                    'WHERE task_id=%s',
                    (코드, 구분, 항목, 설명, body.decision_id, 유형, task_id),
                )
            else:
                _실행(
                    'UPDATE tenant.plan_tasks '
                    'SET 코드=%s, 구분=%s, 항목=%s, 설명=%s, due_date=%s, decision_id=%s, "유형"=%s '
                    'WHERE task_id=%s',
                    (코드, 구분, 항목, 설명, 새_due, body.decision_id, 유형, task_id),
                )
        else:
            생성 += 1
            _실행(
                'INSERT INTO tenant.plan_tasks '
                '(org_id, plan_id, decision_id, 출처, 코드, 구분, 항목, 설명, due_date, "유형") '
                "VALUES (%s,%s,%s,'ai',%s,%s,%s,%s,%s,%s)",
                (계획org, plan_id, body.decision_id, 코드, 구분, 항목, 설명, 새_due, 유형),
            )

    # ③ 코드 소멸 — 이번 회차에 안 나온 ai 행만 지운다 (출처='user' 는 애초에 후보 밖)
    사라진 = [r[0] for r in 기존 if r[1] == "ai" and r[0] not in 살아남은]
    if 사라진:
        # plan_id 를 한 번 더 건다 — task_id 만으로 지우면 호출 경로가 바뀔 때 남의 행을 지울 수 있다
        _실행('DELETE FROM tenant.plan_tasks WHERE task_id = ANY(%s) AND plan_id = %s',
             (사라진, plan_id))

    return 할일동기화응답(
        생성=생성, 갱신=갱신, 보존_user=보존u, 보존_날짜수정=보존날짜,
        코드매칭=코드매칭수, 코드미상=len(body.해야할일) - 코드매칭수,
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
    """소유 판정의 기준은 계획(expense_plans.org_id)이다 — 할일 자체의 org_id 가 아니다.

    `_실_추가`·`_실_동기화` 와 같은 기준으로 통일한 것이다 — 쓰기 경로마다 기준이
    다르면 「추가는 되는데 수정은 404」 같은 어긋남이 생긴다.
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
        수정절.append("날짜_사용자수정=true")     # 이후 재판정이 이 날짜를 덮지 않는다
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
