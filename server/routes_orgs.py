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

# 🔴 데모 org 표식. 스키마를 못 늘리므로(이 세션 허용 변경은 pw_hash 하나) 기관명 접두어로
#    구분한다. 접두어가 «검색 대상 밖» 이어야 심사위원 org 가 목록에 안 뜬다.
데모접두 = "[데모] "
데모_사업명 = os.environ.get("SUDDOE_DEMO_사업명", "예비창업패키지")
# 보존 24h > 토큰 수명 2h — 정리가 «살아 있는 토큰» 을 앞질러 지우지 못하게 한 것이다
데모_보존초 = int(os.environ.get("SUDDOE_DEMO_보존초", "86400"))
# 🔴 인증 없는 «쓰기» 엔드포인트다. 상한이 없으면 tenant.orgs 를 무한히 부풀리는
#    자원고갈 통로가 된다. 넘치면 «거부» — 심사 동시 인원 상한이라 넉넉하다.
데모_상한 = int(os.environ.get("SUDDOE_DEMO_상한", "200"))
# 한 요청에서 지우는 최대 건수. 요청 경로에서 도는 청소라 상한이 필요하다
_정리_한도 = int(os.environ.get("SUDDOE_DEMO_정리한도", "50"))


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


# ── 데모 org 생성 ───────────────────────────────────────────────────
#
# 🔴 **닭-달걀.** `tenant.orgs` 정책이 `org_id = tenant.current_org()` 라, org 를
#    «만드는» 요청인데 org 가 이미 있어야 통과한다. 비특권 롤(`suddoe_app`)에서
#    실측하면 GUC 없이 INSERT 는 42501 이다. 실서버가 503 을 내던 이유가 이거다.
#
# 처방: **앱이 uuid 를 먼저 뽑아 → 그 값으로 `app.org_id` 를 세우고 → 같은 uuid 로
# INSERT** 한다. 정책 DDL 이 필요 없고, 정책 문구 그대로 통과한다. 실측:
#
#        GUC = INSERT 값 (같은 uuid)      ✅ 통과
#        GUC ≠ INSERT 값 (사칭)           🔴 42501
#        GUC 없음 / DEFAULT gen_random    🔴 42501
#
# 🔴 **`새org` 하나를 GUC 와 INSERT 양쪽에 쓴다.** 둘을 따로 받는 순간 그게 곧
#    기관 사칭 통로다 — 이 처방이 «스스로 안전한» 이유가 두 값이 같다는 것뿐이다.

def _샘플계획() -> list[tuple]:
    """홈이 비지 않게 넣는 5건. 🔴 **문안을 여기서 지어내지 않는다.**

    `mock_data.목_계획` 이 유일한 출처다 — 프론트가 그 화면으로 이미 개발했고,
    `scripts/seed_demo.py` 도 같은 곳에서 읽는다. 두 군데에 따로 적으면 갈린다.
    판정은 안 붙인다(`상태='draft'` · `latest_decision_id` NULL) — 심사위원이 직접
    눌러 보는 게 시연 동선이라, 이미 판정된 화면을 주면 그 동선이 사라진다.
    """
    return [(p["제목"], p["질문원문"], p["사업명"], p["확정비목"], p["금액"])
            for p in mock_data.목_계획]


def _데모org_생성(새org: str, 이름: str) -> None:
    """org 1건 + 샘플 계획 5건을 **한 문장(=한 트랜잭션)** 으로 넣는다.

    🔴 왜 한 문장인가. 둘로 나누면 `_실행`/`_질의` 가 호출마다 새 커넥션을 열어
       **트랜잭션이 갈린다.** 계획 INSERT 가 실패하면 「org 는 있는데 홈은 빈」
       상태가 남고, 그건 지금 증상과 똑같아서 원인을 또 못 찾는다.

    🔴 `_실행()` 을 안 쓴다. 그건 실패를 `-1` 로 삼켜서 「DB 다운」과 「RLS 차단」이
       **같은 503** 이 된다 — 오늘 `routes_plans` 에서 고친 것과 같은 모양이다.
       `_질의(..., 예외전파=True)` 로 받아 사유별로 가른다.
       (`_질의` 는 `with conn:` 로 나가면서 커밋한다 — RETURNING 이 있어 쓰기에도 쓴다)
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
    인자: tuple = (새org, 이름, [데모_사업명]) + tuple(x for 행 in 행들 for x in 행)
    try:
        # 🔴 GUC 를 «먼저» 세운다. 같은 트랜잭션이어야 하고, 값은 INSERT 하는 것과 같다
        with org_고정(새org):
            만든 = _질의(sql, 인자, 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        상태 = getattr(e, "sqlstate", None)
        _log.exception("데모 org 생성 실패 — sqlstate=%s org=%s", 상태, 새org)
        # 🔴 사유에 DB 메시지를 안 싣는다. 인증 «전» 엔드포인트라 아무나 받아 본다.
        if 상태 == "42501":
            raise HTTPException(
                500, "데모 세션을 만들 수 없습니다 — 서버 설정 문제입니다") from e
        if 상태 is None:
            raise HTTPException(503, "데모 세션을 만들 수 없다 (DB)") from e
        raise HTTPException(500, "데모 세션을 만들 수 없습니다") from e

    # 🔴 「예외가 안 났다」로 통과시키지 않는다. abort 된 트랜잭션의 COMMIT 은 조용히
    #    ROLLBACK 되고 예외를 안 던진다(`tests/test_교차점_비특권_판정저장.py` 실측).
    #    RETURNING 개수를 세는 게 「정말 들어갔는가」에 제일 가까운 신호다.
    if len(만든) != len(행들):
        _log.error("데모 샘플 계획이 %d/%d 건만 들어갔다 — org=%s",
                   len(만든), len(행들), 새org)
        raise HTTPException(500, "데모 세션을 만들 수 없습니다")


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
    _데모org_생성(새org, 이름)

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

    🔴 **org 하나씩 돈다.** 전에는 `LIKE '[데모] %'` 로 한 방에 지웠는데, RLS 정책이
       `org_id = current_org()` 라 **비특권 롤에서는 0행이 지워진다 — 조용히.**
       예외도 안 나고 `_실행` 은 rowcount 0 을 정상으로 돌려준다. 실측:

           TTL 지난 데모 org 2건 · 정리 호출 → 정리 후 **2건** (0건 삭제)

       그대로 두면 `데모_상한`(200)이 차고 그 뒤로 **모든 데모 세션이 429** 다.
       GUC 는 한 번에 org 하나만 가리키므로, 지울 org 를 «먼저 읽고» 한 건씩
       `org_고정()` 안에서 지운다 — `_데모org_생성` 과 같은 처방이고 DDL 이 없다.
       (`tenant.orgs` 에는 `orgs_read_all (SELECT true)` 이 있어 목록 읽기는 통과한다)

    🔴 한 번에 `_정리_한도` 건까지만 돈다. 요청 경로에서 도는 청소라 상한이 없으면
       밀린 날 첫 요청 하나가 수천 건을 지우며 서 있는다.
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
            # 🔴 decisions 를 먼저 — 위 docstring 의 SET NULL 때문이다
            _실행("DELETE FROM tenant.decisions WHERE org_id = %s", (org,))
            _실행("DELETE FROM tenant.expense_plans WHERE org_id = %s", (org,))
            rc = _실행("DELETE FROM tenant.orgs WHERE org_id = %s", (org,))
        if rc > 0:
            지움 += 1
    if 지움 != len(낡은):
        # 🔴 조용히 넘어가지 않는다. 이게 0 이면 상한이 차서 데모가 통째로 막힌다
        _log.warning("낡은 데모 정리가 %d/%d 건만 지웠다 — RLS·권한을 의심하라",
                     지움, len(낡은))
