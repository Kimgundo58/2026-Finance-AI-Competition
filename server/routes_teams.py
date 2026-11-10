# -*- coding: utf-8 -*-
"""팀 API — 같은 기관의 계정들이 기업 데이터를 함께 쓴다.

GET    /api/team                 내 팀(없으면 팀 null)
POST   /api/team                 팀 만들기(초대 코드는 이 응답과 재발급 응답에만 있다)
POST   /api/team/invite-code     초대 코드 재발급(대표)
POST   /api/team/join            초대 코드로 들어가기(등록된 계정)
POST   /api/team/leave           나가기
DELETE /api/team/members/{id}    내보내기(대표)
POST   /api/accounts/join        새 가입자가 초대 코드로 등록

구조: 팀 데이터는 대표 계정의 account_id 로 저장된다. 로그인 때 tenant.로그인주체() 가 구성원의 데이터 계정을
대표 계정으로 돌려주므로(auth._supabase해석) 다른 경로와 RLS 는 팀을 모른다. 들어오기·나가기는 데이터를 옮기지 않는다.
팀 조작과 작성자 기록은 auth.주체.본인_account_id 를 쓴다. 팀 표는 앱 롤이 직접 읽지 못해 db/init/21_teams.sql 의 함수만 부른다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth, mock_data
from ._common import MOCK, _질의, 클라이언트_ip
from .models import 초대코드입력, 팀만들기입력, 팀응답, 팀정보, 팀구성원
from .rate_limit import 시도제한
from .routes_accounts import _기관명

router = APIRouter(tags=["팀"])

_log = logging.getLogger(__name__)

_코드_글자 = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # 헷갈리는 0·O·1·I·L 을 뺀다
_코드_길이 = 8
_코드_유효일 = 7
_코드_제한 = 시도제한(최대=10, 초=3600)             # 8자리 코드를 맞혀 보는 시도를 막는다

_코드_안내 = "초대 코드가 맞지 않거나 기간이 지났습니다. 대표에게 새 코드를 받아 주세요."
_결과_오류 = {
    "계정없음": (403, "등록되지 않은 계정입니다."),
    "이미팀있음": (409, "이미 팀에 들어가 있습니다. 나간 뒤 다시 시도해 주세요."),
    "코드오류": (404, _코드_안내),
    "다른기관": (403, "다른 주관기관의 팀에는 들어갈 수 없습니다."),
    "이미등록": (409, "이미 등록된 계정입니다. 마이페이지에서 초대 코드를 입력해 주세요."),
    "권한없음": (403, "대표만 할 수 있습니다."),
    "구성원있음": (409, "다른 구성원이 있어 나갈 수 없습니다. 구성원을 먼저 내보내 주세요."),
    "팀없음": (404, "들어가 있는 팀이 없습니다."),
    "대표": (409, "대표는 내보낼 수 없습니다."),
    "없는구성원": (404, "이 팀의 구성원이 아닙니다."),
}


def _코드_만들기() -> tuple[str, datetime]:
    코드 = "".join(secrets.choice(_코드_글자) for _ in range(_코드_길이))
    return f"{코드[:4]}-{코드[4:]}", datetime.now(timezone.utc) + timedelta(days=_코드_유효일)


def _코드_해시(코드: str) -> str:
    """대소문자와 구분 기호를 무시하고 해시한다. DB 에는 해시만 둔다."""
    return hashlib.sha256(re.sub(r"[^A-Z0-9]", "", 코드.upper()).encode()).hexdigest()


def _결과_확인(결과: str | None) -> None:
    if 결과 == "성공":
        return
    if 결과 in _결과_오류:
        raise HTTPException(*_결과_오류[결과])
    raise HTTPException(503, "처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")


def _함수(sql: str, 인자: tuple) -> list[tuple]:
    try:
        return _질의(sql, 인자, 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        _log.exception("팀 함수 실패 sqlstate=%s", getattr(e, "sqlstate", None))
        raise HTTPException(503, "처리하지 못했습니다. 잠시 후 다시 시도해 주세요.") from e


def _본인(요청: Request) -> str:
    """로그인한 본인 계정. 데모 세션·게스트는 팀을 쓰지 못한다."""
    주 = 요청.scope.get("suddoe_주체")
    if 주 is None or not 주.검증됨 or not 주.본인_account_id:
        raise HTTPException(401, "로그인한 계정만 팀을 쓸 수 있습니다.")
    return str(주.본인_account_id)


def _시도_확인(요청: Request, 키: str) -> None:
    if not _코드_제한.통과(f"{키}|{클라이언트_ip(요청)}"):
        raise HTTPException(429, "초대 코드를 너무 많이 시도했습니다. 잠시 후 다시 시도해 주세요.")


def _팀_읽기(본인: str, 초대코드: str | None = None) -> 팀응답:
    행들 = _함수("SELECT 팀이름, 사업명, 대표_account_id, 초대코드_만료, account_id, email FROM tenant.팀조회(%s::uuid)",
              (본인,))
    if not 행들:
        return 팀응답(팀=None)
    팀이름, 사업명, 대표, 만료, *_ = 행들[0]
    return 팀응답(팀=팀정보(
        팀이름=팀이름, 사업명=사업명, 내역할="대표" if str(대표) == 본인 else "구성원",
        초대코드=초대코드, 초대코드_만료=만료.isoformat() if 만료 else None,
        구성원=[팀구성원(account_id=str(a), 이메일=e, 나=str(a) == 본인, 대표=a == 대표)
             for *_x, a, e in 행들]))


# ── mock ────────────────────────────────────────────────────────────

_mock_나 = "mock-account-me"


def _mock_응답(초대코드: str | None = None) -> 팀응답:
    팀 = mock_data.mock_팀.get("현재")
    if not 팀:
        return 팀응답(팀=None)
    return 팀응답(팀=팀정보(
        팀이름=팀["팀이름"], 사업명=팀["사업명"], 내역할="대표" if 팀["대표"] == _mock_나 else "구성원",
        초대코드=초대코드,
        초대코드_만료=(datetime.now(timezone.utc) + timedelta(days=-1 if 팀.get("코드막힘") else _코드_유효일)).isoformat(),
        구성원=[팀구성원(account_id=k, 이메일=v, 나=k == _mock_나, 대표=k == 팀["대표"])
             for k, v in 팀["구성원"].items()]))


def _mock_팀가입(코드: str) -> None:
    if 코드.upper().startswith("X"):
        raise HTTPException(*_결과_오류["코드오류"])
    mock_data.mock_팀["현재"] = {
        "팀이름": "예시 팀", "사업명": "예비창업패키지", "대표": "mock-account-lead",
        "구성원": {"mock-account-lead": "lead@example.test", _mock_나: "me@example.test"}}


# ── 경로 ────────────────────────────────────────────────────────────

@router.get("/api/team", response_model=팀응답)
def 내팀(요청: Request) -> 팀응답:
    if MOCK:
        return _mock_응답()
    return _팀_읽기(_본인(요청))


@router.post("/api/team", response_model=팀응답, status_code=201)
def 팀_만들기(body: 팀만들기입력, 요청: Request) -> 팀응답:
    코드, 만료 = _코드_만들기()
    if MOCK:
        if mock_data.mock_팀.get("현재"):
            raise HTTPException(*_결과_오류["이미팀있음"])
        mock_data.mock_팀["현재"] = {"팀이름": body.팀이름, "사업명": body.사업명, "대표": _mock_나,
                                  "구성원": {_mock_나: "me@example.test"}}
        return _mock_응답(코드)
    본인 = _본인(요청)
    행 = _함수("SELECT tenant.팀만들기(%s::uuid, %s, %s, %s, %s)", (본인, body.팀이름, body.사업명, _코드_해시(코드), 만료))
    _결과_확인(행[0][0] if 행 else None)
    return _팀_읽기(본인, 코드)


@router.post("/api/team/invite-code", response_model=팀응답)
def 초대코드_재발급(요청: Request) -> 팀응답:
    코드, 만료 = _코드_만들기()
    if MOCK:
        mock_data.mock_팀.get("현재", {}).pop("코드막힘", None)
        return _mock_응답(코드)
    본인 = _본인(요청)
    행 = _함수("SELECT tenant.팀코드재발급(%s::uuid, %s, %s)", (본인, _코드_해시(코드), 만료))
    _결과_확인(행[0][0] if 행 else None)
    return _팀_읽기(본인, 코드)


@router.post("/api/team/join", response_model=팀응답)
def 팀_들어가기(body: 초대코드입력, 요청: Request) -> 팀응답:
    if MOCK:
        _mock_팀가입(body.초대코드)
        return _mock_응답()
    본인 = _본인(요청)
    _시도_확인(요청, 본인)
    행 = _함수("SELECT tenant.팀가입(%s::uuid, %s)", (본인, _코드_해시(body.초대코드)))
    _결과_확인(행[0][0] if 행 else None)
    return _팀_읽기(본인)


@router.post("/api/team/leave", response_model=팀응답)
def 팀_나가기(요청: Request) -> 팀응답:
    if MOCK:
        팀 = mock_data.mock_팀.get("현재")
        if not 팀:
            raise HTTPException(*_결과_오류["팀없음"])
        if 팀["대표"] == _mock_나 and len(팀["구성원"]) > 1:
            raise HTTPException(*_결과_오류["구성원있음"])
        mock_data.mock_팀.pop("현재")
        return 팀응답(팀=None)
    본인 = _본인(요청)
    행 = _함수("SELECT tenant.팀나가기(%s::uuid)", (본인,))
    _결과_확인(행[0][0] if 행 else None)
    return 팀응답(팀=None)


@router.delete("/api/team/members/{account_id}", response_model=팀응답)
def 팀원_내보내기(account_id: str, 요청: Request) -> 팀응답:
    if MOCK:
        현재 = mock_data.mock_팀.get("현재", {})
        현재.get("구성원", {}).pop(account_id, None)
        현재["코드막힘"] = True
        return _mock_응답()
    본인 = _본인(요청)
    try:
        uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(*_결과_오류["없는구성원"]) from None
    행 = _함수("SELECT tenant.팀원내보내기(%s::uuid, %s::uuid)", (본인, account_id))
    _결과_확인(행[0][0] if 행 else None)
    return _팀_읽기(본인)


class 초대등록응답(BaseModel):
    기관명: str
    slug: str
    팀이름: str
    사업명: str = Field(description="이 팀의 사업. 화면은 이 사업으로 판정을 요청한다")


@router.post("/api/accounts/join", response_model=초대등록응답, status_code=201)
def 초대코드로_등록(
    body: 초대코드입력,
    요청: Request,
    authorization: str | None = Header(default=None),
) -> 초대등록응답:
    """온보딩의 「초대 코드가 있어요」. 인증 미들웨어 밖이라 토큰을 직접 검증한다(routes_accounts 와 같다)."""
    if MOCK:
        _mock_팀가입(body.초대코드)
        return 초대등록응답(기관명="예시 주관기관", slug="mock-slug", 팀이름="예시 팀", 사업명="예비창업패키지")
    email = auth.가입자_이메일(authorization)
    _시도_확인(요청, email)
    행 = _함수("SELECT 결과, org_id, 팀이름, 사업명 FROM tenant.팀가입_신규(%s, %s)", (email, _코드_해시(body.초대코드)))
    결과, org_id, 팀이름, 사업명 = 행[0] if 행 else (None, None, None, None)
    _결과_확인(결과)
    return 초대등록응답(기관명=_기관명(str(org_id)), slug=auth.slug(org_id), 팀이름=팀이름, 사업명=사업명)
