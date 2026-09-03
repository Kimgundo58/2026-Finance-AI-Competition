# -*- coding: utf-8 -*-
"""서버 공용 — 라우터가 `main.py` 를 import 하면 순환참조가 난다. 그래서 여기로 뺐다.

🔴 **공용 계약 파일이다.** 값을 추가하려면 먼저 합의할 것 — 여러 계통이 같은 파일을 쓴다.

담는 것은 «어느 라우터에서도 필요한데 정의가 한 벌이어야 하는 것» 뿐이다:
DB 접속·질의, 목/실 모드 스위치, 비목·판정 enum, SSE 포맷터.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from pathlib import Path
from typing import Any

from fastapi.responses import StreamingResponse

ROOT = Path(__file__).resolve().parent.parent

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# 🔴 목이 기본값이다. 프론트는 DB·GPU 없이 오늘 붙는다.
MOCK = os.environ.get("SUDDOE_MOCK", "1") == "1"


# ── enum — DB 가 없어도 계약이 지켜지도록 코드에 한 벌 둔다 ──────────

# `프론트 연동 사양.md` §9. 프론트 라벨을 이 문자열 그대로 맞춘다.
비목_ENUM = ["재료비", "외주용역비", "기계장치", "인건비", "지급수수료",
             "여비", "교육훈련비", "광고선전비", "특허권등무형자산취득비", "창업활동비"]

# 창업활동비는 예비창업패키지에만 있다 (§9). 나머지 사업에서는 목록에서 뺀다.
창업활동비_사업 = {"예비창업패키지"}

# 🔴 판정 4-way. 폐쇄 enum 이다 — 프론트 프로토타입의 7종 어휘로 늘리지 않는다.
판정_ENUM = ("가능", "조건부", "불가", "판단불가")

# 진행 상태. 🔴 판정이 아니다. 한 축에 섞지 말 것.
계획상태_ENUM = ("draft", "judged")

# 할일 축 — 판정 4-way 와 또 다른 축이다.
할일상태_ENUM = ("준비필요", "집행예정", "완료")
할일구분_ENUM = ("결제전", "결제후", "집행")
할일유형_ENUM = ("기타", "계약", "비교견적")

# 목록 탭 → 판정 매핑. 프론트 탭 어휘가 배지 3종이라 4-way 를 여기서 접는다.
# (`프론트_데이터요구서_0901.md` §상태 어휘 — 배지는 3종, 백엔드는 4-way 그대로 준다)
탭_판정 = {
    "전체": None,
    "확인필요": ("조건부", "판단불가"),
    "위험": ("불가",),
    "특이사항없음": ("가능",),
    "점검전": (),          # 판정이 아직 없는 draft
}


# ── 테넌트 GUC — RLS 에 「나는 어느 기관인가」를 알린다 ─────────────
#
# 🔴 **왜 필요한가 (2026-09-03 실측).** `tenant.*` 전 테이블에 RLS 가 켜져 있고 정책은
#    `USING (org_id = tenant.current_org())` 다. `cmd=ALL` 인데 `with_check` 가 «없어»
#    Postgres 가 `qual` 을 INSERT 검사로도 쓴다. 그래서 GUC 를 안 세우면
#    `current_org()` 가 NULL 이 되고 — 비특권 롤로 재현한 값이 이렇다:
#
#        GUC 없음 · INSERT expense_plans   🔴 InsufficientPrivilege  (org_id 를 실어도 같다)
#        GUC 없음 · SELECT expense_plans   ✅ 통과하는데 «0행»       ← 더 위험하다
#        GUC 세움 · INSERT 내 org          ✅ 통과
#        GUC 세움 · INSERT 남의 org        🔴 InsufficientPrivilege  (DB 가 격리를 문다)
#
#    여태 안 보인 이유는 로컬 `postgres` 가 superuser(`rolbypassrls=True`)라 RLS 를
#    통째로 «우회»했기 때문이다. 앱 계정(`suddoe_app`)은 우회 못 한다.
#    → 실서버로 바꾸는 순간 **쓰기가 전부 죽고 읽기는 조용히 0행이 된다.**
#
# 🔴 **값의 «출처» 가 이 배관의 전부다.** 검증된 주체(`auth.주체.검증됨` — 출처가
#    token·demo)의 org_id «만» 넣는다. 요청이 자기신고한 `?org_id=` 를 넣으면
#    「클라이언트가 말한 값을 DB 에 도장 찍는」 꼴이라 **RLS 가 장식이 된다** —
#    감사에서는 「RLS 켜져 있음」으로 통과하는데 실제로는 아무것도 안 막는다.
#
#    그래서 이 파일은 `auth` 를 **import 하지 않는다.** 원래 못 한다(순환참조) 는
#    제약이었는데, 여기서는 그게 방어가 된다 — `_common` 은 요청을 볼 길이 아예 없고,
#    값을 넣을 수 있는 곳은 주체를 이미 검증한 `auth.OrgId주입` 하나뿐이다.
#    **「쉬운 길이 틀린 길」인 자리라 구조로 막았다.**

현재_org: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "suddoe_현재_org", default=None)


@contextlib.contextmanager
def org_고정(org_id: str | None):
    """HTTP 밖(스크립트·배경작업·테스트)에서 org 를 세운다.

    🔴 HTTP 경로에서는 쓰지 마라 — 거기서는 `auth.OrgId주입` 이 «검증된» 주체로만
       세운다. 여기에 값을 넣는 것은 「검증을 건너뛴다」는 뜻이다.
    """
    토큰 = 현재_org.set(str(org_id) if org_id else None)
    try:
        yield
    finally:
        현재_org.reset(토큰)


def _org_세우기(conn) -> None:
    """열린 트랜잭션에 `app.org_id` 를 «트랜잭션 한정» 으로 건다.

    🔴 **`SET LOCAL` 이 아니라 `set_config(..., true)` 를 쓴다.** SET 문법은 바인딩
       파라미터를 못 받는다(`SET LOCAL app.org_id = %s` → SyntaxError, 실측).
       그래서 SET 을 쓰려면 값을 문자열로 이어붙여야 하는데 그건 SQL 인젝션이다.
       `set_config` 는 «함수» 라 파라미터가 들어간다. 세 번째 인자 `true` 가 «local».

    🔴 세 가지를 재고 이 조합을 골랐다:
         local=true  + 트랜잭션 넘김   → 안 샌다 ✅
         local=false (세션 GUC)        → **샌다** — 커넥션을 재사용하면 다음 요청이
                                          앞 요청의 org 를 그대로 본다 🔴
         autocommit=True + local=true  → 다음 «문장» 에 이미 없다. 격리가 아니라
                                          «전부 차단» 이 된다 (조용히 0행)
       지금은 호출마다 새 커넥션이라 누수가 구조적으로 없지만, 풀을 넣는 날
       local=false 였다면 그날 바로 TENANT_LEAK 이다. 지금 못 박아 둔다.

    ⚠️ 주체가 없으면(게스트·자기신고) **아무것도 안 세운다.** 그러면 `current_org()`
       가 NULL 이라 RLS 가 쓰기를 막고 읽기는 0행이 된다 — 열어 두는 것보다 낫다
       (「모든 실패의 기본값은 판단불가」). 이 선택의 부작용은 보고서 참조.
    """
    org = 현재_org.get()
    if org is None:
        return
    conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org),))


# ── DB ──────────────────────────────────────────────────────────────

def _질의(sql: str, 인자: tuple = (), *, 예외전파: bool = False) -> list[tuple]:
    """DB 가 없어도 서버는 떠야 한다. 실패하면 빈 리스트.

    🔴 조용히 빈 리스트를 주므로, «0건» 을 «데이터 없음» 으로 읽지 말 것.
       모드 확인은 `/api/health` 의 `모드` 로 한다.

    🔴 **`예외전파=True` 는 그 삼킴을 «호출부별로» 끈다** (2026-09-03).
       삼킴이 해로운 자리가 하나 있다 — `auth._계정조회()` 다. 거기서 빈 리스트는
       곧 403「등록되지 않은 계정이다」인데 **DB 가 죽어도 똑같이 빈 리스트**라,
       DB 장애가 사용자에게 「너는 등록 안 됐다」로 보였다 (실측: DSN 을 죽은
       포트로 돌려도 미등록 email 과 상태코드·문구가 «완전히» 같다).
       판단불가율을 «모델선택/실패경로» 로 갈라 세는 것과 같은 문제다 —
       한 값이면 로그에서 영영 안 갈린다.

       🔴 기본값이 False 인 이유: 이 함수는 호출부가 37곳이다. 시그니처를 통째로
          「예외를 던진다」로 뒤집으면 그 36곳이 전부 500 으로 바뀐다. 그래서
          **필요한 쪽이 켜는** 방향으로 했다 — 기존 호출부는 한 줄도 안 바뀐다.
    """
    try:
        import psycopg
        # 🔴 autocommit 을 켜지 마라 — `_org_세우기` 의 트랜잭션 한정 GUC 가 다음
        #    문장에 이미 사라져 «전부 0행» 이 된다 (docstring 참조). 기본값이 False 다.
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            _org_세우기(conn)          # 🔴 질의와 «같은 트랜잭션» 이어야 한다
            return conn.execute(sql, 인자).fetchall()
    except Exception:                                         # noqa: BLE001
        if 예외전파:
            raise
        return []


def _실행(sql: str, 인자: tuple = ()) -> int:
    """INSERT/UPDATE 용. 성공하면 rowcount, 실패하면 -1."""
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            _org_세우기(conn)          # 🔴 INSERT/UPDATE 와 «같은 트랜잭션». commit 전이다
            cur = conn.execute(sql, 인자)
            conn.commit()
            return cur.rowcount
    except Exception:                                         # noqa: BLE001
        return -1


# ── SSE ─────────────────────────────────────────────────────────────

def _sse(이름: str, 값: Any) -> str:
    return f"event: {이름}\ndata: {json.dumps(값, ensure_ascii=False, default=str)}\n\n"


def _sse응답(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive",
        "X-Accel-Buffering": "no",           # nginx 뒤에서 SSE 가 버퍼링에 갇히는 걸 막는다
    })


# ── 할일 유형 — 캘린더 배지 ─────────────────────────────────────────
#
# 🔴 2026-09-01: 여기 있던 `유형_추정()` 은 **지웠다.**
#    `corpus.check_items` 에 `유형` 컬럼이 생겼다 (ai-25, CHECK IN ('기타','계약','비교견적'),
#    실측 분포 기타50·비교견적1·계약1 — 이 함수의 하드코딩과 값은 같았다).
#    컬럼이 이기는 이유는 값이 달라서가 아니라 **마스터가 52→60 으로 늘 때 코드 분류표는
#    갱신이 빠지고, 표는 DEFAULT '기타' 로 조용히 안 틀리기 때문**이다.
#    지금은 `routes_tasks._실_동기화` 가 코드 집합으로 유형 맵을 한 번 떠서 쓴다.
