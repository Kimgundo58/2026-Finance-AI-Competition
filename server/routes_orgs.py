# -*- coding: utf-8 -*-
"""기관 목록 · 심사위원 데모 진입.

이 파일은 org_id(UUID)를 응답에 싣지 않는다 — 밖으로 나가는 손잡이는 slug 뿐이고
slug→org_id 는 서버 안에서 HMAC 으로 푼다(역산 불가). slug 를 숨기는 것은 격리
수단이 아니라 UUID 를 노출하지 않는다는 뜻이다 — 실제 방어는 SUDDOE_ORG_PARAM=0.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import auth, mock_data
from ._common import _질의, _실행, org_고정

router = APIRouter(tags=["기관·데모"])

_log = logging.getLogger(__name__)

# 데모 org 표식 — 기관명 접두어로 구분한다(스키마 변경 없이). 검색에서 빠져야 목록에 안 뜬다
데모접두 = "[데모] "
데모_사업명 = (os.environ.get("SUDDOE_DEMO_PROGRAM") or os.environ.get("SUDDOE_DEMO_사업명", "예비창업패키지"))
# 보존 24h > 토큰 수명 2h — 살아있는 토큰을 정리가 앞지르지 않게 한다
데모_보존초 = int((os.environ.get("SUDDOE_DEMO_KEEP_SEC") or os.environ.get("SUDDOE_DEMO_보존초", "86400")))
# 인증 없는 쓰기 엔드포인트라 상한이 없으면 자원고갈 통로가 된다. 넘치면 거부한다
데모_상한 = int((os.environ.get("SUDDOE_DEMO_LIMIT") or os.environ.get("SUDDOE_DEMO_상한", "200")))
# 한 요청에서 지우는 최대 건수. 요청 경로에서 도는 청소라 상한이 필요하다
_정리_한도 = int((os.environ.get("SUDDOE_DEMO_PURGE_LIMIT") or os.environ.get("SUDDOE_DEMO_정리한도", "50")))


# ── 응답 모델 ───────────────────────────────────────────────────────

class 기관항(BaseModel):
    slug: str = Field(description="공개 손잡이. 🔴 org_id 가 아니다 — 역산 불가")
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


# ── slug ↔ org_id ───────────────────────────────────────────────────
#
# HMAC 은 한 방향이라 되돌리려면 후보를 훑어야 한다. 413행이라 훑어도 싸다.
# 캐시가 없어도 정답이도록 짰다 — 미스면 DB 를 다시 훑는다(데모 org 는 런타임에 는다).

_맵잠금 = threading.Lock()
_맵: dict[str, tuple[str, str]] = {}          # slug → (org_id, 기관명)


def _맵갱신() -> dict[str, tuple[str, str]]:
    행 = _질의("SELECT org_id, 기관명 FROM tenant.orgs")
    새 = {auth.slug(o): (str(o), n) for o, n in 행}
    with _맵잠금:
        _맵.clear()
        _맵.update(새)
    return 새


def org풀기(slug: str) -> tuple[str, str]:
    """slug → (org_id, 기관명)을 찾는다. 못 찾으면 404 — 실패의 기본값은 거부다."""
    with _맵잠금:
        찾 = _맵.get(slug)
    if 찾 is None:
        찾 = _맵갱신().get(slug)
    if 찾 is None:
        raise HTTPException(404, "그런 기관이 없다")
    return 찾


# ── GET /api/orgs ───────────────────────────────────────────────────

@router.get("/api/orgs", response_model=기관목록응답)
def 기관목록(
    q: str | None = Query(None, description="기관명 부분일치. 공백은 무시하고 맞춘다"),
    사업명: str | None = Query(None, description="이 사업을 하는 기관만"),
    페이지: int = Query(1, ge=1),
    크기: int = Query(50, ge=1, le=200),
) -> 기관목록응답:
    """가입·기관선택 화면용 기관 목록. 응답에 org_id 는 없다.

    검색은 공백을 뗀 뒤 맞춘다 — 「건국대학교 창업지원본부」와 「건국대학교창업지원본부」가
    같은 질의에 다 걸리게 하기 위해서다.
    """
    # 데모 org 는 목록에서 뺀다 — 심사위원용 임시 기관이 가입 화면에 뜨면 안 된다
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


# ── 데모 org 생성 ───────────────────────────────────────────────────
#
# tenant.orgs 의 RLS 정책이 org_id = current_org() 라 org 생성 INSERT 도 GUC 를
# 먼저 세워야 통과한다. 앱이 uuid 를 먼저 뽑아 그 값으로 app.org_id 를 세우고
# 같은 uuid 로 INSERT 한다 — GUC 와 INSERT 값이 다르면 42501 로 막힌다.

def _샘플계획() -> list[tuple]:
    """홈이 비지 않게 넣는 샘플 계획 5건.

    mock_data.목_계획 이 유일한 출처다 — 문안을 여기서 새로 짓지 않는다.
    판정은 안 붙인다(상태='draft').
    """
    return [(p["제목"], p["질문원문"], p["사업명"], p["확정비목"], p["금액"])
            for p in mock_data.목_계획]


def _데모사업명들() -> list[str]:
    """데모 org 의 사업명 배열 — mock_data.목_계획 샘플 5건에서 뽑는다(손으로 적지 않는다).

    여러 사업이 섞인 org 형태가 실제와 같고, 온보딩의 사업 선택 화면도 살려 둔다.
    판정 정확도와는 무관한, 시연 화면 구성을 위한 결정이다.
    """
    본 = [데모_사업명] if 데모_사업명 else []
    for _, _, 사업명, _, _ in _샘플계획():
        if 사업명 and 사업명 not in 본:
            본.append(사업명)
    return 본


def _데모org_생성(새org: str, 이름: str) -> None:
    """org 1건 + 샘플 계획 5건을 한 트랜잭션으로 넣는다.

    둘로 나누면 트랜잭션이 갈려 「org 는 있는데 계획은 없는」 상태가 남을 수 있다.
    `_실행()` 대신 `_질의(..., 예외전파=True)` 를 써서 실패 사유를 구분한다.
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
        # GUC 를 먼저 세운다 — 같은 트랜잭션 안에서, INSERT 하는 값과 같아야 한다
        with org_고정(새org):
            만든 = _질의(sql, 인자, 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        상태 = getattr(e, "sqlstate", None)
        _log.exception("데모 org 생성 실패 — sqlstate=%s org=%s", 상태, 새org)
        # 사유에 DB 메시지를 안 싣는다 — 인증 전 엔드포인트라 아무나 받아 본다
        if 상태 == "42501":
            raise HTTPException(
                500, "데모 세션을 만들 수 없습니다 — 서버 설정 문제입니다") from e
        if 상태 is None:
            raise HTTPException(503, "데모 세션을 만들 수 없다 (DB)") from e
        raise HTTPException(500, "데모 세션을 만들 수 없습니다") from e

    # 예외가 없어도 통과시키지 않는다 — abort 된 트랜잭션의 COMMIT 은 조용히 롤백된다.
    # RETURNING 개수를 세어 실제로 들어갔는지 확인한다
    if len(만든) != len(행들):
        _log.error("데모 샘플 계획이 %d/%d 건만 들어갔다 — org=%s",
                   len(만든), len(행들), 새org)
        raise HTTPException(500, "데모 세션을 만들 수 없습니다")


# ── POST /api/demo/session ──────────────────────────────────────────

@router.post("/api/demo/session", response_model=데모세션응답)
def 데모세션() -> 데모세션응답:
    """심사위원 진입용 — 계정 없이 요청마다 임시 org 를 새로 발급한다.

    고정 데모 org 1개를 쓰면 심사위원끼리 지출계획이 서로 보이게 된다 — 그래서
    클릭마다 org 를 새로 만들고 정리는 TTL 로 접는다.
    """
    산 = _질의("SELECT count(*) FROM tenant.orgs WHERE 기관명 LIKE %s", (f"{데모접두}%",))
    if 산 and 산[0][0] >= 데모_상한:
        _낡은데모정리()
        산 = _질의("SELECT count(*) FROM tenant.orgs WHERE 기관명 LIKE %s", (f"{데모접두}%",))
        if 산 and 산[0][0] >= 데모_상한:
            raise HTTPException(429, "데모 세션이 너무 많다 — 잠시 후 다시 시도할 것")

    # org_id 는 uuid4 — 일반 기관과 달리 이름에서 재계산되지 않는다
    새org = str(uuid.uuid4())
    이름 = f"{데모접두}{새org[:8]}"
    _데모org_생성(새org, 이름)

    _낡은데모정리()
    토큰, 수명 = auth.데모토큰_발급(새org)
    with _맵잠금:
        _맵[auth.slug(새org)] = (새org, 이름)
    return 데모세션응답(access_token=토큰, expires_in=수명,
                        기관명=이름, slug=auth.slug(새org))


def _낡은데모정리() -> None:
    """TTL 지난 데모 org 를 지운다. 요청마다 게으르게 돈다(별도 크론 없음).

    자식(decisions·expense_plans)을 먼저 지운다 — 순서를 안 지키면 decisions.org_id 가
    SET NULL 로 게스트 버킷에 떨어진다. org 하나씩 GUC 를 세워 지운다 — LIKE 로
    한 방에 지우면 RLS 때문에 비특권 롤에서 조용히 0행이 된다. 한 번에 `_정리_한도` 건까지만.
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
            # decisions 를 먼저 지운다 — SET NULL 때문이다
            _실행("DELETE FROM tenant.decisions WHERE org_id = %s", (org,))
            _실행("DELETE FROM tenant.expense_plans WHERE org_id = %s", (org,))
            rc = _실행("DELETE FROM tenant.orgs WHERE org_id = %s", (org,))
        if rc > 0:
            지움 += 1
    if 지움 != len(낡은):
        # 삭제 건수가 모자라면 경고한다 — 방치되면 상한이 차서 데모가 막힌다
        _log.warning("낡은 데모 정리가 %d/%d 건만 지웠다 — RLS·권한을 의심하라",
                     지움, len(낡은))
