# -*- coding: utf-8 -*-
"""할일 「확인필요」 — 화면 11 ⑧ 집행 준비 · 화면 6 ⑥ 다가오는 일정.   **[할일 계통]**

🔴 체크리스트와 캘린더는 **같은 테이블 같은 행**이다. `due_date` 유무로만 갈린다.
   따로 두면 상세에서 체크했는데 캘린더는 「준비 필요」로 남는 식으로 반드시 어긋난다.
   (`db/init/02_frontend.sql` tenant.plan_tasks 주석)

목 경로는 끝까지 구현돼 있다. 🔴 **실 경로는 `_실_*` 함수와 `코드_매칭()` 에 있다.**
응답 모델과 목 경로는 건드리지 않는다.
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
    """🔴 재판정 규칙 — 여기가 제일 틀리기 쉽다.

    ① 출처='user' 행은 **절대** 건드리지 않는다
    ② 날짜_사용자수정=true 인 행의 due_date 는 덮지 않는다
    ③ 같은 `코드` 의 ai 행은 갱신, 없어진 것은 지운다
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
    """조사·존댓말 어미를 뗀다. 안 떼면 "...확인하세요" 같은 공통 꼬리 때문에
    내용이 완전히 달라도 SequenceMatcher 유사도가 뜬다(2026-09-01 실측)."""
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

    🔴 2026-09-01 실측(`tenant.decisions` 73행·해야할일 146건 전수)으로 판정이
       이미 `code` 를 100% 싣는다는 게 확인됐다 — 이 함수는 실경로에서는 거의 안
       탄다(`h.get("code") or 코드_매칭(...)` 라 code 가 있으면 먼저 이긴다).
       그래도 남겨둔다: code 가 비는 경로가 생기면(구버전 판정 재생 등) 여기가
       마지막 안전판이다. 실패하면 None → 호출부가 코드=NULL · 구분='결제전' 기본값으로 넣는다.

    🔴 2026-09-01 검수로 재조정. 원문 그대로 SequenceMatcher 만 쓰던 이전
       버전은 임계값 0.3 에서 확신에 찬 오답을 냈다(실측): "비교견적 3곳 이상 확보"
       → 멘토링한도_예비창업(0.35), "집행 전에 주관기관 승인을 받으세요" →
       국외출장사전보고(0.50). 둘 다 내용은 다른데 존댓말 어미("...하세요")가 겹쳐
       유사도가 뜬 것.
       어미 제거(`_문장_정규화`) + IDF 가중 바이그램 포함도로 바꾸고 임계값을 0.50
       으로 올렸다. 회귀셋(진짜 매칭 23건 + 오답성 문장 7건) 실측: 이전 POS 19/23 ·
       NEG(마땅히 None 이어야 할 것 중 실제 None) 0/7 → 이후 POS 23/23 · NEG 5/7.
       **완벽하진 않다** — "전에 주관기관"처럼 진짜 겹치는 구가 있으면 오답 2건이
       남는다(회귀 스크립트 별도 공유). 판정이 코드를 직접 실어주기 시작하면
       이 경로 자체가 안 탄다 — 그때까지의 안전판일 뿐이다.
    """
    if MOCK:
        return None
    항목 = (항목 or "").strip()
    if not 항목:
        return None
    후보 = _질의('SELECT code, "항목" FROM corpus.check_items')
    if not 후보:
        return None
    # 🔴 2026-09-06 — «code 이름 그 자체» 를 먼저 본다. 유사도보다 앞이다.
    #    실측: LLM 이 「자산 등록」을 냈는데 `코드_매칭` 이 None 을 줬다. code 는
    #    「자산등록」(공백 없음)이고 마스터 «항목» 은 "취득가액 500만원 초과면
    #    자산관리대장에 등록하세요" 라, 짧은 이름은 항목과 바이그램이 거의 안 겹쳐
    #    임계 0.50 을 못 넘는다. 「자산등록」을 그대로 넣어도 실패했다(실측).
    #    🔴 이게 조용히 연쇄를 만든다 — code=NULL → `_할일_보강` 이 `구분` 을 못 붙임
    #       → `_실_동기화` 가 `구분 or "결제전"` 으로 «결제전» 을 박음 → 화면의
    #       「결제 후 필요한 증빙자료」(구분='결제후' 필터)가 통째로 빈다.
    #    공백만 지워 정확히 일치할 때만 받는다 — 유사도가 아니라 «동일 문자열» 이라 오답이 없다.
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
            행["날짜_사용자수정"] = True      # 🔴 이후 재판정이 이 날짜를 덮지 않는다
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
# 🔴 실 경로 구역
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

    🔴 셋 다 LLM 이 만드는 게 아니라 코드가 붙인다 (`프로토타입_해부_구현명세.md` §4-4).

    🔴 `기한근거` 를 «같이» 돌려주는 것이 이 함수의 요점이다 (2026-09-06).
       `기본_오프셋일` 은 52행 중 **45행이 규정 근거가 없다**(`기한근거='운영기본값'`).
       근거를 안 보고 쓰면 우리가 정한 관행이 화면에서 «규정상 기한» 으로 읽힌다.
       판단은 `_due계산()` 이 한다 — 여기서는 재료를 빠짐없이 넘기기만 한다.

    🔴 2026-09-07(F3, ai-5c 실측) — `_질의()` 는 실패하면 **조용히 `[]`** 을 준다
       (`_common.py::_질의` 기본값). 그러면 이 함수는 `(None, None, None)` 을 돌려주고,
       호출부 `_실_동기화()` 는 `구분 = 구분 or "결제전"` 으로 덮는다 — **쿼리가 죽어도
       화면은 에러 없이 뜨고, 결제후 코드까지 전부 결제전으로 찍힌다.**
       실측: 프로덕션 `tenant.plan_tasks` 전 행(외주검수조서·거래처증빙수취 등 결제후
       코드 포함)이 구분='결제전' 이었다 — 로컬 DB 로는 같은 쿼리가 정상 작동해
       코드 자체는 무죄, 프로덕션 쪽 실패(스키마 드리프트 또는 배포 지연 추정)로 보인다.
       실패를 다시 삼키면 다음 사람도 똑같이 못 찾는다 — 여기서 로그로 남긴다.
       (전파는 안 한다 — `_질의` 독스트링 그대로: "DB 가 없어도 서버는 떠야 한다")
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
    """`due_date` — **규정 근거가 있을 때만** 만든다. 없으면 `None`.

    `None` 은 «체크리스트에는 남고 캘린더에는 안 뜬다» 는 뜻이다 (`models.py:169`).
    할일이 사라지는 게 아니라 «날짜만» 안 붙는다.

    갈래는 셋이다:
      ① `기한근거='규정근거'`  → `기본_오프셋일` 을 그대로 쓴다. 이미 규정에서 온 값이다
      ② 그 밖(운영기본값·미확정·NULL) → `l3_deadline` 이 이 기관 L3 에서 찾는다
      ③ L3 에도 없으면        → **날짜를 만들지 않는다**

    🔴 ②에서 L3 가 「사업 종료 후 30일 이내」처럼 «집행예정일로 환산 못 하는» 기준을
       쓰면 `l3_deadline` 이 `None` 을 준다 — 추측해서 띄우지 않는다. 그 판단은
       그 모듈에 있고 여기서 되풀이하지 않는다.
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

    🔴 예전엔 하드코딩 맵(`_common.유형_추정`)을 썼다 — 값은 지금도 같다(52종 중
       비교견적1·계약1·기타50). 표 컬럼으로 옮긴 건 값이 달라서가 아니라 마스터가
       늘어도(52→60 등) 코드가 안 죽는다는 점 때문이다. 코드=NULL 이거나 맵에
       없으면 호출부가 '기타' 로 채운다 — `할일유형_ENUM` 밖 값은 절대 못 나간다
       (`plan_tasks_유형_check` CHECK 가 있어도 여기서부터 막는다).
    """
    if not 코드들:
        return {}
    행 = _질의('SELECT code, "유형" FROM corpus.check_items WHERE code = ANY(%s)', (list(코드들),))
    return {code: (유형 or "기타") for code, 유형 in 행}


def _실_동기화(plan_id: int, body: 할일동기화, org_id: str | None = None) -> 할일동기화응답:
    """판정 결과 → `tenant.plan_tasks` 적재. 재판정 규칙 4개(§4-2)를 지킨다.

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

    # 1차: 코드부터 확정한다 (판정이 코드를 실어주면 그쪽이 이긴다) — 유형 맵을
    # 배치 전체에 한 번만 질의하려고 항목별 루프보다 먼저 뗀다.
    작업 = [
        # 🔴 2026-09-07 — 네 번째 자리 `구분` 은 «코드가 없는 항목» 을 위한 힌트다.
        #    `corpus.rules.증빙`(결제 후 제출 서류)은 check_items 에 없어 code 가 없고,
        #    그러면 아래에서 무조건 '결제전' 으로 찍힌다 — 결제 후 서류가 결제 전
        #    칸에 뜬다. code 가 있으면 «DB 가 이긴다»(힌트는 무시된다).
        (h.get("항목") or "", h.get("설명"), h.get("code") or 코드_매칭(h.get("항목") or ""),
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
        유형 = 유형맵.get(코드, "기타") if 코드 else "기타"   # 🔴 캘린더 배지축 — 판정 4-way 와 무관
        # 🔴 기본_오프셋일 은 부호가 방향이다 — 음수(결제전) = 그만큼 전, 양수(결제후) = 그만큼 후.
        #    "집행일 기준 며칠 전" 이라는 컬럼 주석과 달리 실측(52행)은 -14~-3 이 결제전,
        #    0~30 이 결제후로 부호가 나뉘어 있다. 뺄셈이 아니라 덧셈이다.
        # 🔴 2026-09-06 — 그 값을 «근거 없이» 쓰지 않는다. 판단은 `_due계산()` 으로 옮겼다.
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
        # 🔴 `plan_id` 를 한 번 더 건다. 위에서 계획 소유를 확인했고 `사라진` 도 그 계획의
        #    행에서만 나오므로 지금은 없어도 같은 결과지만, task_id 만으로 지우는 DELETE 는
        #    호출 경로가 하나만 바뀌어도 남의 행을 지우는 문장이 된다 (2026-09-02 정밀검토).
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
    """🔴 소유 판정의 «기준»은 계획이다 — 할일이 아니다 (2026-09-02 정밀검토에서 맞췄다).

    처음엔 `plan_tasks.org_id` 로 봤는데, `_실_추가`·`_실_동기화` 는 `expense_plans.org_id`
    로 본다. **같은 쓰기 3경로가 서로 다른 기준을 쓰면** 두 값이 어긋나는 날
    「추가는 되는데 수정은 404」 같은 게 나온다. 지금은 두 쓰기가 다 계획의 org 를
    복사해 넣어서 어긋난 행이 0이지만(실측), 기준이 둘인 것 자체가 부채다.
    → 셋 다 계획을 기준으로 통일한다. 할일이 그 계획 것인지는 `t.plan_id` 가 잡는다.
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
        수정절.append("날짜_사용자수정=true")     # 🔴 이후 재판정이 이 날짜를 덮지 않는다
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
