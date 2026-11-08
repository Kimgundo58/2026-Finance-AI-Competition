# -*- coding: utf-8 -*-
"""GET/PUT /api/profile — 계정의 기업 프로필(협약 금액·기간). 집행내역·참여인력은 항상 빈 목록이다.

프로필은 계정 단위다. org 는 주관기관이라 같은 org 의 다른 계정(다른 창업기업)과 나눠 쓰지 않는다.
GET 은 토큰으로 검증된 org·계정의 tenant.profile_agreements 를 읽는다(코드 조건 + RLS). ?org_id= 쿼리 값은 믿지 않는다.
프로필이 없거나 게스트면 빈 프로필을 준다. 프로필 없이도 판정은 되고 전제가 늘어날 뿐이다.
PUT 은 협약 다섯 값(협약시작일·협약종료일·정부지원_현금·자기부담_현금·자기부담_현물)을 검증된 org·계정의 행에 저장한다.
  - 자기부담_현물 은 보내지 않거나 null 이면 NULL(미입력)로 저장한다. 0 은 「현물 없음」이다.
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
GET 과 PUT 응답에는 `협약경고: [문장]` 이 함께 온다(5-15 ②). 저장된 협약 금액이 이 계정 사업의 협약 구성 규범
(「자기부담사업비 중 현물은 총사업비의 20% 이하」 등)을 어기면 쉬운 한국어 한 문장씩 담는다. 코드가 계산하고 LLM 은 쓰지 않으며,
판정에는 영향이 없다. 어느 사업의 규범을 볼지는 요청의 `?사업명=`(화면이 고른 사업, 저장하지 않는다) > 프로필 행의 사업명(미지정 아님) >
기관의 사업이 하나일 때 그것 순서로 정한다. 사업을 정할 수 없거나 모르는 값이 있거나 어떤 오류가 나도 빈 목록이다(프로필 요청은 실패하지 않는다).
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date
from decimal import Decimal
from fractions import Fraction

from fastapi import APIRouter, Body, HTTPException, Query, Request

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
    return _구키_포함({"협약": 협약정보().model_dump(), "집행내역": [], "참여인력": [], "협약경고": []})


# GET·PUT 이 돌려주는 협약 칸. 순서는 _협약_dict 인자 순서다
_협약_칸 = '"정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일", "자기부담_현물"'


def _협약_dict(a, b, c, d, e) -> dict:
    """DB 행 값(_협약_칸 순서)을 응답의 협약 dict 로 바꾼다. 자기부담_현물 NULL 은 None(미입력)으로 둔다."""
    return {"정부지원_현금": float(a or 0), "자기부담_현금": float(b or 0),
            "자기부담_현물": float(e) if e is not None else None,
            "협약시작일": str(c) if c else None,
            "협약종료일": str(d) if d else None}


# ── 협약경고 (5-15 ②) ────────────────────────────────────────────────
# 협약 구성 규범의 대상 노드 → (화면에 쓰는 이름, 합칠 협약 칸). 여기에 없는 대상의 규범은 건너뛴다
_협약경고_양 = {
    "절차:자기부담현금": ("자기부담 현금", ("자기부담_현금",)),
    "절차:자기부담현물": ("자기부담 현물", ("자기부담_현물",)),
    "절차:정부지원사업비": ("정부지원사업비", ("정부지원_현금",)),
    "절차:창업기업등자기부담사업비": ("자기부담사업비(현금과 현물 합계)", ("자기부담_현금", "자기부담_현물")),
}
_총사업비_칸 = ("정부지원_현금", "자기부담_현금", "자기부담_현물")      # 총사업비 = 이 셋의 합
_비율_기준 = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트)\s*(이하|이상|미만|초과)")


def _은는(말: str) -> str:
    """마지막 한글 글자의 받침으로 은/는을 고른다(괄호 · 숫자는 건너뛴다)."""
    for ch in reversed(말):
        if "가" <= ch <= "힣":
            return "은" if (ord(ch) - 0xAC00) % 28 else "는"
    return "는"


def _이에요(연산: str) -> str:
    return "이에요" if 연산 in ("이상", "미만") else "예요"


def _어김(비율: Fraction, 기준: Fraction, 연산: str) -> bool:
    return not {"이하": 비율 <= 기준, "미만": 비율 < 기준, "이상": 비율 >= 기준, "초과": 비율 > 기준}[연산]


def _퍼센트_표시(비율: Fraction, 기준: str) -> str:
    """비율을 % 글(소수 첫째 자리, 끝의 .0 은 뺌)로. 반올림하면 기준과 같아 보일 때는 기준과 달라 보일 때까지 소수 자리를 늘린다."""
    기준값 = Fraction(기준)
    for 자리 in range(1, 5):
        표시 = f"{float(비율 * 100):.{자리}f}"
        if Fraction(표시) != 기준값 or 자리 == 4:
            break
    return 표시[:-2] if 표시.endswith(".0") else 표시


def _협약경고_문장들(규범들: list[dict], 값: dict, 사업명: str) -> list[str]:
    """협약 금액(원, Decimal)이 협약 구성 규범의 비율 기준을 어기는 규범마다 쉬운 한국어 한 문장. 규범 순서대로.

    기준 숫자 · 연산(이하 · 이상 · 미만 · 초과)은 규범 문장에서 정규식으로 한 개만 뽑히는 것만 쓴다. 모르는 값(None)이 필요하거나
    총사업비가 0이면 그 규범은 건너뛴다.
    """
    if any(값.get(k) is None for k in _총사업비_칸):
        return []
    총 = sum((Fraction(값[k]) for k in _총사업비_칸), Fraction(0))
    if 총 <= 0:
        return []
    out = []
    for n in 규범들:
        문장 = (n.get("결과") or {}).get("문장") or ""
        기준들 = _비율_기준.findall(문장)
        대상들 = n.get("대상") or []
        if len(기준들) != 1 or "총사업비" not in 문장 or len(대상들) != 1 or 대상들[0] not in _협약경고_양:
            continue
        기준, 연산 = 기준들[0]
        이름, 칸들 = _협약경고_양[대상들[0]]
        비율 = sum((Fraction(값[k]) for k in 칸들), Fraction(0)) / 총
        if not _어김(비율, Fraction(기준) / 100, 연산):
            continue
        근거 = next((g for g in n.get("근거") or [] if g.get("doc_id") and g.get("조번호")), None)
        출처 = f"{근거['doc_id']} {근거['조번호']}" if 근거 else 사업명
        out.append(f"{이름}{_은는(이름)} 총사업비의 {_퍼센트_표시(비율, 기준)}%예요. {출처} 기준은 {기준}% {연산}{_이에요(연산)}.")
    return out


def _요청_사업명(사업명: str | None) -> str | None:
    """요청의 사업명을 표준 표기로(「2026 초기창업패키지」 → 「초기창업패키지」). 등록되지 않은 표기는 그대로 둔다(규범이 없어 경고가 없다). 빈 값은 None."""
    값 = (사업명 or "").strip()[:100]
    if not 값:
        return None
    try:
        from server.catalog import _사업명_키, _사업명_표
        return (_사업명_표() or {}).get(_사업명_키(값)) or 값
    except Exception:                                             # noqa: BLE001
        return 값


def _협약경고(org_id: str | None, account_id, 요청사업명: str | None = None) -> list[str]:
    """저장된 협약 행으로 협약경고를 만든다. 사업은 요청의 사업명 > 행의 사업명(미지정이 아니면) > 기관의 사업이 하나일 때 그것이다. 무슨 오류든 [].

    요청의 사업명은 어느 규범을 볼지만 고른다. 저장하지 않고, 협약 금액은 늘 DB 행에서 읽는다."""
    if MOCK or not org_id:
        return []
    try:
        import ontology_lookup                                   # scripts/ 는 server.main 이 sys.path 에 올린다
        조건, 인자 = _계정조건(org_id, "p", account_id)
        with 트랜잭션(org_id, account_id) as cur:
            행 = cur.execute('SELECT p."정부지원_현금", p."자기부담_현금", p."자기부담_현물", p."사업명" '
                            f'FROM tenant.profile_agreements p WHERE {조건} {_행_순서}', 인자).fetchone()
            if not 행:
                return []
            사업명 = _요청_사업명(요청사업명) or (행[3] if 행[3] and 행[3] != _사업명_미지정 else None)
            if not 사업명:
                o = cur.execute("SELECT 사업명 FROM tenant.orgs WHERE org_id = %s", (org_id,)).fetchone()
                사업들 = [x for x in (o[0] if o else None) or [] if x]
                사업명 = 사업들[0] if len(사업들) == 1 else None
            if not 사업명:
                return []
            규범들 = ontology_lookup.비율_규범(cur, 사업명)
        return _협약경고_문장들(규범들, {"정부지원_현금": 행[0], "자기부담_현금": 행[1], "자기부담_현물": 행[2]}, 사업명)
    except Exception:                                             # noqa: BLE001
        _log.warning("협약경고를 만들지 못했다 — org=%s account=%s", org_id, account_id, exc_info=True)
        return []


@router.get("/api/profile")
def profile_get(req: Request, 사업명: str | None = Query(default=None, description="화면이 고른 사업. 협약경고가 볼 규범만 고른다(저장하지 않음)")) -> dict:
    # 특권 DSN(로컬 postgres 등)에서는 RLS 가 꺼지므로 쿼리 값을 쓰면 다른 기관 협약 정보가 보인다.
    org_id = auth.검증된_org(req)
    if MOCK or not org_id:
        return _빈_프로필()
    account_id = auth.검증된_account(req)
    조건, 인자 = _계정조건(org_id, "p", account_id)
    행 = _질의(f'SELECT {_협약_칸} '
              f'FROM tenant.profile_agreements p WHERE {조건} {_행_순서}', 인자)
    if not 행:
        return _빈_프로필()
    return _구키_포함({"협약": _협약_dict(*행[0]), "집행내역": [], "참여인력": [], "협약경고": _협약경고(org_id, account_id, 사업명)})


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


def _협약_검증(협약: 협약정보) -> tuple[date | None, date | None, Decimal, Decimal, Decimal | None]:
    시작 = _날짜(협약.협약시작일, "협약시작일")
    종료 = _날짜(협약.협약종료일, "협약종료일")
    if 시작 and 종료 and 시작 > 종료:
        raise HTTPException(422, f"협약시작일({시작})이 협약종료일({종료})보다 늦습니다")
    현물 = None if 협약.자기부담_현물 is None else _금액(협약.자기부담_현물, "자기부담_현물")
    return 시작, 종료, _금액(협약.정부지원_현금, "정부지원_현금"), _금액(협약.자기부담_현금, "자기부담_현금"), 현물


def _저장_응답(저장: bool, 사유: str | None, 받은값: dict, 협약경고: list[str] | None = None) -> dict:
    out = {"저장": 저장, "사유": 사유, "받은값": _구키_포함(받은값), "협약경고": 협약경고 or []}
    return 구키_추가(out, {"사유": "이유"})  # 구 이름 호환(제거 예정: 다음 릴리스)


def _새행_사업명(cur, org_id: str) -> str:
    """새 행의 사업명. 기관의 사업이 하나면 그 이름, 아니면 '미지정'. 같은 org 에는 늘 같은 값이다."""
    r = cur.execute("SELECT 사업명 FROM tenant.orgs WHERE org_id = %s", (org_id,)).fetchone()
    사업들 = [s for s in (r[0] if r else None) or [] if s]
    return 사업들[0] if len(사업들) == 1 else _사업명_미지정


def _협약_저장(org_id: str, account_id: str | None, 시작, 종료, 정부, 자기, 현물) -> tuple:
    """한 트랜잭션에서 계정의 협약 행을 UPDATE(있으면) 또는 INSERT 하고 판정 캐시를 지운다. 저장된 값을 돌려준다."""
    조건, 인자 = _계정조건(org_id, "p", account_id)
    with 트랜잭션(org_id, account_id) as cur:
        # FOR UPDATE: 같은 계정의 동시 PUT 이 같은 행을 순서대로 고치게 한다
        r = cur.execute("SELECT profile_id FROM tenant.profile_agreements p "
                        f"WHERE {조건} {_행_순서} FOR UPDATE", 인자).fetchone()
        if r:
            저장됨 = cur.execute(
                'UPDATE tenant.profile_agreements p SET "협약시작일" = %s, "협약종료일" = %s, '
                '"정부지원_현금" = %s, "자기부담_현금" = %s, "자기부담_현물" = %s, updated_at = now() '
                f'WHERE p.profile_id = %s AND {조건} '
                f'RETURNING {_협약_칸}',
                (시작, 종료, 정부, 자기, 현물, r[0], *인자)).fetchone()
        else:
            # 행이 없던 두 요청이 동시에 오면 둘 다 여기로 온다. 사업명이 같아 UNIQUE(org_id, account_id, 사업명)에서 만난다
            저장됨 = cur.execute(
                'INSERT INTO tenant.profile_agreements '
                '(org_id, account_id, "사업명", "협약시작일", "협약종료일", "정부지원_현금", "자기부담_현금", '
                '"자기부담_현물") '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (org_id, account_id, "사업명") DO UPDATE SET '
                '"협약시작일" = EXCLUDED."협약시작일", "협약종료일" = EXCLUDED."협약종료일", '
                '"정부지원_현금" = EXCLUDED."정부지원_현금", "자기부담_현금" = EXCLUDED."자기부담_현금", '
                '"자기부담_현물" = EXCLUDED."자기부담_현물", updated_at = now() '
                f'RETURNING {_협약_칸}',
                (org_id, account_id, _새행_사업명(cur, org_id), 시작, 종료, 정부, 자기, 현물)).fetchone()
        # 협약값은 판정 프롬프트 B5 에 들어간다. 이 계정의 이전 값으로 만든 판정 캐시가 답하지 않게 지운다
        cur.execute(f"DELETE FROM tenant.judge_cache p WHERE {조건} AND p.종류 = 'judge'", 인자)
    return 저장됨


@router.put("/api/profile")
def profile_put(req: Request, body: 프로필 = Body(...),
                사업명: str | None = Query(default=None, description="화면이 고른 사업. 협약경고가 볼 규범만 고른다(저장하지 않음)")) -> dict:
    시작, 종료, 정부, 자기, 현물 = _협약_검증(body.협약)
    if MOCK:
        return _저장_응답(False, "mock 모드(SUDDOE_MOCK=1)에서는 저장하지 않습니다", body.model_dump())

    # 본문에는 org_id 필드가 없다. 쿼리의 ?org_id= 도 쓰지 않고 토큰의 org·계정만 쓴다
    org_id = auth.검증된_org(req)
    if not org_id:
        raise HTTPException(401, "저장하려면 로그인이 필요합니다")
    account_id = auth.검증된_account(req)
    try:
        저장됨 = _협약_저장(org_id, account_id, 시작, 종료, 정부, 자기, 현물)
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
    return _저장_응답(True, 사유, {"협약": _협약_dict(*저장됨), "집행내역": [], "참여인력": []}, _협약경고(org_id, account_id, 사업명))
