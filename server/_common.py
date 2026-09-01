# -*- coding: utf-8 -*-
"""서버 공용 — 라우터가 `main.py` 를 import 하면 순환참조가 난다. 그래서 여기로 뺐다.

🔴 **공용 계약 파일이다.** 값을 추가하려면 먼저 합의할 것 — 여러 계통이 같은 파일을 쓴다.

담는 것은 «어느 라우터에서도 필요한데 정의가 한 벌이어야 하는 것» 뿐이다:
DB 접속·질의, 목/실 모드 스위치, 비목·판정 enum, SSE 포맷터.
"""
from __future__ import annotations

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


# ── DB ──────────────────────────────────────────────────────────────

def _질의(sql: str, 인자: tuple = ()) -> list[tuple]:
    """DB 가 없어도 서버는 떠야 한다. 실패하면 빈 리스트.

    🔴 조용히 빈 리스트를 주므로, «0건» 을 «데이터 없음» 으로 읽지 말 것.
       모드 확인은 `/api/health` 의 `모드` 로 한다.
    """
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            return conn.execute(sql, 인자).fetchall()
    except Exception:                                         # noqa: BLE001
        return []


def _실행(sql: str, 인자: tuple = ()) -> int:
    """INSERT/UPDATE 용. 성공하면 rowcount, 실패하면 -1."""
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=3) as conn:
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
