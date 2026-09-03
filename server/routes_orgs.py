# -*- coding: utf-8 -*-
"""기관 목록 · 심사위원 데모 진입.   **[인증 계통]**

🔴 **이 파일은 org_id(UUID)를 응답에 싣지 않는다.** 밖으로 나가는 손잡이는 `slug` 뿐이고
   slug→org_id 는 서버 안에서만 푼다 (`auth.slug` — HMAC, 역산 불가).
   테스트가 응답 JSON 을 «문자열로» 훑어 UUID 정규식이 걸리면 실패한다.

   다만 이것을 «격리» 로 읽지 말 것. `auth.py` 모듈 docstring 참조 — org_id 는
   공개 기관명에서 재계산된다(411/413 실측). slug 를 숨기는 건 UUID 를 «우리가 흘리지
   않는다» 는 뜻이지, 남이 못 구한다는 뜻이 아니다. 실제 방어는 `SUDDOE_ORG_PARAM=0`.
"""
from __future__ import annotations

import os
import threading
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import auth
from ._common import _질의, _실행

router = APIRouter(tags=["기관·데모"])

# 🔴 데모 org 표식. 스키마를 못 늘리므로(이 세션 허용 변경은 pw_hash 하나) 기관명 접두어로
#    구분한다. 접두어가 «검색 대상 밖» 이어야 심사위원 org 가 목록에 안 뜬다.
데모접두 = "[데모] "
데모_사업명 = os.environ.get("SUDDOE_DEMO_사업명", "예비창업패키지")
# 보존 24h > 토큰 수명 2h — 정리가 «살아 있는 토큰» 을 앞질러 지우지 못하게 한 것이다
데모_보존초 = int(os.environ.get("SUDDOE_DEMO_보존초", "86400"))
# 🔴 인증 없는 «쓰기» 엔드포인트다. 상한이 없으면 tenant.orgs 를 무한히 부풀리는
#    자원고갈 통로가 된다. 넘치면 «거부» — 심사 동시 인원 상한이라 넉넉하다.
데모_상한 = int(os.environ.get("SUDDOE_DEMO_상한", "200"))


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
# 캐시가 «없어도 정답» 이도록 짰다 — 미스면 DB 를 다시 훑는다 (데모 org 는 런타임에 는다).

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
    """slug → (org_id, 기관명). 못 찾으면 404. 🔴 실패의 기본값은 «거부» 다."""
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
    """가입·기관선택 화면용. 🔴 응답에 org_id 는 없다.

    검색은 «공백을 뗀 뒤» 맞춘다 — 「건국대학교 창업지원본부」와 「건국대학교창업지원본부」가
    같은 질의에 다 걸려야 하기 때문이다 (아래 중복 항목 설명 참조).
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


# ── POST /api/demo/session ──────────────────────────────────────────

@router.post("/api/demo/session", response_model=데모세션응답)
def 데모세션() -> 데모세션응답:
    """심사위원 진입 — 계정 없이. **(b) 클릭마다 임시 org 발급** 을 골랐다.

    (a) 고정 데모 org 1개는 «게스트 공용 버킷(org_id IS NULL) 금지» 를 이름만 바꿔
    되풀이한다. NULL 이 UUID 로 바뀔 뿐 심사위원 A 의 지출계획이 심사위원 B 에게 그대로
    보인다 — 금지된 것이 «NULL» 이 아니라 «공유» 이므로 (a) 는 요구를 만족하지 않는다.
    심사가 동시에 두 명 이상이면 첫 화면부터 남의 계획이 섞여 보인다.

    (b) 의 비용은 정리뿐이고, 그건 TTL 로 접힌다 (아래).
    """
    산 = _질의("SELECT count(*) FROM tenant.orgs WHERE 기관명 LIKE %s", (f"{데모접두}%",))
    if 산 and 산[0][0] >= 데모_상한:
        _낡은데모정리()
        산 = _질의("SELECT count(*) FROM tenant.orgs WHERE 기관명 LIKE %s", (f"{데모접두}%",))
        if 산 and 산[0][0] >= 데모_상한:
            raise HTTPException(429, "데모 세션이 너무 많다 — 잠시 후 다시 시도할 것")

    # org_id 는 uuid4 — 🔴 일반 기관과 달리 이름에서 재계산되지 않아야 한다
    새org = str(uuid.uuid4())
    이름 = f"{데모접두}{새org[:8]}"
    rc = _실행(
        "INSERT INTO tenant.orgs (org_id, 기관명, 사업명) VALUES (%s, %s, %s)",
        (새org, 이름, [데모_사업명]),
    )
    if rc < 0:
        raise HTTPException(503, "데모 세션을 만들 수 없다 (DB)")

    _낡은데모정리()
    토큰, 수명 = auth.데모토큰_발급(새org)
    with _맵잠금:
        _맵[auth.slug(새org)] = (새org, 이름)
    return 데모세션응답(access_token=토큰, expires_in=수명,
                        기관명=이름, slug=auth.slug(새org))


def _낡은데모정리() -> None:
    """TTL 지난 데모 org 를 지운다. 요청마다 «게으르게» 돈다 — 크론이 없다.

    🔴 자식을 «먼저» 지운다. `tenant.decisions.org_id` 는 ON DELETE SET NULL 이라
       org 를 그냥 지우면 그 판정 로그가 org_id IS NULL — 즉 «게스트 버킷» 으로
       떨어진다. 격리하려고 만든 데모가 정리 순간에 공용으로 흘러들어가는 셈이다.
       (expense_plans 는 CASCADE 라 저절로 지워지지만, 순서를 지켜 같이 지운다)
    """
    _실행(
        "DELETE FROM tenant.decisions d USING tenant.orgs o "
        "WHERE d.org_id = o.org_id AND o.기관명 LIKE %s "
        "AND o.created_at < now() - make_interval(secs => %s)",
        (f"{데모접두}%", 데모_보존초),
    )
    _실행(
        "DELETE FROM tenant.expense_plans p USING tenant.orgs o "
        "WHERE p.org_id = o.org_id AND o.기관명 LIKE %s "
        "AND o.created_at < now() - make_interval(secs => %s)",
        (f"{데모접두}%", 데모_보존초),
    )
    _실행(
        "DELETE FROM tenant.orgs WHERE 기관명 LIKE %s "
        "AND created_at < now() - make_interval(secs => %s)",
        (f"{데모접두}%", 데모_보존초),
    )
