# -*- coding: utf-8 -*-
"""기관 목록과 데모 세션 API — GET /api/orgs, POST /api/demo/session.

둘 다 인증 없이 호출되는 경로다(auth._보호제외). 응답에 org_id(UUID)를 넣지 않고
HMAC 기반 공개 식별자(slug, auth.slug)만 내보낸다. slug 는 org_id 로 되돌릴 수 없다.
slug 는 UUID 를 노출하지 않기 위한 것이고 테넌트 격리 수단은 아니다(격리는 RLS 와
SUDDOE_ORG_PARAM=0). 데모 세션은 요청마다 임시 기관을 만들고, 오래된 데모 기관은 요청 처리
중에 정리한다. 환경변수: SUDDOE_DEMO_PROGRAM, SUDDOE_DEMO_LIMIT, SUDDOE_DEMO_KEEP_SEC,
SUDDOE_DEMO_PURGE_LIMIT.
"""
from __future__ import annotations

import logging
import os
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import auth, mock_data
from ._common import _int환경, _질의, _실행, org_고정

router = APIRouter(tags=["기관·데모"])

_log = logging.getLogger(__name__)

# 데모 기관 표식. 스키마를 바꾸지 않고 기관명 접두어로 구분하며, 기관 목록에서는 뺀다
데모접두 = "[데모] "
데모_사업명 = os.environ.get("SUDDOE_DEMO_PROGRAM") or "예비창업패키지"
# 보존 기간(기본 24h)은 데모 토큰 수명(기본 2h)보다 길어야 한다. 짧으면 유효한 토큰의 기관이 먼저 지워진다
데모_보존초 = _int환경("SUDDOE_DEMO_KEEP_SEC", 86400)
# 인증 없는 쓰기 경로라 상한이 없으면 자원 고갈에 노출된다. 넘으면 429 로 거부한다
데모_상한 = _int환경("SUDDOE_DEMO_LIMIT", 200)
# 정리가 요청 처리 중에 실행되므로 한 번에 지우는 기관 수를 제한한다
_정리_한도 = _int환경("SUDDOE_DEMO_PURGE_LIMIT", 50)


# ── 응답 모델 ───────────────────────────────────────────────────────

class 기관항(BaseModel):
    slug: str = Field(description="HMAC 기반 공개 식별자. org_id 가 아니며 org_id 로 되돌릴 수 없다")
    기관명: str
    사업명: list[str]


class 기관목록응답(BaseModel):
    총건수: int
    페이지: int
    크기: int
    항목: list[기관항]


class 데모세션응답(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    기관명: str
    slug: str


# ── GET /api/orgs ───────────────────────────────────────────────────

@router.get("/api/orgs", response_model=기관목록응답)
def 기관목록(
    q: str | None = Query(None, description="기관명 부분일치. 공백은 무시하고 맞춘다"),
    사업명: str | None = Query(None, description="이 사업을 하는 기관만"),
    페이지: int = Query(1, ge=1),
    크기: int = Query(50, ge=1, le=200),
) -> 기관목록응답:
    """가입·기관 선택 화면용 기관 목록. 응답에 org_id 는 없다.

    검색은 공백을 지운 뒤 비교한다. '건국대학교 창업지원본부'와 '건국대학교창업지원본부'가
    같은 질의로 검색되게 하기 위해서다.
    """
    # 데모 기관(체험용 임시 기관)은 가입 화면에 나오면 안 되므로 목록에서 뺀다
    조건 = ["기관명 NOT LIKE %s"]
    인자: list = [f"{데모접두}%"]
    if q:
        조건.append("replace(기관명, ' ', '') ILIKE %s")
        인자.append(f"%{q.replace(' ', '')}%")
    if 사업명:
        조건.append("%s = ANY(사업명)")
        인자.append(사업명)
    where = " AND ".join(조건)

    총 = _질의(f"SELECT count(*) FROM tenant.orgs WHERE {where}", tuple(인자))
    총건수 = 총[0][0] if 총 else 0

    행 = _질의(
        f"SELECT org_id, 기관명, 사업명 FROM tenant.orgs WHERE {where} "
        f"ORDER BY 기관명, org_id LIMIT %s OFFSET %s",
        tuple(인자) + (크기, (페이지 - 1) * 크기),
    )
    return 기관목록응답(
        총건수=총건수, 페이지=페이지, 크기=크기,
        항목=[기관항(slug=auth.slug(o), 기관명=n, 사업명=list(p or [])) for o, n, p in 행],
    )


# ── 데모 기관 생성 ──────────────────────────────────────────────────
#
# tenant.orgs 의 RLS 정책이 org_id = current_org() 라서 기관 생성 INSERT 도 GUC 를
# 먼저 설정해야 통과한다. 앱이 uuid 를 먼저 만들어 그 값으로 app.org_id 를 설정하고
# 같은 uuid 로 INSERT 한다. GUC 와 INSERT 값이 다르면 42501 로 거부된다.

def _샘플계획() -> list[tuple]:
    """홈 화면이 비지 않도록 넣는 샘플 계획. 출처는 mock_data.목_계획 하나뿐이다.

    판정은 붙이지 않는다(상태='draft').
    """
    return [(p["제목"], p["질문원문"], p["사업명"], p["확정비목"], p["금액"])
            for p in mock_data.목_계획]


def _데모사업명들() -> list[str]:
    """데모 기관의 사업명 배열. SUDDOE_DEMO_PROGRAM 과 샘플 계획의 사업명을 중복 없이 모은다.

    실제 기관처럼 여러 사업이 섞인 형태로 만들어 온보딩의 사업 선택 화면도 보이게 한다.
    판정과는 무관한 화면 구성용이다.
    """
    사업명들 = [데모_사업명] if 데모_사업명 else []
    for _, _, 사업명, _, _ in _샘플계획():
        if 사업명 and 사업명 not in 사업명들:
            사업명들.append(사업명)
    return 사업명들


def _데모org_생성(새org: str, 이름: str) -> None:
    """기관 1건과 샘플 계획을 한 문장(한 트랜잭션)으로 넣는다.

    둘로 나누면 기관만 있고 계획은 없는 상태가 남을 수 있다. 실패 사유를 구분하려고
    `_실행()` 대신 `_질의(..., 예외전파=True)` 를 쓴다.
    """
    행들 = _샘플계획()
    자리 = ", ".join(["(%s, %s, %s, %s, %s)"] * len(행들))
    sql = f"""
        WITH 새기관 AS (
            INSERT INTO tenant.orgs (org_id, 기관명, 사업명)
            VALUES (%s, %s, %s)
            RETURNING org_id
        )
        INSERT INTO tenant.expense_plans
            (org_id, 제목, 질문원문, 사업명, 확정비목, 금액, 상태, latest_decision_id)
        SELECT (SELECT org_id FROM 새기관), v.제목, v.질문원문, v.사업명,
               v.확정비목, v.금액::numeric, 'draft', NULL
          FROM (VALUES {자리}) AS v(제목, 질문원문, 사업명, 확정비목, 금액)
        RETURNING plan_id
    """
    인자: tuple = (새org, 이름, _데모사업명들()) + tuple(x for 행 in 행들 for x in 행)
    try:
        # 같은 트랜잭션에서 INSERT 하는 org_id 와 같은 값으로 GUC 를 먼저 설정한다
        with org_고정(새org):
            만든 = _질의(sql, 인자, 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        상태 = getattr(e, "sqlstate", None)
        _log.exception("데모 org 생성 실패 — sqlstate=%s org=%s", 상태, 새org)
        # 인증 없는 경로라 응답에 DB 메시지를 넣지 않는다
        if 상태 == "42501":
            raise HTTPException(
                500, "데모 세션을 만들 수 없습니다 — 서버 설정 문제입니다") from e
        if 상태 is None:
            raise HTTPException(503, "데모 세션을 만들 수 없습니다 (DB 연결 실패)") from e
        raise HTTPException(500, "데모 세션을 만들 수 없습니다") from e

    # abort 된 트랜잭션의 COMMIT 은 오류 없이 롤백되므로, 예외가 없어도
    # RETURNING 개수로 실제 저장 여부를 확인한다
    if len(만든) != len(행들):
        _log.error("데모 샘플 계획이 %d/%d 건만 들어갔다 — org=%s",
                   len(만든), len(행들), 새org)
        raise HTTPException(500, "데모 세션을 만들 수 없습니다")


# ── POST /api/demo/session ──────────────────────────────────────────

@router.post("/api/demo/session", response_model=데모세션응답)
def 데모세션() -> 데모세션응답:
    """데모 세션(체험용 임시 기관). 계정 없이 요청마다 임시 기관을 새로 만들고 데모 토큰을 발급한다.

    고정 데모 기관 하나를 쓰면 여러 사용자의 지출계획이 서로 보인다. 그래서 요청마다 기관을
    새로 만들고, 보존 기간이 지난 기관은 `_낡은데모정리()` 가 지운다.
    """
    산 = _질의("SELECT count(*) FROM tenant.orgs WHERE 기관명 LIKE %s", (f"{데모접두}%",))
    if 산 and 산[0][0] >= 데모_상한:
        _낡은데모정리()
        산 = _질의("SELECT count(*) FROM tenant.orgs WHERE 기관명 LIKE %s", (f"{데모접두}%",))
        if 산 and 산[0][0] >= 데모_상한:
            raise HTTPException(429, "데모 세션이 너무 많습니다. 잠시 후 다시 시도해 주세요.")

    # 일반 기관과 달리 데모 기관의 org_id 는 이름에서 계산하지 않고 uuid4 로 만든다
    새org = str(uuid.uuid4())
    이름 = f"{데모접두}{새org[:8]}"
    _데모org_생성(새org, 이름)

    _낡은데모정리()
    토큰, 수명 = auth.데모토큰_발급(새org)
    return 데모세션응답(access_token=토큰, expires_in=수명,
                        기관명=이름, slug=auth.slug(새org))


def _낡은데모정리() -> None:
    """보존 기간이 지난 데모 기관을 지운다. 별도 크론 없이 요청 처리 중에 지연 정리한다.

    자식 행(decisions, expense_plans)을 먼저 지운다. 순서를 지키지 않으면 decisions.org_id 가
    SET NULL 이 되어 게스트 행으로 남는다. 기관마다 GUC 를 설정해 지운다. LIKE 로 한 번에
    지우면 비특권 롤에서는 RLS 정책 때문에 경고 없이 0행이 된다. 한 번에 `_정리_한도` 건까지만.
    """
    낡은 = _질의(
        "SELECT org_id FROM tenant.orgs WHERE 기관명 LIKE %s "
        "AND created_at < now() - make_interval(secs => %s) LIMIT %s",
        (f"{데모접두}%", 데모_보존초, _정리_한도),
    )
    if not 낡은:
        return
    지움 = 0
    for (org,) in 낡은:
        org = str(org)
        with org_고정(org):
            # decisions.org_id 가 SET NULL 되지 않도록 decisions 를 먼저 지운다
            _실행("DELETE FROM tenant.decisions WHERE org_id = %s", (org,))
            _실행("DELETE FROM tenant.expense_plans WHERE org_id = %s", (org,))
            rc = _실행("DELETE FROM tenant.orgs WHERE org_id = %s", (org,))
        if rc > 0:
            지움 += 1
    if 지움 != len(낡은):
        # 정리가 계속 실패하면 상한이 차서 데모 세션을 만들 수 없게 된다
        _log.warning("낡은 데모 정리가 %d/%d 건만 지웠다 — RLS 정책과 권한을 확인할 것",
                     지움, len(낡은))
