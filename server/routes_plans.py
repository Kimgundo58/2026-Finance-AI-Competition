# -*- coding: utf-8 -*-
"""지출계획 API — 홈, 목록, 상세 화면이 쓰는 /api/plans 라우터.

GET/POST /api/plans, GET/DELETE /api/plans/{plan_id}. mock 모드(SUDDOE_MOCK=1)에서는
mock_data 를 쓰고, live 모드 구현은 `_live_*` 함수에 있다. 읽고 쓰는 테이블은
tenant.expense_plans, tenant.decisions, tenant.plan_tasks 이다.
응답 모델(models.계획*)을 바꾸면 프론트가 깨지므로 필드는 합의 후에만 바꾼다.
통계는 별도 API 없이 목록 응답에 포함한다. 다른 기관·다른 계정의 계획은 404 로 숨긴다.
계획은 계정 단위다(같은 주관기관의 다른 계정과 나눠 쓰지 않는다). 계정은 토큰에서 정한 현재_account 다.
판정상세의 이름을 바꾼 키는 한 릴리스 동안 이전 이름으로도 함께 보낸다(judge_response.구_판정키_포함).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from ._common import MOCK, 탭_판정, 트랜잭션, _jsonb, _계정조건, _질의, _실행
from .judge_response import 구_판정키_포함, 사용자답변_공개, 할일_공개
from .models import 계획목록응답, 계획상세, 계획생성, 계획수정, 계획통계, 계획요약
from . import auth, mock_data, routes_tasks

router = APIRouter(prefix="/api/plans", tags=["지출계획"])

_log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 목록 — GET /api/plans
# ════════════════════════════════════════════════════════════════════

@router.get("", response_model=계획목록응답)
def 목록(
    탭: str = Query("전체", description="전체·확인필요·위험·특이사항없음·점검전"),
    사업명: str | None = None,
    확정비목: str | None = None,
    q: str | None = Query(None, description="지출명 검색"),
    금액_최소: float | None = None,
    금액_최대: float | None = None,
    정렬: str = Query("최근수정순", description="최근수정순·금액많은순·금액적은순·지출일순"),
    페이지: int = Query(1, ge=1),
    크기: int = Query(20, ge=1, le=100),
    org_id: str | None = None,
) -> 계획목록응답:
    행 = mock_data.mock_계획요약() if MOCK else _live_목록(org_id)
    통계 = 계획통계(**(mock_data.mock_통계() if MOCK else _live_통계(org_id)))

    # 통계는 필터 적용 전 전체 기준이다. 탭 배지 숫자가 필터에 따라 바뀌면 안 된다.
    필터결과 = _필터(행, 탭, 사업명, 확정비목, q, 금액_최소, 금액_최대)
    필터결과 = _정렬(필터결과, 정렬)
    시작 = (페이지 - 1) * 크기
    return 계획목록응답(
        통계=통계, 건수=len(필터결과), 페이지=페이지, 크기=크기,
        항목=[계획요약(**r) for r in 필터결과[시작:시작 + 크기]],
    )


def _필터(행: list[dict], 탭, 사업명, 확정비목, q, 금액_최소, 금액_최대) -> list[dict]:
    if 탭 not in 탭_판정:
        raise HTTPException(422, f"알 수 없는 탭: {탭}")
    허용 = 탭_판정[탭]
    out = []
    for r in 행:
        if 탭 == "점검전":
            if r.get("판정") is not None:
                continue
        elif 허용 is not None and r.get("판정") not in 허용:
            continue
        if 사업명 and r.get("사업명") != 사업명:
            continue
        if 확정비목 and r.get("확정비목") != 확정비목:
            continue
        if q and q not in (r.get("제목") or ""):
            continue
        금액 = r.get("금액") or 0
        if 금액_최소 is not None and 금액 < 금액_최소:
            continue
        if 금액_최대 is not None and 금액 > 금액_최대:
            continue
        out.append(r)
    return out


def _정렬(행: list[dict], 정렬: str) -> list[dict]:
    키 = {
        "최근수정순": (lambda r: r.get("updated_at") or "", True),
        "금액많은순": (lambda r: r.get("금액") or 0, True),
        "금액적은순": (lambda r: r.get("금액") or 0, False),
        "지출일순": (lambda r: r.get("집행예정일") or "9999-99-99", False),
    }.get(정렬)
    if not 키:
        raise HTTPException(422, f"알 수 없는 정렬: {정렬}")
    f, 역순 = 키
    return sorted(행, key=f, reverse=역순)


# ════════════════════════════════════════════════════════════════════
# 생성 — POST /api/plans
# ════════════════════════════════════════════════════════════════════

def _질의_저장(sql: str, 인자: tuple, 소유_org: str | None) -> list[tuple]:
    """저장용 `_질의`. 실패 사유(DB 연결 실패 / RLS 거부 / 제약 위반)에 따라 HTTP 상태를 나눈다.

    사용자 응답에는 SQLSTATE 나 DB 메시지를 넣지 않는다(호스트·포트가 포함될 수 있다).
    사유는 로그에만 남긴다.
    """
    try:
        return _질의(sql, 인자, 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        상태 = getattr(e, "sqlstate", None)
        _log.exception("지출계획 저장 실패 — sqlstate=%s 소유_org=%s", 상태, 소유_org)
        if 상태 == "42501":              # insufficient_privilege: RLS 정책에 의해 거부됨
            if 소유_org is None:
                # 게스트는 권한 부족이 아니라 신원 미확인이므로 401 로 답한다
                raise HTTPException(401, "저장하려면 로그인이 필요합니다") from e
            raise HTTPException(403, "이 기관으로 저장할 권한이 없습니다") from e
        if 상태 is None:                 # 접속 자체가 실패했다 (OperationalError)
            raise HTTPException(503, "지출계획을 저장하지 못했습니다 (DB 연결 실패)") from e
        raise HTTPException(500, "지출계획을 저장하지 못했습니다") from e


def _계획_org(요청: Request, 요청_org_id: str | None) -> str | None:
    """새 계획의 소유 org 를 정한다. 검증된 주체(토큰)의 org 만 쓰고, 없으면 게스트(None)다.

    본문의 org_id(클라이언트가 보낸 검증되지 않은 값)는 쓰지 않는다. 그대로 INSERT 하면
    다른 기관 이름으로 행을 만들 수 있다. 반환값은 OrgId주입 미들웨어가 GUC app.org_id 에
    설정한 값과 같아야 RLS 정책(org_id = current_org())을 통과한다. 본문 org_id 는 400 으로
    거부하지 않고 무시한 채 로그만 남긴다.
    """
    소유_org = auth.검증된_org(요청)
    if 요청_org_id:
        if 소유_org is None:
            _log.warning("계획 생성 본문에 org_id=%s 가 왔지만 검증된 주체가 없다 "
                         "— 무시한다(게스트로 저장 시도)", 요청_org_id)
        elif str(요청_org_id) != 소유_org:
            _log.warning("계획 생성 org_id 불일치 — 토큰=%s 본문=%s · 토큰을 쓴다",
                         소유_org, 요청_org_id)
        else:
            _log.info("계획 생성 본문에 org_id 가 포함돼 있다(값은 일치). 프론트에서 "
                      "빼도 된다")
    return 소유_org


@router.post("", response_model=계획상세, status_code=201)
def 생성(요청: Request, body: 계획생성) -> 계획상세:
    if MOCK:
        새 = max((p["plan_id"] for p in mock_data.mock_계획), default=0) + 1
        행 = {
            "plan_id": 새, "제목": body.제목 or body.품목, "확정비목": body.확정비목,
            "금액": body.금액, "판정": None, "집행예정일": body.집행예정일,
            "updated_at": _지금(), "created_at": _지금(), "사업명": body.사업명,
            "상태": "draft", "질문원문": body.질문원문 or _합성(body),
            "용도": body.용도, "거래처": body.거래처, "추가설명": body.추가설명,
            "정규화": body.정규화, "latest_decision_id": None,
        }
        mock_data.mock_계획.append(행)
        if body.집행예정일:
            _mock_지출일_변경({**행, "집행예정일": None}, 계획수정(
                집행예정일=date.fromisoformat(body.집행예정일)))
        return 계획상세(**행, 할일=[], 판정상세=None)
    return _live_생성(body, org_id=_계획_org(요청, body.org_id),
                     account_id=auth.검증된_account(요청))


def _합성(body: 계획생성) -> str:
    """폼 값을 질문 문장으로 합성한다. `expense_plans.질문원문` 이 NOT NULL 이라서 필요하다.

    이 문장은 저장·검색·표시 전용이고 LLM 입력으로 다시 쓰지 않는다.
    """
    금액문 = f"{int(body.금액):,}원"
    return f"{body.사업명}에서 {body.용도} {body.품목} {금액문}을 사도 되나요?"


def _지금() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ════════════════════════════════════════════════════════════════════
# 상세 · 삭제 — GET/DELETE /api/plans/{plan_id}
# ════════════════════════════════════════════════════════════════════

@router.get("/{plan_id}", response_model=계획상세)
def 상세(plan_id: int, org_id: str | None = None) -> 계획상세:
    if MOCK:
        행 = next((p for p in mock_data.mock_계획 if p["plan_id"] == plan_id), None)
        if not 행:
            raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
        할일 = [t for t in mock_data.mock_할일 if t["plan_id"] == plan_id]
        # 생성() 이 만든 행에는 '정규화' 키가 있고 시드 데이터에는 없어서 병합해 넘긴다
        return 계획상세(**{**행, "정규화": 행.get("정규화") or {}},
                       할일=할일, 판정상세=None)
    return _live_상세(plan_id, org_id)


@router.patch("/{plan_id}", response_model=계획상세)
def 수정(plan_id: int, body: 계획수정, org_id: str | None = None) -> 계획상세:
    """예상 지출일을 바꾼다. 판정 경로는 집행예정일을 읽지 않아 판정은 그대로다."""
    if MOCK:
        행 = next((p for p in mock_data.mock_계획 if p["plan_id"] == plan_id), None)
        if not 행:
            raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
        if "집행예정일" in body.model_fields_set:
            _mock_지출일_변경(행, body)
        return 상세(plan_id)
    조건, org인자 = _계정조건(org_id, "p")
    with 트랜잭션() as cur:
        cur.execute(f'SELECT p.org_id, p.account_id, p.제목, p."집행예정일" FROM tenant.expense_plans p '
                    f'WHERE p.plan_id = %s AND {조건} FOR UPDATE', (plan_id, *org인자))
        계획 = cur.fetchone()
        if not 계획:
            raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
        계획org, 계획account, 제목, 이전일 = 계획
        if "집행예정일" in body.model_fields_set and body.집행예정일 != 이전일:
            cur.execute('UPDATE tenant.expense_plans SET "집행예정일" = %s WHERE plan_id = %s',
                        (body.집행예정일, plan_id))
            routes_tasks.집행줄_유지(cur, plan_id, 계획org, 계획account, 제목, body.집행예정일)
            routes_tasks.지출일_변경_반영(cur, plan_id, 계획org, 이전일, body.집행예정일, body.일정이동)
    return _live_상세(plan_id, org_id)


def _mock_지출일_변경(행: dict, body: 계획수정) -> None:
    새날짜 = body.집행예정일
    이전일 = date.fromisoformat(행["집행예정일"]) if 행.get("집행예정일") else None
    행["집행예정일"] = 새날짜.isoformat() if 새날짜 else None
    if 새날짜 == 이전일:
        return
    할일들 = [t for t in mock_data.mock_할일 if t["plan_id"] == 행["plan_id"]]
    연동 = next((t for t in 할일들 if t.get("집행예정일_연동")), None)
    if 새날짜 is None:
        if 연동 and 연동["상태"] != "완료":
            mock_data.mock_할일.remove(연동)
        return
    if 연동:
        연동["due_date"] = 새날짜.isoformat()
    else:
        mock_data.mock_할일.append({
            "task_id": max((t["task_id"] for t in mock_data.mock_할일), default=0) + 1,
            "plan_id": 행["plan_id"], "출처": "user", "code": None, "구분": "집행",
            "항목": f'{행["제목"]} 집행', "설명": None, "due_date": 새날짜.isoformat(), "유형": "기타",
            "날짜_사용자수정": True, "상태": "집행예정", "계획제목": 행["제목"], "집행예정일_연동": True})
    for t in 할일들:
        if t.get("집행예정일_연동") or not t.get("due_date"):
            continue
        if not body.일정이동 or t["상태"] == "완료":
            t["날짜_사용자수정"] = True
        elif 이전일:
            t["due_date"] = (date.fromisoformat(t["due_date"]) + (새날짜 - 이전일)).isoformat()


@router.delete("/{plan_id}", status_code=200)
def 삭제(plan_id: int, org_id: str | None = None) -> dict:
    """지출계획 하나를 지운다. 요청한 org 의 계획만 지워진다.

    FK 설정상 plan_tasks 는 CASCADE 로 같이 삭제되고, decisions 는 SET NULL 로 연결만 끊긴다
    (판정 기록은 감사 기록으로 남긴다). 되돌릴 수 없으므로 같이 지워진 할일 건수를 반환한다.
    """
    if MOCK:
        전 = len(mock_data.mock_계획)
        mock_data.mock_계획[:] = [p for p in mock_data.mock_계획 if p["plan_id"] != plan_id]
        if len(mock_data.mock_계획) == 전:
            raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
        할일 = [t for t in mock_data.mock_할일 if t["plan_id"] == plan_id]
        mock_data.mock_할일[:] = [t for t in mock_data.mock_할일 if t["plan_id"] != plan_id]
        return {"삭제": True, "plan_id": plan_id, "할일삭제": len(할일)}

    조건, org인자 = _계정조건(org_id, "p")
    # CASCADE 뒤에는 셀 수 없으므로 지우기 전에 센다
    행 = _질의(
        f"SELECT p.제목, (SELECT count(*) FROM tenant.plan_tasks t WHERE t.plan_id = p.plan_id) "
        f"  FROM tenant.expense_plans p WHERE p.plan_id = %s AND {조건}",
        (plan_id, *org인자))
    if not 행:
        # 다른 org 의 계획이어도 404 다. 403 을 주면 id 존재 여부가 드러난다
        raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
    제목, 할일수 = 행[0]
    n = _실행(f"DELETE FROM tenant.expense_plans p WHERE p.plan_id = %s AND {조건}",
             (plan_id, *org인자))
    if n != 1:
        raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
    _log.info("지출계획 삭제 plan_id=%s 제목=%r 할일 %d건 동반삭제", plan_id, 제목, 할일수)
    return {"삭제": True, "plan_id": plan_id, "제목": 제목, "할일삭제": 할일수}


# ════════════════════════════════════════════════════════════════════
# live 모드 구현
# ════════════════════════════════════════════════════════════════════

def _문자열(v) -> str | None:
    """DATE·TIMESTAMPTZ(psycopg 가 date/datetime 객체로 준다)를 ISO 문자열로 바꾼다."""
    return v.isoformat() if v is not None else None


def _float변환(v) -> float | None:
    """NUMERIC(psycopg 가 Decimal 로 준다)을 float 로 바꾼다."""
    return float(v) if v is not None else None


_목록_컬럼 = ("plan_id", "제목", "확정비목", "금액", "판정", "집행예정일",
             "updated_at", "사업명", "상태")


def _live_목록(org_id: str | None) -> list[dict]:
    """tenant.expense_plans LEFT JOIN tenant.decisions ON latest_decision_id.

    반환 dict 의 키는 `models.계획요약` 필드와 같다. org_id 가 None 이면 게스트(org_id IS NULL)
    행만 본다.
    """
    조건, 인자 = _계정조건(org_id)
    행 = _질의(f"""
        SELECT p.plan_id, p.제목, p.확정비목, p.금액, d.판정, p.집행예정일,
               p.updated_at, p.사업명, p.상태
        FROM tenant.expense_plans p
        LEFT JOIN tenant.decisions d ON d.decision_id = p.latest_decision_id
        WHERE {조건}
        ORDER BY p.updated_at DESC
    """, 인자)
    out = []
    for r in 행:
        d = dict(zip(_목록_컬럼, r))
        d["금액"] = _float변환(d["금액"])
        d["집행예정일"] = _문자열(d["집행예정일"])
        d["updated_at"] = _문자열(d["updated_at"])
        out.append(d)
    return out


def _live_통계(org_id: str | None) -> dict:
    """같은 JOIN 한 번으로 탭별 건수 5개와 금액합계를 센다. 키는 `models.계획통계` 와 같다."""
    조건, 인자 = _계정조건(org_id)
    행 = _질의(f"""
        SELECT
            count(*),
            count(*) FILTER (WHERE d.판정 IN ('조건부', '판단불가')),
            count(*) FILTER (WHERE d.판정 = '불가'),
            count(*) FILTER (WHERE d.판정 = '가능'),
            count(*) FILTER (WHERE d.판정 IS NULL),
            coalesce(sum(p.금액), 0)
        FROM tenant.expense_plans p
        LEFT JOIN tenant.decisions d ON d.decision_id = p.latest_decision_id
        WHERE {조건}
    """, 인자)
    # _질의 는 DB 접속 실패에도 빈 리스트를 준다. 이때는 0 으로 채운다
    if not 행:
        return {"전체": 0, "확인필요": 0, "위험": 0, "특이사항없음": 0, "점검전": 0, "금액합계": 0.0}
    r = 행[0]
    return {
        "전체": r[0], "확인필요": r[1], "위험": r[2],
        "특이사항없음": r[3], "점검전": r[4], "금액합계": float(r[5]),
    }


_생성_컬럼 = ("plan_id", "제목", "확정비목", "금액", "집행예정일", "updated_at", "created_at",
             "사업명", "상태", "질문원문", "거래처", "추가설명", "정규화", "latest_decision_id")


def _live_생성(body: 계획생성, *, org_id: str | None, account_id: str | None = None) -> 계획상세:
    """INSERT INTO tenant.expense_plans. 질문원문이 없으면 `_합성(body)` 을 쓴다.

    org_id·account_id 는 호출자가 검증된 주체에서 정한 값이다(`_계획_org`, auth.검증된_account).
    body.org_id 는 쓰지 않는다. account_id 는 데모 세션·게스트면 None 이다.
    """
    제목 = body.제목 or body.품목
    질문원문 = body.질문원문 or _합성(body)
    # 정규화 전에 임시저장한 계획도 입력값을 남긴다. 상세의 용도와 판정의 금지목록 대조가 정규화 값을 읽는다.
    정규화 = body.정규화 or {"품목": body.품목, "용도": body.용도, "금액": body.금액,
                       "거래처": body.거래처 or "", "질문원문": 질문원문}
    # 지출일이 있으면 집행 줄도 같은 문장으로 만든다(`routes_tasks.집행줄_유지` 와 같은 행 모양). 일정 탭에 지출일이 보인다.
    행 = _질의_저장(
        """
        WITH 새계획 AS (
            INSERT INTO tenant.expense_plans
                (org_id, account_id, 제목, 질문원문, 정규화, 사업명, 확정비목, 금액, 집행예정일, 거래처, 추가설명)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING plan_id, org_id, account_id, 제목, 확정비목, 금액, 집행예정일, updated_at, created_at,
                      사업명, 상태, 질문원문, 거래처, 추가설명, 정규화, latest_decision_id
        ), 집행줄 AS (
            INSERT INTO tenant.plan_tasks (org_id, account_id, plan_id, 출처, 구분, 항목, due_date,
                                           상태, 날짜_사용자수정, 집행예정일_연동)
            SELECT org_id, account_id, plan_id, 'user', '집행', 제목 || ' 집행', 집행예정일,
                   '집행예정', true, true
            FROM 새계획 WHERE 집행예정일 IS NOT NULL
        )
        SELECT plan_id, 제목, 확정비목, 금액, 집행예정일, updated_at, created_at,
               사업명, 상태, 질문원문, 거래처, 추가설명, 정규화, latest_decision_id
        FROM 새계획
        """,
        (org_id, account_id if org_id else None, 제목, 질문원문, _jsonb(정규화), body.사업명,
         body.확정비목, body.금액, body.집행예정일, body.거래처, body.추가설명),
        org_id,
    )
    if not 행:
        # _질의_저장 은 실패를 예외로 던지므로 정상 경로에서는 여기 오지 않는다
        raise HTTPException(500, "지출계획을 저장하지 못했습니다")
    r = dict(zip(_생성_컬럼, 행[0]))
    return 계획상세(
        plan_id=r["plan_id"], 제목=r["제목"], 확정비목=r["확정비목"], 금액=_float변환(r["금액"]),
        판정=None,                              # 방금 만든 계획이라 아직 판정이 없다
        집행예정일=_문자열(r["집행예정일"]), updated_at=_문자열(r["updated_at"]),
        created_at=_문자열(r["created_at"]),
        사업명=r["사업명"], 상태=r["상태"], 질문원문=r["질문원문"], 용도=body.용도,
        거래처=r["거래처"], 추가설명=r["추가설명"], 정규화=r["정규화"] or {},
        latest_decision_id=r["latest_decision_id"],
        판정상세=None, 할일=[],
    )


_상세_컬럼 = ("plan_id", "제목", "확정비목", "금액", "판정", "집행예정일",
             "updated_at", "created_at", "사업명", "상태", "질문원문",
             "거래처", "추가설명", "정규화", "latest_decision_id",
             "요약", "해야할일", "인용", "전제", "신뢰등급", "규정버전",
             "참조관계", "검증사유", "사용자답변")

# 응답 키 순서. DB 컬럼 plan_tasks.코드 는 응답 키 code 로 내보낸다.
_계획할일_컬럼 = ("task_id", "plan_id", "출처", "code", "구분", "항목", "설명", "due_date",
               "유형", "날짜_사용자수정", "상태", "집행예정일_연동")


def _live_상세(plan_id: int, org_id: str | None) -> 계획상세:
    """계획, 최신 판정, plan_tasks 를 함께 돌려준다. 없거나 다른 기관 것이면 404."""
    조건, org인자 = _계정조건(org_id)
    행 = _질의(f"""
        SELECT p.plan_id, p.제목, p.확정비목, p.금액, d.판정, p.집행예정일,
               p.updated_at, p.created_at, p.사업명, p.상태, p.질문원문,
               p.거래처, p.추가설명, p.정규화, p.latest_decision_id,
               d.요약, d.해야할일, d.인용, d.전제, d.신뢰등급, d.규정버전,
               d.참조관계, d.검증사유, d."사용자답변"
        FROM tenant.expense_plans p
        LEFT JOIN tenant.decisions d ON d.decision_id = p.latest_decision_id
        WHERE p.plan_id = %s AND {조건}
    """, (plan_id, *org인자))
    if not 행:
        raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
    r = dict(zip(_상세_컬럼, 행[0]))
    정규화 = r["정규화"] or {}

    판정상세 = None
    if r["latest_decision_id"] is not None:          # 판정을 받은 적이 있다
        판정상세 = {
            "판정": r["판정"], "요약": r["요약"], "해야할일": 할일_공개(r["해야할일"]),
            "인용": r["인용"] or [], "전제": r["전제"] or [], "신뢰등급": r["신뢰등급"],
            "규정버전": r["규정버전"], "참조관계": r["참조관계"] or [],
            "검증사유": r["검증사유"] or [],
        }
        판정상세 = 구_판정키_포함(판정상세)  # 구 이름 호환(제거 예정: 다음 릴리스)
        # 문의초안은 저장하지 않고 조회 때마다 만든다. 저장된 인용·전제·정규화에서 파생되는
        # 글이라 스키마를 늘릴 필요가 없고, 문구를 고치면 이전 판정에도 적용된다.
        # 판단불가가 아니면 문의초안() 이 None 을 준다
        try:
            from .inquiry import 문의초안 as _초안
            판정상세["문의초안"] = _초안(
                정규화=정규화, 판정=r["판정"] or "",
                인용=r["인용"] or [], 전제=r["전제"] or [], 사업명=r["사업명"])
        except Exception:                                     # noqa: BLE001
            # 부가 정보라서 초안 생성이 실패해도 계획 조회는 실패시키지 않는다
            _log.exception("문의초안 재생성 실패 plan_id=%s", plan_id)
            판정상세["문의초안"] = None

    할일행 = _질의(
        """
        SELECT task_id, plan_id, 출처, 코드, 구분, 항목, 설명, due_date,
               "유형", 날짜_사용자수정, 상태, 집행예정일_연동
        FROM tenant.plan_tasks WHERE plan_id = %s
        """,
        (plan_id,),
    )
    할일 = []
    for t in 할일행:
        d = dict(zip(_계획할일_컬럼, t))
        d["due_date"] = _문자열(d["due_date"])
        d["계획제목"] = r["제목"]
        할일.append(d)

    return 계획상세(
        plan_id=r["plan_id"], 제목=r["제목"], 확정비목=r["확정비목"], 금액=_float변환(r["금액"]),
        판정=r["판정"], 집행예정일=_문자열(r["집행예정일"]),
        updated_at=_문자열(r["updated_at"]), created_at=_문자열(r["created_at"]),
        사업명=r["사업명"], 상태=r["상태"], 질문원문=r["질문원문"], 용도=정규화.get("용도"),
        거래처=r["거래처"], 추가설명=r["추가설명"], 정규화=정규화,
        latest_decision_id=r["latest_decision_id"],
        판정상세=판정상세, 사용자답변=사용자답변_공개(r["사용자답변"]), 할일=할일,
    )
