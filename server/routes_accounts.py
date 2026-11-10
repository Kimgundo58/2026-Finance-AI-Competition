# -*- coding: utf-8 -*-
"""계정 등록 API — POST /api/accounts.

회원가입은 Supabase 에서 끝나고, 서버는 로그인 토큰의 email 을 tenant.accounts 에서 찾아 기관을 정한다.
이 경로가 그 행을 만든다. 등록 전 계정은 인증 미들웨어에서 403 이므로 이 경로는 미들웨어에서 제외하고
(auth._인증제외_경로) 토큰은 여기서 직접 검증한다. email 은 토큰에서만 읽고, 기관은 공개 slug 로 고른다.
이미 등록된 계정의 기관은 이 경로로 바꾸지 못한다.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from . import auth
from ._common import MOCK, _질의
from .routes_orgs import 데모접두

router = APIRouter(tags=["계정"])

_log = logging.getLogger(__name__)


class 계정등록요청(BaseModel):
    slug: str = Field(min_length=1, max_length=64, description="GET /api/orgs 가 돌려준 기관 식별자")


class 계정등록응답(BaseModel):
    기관명: str = Field(description="계정이 속한 기관. 이미 등록된 계정이면 요청한 기관과 다를 수 있다")
    slug: str
    새계정: bool = Field(description="이번 요청으로 등록했으면 true, 이미 등록돼 있었으면 false")


def _기관찾기(slug: str) -> tuple[str, str] | None:
    """slug -> (org_id, 기관명). slug 는 역산할 수 없으므로 데모 기관을 뺀 전체를 계산해 비교한다."""
    행 = _질의("SELECT org_id, 기관명 FROM tenant.orgs WHERE 기관명 NOT LIKE %s",
               (f"{데모접두}%",), 예외전파=True)
    for org_id, 기관명 in 행:
        # str 끼리 비교하면 ASCII 가 아닌 값에서 TypeError 가 난다
        if hmac.compare_digest(auth.slug(org_id).encode(), slug.encode()):
            return str(org_id), 기관명
    return None


def _기관명(org_id: str) -> str:
    행 = _질의("SELECT 기관명 FROM tenant.orgs WHERE org_id = %s", (org_id,), 예외전파=True)
    return 행[0][0] if 행 else ""


@router.post("/api/accounts", response_model=계정등록응답, status_code=201)
def 계정등록(
    본문: 계정등록요청,
    응답: Response,
    authorization: str | None = Header(default=None),
) -> 계정등록응답:
    """로그인한 사용자를 고른 기관에 등록한다. 이미 등록돼 있으면 200 과 현재 기관을 돌려준다."""
    if MOCK:
        return 계정등록응답(기관명="예시 주관기관", slug=본문.slug, 새계정=True)

    email = auth.가입자_이메일(authorization)
    기존 = auth._계정조회(email)
    try:
        if 기존 is not None:
            응답.status_code = 200
            return 계정등록응답(기관명=_기관명(기존[0]), slug=auth.slug(기존[0]), 새계정=False)

        기관 = _기관찾기(본문.slug)
        if 기관 is None:
            raise HTTPException(404, "선택한 주관기관을 찾을 수 없습니다. 다시 골라 주세요")
        행 = _질의("SELECT org_id FROM tenant.계정등록(%s, %s)", (email, 기관[0]), 예외전파=True)
        if not 행:
            raise HTTPException(404, "선택한 주관기관을 찾을 수 없습니다. 다시 골라 주세요")
        # 동시에 들어온 다른 요청이 먼저 등록했으면 그 기관이 돌아온다
        org_id = str(행[0][0])
        새계정 = org_id == 기관[0]
        if not 새계정:
            응답.status_code = 200
        return 계정등록응답(기관명=_기관명(org_id), slug=auth.slug(org_id), 새계정=새계정)
    except HTTPException:
        raise
    except Exception as e:                                    # noqa: BLE001
        _log.exception("계정 등록 실패 sqlstate=%s", getattr(e, "sqlstate", None))
        # 응답에 예외 문자열을 넣지 않는다. psycopg 오류 본문에 호스트·포트·사용자명이 들어 있다.
        raise HTTPException(503, "계정을 등록하지 못했습니다. 잠시 후 다시 시도해 주세요") from e
