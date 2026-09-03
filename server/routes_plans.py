# -*- coding: utf-8 -*-
"""지출계획 — 화면 6 홈 · 화면 7 목록 · 화면 11 상세.   **[지출계획 계통]**

목(mock) 경로는 끝까지 구현돼 있다. 프론트는 이 파일 그대로 붙을 수 있다.
🔴 **실 경로는 `_실_*` 세 함수에만 있다.** 목 경로와 응답 모델은 건드리지 않는다.
   응답 필드가 달라지면 프론트가 깨진다 — 필드를 바꾸려면 먼저 합의할 것.

CSV 는 만들지 않는다 — 프론트 요구서 §화면7-③ 이 «브라우저 생성» 으로 확정했다.
통계는 별도 API 가 아니라 목록 응답에 얹는다 (§화면6-④ «통계 전용 API 불필요»).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from ._common import MOCK, 탭_판정, _질의, _실행
from .models import 계획목록응답, 계획상세, 계획생성, 계획통계, 계획요약
from . import mock_data

router = APIRouter(prefix="/api/plans", tags=["지출계획"])

_log = logging.getLogger(__name__)

# 🔴 «본문 값을 쓴다» 와 «주인이 없다(게스트)» 를 갈라야 해서 센티넬을 둔다.
#    None 을 기본값으로 두면 게스트를 뜻하는 None 과 구별이 안 되고, 그러면
#    라우터가 org 를 안 넘긴 실수가 «게스트» 가 아니라 «본문 신뢰» 로 조용히 떨어진다.
_주어지지않음 = object()


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
    행 = mock_data.목_계획요약() if MOCK else _실_목록(org_id)
    통계 = 계획통계(**(mock_data.목_통계() if MOCK else _실_통계(org_id)))

    # 🔴 통계는 «필터 적용 전» 전체 기준이다. 탭 배지가 필터에 따라 흔들리면 안 된다.
    걸린 = _거르기(행, 탭, 사업명, 확정비목, q, 금액_최소, 금액_최대)
    걸린 = _정렬(걸린, 정렬)
    시작 = (페이지 - 1) * 크기
    return 계획목록응답(
        통계=통계, 건수=len(걸린), 페이지=페이지, 크기=크기,
        항목=[계획요약(**r) for r in 걸린[시작:시작 + 크기]],
    )


def _거르기(행: list[dict], 탭, 사업명, 확정비목, q, 금액_최소, 금액_최대) -> list[dict]:
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

def _질의_저장(sql: str, 인자: tuple, 주인: str | None) -> list[tuple]:
    """저장용 `_질의`. 🔴 **실패 사유를 갈라서 내보낸다.**

    ■ 왜 갈라야 하나 (2026-09-03 · 실서버에서 실제로 헛짚었다)
      무인증으로 `POST /api/plans` 를 치면 「DB 연결 실패」가 떴는데 **DB 는 멀쩡했다.**
      같은 서버의 읽기가 413건을 돌려주고 있었다. 실제 기전은 이렇다:

          주체 없음 → GUC 안 세움 → current_org() NULL → RLS 가 INSERT 차단(42501)
                   → `_질의` 가 예외를 삼켜 [] → `if not 행:` → 503 「DB 연결 실패」

      즉 ⓐ DB 다운 ⓑ 권한/RLS 차단 ⓒ 제약 위반이 **한 문구로 뭉개진다.**
      `auth._계정조회()` 가 「죽은 DB」와 「없는 계정」을 가른 것과 «완전히 같은» 축이고,
      처방도 같다 — `예외전파=True`.

    ■ 🔴 `예외전파` 는 **여기 한 자리만** 켠다. `_질의` 호출부가 37곳이라 기본값을
      바꾸면 조용하던 실패가 전부 500 으로 튄다.

    ■ 🔴 사용자에게는 SQLSTATE·DB 메시지를 안 준다. psycopg 의 오류 본문에는 호스트·
      포트·사용자명·컬럼명이 그대로 실린다. 사유는 로그에만 남기고 화면 문구는 셋으로 닫는다.
    """
    try:
        return _질의(sql, 인자, 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        상태 = getattr(e, "sqlstate", None)
        _log.exception("지출계획 저장 실패 — sqlstate=%s 주인=%s", 상태, 주인)
        if 상태 == "42501":              # insufficient_privilege = RLS 가 물었다
            if 주인 is None:
                # 🔴 게스트다. 「권한이 없다」가 아니라 「누군지 모른다」가 맞다 —
                #    403 으로 주면 로그인해도 안 될 것처럼 읽힌다.
                raise HTTPException(401, "저장하려면 로그인이 필요합니다") from e
            raise HTTPException(403, "이 기관으로 저장할 권한이 없습니다") from e
        if 상태 is None:                 # 접속 자체가 안 됐다 (OperationalError)
            raise HTTPException(503, "지출계획을 저장하지 못했습니다 (DB 연결 실패)") from e
        raise HTTPException(500, "지출계획을 저장하지 못했습니다") from e


def _계획_주인(요청: Request, 자기신고: str | None) -> str | None:
    """계획의 «주인» 을 정한다. 🔴 **본문의 `org_id` 는 절대 쓰지 않는다.**

    ■ 왜 본문을 버리나
      본문은 클라이언트가 쓴 값이다. 그걸 그대로 INSERT 하면 남의 기관 이름으로
      행을 심을 수 있다. 지금은 RLS 가 DB 층에서 한 번 더 막지만, 그건 «두 번째»
      방어선이지 근거가 아니다 — `routes_l3._업로드_주인` 과 같은 처방을 같은 축에
      적용한다 (`/api/judge` 는 이미 닫혔고 여기만 남아 있었다).

    ■ 🔴 돌려주는 값은 `auth.OrgId주입` 이 GUC(`app.org_id`)에 세운 값과 «같아야»
      한다. RLS 정책이 `org_id = current_org()` 라서, 둘이 어긋나면 토큰이 멀쩡해도
      INSERT 가 거부된다(503). 그래서 판정식을 미들웨어와 «같은 문장» 으로 둔다:
      `주.검증됨 and 주.org_id`. 여기를 손대려면 `auth.py` 의 그 줄도 같이 봐야 한다.

    ■ 본문에 org_id 가 실려 와도 **400 으로 죽이지 않는다.** 프론트가 아직 보내고
      있을 수 있고, 시연 중에 그걸로 멈추면 손해가 더 크다. 무시하고 로그만 남긴다.
    """
    주 = 요청.scope.get("suddoe_주체")
    주인 = str(주.org_id) if (주 is not None and 주.검증됨 and 주.org_id) else None
    if 자기신고:
        if 주인 is None:
            _log.warning("계획 생성 본문에 org_id=%s 가 왔지만 «검증된 주체» 가 없다 "
                         "— 무시한다(게스트로 저장 시도)", 자기신고)
        elif str(자기신고) != 주인:
            _log.warning("계획 생성 org_id 불일치 — 토큰=%s 본문=%s · 토큰을 쓴다",
                         주인, 자기신고)
        else:
            _log.info("계획 생성 본문이 아직 org_id 를 보낸다(값은 일치) — 프론트에서 "
                      "빼도 된다")
    return 주인


@router.post("", response_model=계획상세, status_code=201)
def 생성(요청: Request, body: 계획생성) -> 계획상세:
    if MOCK:
        새 = max((p["plan_id"] for p in mock_data.목_계획), default=0) + 1
        행 = {
            "plan_id": 새, "제목": body.제목 or body.품목, "확정비목": body.확정비목,
            "금액": body.금액, "판정": None, "집행예정일": body.집행예정일,
            "updated_at": _지금(), "created_at": _지금(), "사업명": body.사업명,
            "상태": "draft", "질문원문": body.질문원문 or _합성(body),
            "용도": body.용도, "거래처": body.거래처, "추가설명": body.추가설명,
            "정규화": body.정규화, "latest_decision_id": None,
        }
        mock_data.목_계획.append(행)
        return 계획상세(**행, 할일=[], 판정상세=None)
    return _실_생성(body, org_id=_계획_주인(요청, body.org_id))


def _합성(body: 계획생성) -> str:
    """폼 값을 문장으로 합성한다 — `expense_plans.질문원문` 이 NOT NULL 이라서다.

    🔴 스키마를 바꾸지 않으려고 택한 방법이다 (`프로토타입_해부_구현명세.md` §6-1 (b)안).
    ⚠️ 이 문장을 다시 LLM 입력으로 쓰지 않는다. 저장·검색·표시 전용이다.
    """
    만원 = f"{int(body.금액):,}원"
    return f"{body.사업명}에서 {body.용도} {body.품목} {만원}을 사도 되나요?"


def _지금() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ════════════════════════════════════════════════════════════════════
# 상세 — GET /api/plans/{plan_id}
# ════════════════════════════════════════════════════════════════════

@router.get("/{plan_id}", response_model=계획상세)
def 상세(plan_id: int, org_id: str | None = None) -> 계획상세:
    if MOCK:
        행 = next((p for p in mock_data.목_계획 if p["plan_id"] == plan_id), None)
        if not 행:
            raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
        할일 = [t for t in mock_data.목_할일 if t["plan_id"] == plan_id]
        # 🔴 `생성()` 이 만든 행에는 "정규화" 키가 이미 있고 시드 5건에는 없다.
        #    `**행` 과 `정규화=` 를 같이 쓰면 새 계획 조회가 TypeError → 500 이었다.
        return 계획상세(**{**행, "정규화": 행.get("정규화") or {}},
                       할일=할일, 판정상세=None)
    return _실_상세(plan_id, org_id)


# ════════════════════════════════════════════════════════════════════
# 🔴 실 경로 구역 — 아래 넷이 전부다
# ════════════════════════════════════════════════════════════════════

def _문자열(v) -> str | None:
    """DATE·TIMESTAMPTZ (psycopg 가 date/datetime 객체로 준다) → ISO 문자열."""
    return v.isoformat() if v is not None else None


def _jsonb(v):
    """JSONB 컬럼 바인딩용. `psycopg` 는 dict 를 자동 변환하지 않는다 (`Json` 래퍼 필요).
    목 모드에선 이 코드가 안 불리므로 psycopg 미설치라도 import 는 지연시킨다."""
    from psycopg.types.json import Json
    return Json(v)


def _org조건(org_id: str | None, 별칭: str = "p") -> tuple[str, tuple]:
    """org_id 가 None 이면 게스트(org_id IS NULL) 행만 — 🔴 남의 기관 행이 새면 TENANT_LEAK."""
    if org_id is None:
        return f"{별칭}.org_id IS NULL", ()
    return f"{별칭}.org_id = %s", (org_id,)


def _실_목록(org_id: str | None) -> list[dict]:
    """tenant.expense_plans LEFT JOIN tenant.decisions ON latest_decision_id.

    반환 dict 의 키는 `models.계획요약` 필드와 정확히 같아야 한다.
    org_id 가 None 이면 게스트(org_id IS NULL) 행만 본다 — 🔴 남의 기관 행이 새면 TENANT_LEAK.
    """
    조건, 인자 = _org조건(org_id)
    행 = _질의(f"""
        SELECT p.plan_id, p.제목, p.확정비목, p.금액, d.판정, p.집행예정일,
               p.updated_at, p.사업명, p.상태
        FROM tenant.expense_plans p
        LEFT JOIN tenant.decisions d ON d.decision_id = p.latest_decision_id
        WHERE {조건}
        ORDER BY p.updated_at DESC
    """, 인자)
    return [
        {
            "plan_id": r[0], "제목": r[1], "확정비목": r[2],
            "금액": float(r[3]) if r[3] is not None else None,
            "판정": r[4], "집행예정일": _문자열(r[5]),
            "updated_at": _문자열(r[6]), "사업명": r[7], "상태": r[8],
        }
        for r in 행
    ]


def _실_통계(org_id: str | None) -> dict:
    """같은 JOIN 한 번으로 5개 카운트 + 금액합계. 키는 `models.계획통계` 와 동일."""
    조건, 인자 = _org조건(org_id)
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
    # 🔴 _질의 는 DB 접속 실패 시 빈 리스트를 준다 — «0건» 과 구분해 0으로 채운다.
    if not 행:
        return {"전체": 0, "확인필요": 0, "위험": 0, "특이사항없음": 0, "점검전": 0, "금액합계": 0.0}
    r = 행[0]
    return {
        "전체": r[0], "확인필요": r[1], "위험": r[2],
        "특이사항없음": r[3], "점검전": r[4], "금액합계": float(r[5]),
    }


def _실_생성(body: 계획생성, *, org_id=_주어지지않음) -> 계획상세:
    """INSERT INTO tenant.expense_plans. 질문원문이 없으면 `_합성(body)` 을 쓴다.

    🔴 `org_id` 는 **HTTP 경로에서 반드시 넘겨라** — `_계획_주인()` 이 검증된 주체에서
       뽑은 값이다. 안 넘기면 `body.org_id`(자기신고)로 떨어지는데, 그건 테스트가
       본문으로 org 를 지정하는 관례(`test_plans.py`)를 살려 두기 위한 «직접 호출»
       전용 통로다. 라우터에서 그 통로를 타면 기관 사칭이 열린다.
    """
    # 🔴 **라우터는 반드시 `org_id=` 를 넘긴다.** 안 넘기면 자기신고(본문)로 떨어지고
    #    그게 곧 기관 사칭이다. 이 통로는 테스트의 «직접 호출» 전용이다.
    주인 = body.org_id if org_id is _주어지지않음 else org_id
    제목 = body.제목 or body.품목
    질문원문 = body.질문원문 or _합성(body)
    행 = _질의_저장(
        """
        INSERT INTO tenant.expense_plans
            (org_id, 제목, 질문원문, 정규화, 사업명, 확정비목, 금액, 집행예정일, 거래처, 추가설명)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING plan_id, 제목, 확정비목, 금액, 집행예정일, updated_at, created_at,
                  사업명, 상태, 질문원문, 거래처, 추가설명, 정규화, latest_decision_id
        """,
        (주인, 제목, 질문원문, _jsonb(body.정규화), body.사업명,
         body.확정비목, body.금액, body.집행예정일, body.거래처, body.추가설명),
        주인,
    )
    if not 행:
        # 🔴 `_질의_저장` 이 예외를 다 세워 내보내므로 여기까지 «빈 리스트» 로 오는 길은
        #    없다 (INSERT ... RETURNING 은 성공하면 반드시 1행). 남겨 두되 503 이 아니라
        #    500 이다 — 여기 걸리면 DB 문제가 아니라 우리가 모르는 상태다.
        raise HTTPException(500, "지출계획을 저장하지 못했습니다")
    r = 행[0]
    return 계획상세(
        plan_id=r[0], 제목=r[1], 확정비목=r[2],
        금액=float(r[3]) if r[3] is not None else None,
        판정=None,                              # 방금 만든 계획이라 아직 판정이 없다
        집행예정일=_문자열(r[4]), updated_at=_문자열(r[5]), created_at=_문자열(r[6]),
        사업명=r[7], 상태=r[8], 질문원문=r[9], 용도=body.용도, 거래처=r[10],
        추가설명=r[11], 정규화=r[12] or {}, latest_decision_id=r[13],
        판정상세=None, 할일=[],
    )


def _실_상세(plan_id: int, org_id: str | None) -> 계획상세:
    """계획 + 최신 판정 + plan_tasks 를 한 번에. 없으면 404."""
    조건, org인자 = _org조건(org_id)
    행 = _질의(f"""
        SELECT p.plan_id, p.제목, p.확정비목, p.금액, d.판정, p.집행예정일,
               p.updated_at, p.created_at, p.사업명, p.상태, p.질문원문,
               p.거래처, p.추가설명, p.정규화, p.latest_decision_id,
               d.요약, d.해야할일, d.인용, d.전제, d.신뢰등급, d.버전스탬프,
               d.참조사슬, d.강등사유
        FROM tenant.expense_plans p
        LEFT JOIN tenant.decisions d ON d.decision_id = p.latest_decision_id
        WHERE p.plan_id = %s AND {조건}
    """, (plan_id, *org인자))
    if not 행:
        raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
    r = 행[0]

    판정상세 = None
    if r[14] is not None:          # latest_decision_id 있음 = 판정을 받은 적 있다
        판정상세 = {
            "판정": r[4], "요약": r[15], "해야할일": r[16] or [], "인용": r[17] or [],
            "전제": r[18] or [], "신뢰등급": r[19], "버전스탬프": r[20],
            "참조사슬": r[21] or [], "강등사유": r[22] or [],
        }

    할일행 = _질의(
        """
        SELECT task_id, plan_id, 출처, 코드, 구분, 항목, 설명, due_date,
               "유형", 날짜_사용자수정, 상태
        FROM tenant.plan_tasks WHERE plan_id = %s
        """,
        (plan_id,),
    )
    할일 = [
        {
            "task_id": t[0], "plan_id": t[1], "출처": t[2], "코드": t[3],
            "구분": t[4], "항목": t[5], "설명": t[6], "due_date": _문자열(t[7]),
            "유형": t[8], "날짜_사용자수정": t[9], "상태": t[10], "계획제목": r[1],
        }
        for t in 할일행
    ]

    정규화 = r[13] or {}
    return 계획상세(
        plan_id=r[0], 제목=r[1], 확정비목=r[2],
        금액=float(r[3]) if r[3] is not None else None,
        판정=r[4], 집행예정일=_문자열(r[5]), updated_at=_문자열(r[6]), created_at=_문자열(r[7]),
        사업명=r[8], 상태=r[9], 질문원문=r[10], 용도=정규화.get("용도"),
        거래처=r[11], 추가설명=r[12], 정규화=정규화, latest_decision_id=r[14],
        판정상세=판정상세, 할일=할일,
    )
