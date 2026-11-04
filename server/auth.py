# -*- coding: utf-8 -*-
"""인증과 테넌트 결정: 요청이 어느 기관(org)의 것인지 서버가 정한다.

server/main.py 가 live 모드에서 OrgId주입 을 ASGI 미들웨어로 등록하고, 라우터는 검증된_org(),
검증된_account(), 관리자_확인, slug(), 데모토큰_발급() 을 쓴다. 토큰은 iss 로 두 종류를 구분한다.
    Supabase  RS256/ES256, JWKS(SUDDOE_JWKS_URL) 검증, email 클레임 -> tenant.accounts (org_id, account_id)
    데모      HS256, 서버 자체 서명(SUDDOE_DEMO_SECRET), org 클레임을 그대로 쓴다(계정 없음)
불변 조건: Authorization 헤더가 있으면 토큰만 보고, 실패하면 거부한다(?org_id= 로 폴백하지 않는다).
토큰 종류마다 허용 알고리즘을 고정한다. 고정하지 않으면 공개키를 HMAC 비밀키로 쓰는 alg confusion 이 가능하다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

from fastapi import Header, HTTPException, Request

from . import _common
from ._common import _질의, _환경값

로그 = logging.getLogger("suddoe.auth")

# ── 환경 설정 ───────────────────────────────────────────────────────

JWKS_URL = os.environ.get("SUDDOE_JWKS_URL", "").strip()
JWKS_TTL = int(os.environ.get("SUDDOE_JWKS_TTL", "600"))          # 초
SUPABASE_AUD = os.environ.get("SUDDOE_JWT_AUD", "authenticated")

DEMO_ISS = "suddoe-demo"
DEMO_TOKEN_TTL = int(_환경값("SUDDOE_DEMO_TOKEN_TTL", "7200",
                            구이름="SUDDOE_DEMO_TTL"))  # 초. # 구 이름 호환(제거 예정: 다음 릴리스)

# 토큰 없는 요청이 보낸 ?org_id=(검증되지 않은 값)를 받을지. 기본은 거부이고, 로컬 개발에서만
# SUDDOE_ALLOW_ORG_PARAM=1 로 켠다. 받더라도 RLS 용 GUC 는 설정하지 않는다(OrgId주입 참조).
ORG_PARAM_허용 = (_환경값("SUDDOE_ALLOW_ORG_PARAM", "0",
                         구이름="SUDDOE_ORG_PARAM") or "").strip() == "1"  # 구 이름 호환(제거 예정: 다음 릴리스)

_개발기본비밀 = "suddoe-dev-only-not-a-secret"


def _비밀(이름: str) -> str | None:
    """환경변수의 비밀값. 미설정이면 mock 모드에서만 개발용 기본값을 쓰고, live 모드에서는 None.

    개발용 기본값은 코드에 공개돼 있어 live 모드에서 쓰면 누구나 데모 토큰을 만들 수 있다.
    """
    v = os.environ.get(이름, "").strip()
    if v:
        return v
    return f"{_개발기본비밀}:{이름}" if _common.MOCK else None


if not _common.MOCK and not os.environ.get("SUDDOE_SLUG_SECRET", "").strip():
    로그.error("SUDDOE_SLUG_SECRET 미설정: 공개 slug 를 개발용 기본 비밀로 계산합니다. "
               "누구나 org_id 에서 slug 를 계산할 수 있으니 운영에서는 반드시 설정하세요.")


# ── 주체 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class 주체:
    """요청의 주체. 출처로 검증된 값인지, 클라이언트가 보낸 값을 그대로 받은 것인지 구분한다."""
    org_id: str | None
    출처: str                    # 'token' | 'demo' | 'param' | 'none'
    email: str | None = None
    account_id: str | None = None    # Supabase 토큰일 때만 있다. 데모 세션·게스트는 None

    @property
    def 검증됨(self) -> bool:
        return self.출처 in ("token", "demo")


# ── JWKS 캐시 ───────────────────────────────────────────────────────

_jwks_잠금 = threading.Lock()
_jwks_캐시: tuple[float, dict] | None = None


def _jwks(강제갱신: bool = False) -> dict:
    """JWKS 를 TTL 캐시로 가져온다. 실패하면 HTTPException(503).

    빈 dict 로 폴백하지 않는다. 실패는 항상 예외로 드러낸다.
    """
    global _jwks_캐시
    with _jwks_잠금:
        if not 강제갱신 and _jwks_캐시 and time.time() - _jwks_캐시[0] < JWKS_TTL:
            return _jwks_캐시[1]
    if not JWKS_URL:
        raise HTTPException(503, "SUDDOE_JWKS_URL 이 설정되지 않아 Supabase 토큰을 검증할 수 없습니다")
    import httpx
    # 미들웨어 밖으로 HTTPException 이 아닌 예외가 나가면 CORS 헤더 없는 500 이 되어
    # 브라우저가 CORS 오류로 보고한다. 503 으로 바꿔 낸다.
    try:
        본문 = httpx.get(JWKS_URL, timeout=5.0).raise_for_status().json()
        if not isinstance(본문, dict):
            raise ValueError(f"JWKS 응답이 객체가 아니다: {type(본문).__name__}")
    except Exception as e:                                    # noqa: BLE001
        로그.exception("JWKS 를 가져오지 못했다")
        raise HTTPException(503, "인증 키를 가져오지 못했습니다") from e
    with _jwks_잠금:
        _jwks_캐시 = (time.time(), 본문)
    return 본문


def _서명키(토큰: str):
    """토큰 헤더의 kid 에 맞는 공개키. 없으면 JWKS 를 한 번 강제 갱신해 다시 찾는다(키 교체 대응)."""
    import jwt
    kid = jwt.get_unverified_header(토큰).get("kid")
    for 강제 in (False, True):
        for k in _jwks(강제).get("keys", []):
            if k.get("kid") == kid:
                return jwt.PyJWK(k).key
    raise HTTPException(401, "알 수 없는 서명 키입니다")


# ── 토큰 해석 ───────────────────────────────────────────────────────

def _pyjwt():
    """pyjwt 지연 import. 설치돼 있지 않으면 통과시키지 않고 503 으로 거부한다."""
    try:
        import jwt
        return jwt
    except ModuleNotFoundError:
        raise HTTPException(503, "pyjwt 가 설치되지 않아 토큰을 검증할 수 없습니다 (pip install 'pyjwt[crypto]')")


def 데모_사용가능() -> bool:
    """데모 토큰을 발급할 수 있는가. live 모드에서 SUDDOE_DEMO_SECRET 이 없으면 False.

    POST /api/demo/session 은 임시 기관을 만들기 전에 이것으로 먼저 확인한다. 발급 단계에서야
    503 을 내면 쓸모없는 데모 기관 행이 남는다.
    """
    return _비밀("SUDDOE_DEMO_SECRET") is not None


def 데모토큰_발급(org_id: str) -> tuple[str, int]:
    """데모 세션용 단기 토큰을 서버 비밀로 서명해 (토큰, 수명초) 를 반환한다.

    live 모드에서 SUDDOE_DEMO_SECRET 이 없으면 HTTPException(503).
    """
    jwt = _pyjwt()
    비밀 = _비밀("SUDDOE_DEMO_SECRET")
    if 비밀 is None:
        raise HTTPException(503, "데모 세션을 사용할 수 없습니다 (서버에 데모 토큰 비밀이 설정되지 않았습니다)")
    만료 = int(time.time()) + DEMO_TOKEN_TTL
    본문 = {"iss": DEMO_ISS, "sub": f"demo:{org_id}", "org": org_id,
            "iat": int(time.time()), "exp": 만료}
    return jwt.encode(본문, 비밀, algorithm="HS256"), DEMO_TOKEN_TTL


def _데모해석(토큰: str) -> 주체:
    jwt = _pyjwt()
    비밀 = _비밀("SUDDOE_DEMO_SECRET")
    if 비밀 is None:
        raise HTTPException(401, "이 서버는 데모 토큰을 받지 않습니다")
    try:
        본문 = jwt.decode(토큰, 비밀,
                          algorithms=["HS256"],          # HS256 만 허용
                          issuer=DEMO_ISS,
                          options={"require": ["exp", "iss", "org"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "데모 세션이 만료됐습니다. 다시 시작해 주세요")
    except jwt.PyJWTError as e:      # 최상위 예외를 잡는다. InvalidKeyError 는 InvalidTokenError 하위가 아니다
        raise HTTPException(401, f"데모 토큰이 유효하지 않습니다: {type(e).__name__}")
    org = 본문.get("org")
    if not _uuid인가(org):
        raise HTTPException(401, "데모 토큰의 org 가 UUID 형식이 아닙니다")
    return 주체(org_id=org, 출처="demo")


def _계정조회(email: str) -> tuple[str, str] | None:
    """email -> (org_id, account_id). 등록되지 않은 계정이면 None, DB 장애면 HTTPException(503).

    이 시점에는 GUC 가 없어 tenant.accounts 를 직접 읽으면 RLS 정책에 의해 0행이 된다.
    그래서 SECURITY DEFINER 함수 tenant.계정찾기() 를 호출한다.
    """
    try:
        행 = _질의("SELECT org_id, account_id FROM tenant.계정찾기(%s)",
                   (email,), 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        로그.exception("계정 조회 실패(DB 오류). 미등록 계정(403)과 구분해 503 으로 응답한다")
        # 응답에 예외 문자열을 넣지 않는다. psycopg 오류 본문에 호스트·포트·사용자명이 들어 있다.
        raise HTTPException(503, "계정 확인에 실패했습니다. 잠시 후 다시 시도해 주세요") from e
    return (str(행[0][0]), str(행[0][1])) if 행 else None


def _supabase해석(토큰: str) -> 주체:
    jwt = _pyjwt()
    try:
        본문 = jwt.decode(
            토큰, _서명키(토큰),
            algorithms=["RS256", "ES256"],                # 비대칭만 허용. HS256 을 넣으면
            audience=SUPABASE_AUD,                        # 공개키가 HMAC 비밀키로 쓰인다
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "토큰이 만료됐습니다")
    except jwt.PyJWTError as e:                          # _데모해석 과 같은 이유로 최상위 예외를 잡는다
        raise HTTPException(401, f"토큰이 유효하지 않습니다: {type(e).__name__}")

    email = (본문.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(403, "토큰에 email 클레임이 없어 기관을 확인할 수 없습니다")
    계정 = _계정조회(email)
    if 계정 is None:
        # 토큰을 보낸 요청은 토큰으로만 판단한다. ?org_id= 로 폴백하지 않는다.
        raise HTTPException(403, "등록되지 않은 계정입니다")
    return 주체(org_id=계정[0], 출처="token", email=email, account_id=계정[1])


def _uuid인가(v) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def 토큰해석(authorization: str | None) -> 주체 | None:
    """Authorization 헤더 -> 주체. 헤더가 없으면 None, 헤더가 있는데 검증에 실패하면 HTTPException."""
    if not authorization:
        return None
    토큰종류, _, 토큰 = authorization.partition(" ")
    if 토큰종류.lower() != "bearer" or not 토큰.strip():
        raise HTTPException(401, "Authorization 헤더는 'Bearer <token>' 형식이어야 합니다")
    토큰 = 토큰.strip()

    jwt = _pyjwt()
    try:                                    # 서명 검증 전 값이다. 해석 경로를 고르는 데만 쓴다
        iss = jwt.decode(토큰, options={"verify_signature": False}).get("iss")
    except Exception:                                                   # noqa: BLE001
        raise HTTPException(401, "토큰을 읽을 수 없습니다")
    return _데모해석(토큰) if iss == DEMO_ISS else _supabase해석(토큰)


# ── 라우터용 헬퍼 ───────────────────────────────────────────────────

def 검증된_org(req: Request) -> str | None:
    """검증된 주체(token, demo)의 org_id. 게스트이거나 ?org_id= 로 받은 값이면 None.

    OrgId주입 은 쿼리스트링만 바꾸므로 요청 본문의 org_id 는 믿지 않고 이 값을 쓴다.
    mock 모드에서는 미들웨어가 없어 항상 None 이다.
    """
    주 = req.scope.get("suddoe_주체")
    return 주.org_id if 주 is not None and 주.검증됨 else None


def 검증된_account(req: Request) -> str | None:
    """검증된 Supabase 토큰의 account_id. 데모 세션·게스트·?org_id= 요청이면 None.

    기업 데이터(프로필·지출계획·할일·판정·캐시)는 org 와 이 값으로 함께 거른다. L3 는 org 만 본다.
    """
    주 = req.scope.get("suddoe_주체")
    return 주.account_id if 주 is not None and 주.검증됨 else None


def 관리자_확인(x_admin_token: str | None = Header(default=None)) -> None:
    """관리 엔드포인트용 FastAPI 의존성. X-Admin-Token 이 SUDDOE_ADMIN_TOKEN 과 다르면 403.

    SUDDOE_ADMIN_TOKEN 이 비어 있으면 항상 거부한다. 비교는 타이밍 공격을 막기 위해
    hmac.compare_digest 로 한다.
    """
    기대 = os.environ.get("SUDDOE_ADMIN_TOKEN", "")
    if not 기대 or not x_admin_token or not hmac.compare_digest(
            x_admin_token.encode(), 기대.encode()):
        raise HTTPException(403, "관리자 토큰이 필요합니다 (SUDDOE_ADMIN_TOKEN)")


# ── org_id 주입 미들웨어 ────────────────────────────────────────────
# 라우터가 보기 전에 쿼리스트링의 org_id 를 토큰에서 얻은 값으로 바꾼다.
# BaseHTTPMiddleware 가 아니라 순수 ASGI 로 구현한다. 응답을 감싸지 않아 SSE 스트림을 깨지 않는다.

# 인증 없이 통과시키는 경로(접두어 비교). /api/gpu/wake 는 프론트가 토큰 없이 호출하는 정적 응답이다.
_인증제외_경로 = ("/api/health", "/api/orgs", "/api/demo/session", "/docs", "/openapi.json",
                "/redoc", "/api/gpu/wake")


class OrgId주입:
    """app.add_middleware(auth.OrgId주입) 으로 등록한다.

    요청 scope 에 suddoe_주체 를 넣고, 검증된 주체일 때만 _common.현재_org·현재_account 를 설정한다.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        경로 = scope.get("path", "")
        if not 경로.startswith("/api") or 경로.startswith(_인증제외_경로):
            return await self.app(scope, receive, send)

        헤더 = {k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])}
        try:
            주 = 토큰해석(헤더.get("authorization"))
        except HTTPException as e:
            return await _거부(send, e.status_code, e.detail)

        쿼리 = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        요청_org_id = next((v for k, v in 쿼리 if k == "org_id"), None)

        if 주 is not None:
            # org_id 를 모두 제거한다. 하나만 지우면 org_id 를 두 번 보내 우회할 수 있다.
            쿼리 = [(k, v) for k, v in 쿼리 if k != "org_id"]
            if 주.org_id:
                쿼리.append(("org_id", 주.org_id))
        elif 요청_org_id is not None and not ORG_PARAM_허용:
            return await _거부(send, 401, "이 서버는 org_id 파라미터를 받지 않습니다. 로그인해 주세요")

        scope = dict(scope)
        scope["query_string"] = urlencode(쿼리).encode()
        scope["suddoe_주체"] = 주 or 주체(org_id=요청_org_id,
                                          출처="param" if 요청_org_id else "none")

        # RLS 용 GUC 는 검증된 주체(token, demo)일 때만 설정한다. ?org_id= 와 게스트는 None 이라
        # current_org() 가 NULL 이 되어 쓰기는 거부되고 읽기는 0행이 된다.
        # 태스크 재사용에 기대지 않고 finally 에서 원래 값으로 되돌린다.
        검증 = 주 is not None and 주.검증됨 and bool(주.org_id)
        토큰 = _common.현재_org.set(str(주.org_id) if 검증 else None)
        계정토큰 = _common.현재_account.set(str(주.account_id) if 검증 and 주.account_id else None)
        try:
            return await self.app(scope, receive, send)
        finally:
            _common.현재_account.reset(계정토큰)
            _common.현재_org.reset(토큰)


async def _거부(send, 코드: int, 사유: str) -> None:
    """미들웨어 단계의 오류 응답. 다른 오류와 같은 형식({"오류", "상태"})으로 보낸다."""
    오류본문 = {"오류": 사유, "상태": 코드}
    오류본문["detail"] = 사유  # 구 이름 호환(제거 예정: 다음 릴리스)
    본문 = json.dumps(오류본문, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": 코드, "headers": [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(본문)).encode())]})
    await send({"type": "http.response.body", "body": 본문})


# ── 공개 slug ───────────────────────────────────────────────────────

def slug(org_id) -> str:
    """응답에 org_id 대신 넣는 HMAC 기반 공개 식별자. slug 에서 org_id 를 역산할 수 없다."""
    # 미설정이면 live 모드에서도 개발용 기본값으로 계산한다. 시작 시점에 오류 로그를 남긴다.
    비밀 = (os.environ.get("SUDDOE_SLUG_SECRET", "").strip()
            or f"{_개발기본비밀}:SUDDOE_SLUG_SECRET")
    h = hmac.new(비밀.encode(), str(org_id).encode(), hashlib.sha256)
    return h.hexdigest()[:16]
