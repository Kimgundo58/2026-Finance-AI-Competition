# -*- coding: utf-8 -*-
"""GET/PUT /api/profile — 계정의 기업 프로필(협약 금액·기간). 집행내역·참여인력은 항상 빈 목록이다.

프로필은 계정 단위다. org 는 주관기관이라 같은 org 의 다른 계정(다른 창업기업)과 나눠 쓰지 않는다.
GET 은 토큰으로 검증된 org·계정의 tenant.profile_agreements 를 읽는다(코드 조건 + RLS). ?org_id= 쿼리 값은 믿지 않는다.
프로필이 없거나 게스트면 빈 프로필을 준다. 프로필 없이도 판정은 되고 전제가 늘어날 뿐이다.
PUT 은 협약 네 값(협약시작일·협약종료일·정부지원_현금·자기부담_현금)을 검증된 org·계정의 행에 저장한다.
  - org·계정은 토큰에서만 정한다. 데모 토큰은 계정이 없어 account_id NULL 행을 쓴다. 게스트는 401 이다.
  - 판정(orchestrate._프로필값_읽기, B5)은 계정당 한 행을 읽는다. 표의 UNIQUE 는 (org_id, account_id, 사업명)
    (NULLS NOT DISTINCT)이라 계정 하나에 행이 둘 생길 수 있으므로, 코드가 기존 행(최근 수정 순 첫 행)을 UPDATE
    하고 없을 때만 INSERT 한다. INSERT 의 사업명은 org 마다 정해진 값(_새행_사업명)이라 동시 요청도
    ON CONFLICT 로 한 행에 모인다.
  - 같은 트랜잭션에서 이 계정의 판정 캐시(judge_cache 종류 judge)를 지운다. 캐시키에도 프로필 해시가 들어간다.
  - 집행내역·참여인력은 받기만 하고 저장하지 않는다(판정이 읽는 표가 여러 행이면 한 값으로 정할 수 없다).
  - mock 모드는 저장하지 않고 {"저장": false} 와 받은 값을 돌려준다(프론트가 이 값으로 mock 안내를 띄운다).
PMS 원문·실명·과제번호는 스키마에 필드가 없어 받지 않는다.
응답 키는 한 릴리스 동안 이전 이름으로도 함께 보낸다(_구키_포함).
"""
from __future__ import annotations

import logging
import math
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Body, HTTPException, Request

from server import auth
from server._common import MOCK, _계정조건, _질의, 구키_추가, 트랜잭션
from server.models import 프로필, 협약정보

router = APIRouter()

_log = logging.getLogger(__name__)

_구_프로필키 = {"협약": "f1", "집행내역": "f3", "참여인력": "f4"}  # 구 이름 호환(제거 예정: 다음 릴리스)

# org 에 사업이 하나로 정해지지 않을 때 새 행의 사업명. 판정은 사업명으로 행을 고르지 않는다
_사업명_미지정 = "미지정"

# 금액 상한(1000조원). 이보다 크면 입력 실수로 보고 거부한다
_금액_상한 = 1e15

# 판정(orchestrate._프로필값_읽기)과 같은 행을 고르는 순서
_행_순서 = "ORDER BY updated_at DESC, profile_id LIMIT 1"


def _구키_포함(d: dict) -> dict:
    """프로필 dict 에 이전 키 이름을 덧붙인다."""
    return 구키_추가(d, _구_프로필키)


def _빈_프로필() -> dict:
    return _구키_포함({"협약": 협약정보().model_dump(), "집행내역": [], "참여인력": []})


def _협약_dict(a, b, c, d) -> dict:
    """DB 행 값(정부지원_현금, 자기부담_현금, 협약시작일, 협약종료일)을 응답의 협약 dict 로 바꾼다."""
    return {"정부지원_현금": float(a or 0), "자기부담_현금": float(b or 0),
            "협약시작일": str(c) if c else None,
            "협약종료일": str(d) if d else None}


@router.get("/api/profile")
def profile_get(req: Request) -> dict:
    # 특권 DSN(로컬 postgres 등)에서는 RLS 가 꺼지므로 쿼리 값을 쓰면 다른 기관 협약 정보가 보인다.
    org_id = auth.검증된_org(req)
    if MOCK or not org_id:
        return _빈_프로필()
    조건, 인자 = _계정조건(org_id, "p", auth.검증된_account(req))
    행 = _질의('SELECT "정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일" '
              f'FROM tenant.profile_agreements p WHERE {조건} {_행_순서}', 인자)
    if not 행:
        return _빈_프로필()
    return _구키_포함({"협약": _협약_dict(*행[0]), "집행내역": [], "참여인력": []})


# ── PUT 입력 검증 ───────────────────────────────────────────────────

def _날짜(값: str | None, 이름: str) -> date | None:
    """'YYYY-MM-DD' 를 date 로 바꾼다. None·빈 문자열은 None(미입력). 형식이 틀리면 422."""
    if 값 is None or not str(값).strip():
        return None
    try:
        return date.fromisoformat(str(값).strip())
    except ValueError:
        raise HTTPException(422, f"{이름}은 YYYY-MM-DD 형식의 날짜여야 합니다 (받은 값: {str(값)[:20]})") from None


def _금액(값: float, 이름: str) -> Decimal:
    """금액을 NUMERIC 바인딩용 Decimal 로 바꾼다. 음수·무한대·NaN·상한 초과면 422. 정수 금액은 소수점 없이 저장한다."""
    if not math.isfinite(값) or 값 < 0:
        raise HTTPException(422, f"{이름}은 0 이상의 숫자여야 합니다 (받은 값: {값})")
    if 값 > _금액_상한:
        raise HTTPException(422, f"{이름}이 너무 큽니다 (받은 값: {값:.0f}, 상한 {_금액_상한:.0f}원)")
    d = Decimal(str(값))
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d


def _협약_검증(협약: 협약정보) -> tuple[date | None, date | None, Decimal, Decimal]:
    시작 = _날짜(협약.협약시작일, "협약시작일")
    종료 = _날짜(협약.협약종료일, "협약종료일")
    if 시작 and 종료 and 시작 > 종료:
        raise HTTPException(422, f"협약시작일({시작})이 협약종료일({종료})보다 늦습니다")
    return 시작, 종료, _금액(협약.정부지원_현금, "정부지원_현금"), _금액(협약.자기부담_현금, "자기부담_현금")


def _저장_응답(저장: bool, 사유: str | None, 받은값: dict) -> dict:
    out = {"저장": 저장, "사유": 사유, "받은값": _구키_포함(받은값)}
    return 구키_추가(out, {"사유": "이유"})  # 구 이름 호환(제거 예정: 다음 릴리스)


def _새행_사업명(cur, org_id: str) -> str:
    """새 행의 사업명. 기관의 사업이 하나면 그 이름, 아니면 '미지정'. 같은 org 에는 늘 같은 값이다."""
    r = cur.execute("SELECT 사업명 FROM tenant.orgs WHERE org_id = %s", (org_id,)).fetchone()
    사업들 = [s for s in (r[0] if r else None) or [] if s]
    return 사업들[0] if len(사업들) == 1 else _사업명_미지정


def _협약_저장(org_id: str, account_id: str | None, 시작, 종료, 정부, 자기) -> tuple:
    """한 트랜잭션에서 계정의 협약 행을 UPDATE(있으면) 또는 INSERT 하고 판정 캐시를 지운다. 저장된 값을 돌려준다."""
    조건, 인자 = _계정조건(org_id, "p", account_id)
    with 트랜잭션(org_id, account_id) as cur:
        # FOR UPDATE: 같은 계정의 동시 PUT 이 같은 행을 순서대로 고치게 한다
        r = cur.execute("SELECT profile_id FROM tenant.profile_agreements p "
                        f"WHERE {조건} {_행_순서} FOR UPDATE", 인자).fetchone()
        if r:
            저장됨 = cur.execute(
                'UPDATE tenant.profile_agreements p SET "협약시작일" = %s, "협약종료일" = %s, '
                '"정부지원_현금" = %s, "자기부담_현금" = %s, updated_at = now() '
                f'WHERE p.profile_id = %s AND {조건} '
                'RETURNING "정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일"',
                (시작, 종료, 정부, 자기, r[0], *인자)).fetchone()
        else:
            # 행이 없던 두 요청이 동시에 오면 둘 다 여기로 온다. 사업명이 같아 UNIQUE(org_id, account_id, 사업명)에서 만난다
            저장됨 = cur.execute(
                'INSERT INTO tenant.profile_agreements '
                '(org_id, account_id, "사업명", "협약시작일", "협약종료일", "정부지원_현금", "자기부담_현금") '
                'VALUES (%s, %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (org_id, account_id, "사업명") DO UPDATE SET '
                '"협약시작일" = EXCLUDED."협약시작일", "협약종료일" = EXCLUDED."협약종료일", '
                '"정부지원_현금" = EXCLUDED."정부지원_현금", "자기부담_현금" = EXCLUDED."자기부담_현금", '
                'updated_at = now() '
                'RETURNING "정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일"',
                (org_id, account_id, _새행_사업명(cur, org_id), 시작, 종료, 정부, 자기)).fetchone()
        # 협약값은 판정 프롬프트 B5 에 들어간다. 이 계정의 이전 값으로 만든 판정 캐시가 답하지 않게 지운다
        cur.execute(f"DELETE FROM tenant.judge_cache p WHERE {조건} AND p.종류 = 'judge'", 인자)
    return 저장됨


@router.put("/api/profile")
def profile_put(req: Request, body: 프로필 = Body(...)) -> dict:
    시작, 종료, 정부, 자기 = _협약_검증(body.협약)
    if MOCK:
        return _저장_응답(False, "mock 모드(SUDDOE_MOCK=1)에서는 저장하지 않습니다", body.model_dump())

    # 본문에는 org_id 필드가 없다. 쿼리의 ?org_id= 도 쓰지 않고 토큰의 org·계정만 쓴다
    org_id = auth.검증된_org(req)
    if not org_id:
        raise HTTPException(401, "저장하려면 로그인이 필요합니다")
    account_id = auth.검증된_account(req)
    try:
        저장됨 = _협약_저장(org_id, account_id, 시작, 종료, 정부, 자기)
    except Exception as e:                                    # noqa: BLE001
        상태 = getattr(e, "sqlstate", None)
        # 응답에는 SQLSTATE·DB 메시지를 넣지 않는다(호스트·포트가 포함될 수 있다)
        _log.exception("기업 프로필 저장 실패 — sqlstate=%s org=%s account=%s", 상태, org_id, account_id)
        if 상태 == "42501":              # RLS 정책 또는 권한에 의해 거부됨
            raise HTTPException(403, "이 기관의 프로필을 저장할 권한이 없습니다") from e
        if 상태 is None:                 # 접속 자체가 실패했다
            raise HTTPException(503, "기업 프로필을 저장하지 못했습니다 (DB 연결 실패)") from e
        raise HTTPException(500, "기업 프로필을 저장하지 못했습니다") from e

    사유 = None
    if body.집행내역 or body.참여인력:
        사유 = "협약 정보만 저장했습니다. 집행내역·참여인력은 저장하지 않습니다"
    return _저장_응답(True, 사유, {"협약": _협약_dict(*저장됨), "집행내역": [], "참여인력": []})
