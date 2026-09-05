# -*- coding: utf-8 -*-
"""인증·테넌트 귀속 — 「너는 어느 기관인가」를 «서버가» 정한다.   **[인증 계통]**

지금까지 org_id 는 쿼리파라미터 자기신고였다. 이 파일은 그 위에 «토큰이 있으면
토큰이 이긴다» 를 얹는다. 파라미터 폴백은 **남긴다** — 마감 4일 전에 떼면 프론트가
통째로 깨진다 (오너 결정 R4). 대신 `SUDDOE_ORG_PARAM=0` 한 줄로 끌 수 있게 뺐다.

🔴 **org_id 는 비밀이 아니다 — 측정으로 확인했다.**
   `scripts/archive/seed/load_org_programs.py` 가 `org_id = uuid5(uuid5(NAMESPACE_DNS,"suddoe.org"), 공백뗀 기관명)`
   으로 만든다. 네임스페이스 문자열이 레포 안에 그대로 있고 기관명은 공개 정보라,
   **413행 중 411행의 org_id 를 이름만으로 재계산했다** (남은 2행은 L3 픽스처 — 다른 네임스페이스).
   그러므로 「org_id 를 응답에 안 실으니까 아무도 모른다」는 방어가 아니다.
   `?org_id=` 폴백이 열려 있는 동안은 남의 지출계획이 열려 있다. 폴백을 끄는 것만이 막는다.

## 무엇이 이기는가

    Authorization 헤더 있음  → 토큰만 본다. 위조·만료·미매칭이면 «거부». 파라미터로 안 흐른다
    Authorization 헤더 없음  → (폴백 켜짐) ?org_id= 자기신고 · (꺼짐) 게스트(None)

## 토큰 두 갈래 — `iss` 로 가른다

    Supabase  RS256/ES256 · JWKS(SUDDOE_JWKS_URL) 검증 · email 클레임 → tenant.accounts
    데모      HS256       · 서버 자체 서명(SUDDOE_DEMO_SECRET) · org 클레임 직결

🔴 갈래마다 허용 알고리즘을 «고정»한다. 안 고정하면 공개키(JWKS)를 HMAC 비밀키로 써서
   서명을 위조하는 alg confusion 이 성립한다. 데모 비밀로 서명한 토큰이 Supabase 로
   들어오는 것도 `iss` 화이트리스트로 막는다.

## `sub` 이 아니라 `email` 로 붙는 이유

`tenant.accounts` 에 Supabase 의 `sub`(UUID)을 담을 컬럼이 없다. 이 세션이 만질 수 있는
스키마는 `pw_hash` NOT NULL 해제 «하나» 뿐이라, 조인 열쇠는 UNIQUE 인 `email` 밖에 없다.
비밀번호는 Supabase 가 들고 우리는 (email → org_id) 만 든다 — 그래서 `pw_hash` 가 비어야 한다.
→ 나중에 `accounts.sub UUID UNIQUE` 를 추가하면 `_계정조회()` 한 함수만 바꾸면 된다.
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
DEMO_TTL = int(os.environ.get("SUDDOE_DEMO_TTL", "7200"))         # 초. 심사 1회 분량

# 🔴 폴백 스위치. 기본은 «켜짐»(R4). 심사 직전에 `SUDDOE_ORG_PARAM=0` 으로 끈다.
ORG_PARAM_허용 = os.environ.get("SUDDOE_ORG_PARAM", "1") != "0"

_개발기본비밀 = "suddoe-dev-only-not-a-secret"


def _비밀(이름: str) -> str:
    """운영에서 미설정이면 로그로 «크게» 알린다. 조용히 개발기본값을 쓰면 그게 사고다."""
    v = os.environ.get(이름, "").strip()
    if v:
        return v
    로그.warning("🔴 %s 미설정 — 개발 기본 비밀을 쓴다. 배포 전에 반드시 설정할 것", 이름)
    return f"{_개발기본비밀}:{이름}"


# ── 주체 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class 주체:
    """이 요청이 «누구» 인가. org_id 는 여기서만 나온다.

    `출처` 를 남기는 이유: 판단불가율을 «모델선택/실패경로» 로 갈라 세는 것과 같다.
    「격리됐다」와 「자기신고를 믿었다」가 한 값이면 로그에서 영영 안 갈린다.
    """
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
    """JWKS 를 TTL 캐시로 든다. 실패하면 «예외» — 빈 dict 를 주면 검증이 통과한다."""
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
    """kid 로 JWKS 에서 공개키를 고른다. 못 찾으면 «한 번만» 갱신하고 다시 본다
    (키 회전 직후). 그래도 없으면 거부 — 검증 없이 통과시키지 않는다."""
    import jwt
    kid = jwt.get_unverified_header(토큰).get("kid")
    for 강제 in (False, True):
        for k in _jwks(강제).get("keys", []):
            if k.get("kid") == kid:
                return jwt.PyJWK(k).key
    raise HTTPException(401, "알 수 없는 서명 키")


# ── 토큰 해석 ───────────────────────────────────────────────────────

def _pyjwt():
    """지연 import. 이 레포엔 의존성 매니페스트가 없어서 pyjwt 가 빠진 채로 뜰 수 있다.
    🔴 그때 «통과» 가 아니라 «거부» 로 떨어져야 한다."""
    try:
        import jwt
        return jwt
    except ModuleNotFoundError:
        raise HTTPException(503, "pyjwt 미설치 — 토큰을 검증할 수 없다 (pip install 'pyjwt[crypto]')")


def 데모토큰_발급(org_id: str) -> tuple[str, int]:
    """서버 자체 서명 단기 토큰. Supabase 를 안 거친다 (심사위원은 계정이 없다)."""
    jwt = _pyjwt()
    만료 = int(time.time()) + DEMO_TTL
    본문 = {"iss": DEMO_ISS, "sub": f"demo:{org_id}", "org": org_id,
            "iat": int(time.time()), "exp": 만료}
    return jwt.encode(본문, _비밀("SUDDOE_DEMO_SECRET"), algorithm="HS256"), DEMO_TTL


def _데모해석(토큰: str) -> 주체:
    jwt = _pyjwt()
    try:
        본문 = jwt.decode(토큰, _비밀("SUDDOE_DEMO_SECRET"),
                          algorithms=["HS256"],          # 🔴 HS256 «만»
                          issuer=DEMO_ISS,
                          options={"require": ["exp", "iss", "org"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "데모 세션이 만료됐다 — 다시 시작할 것")
    except jwt.PyJWTError as e:      # 🔴 InvalidTokenError 가 아니라 «최상위» 를 잡는다.
        #   InvalidKeyError 는 InvalidTokenError 의 하위가 아니라서, 좁게 잡으면
        #   alg confusion 시도가 401 이 아니라 500 으로 샌다 (2026-09-03 실측).
        raise HTTPException(401, f"데모 토큰이 유효하지 않다: {type(e).__name__}")
    org = 본문.get("org")
    if not _uuid인가(org):
        raise HTTPException(401, "데모 토큰의 org 가 UUID 가 아니다")
    return 주체(org_id=org, 출처="demo")


def _계정조회(email: str) -> tuple[str, str] | None:
    """email → (org_id, account_id). 🔴 조인 열쇠가 email 인 이유는 모듈 docstring 참조.

    🔴 **「없는 계정」과 「죽은 DB」를 갈라 낸다** (2026-09-03).
       `_질의` 는 기본이 «실패하면 빈 리스트» 인데 여기서 빈 리스트는 곧 403 이다.
       그래서 그전까지는 **DB 가 통째로 죽어도 403「등록되지 않은 계정이다」** 였다 —
       재현했다: DSN 을 아무도 안 듣는 포트로 돌리면 미등록 email 과 상태코드도
       문구도 «완전히» 같다. 운영에서 이건 「시연 계정 등록이 빠졌나」를 몇 시간
       뒤지게 만드는 종류의 오류다. 인증 경로만 `예외전파=True` 로 켜서 503 으로 뺀다.

    ⚠️ 503 은 「등록 여부를 모른다」이지 「등록됐다」가 아니다. 호출부가 이걸
       통과로 읽으면 안 된다 — 그래서 None 이 아니라 «예외» 로 나간다.

    🔴 **테이블을 직접 안 읽고 `tenant.계정찾기()` 를 부른다** (2026-09-03).
       `accounts` 정책은 `org_id = current_org()` 하나뿐인데, 이 함수는 «org 를
       알아내려고» 읽는다 — 읽는 시점에 GUC 가 아직 없다. 그래서 비특권 롤에서는
       0행이고 곧 403 이다. 실측:

           postgres(superuser) → 찾음    ·    suddoe_app(비특권) → None → 403

       로컬 `postgres` 가 superuser 라 **이 자리는 로컬에서 안 보인다.** 명부를
       채우는 것으로도, GRANT 를 여는 것으로도 안 열린다 — 정책이 막는 것이다.
       → `db/init/11_accounts_login.sql` 의 SECURITY DEFINER 함수로 «이메일 1건» 만
         RLS 밖으로 낸다.

    🔴 함수가 없으면 **503 으로 죽는다. 직접 SELECT 로 물러서지 않는다.**
       물러서는 순간 superuser 인 로컬에서는 통과하고 운영에서만 0행이 되는데,
       그건 정확히 「로컬에선 되는데 운영에서 안 되는」 오늘의 그 사고다.
    """
    try:
        행 = _질의("SELECT org_id, account_id FROM tenant.계정찾기(%s)",
                   (email,), 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        로그.exception("계정 조회가 DB 경로에서 실패했다 — 미등록(403)과 갈라 낸다")
        # 🔴 사유에 예외 «문자열» 을 싣지 않는다. psycopg 의 접속 오류는 본문에
        #    호스트·포트·사용자명을 그대로 담는다 — 401/503 응답은 인증 «전» 이라
        #    아무나 받아 본다.
        raise HTTPException(503, "계정 확인에 실패했습니다 — 잠시 후 다시 시도해 주세요") from e
    return (str(행[0][0]), str(행[0][1])) if 행 else None


def _supabase해석(토큰: str) -> 주체:
    jwt = _pyjwt()
    try:
        본문 = jwt.decode(
            토큰, _서명키(토큰),
            algorithms=["RS256", "ES256"],                # 🔴 비대칭 «만». HS256 을 넣으면
            audience=SUPABASE_AUD,                        #    공개키가 HMAC 비밀키가 된다
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
        # 🔴 여기서 파라미터로 «흐르지» 않는다. 토큰을 냈으면 토큰으로 끝난다.
        raise HTTPException(403, "등록되지 않은 계정이다")
    return 주체(org_id=계정[0], 출처="token", email=email, account_id=계정[1])


def _uuid인가(v) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def 토큰해석(authorization: str | None) -> 주체 | None:
    """Authorization 헤더 → 주체. 헤더가 없으면 None (폴백으로 넘긴다).
    헤더가 «있으면» 반드시 주체를 내거나 예외다 — 조용히 None 을 주지 않는다."""
    if not authorization:
        return None
    갈래, _, 토큰 = authorization.partition(" ")
    if 갈래.lower() != "bearer" or not 토큰.strip():
        raise HTTPException(401, "Authorization 은 'Bearer <token>' 형식이어야 한다")
    토큰 = 토큰.strip()

    jwt = _pyjwt()
    try:                                    # 서명 검증 «전» 이다 — 갈래를 고르는 데만 쓴다
        iss = jwt.decode(토큰, options={"verify_signature": False}).get("iss")
    except Exception:                                                   # noqa: BLE001
        raise HTTPException(401, "토큰을 읽을 수 없다")
    return _데모해석(토큰) if iss == DEMO_ISS else _supabase해석(토큰)


# ── FastAPI 의존성 ──────────────────────────────────────────────────
#
# 미들웨어가 아니라 Depends 인 이유: 미들웨어는 테스트에서 앱을 통째로 세워야 하는데
# 의존성 함수는 그냥 «부르면» 된다. 아래 자가검토가 실제로 이 함수를 직접 태운다.

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
# 🔴 **이게 없으면 `auth.py` 는 아무것도 막지 못한다.**
#    기존 `routes_plans`·`routes_tasks`·`routes_l3` 는 `org_id: str | None = None`
#    쿼리파라미터를 직접 읽는다. 그 파일들을 이 세션이 못 고치므로(소유 밖),
#    라우터가 «보기 전에» 쿼리스트링의 org_id 를 토큰값으로 갈아끼운다.
#    → 라우터 코드는 한 줄도 안 바뀌는데 「토큰이 있으면 토큰이 이긴다」가 전 경로에 걸린다.
#
# 🔴 `@app.middleware("http")`(=BaseHTTPMiddleware) 가 아니라 «순수 ASGI» 다.
#    BaseHTTPMiddleware 는 응답을 한 겹 싸는데 이 서버는 SSE(`_sse응답`)를 쓴다.
#    여기서는 응답을 건드릴 일이 «전혀» 없고 요청 scope 만 고치면 되므로,
#    응답 경로를 통째로 안 건드리는 쪽이 SSE 를 깨뜨릴 여지가 없다.
#
#    임시 배선이다. 각 라우터가 `Depends(현재주체)` 로 옮겨가면 이 클래스는 지운다.

# 🔴 /api/gpu 는 테넌트 데이터를 안 만지고 GPU 유휴초만 돌려준다. 게스트 경로에서도
#    폴링되고, 프론트 fetch 래퍼가 모든 요청에 Bearer 를 붙이므로 «만료 토큰» 하나에
#    상태 폴링이 죽으면 안 된다 — 헤더가 있든 없든 200 이어야 한다 (S4 지시).
_보호제외 = ("/api/health", "/api/orgs", "/api/demo/session", "/docs", "/openapi.json", "/redoc", "/api/gpu")


class OrgId주입:
    """main.py 가 `app.add_middleware(auth.OrgId주입)` 한 줄로 켠다."""

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
            쿼리 = [(k, v) for k, v in 쿼리 if k != "org_id"]   # 🔴 «전부» 뗀다.
            if 주.org_id:                                        #   하나만 떼면 org_id 를
                쿼리.append(("org_id", 주.org_id))               #   두 번 실어 우회한다
        elif 자기신고 is not None and not ORG_PARAM_허용:
            return await _거부(send, 401, "org_id 자기신고가 꺼져 있다 — 로그인이 필요하다")

        scope = dict(scope)
        scope["query_string"] = urlencode(쿼리).encode()
        scope["suddoe_주체"] = 주 or 주체(org_id=자기신고,
                                          출처="param" if 자기신고 else "none")

        # 🔴 **RLS 용 GUC 는 «검증된» 주체에서만 온다** (2026-09-03).
        #    `_common._질의`/`_실행` 이 이 값을 트랜잭션마다 `app.org_id` 로 걸고,
        #    `tenant.*` 의 `org_isolation` 정책이 그걸 본다.
        #
        #    `주.검증됨` 은 출처가 token·demo 일 때만 True 다. `param`(자기신고)·
        #    `none`(게스트)에는 **세우지 않는다** — 자기신고를 GUC 에 넣으면
        #    「클라이언트가 말한 값을 DB 에 도장 찍는」 꼴이라 RLS 가 장식이 된다.
        #    감사에서는 「RLS 켜져 있음」으로 통과하는데 실제로는 아무것도 안 막는다.
        #
        #    안 세우면 `current_org()` 가 NULL → 쓰기는 RLS 가 막고 읽기는 0행이다.
        #    거부(401)가 아니라 이 쪽을 고른 이유: 폴백은 아직 R4 로 살아 있어야 하고
        #    (프론트 헤더 전환 전), 여기서 401 을 내면 `SUDDOE_ORG_PARAM` 스위치와
        #    무관하게 폴백이 즉시 죽는다. 판단을 DB 층에 맡기고 «조용히 열지는» 않는다.
        #
        # 🔴 `finally` 에서 되돌린다. 지금은 요청마다 태스크가 새로 나서 안 새지만
        #    (실측: 동시 8건 불일치 0), 되돌리기를 빼면 그 사실에 기대는 코드가 된다.
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
#
# 🔴 org_id(UUID)를 응답에 싣지 않기 위한 대체 손잡이다.
#    HMAC 이라 슬러그에서 org_id 로 «되돌아갈 수 없다». 되돌리기는 서버가 413행을
#    훑어 맞추는 방향으로만 한다. 이름 기반 슬러그로 하면 위 uuid5 문제를 그대로
#    되풀이하므로(이름 → org_id 재계산) HMAC 을 쓴다.

def slug(org_id) -> str:
    h = hmac.new(_비밀("SUDDOE_SLUG_SECRET").encode(), str(org_id).encode(), hashlib.sha256)
    return h.hexdigest()[:16]
