# -*- coding: utf-8 -*-
"""인증·테넌트 귀속 — 요청이 어느 기관 것인지 서버가 정한다.

    Authorization 헤더 있음  → 토큰만 본다. 위조·만료·미매칭이면 거부. 파라미터로 안 흐른다
    Authorization 헤더 없음  → (폴백 켜짐) ?org_id= 자기신고 · (꺼짐) 게스트(None)

토큰은 `iss` 로 두 갈래로 가른다.

    Supabase  RS256/ES256 · JWKS(SUDDOE_JWKS_URL) 검증 · email 클레임 → tenant.accounts
    데모      HS256       · 서버 자체 서명(SUDDOE_DEMO_SECRET) · org 클레임 직결

갈래마다 허용 알고리즘을 고정한다 — 안 고정하면 공개키를 HMAC 비밀키로 쓰는
alg confusion 이 성립한다. 계정 조인 열쇠는 `tenant.accounts.email` 이다.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass

from fastapi import Header, HTTPException, Query

from . import _common
from ._common import _질의

로그 = logging.getLogger("suddoe.auth")

# ── 환경 ────────────────────────────────────────────────────────────

JWKS_URL = os.environ.get("SUDDOE_JWKS_URL", "").strip()
JWKS_TTL = int(os.environ.get("SUDDOE_JWKS_TTL", "600"))          # 초
SUPABASE_AUD = os.environ.get("SUDDOE_JWT_AUD", "authenticated")

DEMO_ISS = "suddoe-demo"
DEMO_TTL = int(os.environ.get("SUDDOE_DEMO_TTL", "7200"))         # 초

# ?org_id= 자기신고 폴백 스위치. 기본은 켜짐, `SUDDOE_ORG_PARAM=0` 으로 끈다.
ORG_PARAM_허용 = os.environ.get("SUDDOE_ORG_PARAM", "1") != "0"

_개발기본비밀 = "suddoe-dev-only-not-a-secret"


def _비밀(이름: str) -> str:
    """환경변수의 비밀값. 미설정이면 경고 로그를 남기고 개발 기본값을 쓴다."""
    v = os.environ.get(이름, "").strip()
    if v:
        return v
    로그.warning("🔴 %s 미설정 — 개발 기본 비밀을 쓴다. 배포 전에 반드시 설정할 것", 이름)
    return f"{_개발기본비밀}:{이름}"


# ── 주체 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class 주체:
    """이 요청이 누구인가. `출처` 로 「검증됐다」와 「자기신고를 믿었다」를 가른다."""
    org_id: str | None
    출처: str                    # 'token' · 'demo' · 'param' · 'none'
    email: str | None = None
    account_id: str | None = None

    @property
    def 검증됨(self) -> bool:
        return self.출처 in ("token", "demo")


# ── JWKS 캐시 ───────────────────────────────────────────────────────

_jwks_잠금 = threading.Lock()
_jwks_캐시: tuple[float, dict] | None = None


def _jwks(강제갱신: bool = False) -> dict:
    """JWKS 를 TTL 캐시로 든다. 실패하면 예외 — 빈 dict 를 주면 검증이 통과한다."""
    global _jwks_캐시
    with _jwks_잠금:
        if not 강제갱신 and _jwks_캐시 and time.time() - _jwks_캐시[0] < JWKS_TTL:
            return _jwks_캐시[1]
    if not JWKS_URL:
        raise HTTPException(503, "SUDDOE_JWKS_URL 미설정 — Supabase 토큰을 검증할 수 없다")
    import httpx
    본문 = httpx.get(JWKS_URL, timeout=5.0).raise_for_status().json()
    with _jwks_잠금:
        _jwks_캐시 = (time.time(), 본문)
    return 본문


def _서명키(토큰: str):
    """kid 로 JWKS 에서 공개키를 고른다. 못 찾으면 한 번만 갱신하고 다시 본다(키 회전)."""
    import jwt
    kid = jwt.get_unverified_header(토큰).get("kid")
    for 강제 in (False, True):
        for k in _jwks(강제).get("keys", []):
            if k.get("kid") == kid:
                return jwt.PyJWK(k).key
    raise HTTPException(401, "알 수 없는 서명 키")


# ── 토큰 해석 ───────────────────────────────────────────────────────

def _pyjwt():
    """pyjwt 지연 import. 미설치면 통과가 아니라 503 거부다."""
    try:
        import jwt
        return jwt
    except ModuleNotFoundError:
        raise HTTPException(503, "pyjwt 미설치 — 토큰을 검증할 수 없다 (pip install 'pyjwt[crypto]')")


def 데모토큰_발급(org_id: str) -> tuple[str, int]:
    """서버 자체 서명 단기 토큰. Supabase 를 안 거친다. (토큰, 수명초) 를 돌려준다."""
    jwt = _pyjwt()
    만료 = int(time.time()) + DEMO_TTL
    본문 = {"iss": DEMO_ISS, "sub": f"demo:{org_id}", "org": org_id,
            "iat": int(time.time()), "exp": 만료}
    return jwt.encode(본문, _비밀("SUDDOE_DEMO_SECRET"), algorithm="HS256"), DEMO_TTL


def _데모해석(토큰: str) -> 주체:
    jwt = _pyjwt()
    try:
        본문 = jwt.decode(토큰, _비밀("SUDDOE_DEMO_SECRET"),
                          algorithms=["HS256"],          # HS256 만
                          issuer=DEMO_ISS,
                          options={"require": ["exp", "iss", "org"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "데모 세션이 만료됐다 — 다시 시작할 것")
    except jwt.PyJWTError as e:      # 최상위를 잡는다 — InvalidKeyError 는 InvalidTokenError 하위가 아니다
        raise HTTPException(401, f"데모 토큰이 유효하지 않다: {type(e).__name__}")
    org = 본문.get("org")
    if not _uuid인가(org):
        raise HTTPException(401, "데모 토큰의 org 가 UUID 가 아니다")
    return 주체(org_id=org, 출처="demo")


def _계정조회(email: str) -> tuple[str, str] | None:
    """email → (org_id, account_id). 없으면 None, DB 장애면 503.

    `tenant.계정찾기()`(SECURITY DEFINER) 를 부른다 — 이 시점엔 GUC 가 없어 테이블을
    직접 읽으면 RLS 에 걸려 0행이다. 「없는 계정」(None) 과 「죽은 DB」(503) 를 가른다.
    """
    try:
        행 = _질의("SELECT org_id, account_id FROM tenant.계정찾기(%s)",
                   (email,), 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        로그.exception("계정 조회가 DB 경로에서 실패했다 — 미등록(403)과 갈라 낸다")
        # 사유에 예외 문자열을 싣지 않는다 — psycopg 오류 본문에 호스트·포트·사용자명이 실린다.
        raise HTTPException(503, "계정 확인에 실패했습니다 — 잠시 후 다시 시도해 주세요") from e
    return (str(행[0][0]), str(행[0][1])) if 행 else None


def _supabase해석(토큰: str) -> 주체:
    jwt = _pyjwt()
    try:
        본문 = jwt.decode(
            토큰, _서명키(토큰),
            algorithms=["RS256", "ES256"],                # 비대칭만. HS256 을 넣으면
            audience=SUPABASE_AUD,                        # 공개키가 HMAC 비밀키가 된다
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "토큰이 만료됐다")
    except jwt.PyJWTError as e:                          # 위와 같은 이유 — 최상위를 잡는다
        raise HTTPException(401, f"토큰이 유효하지 않다: {type(e).__name__}")

    email = (본문.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(403, "토큰에 email 클레임이 없다 — 기관을 특정할 수 없다")
    계정 = _계정조회(email)
    if 계정 is None:
        # 토큰을 냈으면 토큰으로 끝난다. 파라미터로 흐르지 않는다.
        raise HTTPException(403, "등록되지 않은 계정이다")
    return 주체(org_id=계정[0], 출처="token", email=email, account_id=계정[1])


def _uuid인가(v) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def 토큰해석(authorization: str | None) -> 주체 | None:
    """Authorization 헤더 → 주체. 헤더가 없으면 None, 있으면 주체 아니면 예외다."""
    if not authorization:
        return None
    갈래, _, 토큰 = authorization.partition(" ")
    if 갈래.lower() != "bearer" or not 토큰.strip():
        raise HTTPException(401, "Authorization 은 'Bearer <token>' 형식이어야 한다")
    토큰 = 토큰.strip()

    jwt = _pyjwt()
    try:                                    # 서명 검증 전이다 — 갈래를 고르는 데만 쓴다
        iss = jwt.decode(토큰, options={"verify_signature": False}).get("iss")
    except Exception:                                                   # noqa: BLE001
        raise HTTPException(401, "토큰을 읽을 수 없다")
    return _데모해석(토큰) if iss == DEMO_ISS else _supabase해석(토큰)


# ── FastAPI 의존성 ──────────────────────────────────────────────────

def 현재주체(
    authorization: str | None = Header(None),
    org_id: str | None = Query(None, description="🔴 자기신고 폴백 (R4). 토큰이 있으면 무시된다"),
) -> 주체:
    주 = 토큰해석(authorization)
    if 주 is not None:
        return 주                                         # 토큰이 이긴다
    if org_id is None:
        return 주체(org_id=None, 출처="none")             # 게스트
    if not ORG_PARAM_허용:
        raise HTTPException(401, "org_id 자기신고가 꺼져 있다 — 로그인이 필요하다")
    if not _uuid인가(org_id):
        raise HTTPException(422, "org_id 가 UUID 가 아니다")
    return 주체(org_id=org_id, 출처="param")


# ── org_id 주입 미들웨어 ────────────────────────────────────────────
#
# 라우터가 보기 전에 쿼리스트링의 org_id 를 토큰값으로 갈아끼운다.
# BaseHTTPMiddleware 가 아니라 순수 ASGI 다 — 응답을 감싸지 않아 SSE 를 깨뜨리지 않는다.

# 인증을 거치지 않는 경로. /api/gpu 는 게스트도 폴링하므로 만료 토큰에도 200 이어야 한다.
_보호제외 = ("/api/health", "/api/orgs", "/api/demo/session", "/docs", "/openapi.json", "/redoc", "/api/gpu")


class OrgId주입:
    """`app.add_middleware(auth.OrgId주입)` 으로 켠다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        경로 = scope.get("path", "")
        if not 경로.startswith("/api") or 경로.startswith(_보호제외):
            return await self.app(scope, receive, send)

        from urllib.parse import parse_qsl, urlencode

        머리 = {k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])}
        try:
            주 = 토큰해석(머리.get("authorization"))
        except HTTPException as e:
            return await _거부(send, e.status_code, e.detail)

        쿼리 = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        자기신고 = next((v for k, v in 쿼리 if k == "org_id"), None)

        if 주 is not None:
            쿼리 = [(k, v) for k, v in 쿼리 if k != "org_id"]   # 전부 뗀다 — 하나만 떼면
            if 주.org_id:                                        # org_id 를 두 번 실어 우회한다
                쿼리.append(("org_id", 주.org_id))
        elif 자기신고 is not None and not ORG_PARAM_허용:
            return await _거부(send, 401, "org_id 자기신고가 꺼져 있다 — 로그인이 필요하다")

        scope = dict(scope)
        scope["query_string"] = urlencode(쿼리).encode()
        scope["suddoe_주체"] = 주 or 주체(org_id=자기신고,
                                          출처="param" if 자기신고 else "none")

        # RLS 용 GUC 는 검증된 주체(token·demo)에서만 세운다. 자기신고·게스트는 None —
        # 그러면 current_org() 가 NULL 이라 쓰기는 막히고 읽기는 0행이다.
        # finally 에서 되돌린다 — 태스크 재사용에 기대지 않는다.
        토큰 = _common.현재_org.set(
            str(주.org_id) if (주 is not None and 주.검증됨 and 주.org_id) else None)
        try:
            return await self.app(scope, receive, send)
        finally:
            _common.현재_org.reset(토큰)


async def _거부(send, 코드: int, 사유: str) -> None:
    import json
    본문 = json.dumps({"detail": 사유}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": 코드, "headers": [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(본문)).encode())]})
    await send({"type": "http.response.body", "body": 본문})


# ── 공개 슬러그 ─────────────────────────────────────────────────────

def slug(org_id) -> str:
    """org_id 를 응답에 싣지 않기 위한 HMAC 손잡이. 슬러그에서 org_id 로 되돌릴 수 없다."""
    h = hmac.new(_비밀("SUDDOE_SLUG_SECRET").encode(), str(org_id).encode(), hashlib.sha256)
    return h.hexdigest()[:16]
