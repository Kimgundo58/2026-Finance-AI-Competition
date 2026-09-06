# -*- coding: utf-8 -*-
"""「써도돼요」 백엔드 — `프론트 연동 사양.md` §8 계약 구현.

🔴 **목(mock) 이 먼저다.** 고정 JSON 만 돌려줘도 팀원이 화면 5·6·7 을 끝까지 만들 수 있다.
   그게 프론트 병렬화의 열쇠다 (`프론트 연동 사양.md` §11 순위 1). A 의 `orchestrate.판정()`
   이 준비되면 환경변수 하나로 실호출로 갈아끼운다 — 프론트 코드는 한 줄도 안 바뀐다.

    SUDDOE_MOCK=1  (기본)  고정 JSON. DB·GPU 없이 뜬다
    SUDDOE_MOCK=0          normalize_run.정규화() + orchestrate.판정() 실호출

엔드포인트

    GET  /api/health                  살아있음 + 모드
    GET  /api/vocab                   비목 enum 10종 (§9)
    GET  /api/programs                사업 8종 + 별칭 (§10 확인필요 #1 의 재료)
    POST /api/normalize   SSE         화면 3 → 4
    POST /api/judge       SSE         화면 4 → 5 / 6 / 7
    GET  /api/profile   PUT           F 프로필 (§7)
    GET  /admin/cost                  비용 가드 상태 (H6)
    GET  /admin/gate                  f_axis 차단 로그 (H5)
    GET  /admin/queue                 재검수 큐 — 「제N차 개정 · 변경 M개 조항」 화면 재료
    POST /admin/warmup                발표 30분 전 워밍업 (H6)

⚠️ **현물이 없다** (2026-08-31 확정). `f1` 은 4칸이 아니라 **2칸**이다 —
   `{정부지원_현금, 자기부담_현금}` + 협약기간. `f3` 에 `형태` 가 없다.
   현물 계상은 지출이 아니고 우리 서비스는 "이 돈 써도 되나" 를 판정한다.

🔴 **PMS 원문·실명은 서버로 오지 않는다** (`프론트 연동 사양.md` §7).
   브라우저에서 파싱해 확인된 숫자만 보낸다. 서버는 받을 자리를 만들지 않는다.

실행:
    PYTHONIOENCODING=utf-8 python -m uvicorn server.main:app --reload --port 8080
    PYTHONIOENCODING=utf-8 python server/main.py --selftest      # 서버 없이 계약 검증
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

_log = logging.getLogger("suddoe.main")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))          # `python server/main.py` 로도 server 패키지가 보이게

# 🔴 공용 정의는 `server/_common.py` · 계약은 `server/models.py` 가 기준이다 (2026-09-01 동결).
#    라우터가 main 을 import 하면 순환참조가 나서 밖으로 뺐다. 여기서 다시 정의하지 말 것.
from server._common import (DSN, MOCK, _sse, _sse응답, _실행, _질의,  # noqa: E402
                            비목_ENUM, 판정_ENUM, 창업활동비_사업 as _창업활동비_사업)
from server.models import (F1, F3항, F4항, F5, 정규화요청,          # noqa: E402
                           판정요청, 프로필)
from server import inquiry                                       # noqa: E402
from server import (auth, gpu_watchdog, routes_l3, routes_orgs,     # noqa: E402
                    routes_plans, routes_tasks)

_워치독 = gpu_watchdog.워치독


# ════════════════════════════════════════════════════════════════════
# H6. 비용 방어 (`Agent.md` §9 마지막 문단)
# ════════════════════════════════════════════════════════════════════

def _int환경(키: str, 기본: int) -> int:
    try:
        return int(os.environ.get(키, 기본))
    except ValueError:
        return 기본


class 비용가드:
    """공개 URL + GPU 서빙의 지갑을 지킨다.

    막는 것 넷 — 전부 `Agent.md` §9 가 이름을 붙여둔 것들이다:
      ① 일일 총 호출 하드 캡     잔액이 밤새 증발하는 걸 막는다
      ② IP 시간당 제한           한 사람이 캡을 통째로 먹는 걸 막는다
      ③ 동일 질문 캐시           심사위원 여럿이 같은 예시를 누른다. 그게 다수다
      ④ 심사 기간만 개방         창이 닫히면 GPU 호출 자체가 안 나간다

    🔴 캐시 열쇠는 **해시**다. 원문·F5 체크박스 값을 메모리에 들고 있지 않는다 —
       F5 는 "판정 후 폐기"(`서비스 아키텍쳐.md` §6)라 캐시가 그 규칙을 깨면 안 된다.
    🔴 org_id 가 열쇠에 들어간다. 안 넣으면 A 기관의 판정이 B 기관에 나간다 (TENANT_LEAK).
    """

    def __init__(self) -> None:
        self.일일캡 = _int환경("SUDDOE_DAILY_CAP", 2000)
        self.IP시간당 = _int환경("SUDDOE_IP_HOURLY", 60)
        self.캐시TTL = _int환경("SUDDOE_CACHE_TTL", 900)
        self.캐시상한 = _int환경("SUDDOE_CACHE_MAX", 512)
        self.개방시작 = os.environ.get("SUDDOE_OPEN_FROM", "").strip()
        self.개방종료 = os.environ.get("SUDDOE_OPEN_UNTIL", "").strip()

        self._날짜 = self._오늘()
        self.오늘_호출 = 0
        self._ip: dict[str, deque] = {}
        self._캐시: dict[str, tuple[float, Any]] = {}
        self.적중 = 0
        self.차단: dict[str, int] = {}
        self.워밍업시각: str | None = None

    @staticmethod
    def _오늘() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── ④ 개방 창 ────────────────────────────────────────────────
    def 열려있나(self) -> tuple[bool, str]:
        지금 = datetime.now(timezone.utc)
        for 값, 방향 in ((self.개방시작, "전"), (self.개방종료, "후")):
            if not 값:
                continue
            try:
                t = datetime.fromisoformat(값)
            except ValueError:
                continue                      # 형식이 틀리면 제한 없음으로 본다
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            if (방향 == "전" and 지금 < t) or (방향 == "후" and 지금 > t):
                return False, f"심사 기간이 아닙니다 (개방 {self.개방시작 or '-'} ~ {self.개방종료 or '-'})"
        return True, ""

    # ── ①② 세기 ─────────────────────────────────────────────────
    def 통과(self, ip: str) -> tuple[bool, str]:
        열림, 사유 = self.열려있나()
        if not 열림:
            self.차단["개방창"] = self.차단.get("개방창", 0) + 1
            return False, 사유
        if self._날짜 != self._오늘():          # 자정 롤오버
            self._날짜, self.오늘_호출, self._ip = self._오늘(), 0, {}
        if self.오늘_호출 >= self.일일캡:
            self.차단["일일캡"] = self.차단.get("일일캡", 0) + 1
            return False, f"오늘의 판정 한도({self.일일캡}건)를 모두 썼습니다. 내일 다시 시도해 주세요."
        지금 = time.time()
        q = self._ip.setdefault(ip, deque())
        while q and 지금 - q[0] > 3600:
            q.popleft()
        if len(q) >= self.IP시간당:
            self.차단["IP시간당"] = self.차단.get("IP시간당", 0) + 1
            return False, f"시간당 요청 한도({self.IP시간당}건)를 넘었습니다. 잠시 후 다시 시도해 주세요."
        q.append(지금)
        self.오늘_호출 += 1
        return True, ""

    # ── ③ 캐시 ──────────────────────────────────────────────────
    @staticmethod
    def 열쇠(*조각: Any) -> str:
        raw = json.dumps(조각, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def 꺼내기(self, k: str):
        v = self._캐시.get(k)
        if not v:
            return None
        t, 값 = v
        if time.time() - t > self.캐시TTL:
            self._캐시.pop(k, None)
            return None
        self.적중 += 1
        # 캐시로 답하면 GPU 를 안 쓴 것이다 — 일일 카운트를 되돌려준다
        self.오늘_호출 = max(0, self.오늘_호출 - 1)
        return 값

    def 넣기(self, k: str, 값: Any) -> None:
        if len(self._캐시) >= self.캐시상한:
            self._캐시.pop(next(iter(self._캐시)))     # FIFO. LRU 까지는 필요 없다
        self._캐시[k] = (time.time(), 값)

    def 상태(self) -> dict:
        열림, 사유 = self.열려있나()
        return {"모드": "mock" if MOCK else "live",
                "오늘": self._날짜, "오늘_호출": self.오늘_호출, "일일캡": self.일일캡,
                "IP시간당": self.IP시간당, "추적중인_IP": len(self._ip),
                "캐시": {"항목": len(self._캐시), "TTL초": self.캐시TTL, "적중": self.적중},
                "개방": {"열림": 열림, "사유": 사유,
                        "시작": self.개방시작 or None, "종료": self.개방종료 or None},
                "차단": self.차단, "워밍업": self.워밍업시각}


가드 = 비용가드()

# 🔴 실 판정(`_실_판정`)은 LLM 왕복이 껴서 초 단위로 블로킹한다 — SSE 연결에 그동안
#    바이트가 하나도 안 나가면 프록시·게이트웨이 idle timeout 이 스트림을 끊는다
#    (`X-Accel-Buffering: no` 는 버퍼링만 막지 idle 은 안 막는다). 10~15초 사이로 잡는다.
_하트비트_주기초 = _int환경("SUDDOE_JUDGE_HEARTBEAT_SEC", 12)


def _ip(req: Request) -> str:
    # 프록시 뒤일 수 있다. 신뢰 프록시가 정해지기 전이라 X-Forwarded-For 를 그대로 믿지 않고
    # 첫 홉만 쓰되, 없으면 소켓 주소를 쓴다. (배포 시 프록시 확정되면 이 자리를 고친다)
    xff = req.headers.get("x-forwarded-for", "")
    return (xff.split(",")[0].strip() if xff else None) or (req.client.host if req.client else "?")




# ════════════════════════════════════════════════════════════════════
# 목 픽스처 — 화면 5·6·7 을 끝까지 그릴 수 있는 최소 한 벌
# ════════════════════════════════════════════════════════════════════

_목_정규화 = {
    "품목": "맥북", "금액": 2_500_000, "금액_추정여부": False,
    "용도": "디자이너 작업용",
    "비목후보": [{"비목": "기계장치", "신뢰도": 0.82}, {"비목": "재료비", "신뢰도": 0.31}],
    "결제수단": None, "구매명의": None, "신청일": None, "비교견적": None, "하위항목": None,
    # A-3(2026-09-06) — 목 모드는 check_items 를 안 읽는다. 빈 리스트로 실 모드와 키를 맞춘다
    # (`test_정규화_SSE_가_목과_실에서_같은_키를_준다`). ai-7d 산출이 들어와도 목은 그대로 둔다
    # — 목은 «화면을 끝까지 그릴 최소 한 벌」이지 실 데이터 재현이 아니다.
    "심층질문": [],
}

# 🔴 4-way 를 전부 그려봐야 한다. 판단불가는 에러 화면이 아니라 정상 경로다 (§3).
#    프론트는 ?목=조건부|불가|판단불가 로 갈아끼워 네 화면을 다 만든다.
_목_판정: dict[str, dict] = {
    "가능": {
        "판정": "가능",
        "요약": "1인 1대 한도 내에서 구매 가능합니다.",
        # 🔴 `code` 는 `corpus.check_items` 의 **실제 마스터 값만** 쓴다. 지어내면 화면이
        #    그 code 로 마스터 설명을 조회하다 조용히 빈손이 된다. 마스터에 맞는 항목이
        #    없으면 `code: None` 으로 두고 `구분` 을 안 붙인다 — 실경로 규칙과 같다.
        # 🔴 SSE 는 `code`(영문), 저장 할일(`models.할일`)은 `코드`(한글)다.
        #    **이름을 통일하지 않는다** — 앞은 프론트 계약, 뒤는 DB 컬럼 계약이라 둘 다
        #    정본이다. 「목 키집합 == 실경로 키집합」 회귀 테스트는 이 한 쌍을 예외로 둔다.
        "해야할일": [
            {"code": "비교견적준비", "구분": "결제전", "유형": "기타",
             "항목": "비교견적 2곳 이상 확보", "설명": "50만원 이상 구매는 비교견적을 남깁니다."},
            {"code": "사업비카드사용", "구분": "결제전", "유형": "기타",
             "항목": "사업비 카드로 결제", "설명": "현금·개인카드 결제는 인정되지 않습니다."},
            {"code": "자산등록", "구분": "결제후", "유형": "기타",
             "항목": "자산 등록", "설명": "취득일로부터 30일 이내에 자산으로 등록합니다."},
        ],
        "인용": [
            {"조번호": "제39조", "조제목": "기계장치",
             "원문": "기계장치는 사업계획서에 명시된 범위에서 취득할 수 있으며, 1인 1대를 원칙으로 한다.",
             "doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"},
            {"조번호": "붙임2", "조제목": "비목별 집행기준",
             "원문": "기계장치(공구·기구, 비품, SW 등) — 창업기업이 직접 사용하는 것에 한한다.",
             "doc_id": "예비창업패키지 세부관리기준(2025년)"},
        ],
        "전제": [
            {"사실": "해당 인력이 협약상 참여인력이다", "근거조항": "S14",
             "매핑": ["F4.역할"], "미충족시": "불가"},
        ],
        "신뢰등급": "A",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [
            {"from": {"doc_id": "예비창업패키지 세부관리기준(2025년)", "조번호": "제22조"},
             "표기": "지침 제39조", "관계": "준용",
             "to": {"doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223",
                    "조번호": "제39조", "조제목": "기계장치"},
             "보정": None},
        ],
    },
    "조건부": {
        "판정": "조건부",
        "요약": "사전승인을 받으면 구매할 수 있습니다.",
        "해야할일": [
            # 🔴 마스터에 기계장치 사전승인 code 가 없다 (`seed_check_items.py` 기계장치
            #    4건: 중고개인거래아님·범용SW사전검토·사무용집기아님·자산등록).
            #    없는 code 를 지어내지 않고 `구분` 도 안 붙인다.
            {"code": None,
             "항목": "사전승인 신청", "설명": "100만원 이상 기계장치는 주관기관 사전승인이 필요합니다."},
            {"code": "비교견적준비", "구분": "결제전", "유형": "기타",
             "항목": "비교견적 2곳 이상 확보", "설명": "50만원 이상 구매는 비교견적을 남깁니다."},
        ],
        "인용": [
            {"조번호": "제39조", "조제목": "기계장치",
             "원문": "기계장치는 사업계획서에 명시된 범위에서 취득할 수 있으며, 1인 1대를 원칙으로 한다.",
             "doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"},
        ],
        "전제": [
            {"사실": "협약 총액이 확인되지 않았다", "근거조항": "S07",
             "매핑": ["F1.정부지원_현금"], "미충족시": "판단불가"},
        ],
        "신뢰등급": "B",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [],
    },
    "불가": {
        "판정": "불가",
        "요약": "개인 명의 구매는 사업비로 집행할 수 없습니다.",
        "해야할일": [
            # 🔴 마스터에 「구매 명의」 code 가 없다 (`출원인명의확인` 은 무형자산 전용).
            {"code": None,
             "항목": "법인·사업자 명의로 재구매", "설명": "구매 명의가 사업자와 일치해야 합니다."},
        ],
        "인용": [
            {"조번호": "제36조", "조제목": "사업비의 집행",
             "원문": "사업비는 창업기업 명의의 사업비 관리계좌 및 카드로 집행하여야 한다.",
             "doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"},
        ],
        "전제": [],
        "신뢰등급": "A",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [],
    },
    # 🔴 판단불가는 빨간 에러가 아니다. 화면 9(문의 초안)로 이어지는 안내다 (§3).
    "판단불가": {
        "판정": "판단불가",
        "요약": "이 지출을 판정할 근거 조항을 찾지 못했습니다. 주관기관에 문의가 필요합니다.",
        "해야할일": [],
        "인용": [],
        "전제": [
            {"사실": "협약 총액이 확인되지 않았다", "근거조항": None,
             "매핑": ["F1.정부지원_현금"], "미충족시": "판단불가"},
            {"사실": "해당 품목의 비목이 확정되지 않았다", "근거조항": None,
             "매핑": [], "미충족시": "판단불가"},
        ],
        "신뢰등급": "B",
        "버전스탬프": "제14차, 2025.12.23 기준",
        "참조사슬": [],
        "문의초안": ("안녕하세요. 예비창업패키지 참여기업입니다.\n\n"
                    "디자이너 업무용 노트북(250만원) 구매가 사업비로 집행 가능한지 문의드립니다.\n"
                    "통합관리지침 제39조(기계장치)의 '1인 1대' 기준에서 참여인력의 범위를 "
                    "어떻게 보아야 하는지 확인이 필요합니다.\n\n확인 부탁드립니다. 감사합니다."),
    },
}

_목_프로필 = {"f1": F1().model_dump(), "f3": [], "f4": []}


# ════════════════════════════════════════════════════════════════════
# 앱
# ════════════════════════════════════════════════════════════════════

app = FastAPI(title="써도돼요 API", version="0.1.0",
              description="창업지원금 지출비 사전승인 판정. `프론트 연동 사양.md` §8 계약.")

# ── 인증·테넌트 귀속 (2026-09-03 배선) ──────────────────────────────────
# 🔴 CORS 보다 «먼저» add 한다. add_middleware 는 마지막에 넣은 것이 «바깥» 이라,
#    이 순서라야 CORS 가 바깥에 서서 401·503 응답에도 Access-Control-Allow-Origin 이
#    붙는다. 반대로 두면 인증 거부가 CORS 밖으로 나가 브라우저 콘솔엔 「CORS 차단」
#    으로 찍히고, 프론트는 «인증 문제» 라는 걸 영영 모른다. (검증 세션 실측:
#     CORS→auth 순서 = 401 에 ACAO «없음» / auth→CORS 순서 = 401 에 ACAO 있음)
# 🔴 목 모드에선 붙이지 않는다. 목 서버엔 SUDDOE_JWKS_URL 도 DB 도 없어서, 로그인한
#    프론트가 보내는 Bearer 를 검증하려다 «/api 전 경로»가 503(JWKS 미설정) 또는
#    403(계정 조회 빈 결과)이 된다. 목은 지금처럼 ?org_id= 자기신고로 돈다.
if not MOCK:
    app.add_middleware(auth.OrgId주입)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get(
        "SUDDOE_CORS", "http://localhost:3000,http://localhost:5173").split(",") if o],
    allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

# ── 라우터 (2026-09-01 분해) ────────────────────────────────────────────
# 계통이 갈린다. 지출계획=routes_plans · 할일=routes_tasks · L3=routes_l3 + 아래 normalize.
#    남의 라우터 파일을 고치지 말 것 — 훅이 막고, 막히기 전에 이미 충돌한다.
app.include_router(routes_plans.router)
app.include_router(routes_tasks.router)
app.include_router(routes_l3.router)
app.include_router(routes_orgs.router)         # 기관 목록 — org_id 를 «안» 싣는다
app.include_router(gpu_watchdog.router)        # /api/gpu/status · keepalive

# 🔴 IDLE_MIN=0 이면 스레드조차 만들지 않는다. 목 모드 가드는 gpu_watchdog 안에 있다
#    — 목 서버는 GPU 를 안 부르니 «영원히 유휴» 라, 가드가 없으면 30분 뒤 목이
#    실 팟에 stop 을 쏜다 (S4 가 재현·차단 확인).
_워치독.시작_루프()


def _관리자(token: str | None) -> None:
    """🔴 fail closed. 토큰이 설정 안 돼 있으면 관리 엔드포인트는 열리지 않는다."""
    기대 = os.environ.get("SUDDOE_ADMIN_TOKEN", "")
    if not 기대 or token != 기대:
        raise HTTPException(403, "관리자 토큰이 필요합니다 (SUDDOE_ADMIN_TOKEN)")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "모드": "mock" if MOCK else "live",
            "판정_enum": list(판정_ENUM), "시각": datetime.now(timezone.utc).isoformat()}


# ── 사업명 정본화 ────────────────────────────────────────────────────
#
# 🔴 **진입점에서만 한다.** 다운스트림 250여 곳은 전부 파라미터화 쿼리라
#    모르는 표기가 들어가면 «조용한 0행» 으로 똑같이 실패한다 — 그래서 막을 자리가
#    흩어져 있지 않고 여기 둘(`사업명` 쿼리파라미터 · `body.사업명`)뿐이다.
#
# 🔴 **왜 조용히 통과시키면 안 되나** (2026-09-02, ai-68 추적):
#    `rule_lookup.비목계통()` 은 `corpus.programs` 에서 못 찾으면 **조용히 「창업」으로**
#    떨어진다. 그러면 TIPS 질문이 창업 계통 L1 지침으로 분류돼 **근거를 달고 틀린 답**이
#    나간다. 판단불가가 아니라 «근거 있는 척하는 오답» 이라 제일 나쁘다.
#
# 🔴 **정규화가 흡수하는 것과 못 하는 것을 갈라 둔다.**
#    흡수: 「2026 」 연도 접두사 · 공백 · 가운뎃점 흔들림(U+30FB ・ → U+00B7 ·).
#          프론트 번들에 두 코드포인트가 실제로 섞여 있다(실측). 어떤 유니코드 정규화로도
#          안 합쳐지므로(NFC/NFD/NFKC/NFKD 전부 확인) **명시적 치환**으로 처리한다.
#    못 함: 「모두의 창업 일반・기술」↔「모두의 창업 프로젝트」처럼 **이름 자체가 다른 것**.
#          그건 정규화가 아니라 **별칭**이라 `corpus.programs.별칭` 에 넣어야 한다.
#          지금 프론트 8종 중 3종이 여기 걸린다 — 별칭이 들어오면 자동으로 풀린다.

def _사업명_키(값: str) -> str:
    """비교 전용 키. 표시용으로 쓰지 말 것."""
    s = re.sub(r"^\s*20\d{2}\s+", "", (값 or "").strip())   # 「2026 」 접두사
    return re.sub(r"\s+", "", s.replace("・", "·"))          # 공백 제거 · 가운뎃점 통일


_사업명_캐시: tuple[float, dict[str, str]] | None = None
_사업명_캐시TTL = _int환경("SUDDOE_PROGRAM_TTL", 300)


def _사업명_표(강제새로: bool = False) -> dict[str, str]:
    """`키 → 정본`. 🔴 DB 를 못 읽으면 **빈 dict** — 호출부가 모드를 보고 판단한다.

    🔴 **캐시한다.** `_질의()` 는 호출마다 psycopg 접속을 새로 열어 8행 테이블에
       요청당 **약 25ms** 를 쓴다(실측). judge·normalize·vocab 이 전부 이걸 부르는데
       판정 1건 = LLM 2회라 지연 예산이 빠듯하다. `corpus.programs` 는 8행이고
       거의 안 바뀐다 — TTL 캐시가 맞는 자리다.
       🔴 **빈 표는 캐시하지 않는다.** DB 가 깜빡인 순간을 5분간 물고 있으면
       그 사이 전 요청이 503 이 된다. 실패는 다음 요청에서 다시 시도하게 둔다.
    """
    global _사업명_캐시
    if not 강제새로 and _사업명_캐시 is not None:
        받은시각, 표 = _사업명_캐시
        if time.time() - 받은시각 < _사업명_캐시TTL:
            return 표
    표: dict[str, str] = {}
    for 정본, 별칭들 in _질의('SELECT "사업명", "별칭" FROM corpus.programs WHERE "활성"'):
        for v in [정본, *(별칭들 or [])]:
            표[_사업명_키(v)] = 정본
    if 표:                              # 🔴 성공한 것만 캐시한다
        _사업명_캐시 = (time.time(), 표)
    return 표


def _사업명_정본(값: str | None) -> str | None:
    """모르는 표기면 422 로 **명시적으로 거부**한다. 조용히 통과시키지 않는다.

    🔴 **2026-09-02 정정 — 처음엔 「표가 비면 통과」로 짰는데 그게 구멍이었다.**
       `_질의()` 는 `except Exception: return []` 라 **DB 3초 미응답도 빈 표**를 준다.
       그래서 「표가 비었다」는 «목 모드»(정상)와 «DB 가 깜빡였다»(사고)를 **구분하지 못한다.**
       실증: 라이브 모드에서 `_질의` 를 []로 막으니 모르는 표기가 **200 으로 통과**했다 —
       닫으려던 그 구멍(`비목계통()` 의 조용한 「창업」 폴백 → 근거 달린 오답)이 다시 열린다.

       → 조건을 «표가 비었나» 가 아니라 **«MOCK 인가»** 로 바꾼다. 추측된 상태가 아니라
         **명시된 상태**를 본다. 라이브인데 표가 비면 그건 통과가 아니라 **503** 이다.
         (감독 세션 ai-da 지적, 2026-09-02)
    """
    if not 값:
        return 값
    if MOCK:
        return 값                      # 목은 DB 가 없는 게 정상이다. 판단하지 않는다
    표 = _사업명_표()
    if not 표:
        # 🔴 라이브인데 사업 목록을 못 읽었다 — 「모르는 사업」이 아니라 «판단할 수 없음» 이다.
        #    통과시키면 아래 층이 조용히 틀린 계통으로 떨어진다. 여기서 멈춘다.
        raise HTTPException(503, "사업 목록을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.")
    정본 = 표.get(_사업명_키(값))
    if 정본 is None:
        raise HTTPException(422, f"모르는 사업명입니다: {값!r}. "
                                 f"아는 사업: {sorted(set(표.values()))}")
    return 정본


# ── 비목 정본화 ──────────────────────────────────────────────────────
#
# 🔴 **DB 를 고칠 게 없다. 이미 다 들어 있고 서버가 안 봤을 뿐이다.**
#    `corpus.item_vocab.별칭` 에 프론트 표기가 이미 있다(실측 10/10 해석·충돌 0).
#    `main.py` 의 `확정비목 not in 비목_ENUM` 검사가 **정본만** 보고 422 를 던졌다.
#
# 🔴 **어느 쪽이 «맞다» 로는 못 푼다 — 답이 둘로 갈린다** (ai-68 원문 확인):
#      기계장치            세부관리기준·모집공고 3벌 전부 「기계장치」.
#                         「기계장치비」는 확인한 5건 어디에도 없다 → 프론트가 만든 말
#      특허권 등 무형자산 취득비  원문에 띄어쓰기가 **있다** → 프론트가 원문 그대로고
#                         DB 가 조인 키를 위해 공백을 압축한 쪽이다
#    한쪽을 정답으로 정하면 다른 하나가 틀린다. 그래서 **별칭으로 흡수**한다.
#
# ⚠️ **지금 «우연히» 안 터지는 5건이 있다.** 광고선전비·교육훈련비·외주용역비·
#    지급수수료·창업활동비는 세부관리기준 원문에 공백이 있다(「광고 선전비」…).
#    DB 와 프론트가 둘 다 공백 없이 써서 맞는 것뿐이다 — 프론트가 어느 화면에서 원문
#    표기를 쓰면 같은 422 가 난다. **공백 접기**를 2차 그물로 둬서 그 5개를 같이 닫는다.
#    (「창업 활동비」는 별칭에도 없어서 접기가 유일한 그물이다 — 실측)

def _비목_키(값: str) -> str:
    return re.sub(r"\s+", "", (값 or "").strip())


_비목_캐시: tuple[float, dict[str, str]] | None = None


def _비목_표() -> dict[str, str]:
    """`키 → 정본`. 정본·별칭 둘 다 담고, **공백 접은 키도 같이** 담는다.

    공백 접기 후 충돌 0건을 실측하고 넣었다 — 접었을 때 두 정본을 가리키는 표기가 없다.
    🔴 사업명 표와 같은 이유로 캐시한다(요청당 새 psycopg 접속). 빈 표는 캐시하지 않는다.
    """
    global _비목_캐시
    if _비목_캐시 is not None:
        받은시각, 표 = _비목_캐시
        if time.time() - 받은시각 < _사업명_캐시TTL:
            return 표
    표: dict[str, str] = {}
    for 정본, 별칭들 in _질의(
            'SELECT "비목", "별칭" FROM corpus.item_vocab WHERE "계통"=\'창업\''):
        for v in [정본, *(별칭들 or [])]:
            표[v] = 정본                    # 원문 그대로
            표.setdefault(_비목_키(v), 정본)  # 공백 접은 2차 그물
    if 표:
        _비목_캐시 = (time.time(), 표)
    return 표


def _비목_정본(값: str | None) -> str | None:
    """별칭·공백 흔들림을 흡수해 정본으로. 모르면 원값을 그대로 돌려준다.

    🔴 여기서 422 를 던지지 않는다 — 호출부(`judge`)의 `비목_ENUM` 검사가 이미
       그 일을 하고 있고, **메시지가 「enum 10종 뿐입니다」로 더 친절하다.**
       이 함수는 «풀 수 있으면 풀어준다» 까지만 한다. 못 풀면 원값이 그대로 가서
       기존 422 를 그대로 맞는다 — 동작이 안 바뀌는 게 맞다.
    🔴 DB 를 못 읽으면(목 모드·미기동) 표가 비어 원값이 그대로 간다. 사업명 때와 같다.
    """
    if not 값:
        return 값
    표 = _비목_표()
    return 표.get(값) or 표.get(_비목_키(값)) or 값


# ── 판정 캐시 — DB (Q5, 2026-09-04) ──────────────────────────────────
#
# 🔴 Cloud Run 재배포로 날아가던 `비용가드._캐시`(인메모리)를 인증된 요청(org 있음)에
#    한해 DB(`tenant.judge_cache`)로 옮긴다. 키 설계·굵기 판단·설정_해시 무효화 축의
#    이유는 `db/init/13_judge_cache.sql` 머리말이 정본이다 — 여기는 그걸 코드로 잇는다.
#
# 🔴 **게스트(org 없음)는 그대로 인메모리다 — DB 로 안 옮긴다.**
#    `tenant.*` RLS 정책이 `org_id = current_org()` 라 NULL 은 어느 쪽이든 통과하지
#    못한다(`10_rls_guc.sql` 미결 ②, 정책은 오너 결정 — 여기서 새로 풀지 않는다).
#    게스트 캐시는 재배포로 날아가도 안전(다음 요청이 다시 계산)하니 굳이 그 정책
#    논쟁을 지금 열 이유가 없다. `--selftest` 의 게스트 캐시 검증도 이 경로를 탄다 —
#    바꾸면 그 검증이 깨진다.

_설정_해시_캐시: tuple[float, str] | None = None
_설정_해시_TTL = _int환경("SUDDOE_CONFIG_HASH_TTL", 300)


def _설정_해시(강제새로: bool = False) -> str:
    """캐시 무효화 축. 코퍼스·룰이 바뀌면 이 값이 바뀌어 캐시 조회가 미스로 본다.

    `scripts/eval_store.코퍼스버전()` 과 같은 발상(청크수·임베딩수·refs수·문서수·
    최대chunk_id)에 룰 축을 더했다 — 캐시는 룰 재검수도 알아야 한다.
    조건이 다른 run 을 안 섞는 것과 같은 이유다(CLAUDE.md 「지표를 읽을 때」).

    🔴 **2026-09-04 정정(ai-c5 지적) — 룰 축이 처음엔 개수 둘(룰수·검수룰수)뿐이었다.**
       그러면 **칸 단위 UPDATE**(행수·verified 는 그대로 두고 `금지예시`·`허용` 같은
       내용만 고치는 수정 — rule 440 「틀린 불가」 수정이 정확히 이 모양이다)는
       해시를 안 바꾼다 → 캐시가 계속 적중 → **룰을 고쳐도 프론트는 옛 판정을 계속 본다.**
       그래서 판정에 실제로 쓰이는 컬럼 전부를 rule_id 순으로 이어붙여 md5 한다
       (`corpus.rules` 실측 컬럼명 — 배열은 `array_to_string`, `근거` 는 jsonb 라 `::text`).
       한 칸만 바뀌어도 이 문자열이 달라진다 — 개수 축과 다른 자리다.
    🔴 TTL 로 캐시한다 — 다른 `_*_캐시` 들과 같은 이유(요청당 새 psycopg 접속 25ms).
       DB 를 못 읽으면 `"unknown"` 을 주고 **캐시하지 않는다** — DB 가 돌아오면
       바로 다시 잰다. `unknown` 으로 저장된 캐시 행은 없다(넣기 전에 매번 다시 잰다).
    """
    global _설정_해시_캐시
    if not 강제새로 and _설정_해시_캐시 is not None:
        받은시각, 값 = _설정_해시_캐시
        if time.time() - 받은시각 < _설정_해시_TTL:
            return 값
    행 = _질의("""SELECT (SELECT count(*) FROM corpus.chunks),
                        (SELECT count(*) FROM corpus.chunks WHERE embedding IS NOT NULL),
                        (SELECT count(*) FROM corpus.refs),
                        (SELECT count(*) FROM corpus.documents),
                        (SELECT coalesce(max(chunk_id), 0) FROM corpus.chunks),
                        (SELECT count(*) FROM corpus.rules),
                        (SELECT count(*) FILTER (WHERE verified) FROM corpus.rules),
                        (SELECT coalesce(md5(string_agg(
                             rule_id::text || '|' || coalesce("사업명",'') || '|' ||
                             coalesce(비목,'') || '|' || 허용 || '|' ||
                             사전승인::text || '|' ||
                             -- 🔴 2026-09-06 배열화 — TEXT[] 가 됐다. 원소 순서(삽입·append
                             -- 순서)가 흔들리면 «내용은 같은데 문자열이 달라» 캐시가 매번
                             -- 깨진다. 정렬해서 순서를 고정한다(원소 순서를 계약으로
                             -- 강제하는 대신 — 그쪽은 쓰기 경로마다 지켜야 해서 더 깨지기 쉽다).
                             coalesce(array_to_string(
                                 (SELECT array_agg(x ORDER BY x)
                                    FROM unnest(사전승인_조건) AS x), '~'), '') || '|' ||
                             coalesce(한도_유형,'') || '|' || coalesce(한도_값::text,'') || '|' ||
                             coalesce(한도_단위,'') || '|' ||
                             coalesce(array_to_string(증빙,'~'),'') || '|' ||
                             coalesce(array_to_string(금지예시,'~'),'') || '|' ||
                             coalesce(array_to_string(허용예시,'~'),'') || '|' ||
                             근거::text || '|' || verified::text,
                             '#' ORDER BY rule_id)), 'empty')
                         FROM corpus.rules)""")
    if not 행:
        return "unknown"
    n = 행[0]
    h = hashlib.sha1("|".join(map(str, n)).encode()).hexdigest()[:10]
    값 = f"c{n[0]}-e{n[1]}-r{n[2]}-d{n[3]}-rule{n[5]}v{n[6]}-rc{str(n[7])[:8]}-{h}"
    _설정_해시_캐시 = (time.time(), 값)
    return 값


def _판정캐시_꺼내기(k: str, org: str | None) -> Any | None:
    """org 가 있으면 `tenant.judge_cache`(DB), 없으면 `비용가드._캐시`(인메모리)."""
    if org is None:
        return 가드.꺼내기(k)
    행 = _질의(
        'SELECT value FROM tenant.judge_cache '
        'WHERE key=%s AND org_id=%s AND expires_at > now() AND 설정_해시=%s',
        (k, org, _설정_해시()))
    if not 행:
        return None
    가드.적중 += 1
    # 캐시로 답하면 GPU 를 안 쓴 것이다 — 일일 카운트를 되돌려준다 (비용가드.꺼내기 와 동일 규칙)
    가드.오늘_호출 = max(0, 가드.오늘_호출 - 1)
    return 행[0][0]


def _판정캐시_넣기(k: str, 값: Any, *, 종류: str, org: str | None) -> None:
    if org is None:
        가드.넣기(k, 값)
        return
    _실행(
        'INSERT INTO tenant.judge_cache (key, org_id, 종류, value, 설정_해시, expires_at) '
        'VALUES (%s,%s,%s,%s,%s, now() + make_interval(secs => %s)) '
        'ON CONFLICT (key) DO UPDATE SET '
        '  value = EXCLUDED.value, 설정_해시 = EXCLUDED.설정_해시, '
        '  expires_at = EXCLUDED.expires_at, created_at = now()',
        (k, org, 종류, routes_plans._jsonb(값), _설정_해시(), 가드.캐시TTL))


@app.get("/api/vocab")
def vocab(사업명: str | None = None) -> dict:
    """비목 enum 10종. 🔴 창업활동비는 예비창업패키지에만 있다 (§9)."""
    사업명 = _사업명_정본(사업명)          # 🔴 `_창업활동비_사업` 비교도 정본 기준이라 먼저 푼다
    목록 = [b for b in 비목_ENUM
            if b != "창업활동비" or 사업명 is None or 사업명 in _창업활동비_사업]
    별칭: dict[str, list[str]] = {}
    for r in _질의("SELECT \"비목\", \"별칭\" FROM corpus.item_vocab WHERE \"계통\"='창업'"):
        별칭[r[0]] = list(r[1] or [])
    return {"비목": 목록, "별칭": 별칭 or None, "사업명": 사업명,
            "비고": "프론트 라벨을 이 문자열 그대로 맞춘다. 별칭 매핑은 서버가 한다"}


@app.get("/api/programs")
def programs() -> dict:
    """사업 목록. 게스트에게 화면 3 에서 사업을 묻기 위한 재료 (§10 확인필요 #1).

    룰 조회 키가 `사업 × 비목` 이라 사업을 모르면 한도를 못 고른다 — 그래서
    게스트에게도 물어야 한다. 그 UI 결정의 데이터는 여기가 준다.
    """
    행 = _질의('SELECT "사업명", "별칭", "비목계통", "트랙범위" FROM corpus.programs '
              'WHERE "활성" ORDER BY "사업명"')
    if 행:
        return {"사업": [{"사업명": a, "별칭": list(b or []), "비목계통": c, "트랙범위": d}
                        for a, b, c, d in 행]}
    return {"사업": [{"사업명": n, "별칭": [], "비목계통": "창업", "트랙범위": None} for n in (
        "예비창업패키지", "초기창업패키지", "재도전성공패키지", "창업도약패키지",
        "창업중심대학", "초격차 스타트업 프로젝트", "모두의 창업 프로젝트", "TIPS")],
        "비고": "corpus.programs 를 못 읽어 코드 상수로 답했다"}


@app.post("/api/normalize")
def normalize(req: Request, body: 정규화요청):
    """화면 3 → 4. SSE.

    이벤트 순서: `진행`* → `결과`(전체 JSON) → `완료`
    프론트가 단순하게 가고 싶으면 `결과` 하나만 들어도 된다.
    """
    ok, 사유 = 가드.통과(_ip(req))
    if not ok:
        raise HTTPException(429, 사유)

    # 🔴 캐시 열쇠를 만들기 «전» 에 정본화한다 — 안 그러면 「2026 초기창업패키지」와
    #    「초기창업패키지」가 서로 다른 열쇠가 돼 같은 질문이 두 번 돈다.
    body.사업명 = _사업명_정본(body.사업명)

    # F5 는 판정 후 폐기다. 캐시 열쇠에는 해시로만 들어간다
    # 🔴 폼 경로에서는 질문이 None 이다. 폼 값도 열쇠에 넣는다
    k = 비용가드.열쇠("normalize", (body.질문 or "").strip(),
                     body.품목, body.금액, body.용도, body.사업명,
                     body.f5.친족거래, body.f5.전직임직원업체)
    _org = _주체org(req)
    캐시 = _판정캐시_꺼내기(k, _org)

    def gen():
        yield _sse("진행", {"단계": "정규화", "설명": "질문에서 사실을 뽑는 중"})
        if 캐시 is not None:
            yield _sse("결과", 캐시)
            yield _sse("완료", {"캐시": True})
            return
        # 🔴 첫 진행 이벤트 «뒤» 다. 앞에 두면 팟 상태 조회(타임아웃 15초)가
        #    첫 바이트를 막아 화면이 최대 15초 빈다.
        if not MOCK and body.질문 and body.질문.strip():
            for 진 in _워치독.기동_진행():
                yield _sse("진행", 진)
        try:
            out = dict(_목_정규화) if MOCK else _실_정규화(body)
        except Exception as e:                                # noqa: BLE001
            # 🔴 모든 실패의 기본값은 판단불가다. 500 을 던지지 않는다
            yield _sse("오류", {"메시지": "정규화에 실패했습니다", "종류": type(e).__name__})
            yield _sse("완료", {"실패": True})
            return
        # 목/실 공통 — 질문원문(합성 문장)을 싣는다. 저장·검색·표시 전용이다
        out.setdefault("질문원문", _합성_질문(body))
        _판정캐시_넣기(k, out, 종류="normalize", org=_org)
        for 필드 in ("품목", "금액", "금액_추정여부", "용도", "비목후보"):
            if 필드 in out:
                yield _sse("필드", {필드: out[필드]})
        yield _sse("결과", out)
        yield _sse("완료", {"캐시": False})

    return _sse응답(gen())


def _합성_질문(body: 정규화요청) -> str:
    """폼 값을 문장으로 합성한다. `routes_plans._합성` 과 같은 문장 규칙.

    ⚠️ 이 문장을 다시 LLM 입력으로 쓰지 않는다 (필드→문장→필드 왕복은 정보를 잃는다).
       저장·검색·표시 전용이다.
    """
    if body.질문 and body.질문.strip():
        return body.질문.strip()
    사업 = f"{body.사업명}에서 " if body.사업명 else ""
    만원 = f"{int(body.금액):,}원" if body.금액 is not None else ""
    return f"{사업}{body.용도 or ''} {body.품목 or ''} {만원}을 사도 되나요?".strip()


def _주체org(req: Request) -> str | None:
    """검증된 주체의 org_id. 없으면 None.

    🔴 `auth.OrgId주입` 은 **쿼리스트링만** 갈아끼운다. org_id 가 들어오는 축이 셋인데
       미들웨어가 닿는 건 하나뿐이다:
           쿼리스트링  ?org_id=      → 미들웨어가 교정한다 ✅
           multipart   Form(...)     → 못 닿는다 (routes_l3._업로드_주인 이 따로 막는다)
           요청 본문    body.org_id   → 못 닿는다 ← 여기가 이 함수의 자리
       본문을 미들웨어에서 고치려면 `receive` 를 소진해야 하고 그러면 SSE·업로드가 깨진다.
       라우터가 `scope["suddoe_주체"]` 를 집어 쓰는 쪽이 맞다.
    """
    주 = req.scope.get("suddoe_주체")
    return 주.org_id if 주 is not None and 주.검증됨 else None


@app.post("/api/judge")
def judge(req: Request, body: 판정요청, 목: str | None = None):
    """화면 4 → 5 / 6 / 7. SSE.

    이벤트 순서: `진행`* → `판정` → `해야할일` → `인용` → `전제` → `참조사슬`
                → `결과`(전체 JSON) → `저장`(계획 연결 결과) → `완료`

    목 모드에서 `?목=조건부|불가|판단불가` 로 4-way 를 전부 그려볼 수 있다.
    🔴 판단불가는 에러가 아니라 정상 경로다 — 빨간 화면이 아니라 화면 9 로 잇는다.
    🔴 `저장` 은 부수 효과다 — `plan_id` 가 없거나 캐시 적중이거나 저장이 실패해도
       스트림은 항상 `완료` 까지 간다 (`_판정_저장_시도` 참조).
    🔴 실 판정 대기 중에는 `SUDDOE_JUDGE_HEARTBEAT_SEC`(기본 12초) 마다 SSE 주석
       (`: keep-alive`)이 낀다 — EventSource 가 무시하는 라인이라 이벤트 목록·순서에는
       안 잡힌다. 프록시·게이트웨이의 idle timeout 으로 스트림이 끊기는 걸 막는다.
    """
    ok, 사유 = 가드.통과(_ip(req))
    if not ok:
        raise HTTPException(429, 사유)
    # 🔴 422 «앞» 에서 별칭을 푼다. 「기계장치비」·「특허권 등 무형자산 취득비」처럼
    #    프론트 표기가 정본과 다른 게 둘 있고, 원문 근거상 **어느 한쪽이 맞다고 못 정한다.**
    body.확정비목 = _비목_정본(body.확정비목)
    if body.확정비목 and body.확정비목 not in 비목_ENUM:
        raise HTTPException(422, f"비목은 enum 10종 뿐입니다: {비목_ENUM}")
    # 🔴 비목과 같은 자리에서 닫는다. 여기를 통과하면 `orchestrate.판정()` 이 이 값을
    #    그대로 쓰고, `rule_lookup.비목계통()` 이 모르는 표기를 「창업」으로 삼켜버린다.
    body.사업명 = _사업명_정본(body.사업명)
    # 🔴 토큰이 있으면 본문의 org_id 를 «버린다». 안 하면 판정이 org_id=None 으로 돌아
    #    L3(주관기관 규정)가 아예 안 실리고, 아래 비용가드 열쇠도 org 없이 잡혀
    #    A 기관의 판정이 B 기관에 나간다 (위 열쇠 주석의 TENANT_LEAK 이 그것이다).
    #    프론트는 본문에 org_id 를 안 싣는다 — 지금까지 실 경로가 전부 None 이었다.
    if (_주 := _주체org(req)) is not None:
        body.org_id = _주

    k = 비용가드.열쇠("judge", body.org_id, body.사업명, body.확정비목,
                     json.dumps(body.정규화, ensure_ascii=False, sort_keys=True),
                     body.f5.친족거래, body.f5.전직임직원업체, 목)
    캐시 = _판정캐시_꺼내기(k, body.org_id)

    def gen():
        for 단계, 설명 in (("검색", "관련 조항을 찾는 중"),
                          ("룰조회", "비목별 한도·증빙을 확인하는 중"),
                          ("조립", "판정을 작성하는 중")):
            yield _sse("진행", {"단계": 단계, "설명": 설명})
        if 캐시 is not None:
            out = 캐시
            # 저장 시점에 이미 벗겨낸 뒤에 캐시에 들어간다 — 안전망으로 한 번 더 확인
            _decision_id = out.pop("decision_id", None)
        elif MOCK:
            out = _목_판정.get(목 or "가능", _목_판정["가능"])
            _decision_id = out.pop("decision_id", None)
            _판정캐시_넣기(k, out, 종류="judge", org=body.org_id)
        else:
            # 🔴 기존 이벤트 이름(`진행`)만 쓴다 — 이벤트 목록·순서는 계약이다.
            #    가동 중이면 0건이라 평상시 이벤트열은 그대로다.
            for 진 in _워치독.기동_진행():
                yield _sse("진행", 진)
            # 🔴 별도 스레드에서 돌리고 짧은 주기로 폴링한다 — 대기 중에는 SSE 주석
            #    (`: keep-alive`)만 흘린다. **새 이벤트 이름을 안 만든다** — 이벤트
            #    목록·순서는 계약이고 D 의 테스트가 그대로 잠근다. 무한정 기다리되
            #    흘리기만 한다 — 하트비트가 판정보다 먼저 포기하지 않는다.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_실_판정, body)
                while True:
                    try:
                        out = future.result(timeout=_하트비트_주기초)
                        break
                    except concurrent.futures.TimeoutError:
                        yield ": keep-alive\n\n"
                    except Exception as e:                        # noqa: BLE001
                        # 🔴 실패해도 4-way 밖으로 나가지 않는다 — 판정 어휘는 닫혀
                        #    있다. `_오류` 는 계약 밖 키라 SSE 로는 안 내보낸다.
                        #    대신 서버 로그에 남긴다 — 조용히 삼키면 실서버에서
                        #    판단불가가 늘 때 원인을 못 찾는다 (2026-09-01 ai-14).
                        _log.exception("judge 실패 — 판단불가로 닫음 (plan_id=%s)",
                                       body.plan_id)
                        out = {**_목_판정["판단불가"],
                               "요약": "일시적인 오류로 판정하지 못했습니다. "
                                       "주관기관 문의가 필요합니다."}
                        break
            # 🔴 decision_id 는 캐시에도 `결과` 응답에도 안 실린다 — 캐시에 박히면
            #    다른 요청이 남의 계획에 그 판정을 저장하려 든다 (TENANT_LEAK 류).
            _decision_id = out.pop("decision_id", None)
            _판정캐시_넣기(k, out, 종류="judge", org=body.org_id)
        yield _sse("판정", {키: out.get(키) for 키 in
                          ("판정", "요약", "신뢰등급", "버전스탬프")})
        yield _sse("해야할일", out.get("해야할일", []))
        yield _sse("인용", out.get("인용", []))
        yield _sse("전제", out.get("전제", []))
        yield _sse("참조사슬", out.get("참조사슬", []))
        if out.get("판정") == "판단불가" and out.get("문의초안"):
            yield _sse("문의초안", out["문의초안"])          # 화면 9
        yield _sse("결과", out)
        yield _sse("저장", _판정_저장_시도(body, out, 캐시 is not None, _decision_id))
        yield _sse("완료", {"캐시": 캐시 is not None})

    return _sse응답(gen())


@app.get("/api/profile")
def profile_get(org_id: str | None = None) -> dict:
    """게스트도 판정은 된다 — 프로필이 없으면 빈 값을 돌려주고 전제[] 가 늘어날 뿐이다."""
    if MOCK or not org_id:
        return _목_프로필
    return _실_프로필(org_id)


@app.put("/api/profile")
def profile_put(body: 프로필 = Body(...), org_id: str | None = None) -> dict:
    """🔴 PMS 원문·실명·과제번호는 받지 않는다. 스키마에 그 자리가 없다 (§7)."""
    if MOCK or not org_id:
        return {"저장": False, "이유": "목 모드 — 브라우저 상태로만 유지한다",
                "받은값": body.model_dump()}
    return _실_프로필_저장(org_id, body)


@app.get("/admin/cost")
def admin_cost(x_admin_token: str | None = Header(default=None)) -> dict:
    _관리자(x_admin_token)
    return 가드.상태()


@app.get("/admin/gate")
def admin_gate(x_admin_token: str | None = Header(default=None)) -> dict:
    """H5 f_axis 차단 로그. 영구 기록은 `tenant.incidents(종류='ROUTING_BLOCK')`."""
    _관리자(x_admin_token)
    try:
        from adapter import 기본어댑터, 슬롯표
        return {"차단": 기본어댑터.차단로그,
                "슬롯": {k: {"f_axis": v.f_axis, "설명": v.설명} for k, v in 슬롯표.items()}}
    except Exception as e:                                    # noqa: BLE001
        return {"오류": f"adapter 를 못 읽었다: {type(e).__name__}"}


@app.get("/admin/queue")
def admin_queue(x_admin_token: str | None = Header(default=None),
                종류: str | None = None, 사유코드: str | None = None,
                사업명: str | None = None, 상태: str = "대기",
                limit: int = 50, offset: int = 0) -> dict:
    """재검수 큐 — 관리 화면 「제N차 개정 · 변경 M개 조항 자동 반영」의 재료.

    `corpus.recheck_queue` 를 그대로 읽는다. H1(A4 개정)·H2(A2 엄격조항)·
    H3(A5 기관답변)·H4(정산 리허설)가 전부 이 한 표에 쌓인다.

    🔴 **읽기 전용이다.** 큐를 승인해 `corpus.rules` 로 올리는 경로는 여기 없다 —
       그건 사람이 검수하는 일이고(`rule_base.md` §6), API 로 열면 검수 없이
       룰이 바뀔 길이 생긴다. 계약서 §3 상 `rules` 쓰기는 G 소유이기도 하다.
    """
    _관리자(x_admin_token)
    where, 인자 = ['"상태" = %s'], [상태]
    for 칼럼, 값 in (('"종류"', 종류), ('"사유코드"', 사유코드), ('"사업명"', 사업명)):
        if 값:
            where.append(f"{칼럼} = %s")
            인자.append(값)
    조건 = " AND ".join(where)

    요약 = _질의(f'SELECT "종류", "사유코드", count(*) FROM corpus.recheck_queue '
                f'WHERE {조건} GROUP BY 1,2 ORDER BY 1, 3 DESC', tuple(인자))
    전체 = _질의(f'SELECT count(*) FROM corpus.recheck_queue WHERE {조건}', tuple(인자))
    상태별 = _질의('SELECT "상태", count(*) FROM corpus.recheck_queue GROUP BY 1 ORDER BY 1')
    행 = _질의(f"""
        SELECT queue_id, "종류", "사유코드", "대상종류", "대상id", "사업명", "비목",
               doc_id, "조번호", "구doc_id", "구조번호", "변경유형", "유사도",
               "요약", "상태", "발견일"
        FROM corpus.recheck_queue WHERE {조건}
        ORDER BY "발견일" DESC, queue_id LIMIT %s OFFSET %s
    """, tuple(인자) + (min(limit, 200), offset))
    키 = ("queue_id", "종류", "사유코드", "대상종류", "대상ID", "사업명", "비목",
          "doc_id", "조번호", "구doc_id", "구조번호", "변경유형", "유사도",
          "요약", "상태", "발견일")
    return {
        "전체": 전체[0][0] if 전체 else 0,
        "요약": [{"종류": a, "사유코드": b, "건수": c} for a, b, c in 요약],
        "상태별": {a: b for a, b in 상태별},
        "항목": [dict(zip(키, r)) for r in 행],
        "필터": {"종류": 종류, "사유코드": 사유코드, "사업명": 사업명, "상태": 상태},
        "비고": "읽기 전용. 승인·반영은 사람이 검수 절차로 한다 (rule_base.md §6)",
    }


@app.post("/admin/gpu/pod")
def admin_gpu_pod(pod_id: str = Body(..., embed=True),
                   x_admin_token: str | None = Header(default=None)) -> dict:
    """`ops.gpu_pod` 의 pod_id·vllm_url 을 채운다 — 사람이 SQL UPDATE 를 손으로
    치는 자리를 없앤다(레인 ι, 2026-09-06).

    🔴 **적재 경로만이다.** 판정 로직(`gpu_watchdog.py`·`adapter.py`)은 한 글자도
    안 고쳤다 — 그쪽은 이미 `ops.gpu_pod` 를 «읽기만» 정확히 하고 있었다. 비어 있던
    건 «누가 채우는가» 였고, 그게 지금까지 사람의 SQL 이었다. 이 엔드포인트가
    그 자리를 대신한다.

    vllm_url 은 손으로 안 받는다 — RunPod 프록시 규칙(`scratchpad/W-GPU_기동절차.md`
    에서도 쓰던 `{pod_id}-8000.proxy.runpod.net`, `scripts/pod_serve.sh` 의
    고정 포트 8000)으로 pod_id 하나에서 유도한다. 사람이 입력할 값을 하나로 줄이는
    것이 목적이다 — URL을 손으로 옮겨 적다 오타 나는 자리를 없앤다.

    🔴 `상태` 컬럼은 여기서 안 건드린다. 가동 여부 판정은 여전히 `/api/gpu/wake`
    →`GPU워치독.기동_진행()`(vLLM 헬스체크로 확인) 몫이다 — 이 엔드포인트는
    "어느 팟을 볼지" 만 알려준다.
    """
    _관리자(x_admin_token)
    pod_id = (pod_id or "").strip()
    if not pod_id:
        raise HTTPException(400, "pod_id 가 비어 있다")
    vllm_url = f"https://{pod_id}-8000.proxy.runpod.net"
    # 🔴 2026-09-06(레인 D) — `예외전파=True`. 실측: Cloud Run 에서만 이 UPDATE 가
    #    죽는데 `_실행()` 기본값(삼킴)이면 `rowcount=-1` 뿐이라 «무엇이 죽었는지»
    #    영영 안 보인다. 여기서 켜서 진짜 예외를 응답에 싣는다 — 다른 호출부는
    #    그대로다(`_실행()` 독스트링 참조).
    try:
        n = _실행(
            "UPDATE ops.gpu_pod SET pod_id=%s, vllm_url=%s, updated_at=now(), "
            "updated_by='admin_gpu_pod' WHERE id='default'",
            (pod_id, vllm_url), 예외전파=True)
    except Exception as e:                                    # noqa: BLE001
        raise HTTPException(
            500, f"ops.gpu_pod 갱신 실패 — {type(e).__name__}: {e}") from e
    if n <= 0:
        raise HTTPException(500, "ops.gpu_pod 갱신 실패 — 대상 행이 없다"
                                  f" (rowcount={n}, id='default' 행이 있는지 확인해라)")
    # 🔴 adapter.vllm_url() 은 30초 TTL 캐시다 — 강제갱신 안 하면 최대 30초간
    #    옛 주소(또는 env 폴백)를 계속 쓴다. 시연 중엔 그 30초도 아깝다
    try:
        from adapter import vllm_url as _vllm_url_읽기
        갱신값 = _vllm_url_읽기(강제갱신=True)
    except Exception as e:                                    # noqa: BLE001
        갱신값 = f"갱신 실패 {type(e).__name__} — 다음 호출 시 자연 갱신됨(TTL 30초)"
    return {"pod_id": pod_id, "vllm_url": vllm_url, "캐시_갱신확인": 갱신값}


@app.post("/admin/warmup")
def admin_warmup(x_admin_token: str | None = Header(default=None)) -> dict:
    """발표 30분 전 워밍업 (`Agent.md` §9).

    🔴 콜드 스타트가 두 군데다 — 임베딩 모델 로딩(수십 초)과 vLLM 의 `guided_json`
       첫 호출 스키마 컴파일(`LLM.md` §1 vLLM 운영 규칙). 둘 다 미리 태운다.
    """
    _관리자(x_admin_token)
    결과 = {}
    t = time.time()
    try:
        # 🔴 `retrieve.질문벡터` 는 **실재하지 않는다.** 실제 공개 표면은
        #    `워밍업()`·`임베딩()` 이다. 그래서 이 갈래는 항상 ImportError 로
        #    떨어졌고, `/admin/warmup` 은 **아무것도 데우지 못했다**
        #    (2026-09-03 ai-98 실측: `{"임베딩": "실패 ImportError"}`).
        #    8-5 §7 은 워밍업 응답을 배포 go/no-go 로 쓰는데 그게 항상 실패를
        #    가리키고 있었다. 이름을 고치지 말고 **실재하는 이름을 부른다.**
        from retrieve import 워밍업  # type: ignore
        워밍업()
        결과["임베딩"] = f"{time.time() - t:.1f}초"
    except Exception as e:                                    # noqa: BLE001
        # 🔴 예전에는 `orchestrate._임베딩` 으로 되짚었다. 밑줄 심볼이라 Agent 쪽이
        #    이름만 바꿔도 서버가 **기동 시점에** 죽는다 (2026-09-01 ai-25 경고).
        #    파이프라인의 공개 표면(`retrieve.질문벡터` · `orchestrate.판정`)만 쓴다.
        결과["임베딩"] = f"실패 {type(e).__name__}"
    t = time.time()
    try:
        from adapter import 호출
        호출("문장생성", "워밍업", None, 타임아웃=60)
        결과["vLLM"] = f"{time.time() - t:.1f}초"
    except Exception as e:                                    # noqa: BLE001
        결과["vLLM"] = f"미가동 ({type(e).__name__})"
    _워치독.호출기록()          # 🔴 워밍업도 GPU 사용이다. 30분 뒤 죽으면 안 된다
    가드.워밍업시각 = datetime.now(timezone.utc).isoformat()
    return {"워밍업": 결과, "시각": 가드.워밍업시각}


@app.exception_handler(RequestValidationError)
def _검증오류(req: Request, exc: RequestValidationError):
    """422 도 같은 모양으로 돌려준다.

    🔴 pydantic 기본 형식은 `{"detail":[{...}]}` 이라 프론트가 오류 렌더러를 두 벌
       만들어야 한다. 계약상 모든 4xx·5xx 는 `{오류, 상태}` 다 (`models.오류응답`).
    """
    첫 = (exc.errors() or [{}])[0]
    말 = str(첫.get("msg", "입력이 올바르지 않습니다")).replace("Value error, ", "")
    자리 = [str(x) for x in (첫.get("loc") or []) if x != "body"]
    return JSONResponse(status_code=422,
                        content={"오류": 말, "상태": 422,
                                 "필드": ".".join(자리) or None})


@app.exception_handler(HTTPException)
def _http오류(req: Request, exc: HTTPException):
    # 프론트가 판정 화면과 같은 모양으로 그릴 수 있게 한다
    return JSONResponse(status_code=exc.status_code,
                        content={"오류": exc.detail, "상태": exc.status_code})


# ════════════════════════════════════════════════════════════════════
# 실호출 경로 — A 의 모듈이 준비되면 여기만 산다
# ════════════════════════════════════════════════════════════════════

def _폼_비목후보(품목: str | None, 용도: str, 금액: float | None,
              사업명: str | None) -> list[dict]:
    """폼 값(구조화)에서 비목후보만 뽑는다 — 호출 자리 ①. `_실_정규화` 전용 헬퍼.

    🔴 응답 모양은 자연어 경로와 **동일**하다 — `[{"비목", "신뢰도"}]`. 스키마의
       `비목후보` 조각을 `호출자리1_스키마()` 에서 그대로 떼어 쓰므로 enum 10종이
       guided_json 으로 닫힌다 (목록을 여기 다시 적으면 두 경로가 갈린다).
    🔴 실패는 판정을 죽이지 않는다 — 빈 배열로 물러난다. 비목은 화면 9 에서 사용자가
       확정하므로 여기서 못 뽑아도 흐름이 끊기지 않는다 (자연어 경로는 반대로 예외를
       올린다 — 거기선 품목·금액까지 이 호출로 뽑기 때문에 물러설 자리가 없다).
    """
    try:
        from normalize_run import MODEL_1, llm_호출, 호출자리1_스키마
        스키마 = {"type": "object", "additionalProperties": False,
                 "required": ["비목후보"],
                 "properties": {"비목후보":
                                호출자리1_스키마(비목_ENUM)["properties"]["비목후보"]}}
        금액문 = f"{int(금액):,}원" if 금액 is not None else "미상"
        프롬프트 = (
            "창업지원금으로 아래 지출을 하려 한다. 어느 비목으로 집행되는지 "
            "목록 안에서만 고르라.\n"
            "확신이 없으면 **비우거나 둘을 나란히** 둔다. 억지로 하나를 고르지 마라 — "
            "갈리면 코드가 두 경로를 모두 판정한다.\n"
            "판정을 하지 마라. 가능·불가를 여기서 말하지 않는다.\n\n"
            f"품목: {품목}\n금액: {금액문}\n용도: {용도 or '(적지 않음)'}\n"
            + (f"사업명: {사업명}\n" if 사업명 else "")
            + f"\n비목 목록: {', '.join(비목_ENUM)}")
        출력, _메타 = llm_호출(프롬프트, 스키마, 모델=MODEL_1 or None,
                          최대토큰=200, 타임아웃=60)
        후보 = (출력 or {}).get("비목후보") if isinstance(출력, dict) else None
        # 🔴 enum 밖을 먼저 버리고 «그다음에» 3건으로 자른다. 순서를 바꾸면 앞자리에
        #    낀 enum 밖 한 건이 멀쩡한 후보를 밀어낸다 (스키마 maxItems 도 3이다).
        return [{"비목": c["비목"], "신뢰도": float(c.get("신뢰도") or 0.0)}
                for c in (후보 or [])
                if isinstance(c, dict) and c.get("비목") in 비목_ENUM][:3]
    except Exception:                                         # noqa: BLE001
        _log.exception("폼 경로 비목후보 추출 실패 — 빈 후보로 간다")
        return []


def _실_정규화(body: 정규화요청) -> dict:
    if body.질문 and body.질문.strip():
        _워치독.게이트()      # 못 깨우면 raise → 기존 except → 판단불가
        from normalize_run import 정규화
        out, _메타 = 정규화(body.질문, 비목목록=비목_ENUM)
    else:
        # 🔴 폼 경로 — 품목·금액·용도는 이미 구조화돼 왔다. 그 셋은 다시 뽑지 않는다.
        #    비목후보만 LLM 으로 채운다 (2026-09-03 오너 결정 Q3). 원칙 위반이 아니라
        #    **복귀**다 — 「판정 1건 = LLM 2회(① 정규화 · ④ 조립)」에서 폼 경로는 ①을
        #    건너뛰어 1회만 쓰고 있었다. 이 호출로 정확히 2회가 된다.
        #    🔴 `_합성_질문()` 문장을 되먹이지 않는다 (필드→문장→필드는 정보를 잃는다).
        #       구조화된 값을 그대로 프롬프트에 넣는다.
        용도 = body.용도 or ""
        if body.추가설명:
            용도 = f"{용도} ({body.추가설명})".strip()
        out = {"품목": body.품목, "금액": body.금액, "금액_추정여부": False,
               "용도": 용도, "비목후보": _폼_비목후보(body.품목, 용도, body.금액,
                                                body.사업명)}
    out.setdefault("결제수단", None)
    out.setdefault("구매명의", None)
    out.setdefault("신청일", None)
    out.setdefault("비교견적", None)
    # 🔴 자연어 경로(`normalize_run.정규화`)는 지급수수료류에서 하위항목을 뽑아 실을 수
    #    있다 — 폼 경로는 못 뽑으니 없애지 말고 None 으로라도 키를 살린다. 경로마다
    #    키가 있다 없다 하면 프론트가 `'하위항목' in 결과` 로 분기할 때 갈린다
    #    (§8 「실서버로 갈아끼워도 프론트 코드는 한 줄도 안 바뀐다」, 2026-09-01 ai-14).
    out.setdefault("하위항목", None)
    # 🔴 내부 키는 여기서 벗긴다 — `누락필드`·`_출처` 는 `normalize_run.정규화` 의 산출물
    #    이지 API 계약이 아니다 (`tests/test_contract.py` 「정규화결과」는 선택 키가
    #    **빈 집합**이라 늘어난 키도 위반으로 잡는다). 목 모드엔 원래 없어서 목으로
    #    개발한 프론트가 실서버에서만 키를 두 개 더 받고 있었다 — 목에 맞춰 실을 깎는다
    #    (2026-09-03 S3). 필요해지면 계약에 먼저 올리고 세 경로에 같이 싣는다.
    out.pop("누락필드", None)
    out.pop("_출처", None)
    # A-3(2026-09-06, 레인 Y) — check_items 기반 심층질문. LLM 호출 0 (배선일 뿐).
    out["심층질문"] = _심층질문_선별(body.사업명, out.get("비목후보"))
    return out


def _할일_중복제거(할일: list) -> list:
    """🔴 조립 응답에 반복 생성이 나온다 — 같은 항목이 그대로 화면에 찍힌다.

    실측 (2026-09-03 ai-98): 자연어 원문주입 판에서 `해야할일` 10건이 **전부 같은
    항목**이었고 중복 제거 없이 그대로 사용자에게 나갔다 (completion 1151/1500).

    🔴 순서를 보존한다 — LLM 이 중요도 순으로 낸다는 보장은 없지만, 재정렬하면
       같은 질문에 화면 순서가 달라져 재현성이 깨진다.
    🔴 개수를 줄이는 것이 목적이 아니다 — 같은 코드에 다른 항목(사전승인·증빙서·
       자산등록)은 정상이다. 그래서 `code` 만이 아니라 **세 필드 전부**를 키로 쓴다.
    """
    본, 본것 = [], set()
    for h in 할일 or []:
        if not isinstance(h, dict):
            continue
        k = (h.get("code"), h.get("항목"), h.get("설명"))
        if k in 본것:
            continue
        본것.add(k)
        본.append(h)
    return 본


_체크마스터_캐시: tuple[float, dict[str, tuple[str, str]]] | None = None
_체크마스터_캐시TTL = _int환경("SUDDOE_CHECKITEM_TTL", 300)


def _체크마스터() -> dict[str, tuple[str, str]]:
    """`check_items.code → (구분, 유형)`. 🔴 못 읽으면 **빈 dict** — 호출부가 안 붙인다.

    🔴 **표를 통째로 캐시한다.** `code = ANY(...)` 로 요청마다 좁혀 치는 것보다 적다 —
       `_질의()` 는 호출마다 psycopg 접속을 새로 열어(`main.py:400` 실측 약 25ms)
       비용이 행 수가 아니라 **접속 수**에 붙기 때문이다. 52행 고정 마스터라
       (실측 2026-09-03 ai-43: 결제전 43 · 결제후 9) 통째로 들고 있어도 싸다.
    🔴 **빈 표는 캐시하지 않는다** — `_사업명_표()` 와 같은 이유다. DB 가 깜빡인
       순간을 5분간 물고 있으면 그 사이 전 판정이 `구분` 없이 나간다.
    """
    global _체크마스터_캐시
    if _체크마스터_캐시 is not None:
        받은시각, 표 = _체크마스터_캐시
        if time.time() - 받은시각 < _체크마스터_캐시TTL:
            return 표
    표: dict[str, tuple[str, str]] = {
        c: (구분, 유형) for c, 구분, 유형 in
        _질의('SELECT "code", "구분", "유형" FROM corpus.check_items')}
    if 표:                              # 🔴 성공한 것만 캐시한다
        _체크마스터_캐시 = (time.time(), 표)
    return 표


_심층질문_캐시: tuple[float, list[dict]] | None = None
_심층질문_캐시TTL = _int환경("SUDDOE_CHECKITEM_TTL", 300)

# 🔴 A-3(2026-09-06, 레인 Y) — ai-7d 가 만드는 A-1(질문문)·A-2(유형·선택지·필요F필드)
#    컬럼 이름의 예상값이다. `corpus.check_items` 에 이미 «유형」(기타/계약/비교견적,
#    `할일.유형` 용) 이 있어 그것과 겹치지 않게 «질문유형」으로 잡았다 — ai-7d 산출이
#    다른 이름으로 오면 여기만 고치면 된다(호출부는 아래 dict 모양만 본다).
_심층질문_컬럼 = {"질문문": "질문문", "유형": "질문유형", "선택지": "선택지", "필요F필드": "필요F필드"}


def _심층질문마스터() -> list[dict]:
    """`corpus.check_items` 에서 심층질문 후보 전부를 읽는다 (52행 고정, `_체크마스터()`
    와 같은 이유로 통째로 캐시한다).

    🔴 A-1·A-2 컬럼이 «아직 없으면» `_질의()` 가 예외를 삼켜 빈 리스트를 준다 —
       그게 정상이다(기능이 아직 안 켜진 것이지 고장이 아니다). 컬럼이 생기면 재배포
       없이 다음 TTL 만료 때 자동으로 채워진다.
    🔴 근거가 없는 행은 절대 안 낸다 — 「왜 묻는지」가 근거다(ai-8c 지시).
    """
    global _심층질문_캐시
    if _심층질문_캐시 is not None:
        받은시각, 표 = _심층질문_캐시
        if time.time() - 받은시각 < _심층질문_캐시TTL:
            return 표
    c = _심층질문_컬럼
    행 = _질의(f'''SELECT "code", "사업명", "비목", "{c["질문문"]}", "{c["유형"]}",
                        "{c["선택지"]}", "근거", "{c["필요F필드"]}"
                  FROM corpus.check_items''')
    표 = []
    for code, 사업명, 비목, 질문문, 유형, 선택지, 근거, 필요F필드 in 행:
        if not 질문문 or not 근거:              # 질문문 없거나 근거 없으면 못 낸다
            continue
        근거들 = 근거 if isinstance(근거, list) else [근거]
        if not 근거들 or not isinstance(근거들[0], dict):
            continue
        표.append({
            "code": code, "사업명": 사업명, "비목": 비목,
            "질문문": 질문문, "유형": 유형 or "예아니오",
            "선택지": 선택지 or [],
            "근거": {"doc_id": 근거들[0].get("doc_id"), "조번호": 근거들[0].get("조번호")},
            "필요F필드": list(필요F필드 or []),
        })
    if 표:                                       # 🔴 빈 표는 캐시하지 않는다 (`_체크마스터()` 와 같은 이유)
        _심층질문_캐시 = (time.time(), 표)
    return 표


def _심층질문_선별(사업명: str | None, 비목후보: list[dict] | None) -> list[dict]:
    """정규화 시점엔 비목이 «후보» 뿐이다 — 사업명·최우선 비목후보로만 거칠게 거른다.

    🔴 못 거르면(사업명 미확정) 전부 준다 — 화면이 접어 보여주면 되지, 서버가
       추측으로 줄이면 「해당 없음」이 「아직 모름」을 삼킨다.
    """
    표 = _심층질문마스터()
    if not 표:
        return []
    최우선비목 = None
    if 비목후보:
        try:
            최우선비목 = max(비목후보, key=lambda c: c.get("신뢰도", 0)).get("비목")
        except (TypeError, ValueError):
            최우선비목 = None
    out = []
    for q in 표:
        if q["사업명"] and 사업명 and q["사업명"] != 사업명:
            continue
        if q["비목"] and 최우선비목 and q["비목"] != 최우선비목:
            continue
        out.append({k: v for k, v in q.items() if k not in ("사업명", "비목")})
    return out


def _할일_보강(할일: list) -> list:
    """`code` 로 마스터를 봐서 `구분`·`유형` 을 얹는다 (§3-5 Q2). **LLM 호출 증가 0.**

    프론트가 저장 뒤 다시 읽어서 구분을 알아내고 있었다 — 목 모드에선 영영 못 갈랐다.

    🔴 **LLM 이 내지 않는다.** 조립 스키마의 `해야할일` 은 `{code, 항목, 설명}` 뿐이고
       (`llm_schema.py:106`), `구분`·`유형` 의 정본은 `corpus.check_items` 컬럼이다
       (`02_frontend.sql:28·33` — "서버가 항목 텍스트로 분류하면 마스터가 늘 때 갱신이
       빠진다"). 코드가 조회해 얹는 자리다.
    🔴 **`check_items.구분` 이 2종(결제전·결제후)인 것은 `models.할일.구분` 3종과
       모순이 아니다.** 이 마스터는 «AI 가 만드는 할일» 의 폐쇄 목록이고 AI 할일은
       결제 시점 기준이라 둘로 닫힌다. 「집행」은 사용자 직접 추가(`할일생성`)와 캘린더
       집행 일정에서 온다 — 즉 3종은 할일 테이블 전체의 어휘, 2종은 그 부분집합이다.
       CHECK 를 늘리면 AI 가 「집행」을 낼 수 있게 돼 어휘가 흐려진다 (2026-09-03 중앙 판단).
    🔴 **마스터에 없는 code 면 안 붙인다.** 임의로 「결제전」을 박으면 없는 걸 있는
       것처럼 만든다 — 화면은 그 값을 마스터에서 온 것으로 읽는다.
    🔴 **DB 가 죽어도 판정을 죽이지 않는다** — `구분` 없이 지금 모양 그대로 나간다.
    """
    표 = _체크마스터()
    if not 표:
        return 할일
    본 = []
    for h in 할일 or []:
        m = 표.get(h.get("code")) if isinstance(h, dict) else None
        본.append({**h, "구분": m[0], "유형": m[1]} if m else h)
    return 본


def _사용자F값_조립(답변: list) -> dict | None:
    """B(2026-09-06, 레인 Y) — 심층질문 답 → `orchestrate.판정(사용자F값=...)` 입력.

    🔴 «컬럼명» 그대로 담는다(`_심층질문마스터()` 의 `필요F필드` 그대로 옮긴다) —
       dotted-path(`F1.정부지원.현금`) 변환은 `f값_경로키()` 안에서 «한 번만» 일어난다
       (orchestrate.py 주석 참조. 여기서 또 접두사를 매기면 두 곳이 어긋난다).
    🔴 목록 밖(마스터에 없는) code 는 조용히 버린다 — 지어낸 F필드를 만들지 않는다.
    """
    if not 답변:
        return None
    표 = {q["code"]: q.get("필요F필드") or [] for q in _심층질문마스터()}
    out: dict = {}
    for a in 답변:
        for 컬럼 in 표.get(a.code, []):
            out[컬럼] = a.값
    return out or None


def _실_판정(body: 판정요청) -> dict:
    _워치독.게이트()
    import orchestrate
    # 🔴 `질문원문` 을 빼먹어 옳에 있는 원문을 두고 문장을 다시 만들고 있었다.
    #    `모델의 생성`이 아니라 계약 필드다 (`models.py:100·191·232`, `main.py:585` 가 싣는다).
    #    실측 (2026-09-03 ai-98): 되짚은 문장 `"맥북 프로 디자이너가 쓸 2500000원"` 으로
    #    정규화를 다시 돌렸더니 `비목후보` 가 `[{"비목":"기계장치","신뢰도":1}]` → `[]` 로
    #    죽었다. 같은 질문인데 인용 3건→1건·할일 10건→1건 으로 갈렸다 —
    #    **사용자 확정값이 조용히 덤인다.** CLAUDE.md 가 못박은 그 자리:
    #    «`_합성_질문()` 문장을 되먹이지 않는다 — 필드→문장→필드는 정보를 잃는다».
    질문 = (body.정규화.get("_원문") or body.정규화.get("질문")
          or body.정규화.get("질문원문") or "")
    if not 질문:
        # 정규화 결과만 온 경우 — 품목·용도·금액으로 되짚어 문장을 만든다
        조각 = [str(body.정규화.get(k)) for k in ("품목", "용도") if body.정규화.get(k)]
        금액 = body.정규화.get("금액")
        질문 = " ".join(조각) + (f" {금액}원" if 금액 else "")
    # 🔴 plan_id 를 넘기면 decisions 행이 처음부터 plan_id 를 달고 태어난다 —
    #    persist.py 의 「UPDATE 로 plan_id 잇기」가 필요 없어지는 방향
    #    (orchestrate.py:405, 2026-09-01 ai-25 시그니처 확장 · ai-14 후속 지시).
    #    🔴 그래도 persist.py 의 UPDATE 는 아직 걷어내지 마라 —
    #       실측으로 decisions.plan_id 가 채워지는 걸 확인한 뒤에나 A 가 정리한다.
    # 🔴 사용자가 화면 9 에서 확정한 비목을 엔진에 넘긴다 (2026-09-03 오너 결정 Q2).
    #    `orchestrate.py:500` 이 후보를 이 값으로 필터하고, 없으면
    #    `{"출처": "갈래고정"}` 으로 세워 그 비목으로 판정한다. 덤으로 `:512` 의
    #    비목갈림 재귀가 끊긴다 — 사용자가 이미 골랐으니 두 경우를 보여줄 이유가 없다.
    # 🔴 이미 돈 (1) 을 넘긴다. 안 넘기면 orchestrate 안에서 (1)이 또 돌아
    #    판정 1건이 **LLM 3회**가 된다 — 확정 원칙은 2회다 (0903 ai-43 발견).
    #    되짚은 문장으로 다시 정규화하면 사용자 확정값이 조용히 죽는다(위 주석의 실측).
    #    인자 이름이 `정규화` 가 아니라 `정규화결과` 인 이유는 orchestrate 쪽 주석에 있다 —
    #    `정규화` 로 두면 모듈 전역 함수를 파라미터가 가린다.
    r = orchestrate.판정(질문, 사업명=body.사업명, org_id=body.org_id, plan_id=body.plan_id,
                       _비목고정=body.확정비목 or None,
                       정규화결과=body.정규화,
                       # B(2026-09-06, 레인 Y) — 심층질문 답. 없으면(빈 리스트) None 이라
                       # orchestrate 쪽에서 종전과 바이트 단위로 같은 경로를 탄다.
                       사용자F값=_사용자F값_조립(body.답변))
    전제 = r.get("전제") or r.get("전제목록") or []
    # 🔴 후보에 없는 비목을 고정하면 «그 비목으로 본 판정» 이 나온다 — 조용히 내보내면
    #    사용자는 시스템이 동의한 걸로 읽는다 (2026-09-03 오너 결정 R7). 새 SSE 이벤트를
    #    만들지 않고 `전제` 맨 앞에 한 줄 얹는다 — 이벤트 이름·순서가 계약이다.
    후보 = [n for n in (_비목_정본(c.get("비목")) if isinstance(c, dict) else None
                      for c in (body.정규화.get("비목후보") or [])) if n]
    if body.확정비목 and 후보 and body.확정비목 not in 후보:
        # 🔴 조사는 「로」로 고정한다 — 비목 10종이 모두 모음으로 끝난다(비·치·료).
        #    「이라는/라는」류를 쓰면 비목마다 문장이 틀린다.
        전제 = [{"사실": f"비목을 「{body.확정비목}」로 보고 판정했습니다 — 정규화가 제시한 "
                      f"후보({' · '.join(후보)})에 없는 비목을 직접 고르셨습니다",
                "근거조항": None, "매핑": [], "미충족시": "판단불가"}, *전제]
    # 🔴 문의초안 — **실경로에는 이걸 만드는 코드가 0줄이었다.** 목(`_목_판정`)만
    #    값을 갖고 있어서, 목에서는 이벤트가 나가고 실서버에서는 «아무것도 안 나갔다».
    #    자체점검조차 `?목=판단불가` 로만 돌아 그 사실을 못 잡았다(0903 ai-43).
    #    🔴 LLM 을 부르지 않는다 — 확정 원칙이 「판정 1건 = LLM 2회」다. 이미 손에 쥔
    #       값으로 `server/inquiry.py` 가 문장을 조립한다. 판단불가가 아니면 None 이고,
    #       아래 dict 가 falsy 면 키를 안 싣는 모양이라 «안 만든다» 와 같아진다.
    #    오케가 언젠가 자기 초안을 주기 시작하면 그걸 이긴다 — 덮어쓰지 않는다.
    if not r.get("문의초안"):
        r["문의초안"] = inquiry.문의초안(
            body.정규화 or {}, r.get("판정", "판단불가"),
            r.get("인용") or r.get("인용목록") or [], 전제, body.사업명)

    # 오케 반환값 → API 계약 (`프론트 연동 사양.md` §8). 이름이 다른 것만 옮긴다
    return {
        "판정": r.get("판정", "판단불가"),
        "요약": r.get("요약", ""),
        "해야할일": _할일_보강(_할일_중복제거(r.get("해야할일", []))),
        # 🔴 오케는 `인용목록` 으로 돌려준다 (`llm_schema.최종응답`). `r.get("인용")` 만 써서
        #    실서버 판정 전수가 화면 7(근거) 무지로 나갔다 (2026-09-03 ai-43 실측).
        #    목 응답(`_목_판정`)은 `인용` 을 직접 박아 놓아 테스트가 전부 통과했다 —
        #    목과 실경로의 키 이름이 달란 것이 결함이다. 아래 `전제` 와 같은 폴백 모양으로 맞춘다.
        "인용": r.get("인용") or r.get("인용목록") or [],
        "전제": 전제,
        "신뢰등급": r.get("신뢰등급"),
        "버전스탬프": r.get("버전스탬프"),
        "참조사슬": r.get("참조사슬", []),
        **({"문의초안": r["문의초안"]} if r.get("문의초안") else {}),
        # 🔴 내부 저장용 키다 — `gen()` 이 `결과` 로 내보내기 전에 벗겨내고 `저장`
        #    이벤트에만 따로 실어준다 (2026-09-01 ai-14 정정).
        **({"decision_id": r["decision_id"]} if r.get("decision_id") else {}),
    }


def _판정_저장_시도(body: 판정요청, out: dict, 캐시적중: bool, decision_id: int | None) -> dict:
    """`judge()` 의 `gen()` 안에서만 부른다 — 판정 스트림의 부수 효과다.

    🔴 **지연 import.** `server.persist` 는 지출계획 계통이라 이 파일보다 늦게 생길 수 있다.
       모듈 상단에서 import 하면 그 파일 하나 없다고 앱 전체(+ 할일 테스트의
       `server.main` import)가 못 뜬다 — `_실_판정` 이 `orchestrate` 를 지연 import
       하는 것과 같은 결 (2026-09-01 ai-14 정정).
    🔴 저장이 실패해도 SSE 를 죽이지 않는다 — 판정은 이미 사용자 손에 갔다.
    """
    if body.plan_id is None:
        return {"저장": False, "사유": "plan_id 없음"}
    if 캐시적중:
        # 캐시로 답한 건 이번 요청에서 새로 만든 판정 행이 없다 — 남의 decision_id 를
        # 이 plan_id 에 잘못 붙이지 않으려고 저장을 건너뛴다 (비용가드는 저장 경로가 아니다).
        return {"저장": False, "사유": "캐시 적중 — 새 판정 기록 없음"}
    try:
        from .persist import 판정_저장
        return 판정_저장(body.plan_id, body, out, body.org_id, decision_id=decision_id)
    except ImportError:
        return {"저장": False, "사유": "저장 계층 미배선"}
    except Exception as e:                                    # noqa: BLE001
        return {"저장": False, "사유": f"저장 실패 ({type(e).__name__})"}


def _실_프로필(org_id: str) -> dict:
    행 = _질의('SELECT "정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일" '
              'FROM tenant.f_profile WHERE org_id = %s LIMIT 1', (org_id,))
    if not 행:
        return _목_프로필
    a, b, c, d = 행[0]
    return {"f1": {"정부지원_현금": float(a or 0), "자기부담_현금": float(b or 0),
                   "협약시작일": str(c) if c else None, "협약종료일": str(d) if d else None},
            "f3": [], "f4": []}


def _실_프로필_저장(org_id: str, body: 프로필) -> dict:
    # E 세션이 tenant.orgs·f_profile 을 소유한다. 쓰기 경로는 그쪽 확정 후에 붙인다.
    return {"저장": False, "이유": "f_profile 쓰기 경로 미배선 (tenant 소유는 E 세션)",
            "받은값": body.model_dump()}


# ════════════════════════════════════════════════════════════════════

def _selftest() -> int:
    """서버를 띄우지 않고 계약을 검증한다. `프론트 연동 사양.md` §8 대조."""
    from fastapi.testclient import TestClient
    실패 = 0

    def 확인(설명, 조건, 부연=""):
        nonlocal 실패
        print(("  ✅ " if 조건 else "  🔴 ") + 설명 + (f"  {부연}" if 부연 else ""))
        실패 += 0 if 조건 else 1

    c = TestClient(app)
    print("서버 계약 자가검사 (프론트 연동 사양.md §8)")

    print("\n[정적 엔드포인트]")
    h = c.get("/api/health").json()
    확인("GET /api/health", h.get("ok") is True, h.get("모드"))
    v = c.get("/api/vocab").json()
    확인("GET /api/vocab — 비목 10종", len(v["비목"]) == 10)
    v2 = c.get("/api/vocab?사업명=초기창업패키지").json()
    확인("창업활동비는 예비창업패키지에만", "창업활동비" not in v2["비목"] and len(v2["비목"]) == 9)
    p = c.get("/api/programs").json()
    확인("GET /api/programs — 8사업", len(p["사업"]) == 8, str(len(p["사업"])))

    print("\n[SSE]")

    def sse(경로, 본문=None):
        r = c.post(경로, json=본문) if 본문 is not None else c.post(경로)
        evts = {}
        이름 = None
        for 줄 in r.text.splitlines():
            if 줄.startswith("event: "):
                이름 = 줄[7:]
            elif 줄.startswith("data: ") and 이름:
                evts.setdefault(이름, []).append(json.loads(줄[6:]))
        return r, evts

    r, e = sse("/api/normalize", {"질문": "디자이너 쓸 맥북 250만원 사도 되나요?",
                                 "사업명": "예비창업패키지",
                                 "f5": {"친족거래": False, "전직임직원업체": False}})
    확인("POST /api/normalize 200 + text/event-stream",
         r.status_code == 200 and "event-stream" in r.headers.get("content-type", ""))
    n = (e.get("결과") or [{}])[0]
    확인("normalize 결과에 §8 필드 전부",
         all(k in n for k in ("품목", "금액", "금액_추정여부", "용도", "비목후보",
                              "결제수단", "구매명의", "신청일", "비교견적")))
    확인("normalize 완료 이벤트", bool(e.get("완료")))

    for 원하는 in ("가능", "조건부", "불가", "판단불가"):
        r, e = sse(f"/api/judge?목={원하는}",
                   {"정규화": _목_정규화, "확정비목": "기계장치", "사업명": "예비창업패키지"})
        j = (e.get("결과") or [{}])[0]
        확인(f"judge ?목={원하는} → 4-way 배지",
             j.get("판정") == 원하는 and j.get("판정") in 판정_ENUM)
        if 원하는 == "가능":
            확인("judge 결과에 §8 필드 전부",
                 all(k in j for k in ("판정", "요약", "해야할일", "인용", "전제",
                                      "신뢰등급", "버전스탬프", "참조사슬")))
            확인("인용은 {조번호,조제목,원문,doc_id}",
                 all(set(x) >= {"조번호", "조제목", "원문", "doc_id"} for x in j["인용"]))
            확인("전제는 {사실,근거조항,매핑,미충족시}",
                 all(set(x) >= {"사실", "근거조항", "매핑", "미충족시"} for x in j["전제"]))
            확인("이벤트가 화면별로 쪼개져 온다",
                 all(k in e for k in ("판정", "해야할일", "인용", "전제", "참조사슬")))
        if 원하는 == "판단불가":
            확인("판단불가 → 문의초안 이벤트 (화면 9)", bool(e.get("문의초안")))
            확인("판단불가는 200 이다 (에러 아님)", r.status_code == 200)

    # 🔴 **여기까지는 전부 `?목=` 이다.** 목에서 서는 것은 「계약을 만족한다」의 증거이지
    #    「실경로가 판다」의 증거가 아니다 — `문의초안` 이 정확히 그 자리였다(목만 값이
    #    있었고 엔진에는 만드는 코드가 0줄이었다). 그래서 값을 만드는 함수 자체를
    #    «목을 통하지 않고» 직접 태운다. LLM·DB·GPU 를 안 쓰는 순수 함수라 여기서 된다.
    초안 = inquiry.문의초안(
        {"품목": "디자이너 업무용 노트북", "금액": 2500000}, "판단불가",
        [{"조번호": "제39조", "조제목": "기계장치", "doc_id": "L1_통합관리지침_20251223"}],
        [{"사실": "협약 총액이 확인되지 않았다"}], "예비창업패키지")
    확인("문의초안: 실경로 함수가 초안을 만든다 (목 아님)", bool(초안))
    확인("문의초안: 판단불가가 아니면 안 만든다",
         all(inquiry.문의초안({"품목": "노트북", "금액": 2500000}, p, [], [], "예비창업패키지")
             is None for p in ("가능", "조건부", "불가")))
    확인("문의초안: 값이 없으면 지어내지 않는다 (빈 괄호·None·제0조 흔적 0)",
         inquiry.문의초안({}, "판단불가", [], [], None) is None
         and not any(x in (inquiry.문의초안({"품목": "노트북"}, "판단불가", [],
                                          [{"사실": "비목이 확정되지 않았다"}], None) or "")
                     for x in ("()", "None", "미상", "제0조")))
    확인("확률·점수 필드가 없다 (§3 ❌)",
         not any(k in j for k in ("확률", "점수", "confidence", "score")))

    # 🔴 ⓐ — `_목_판정` 이 `인용` 을 직접 박아 놔서 위 judge 자가검사는 전부 목이 서는
    #    것만 본다. 오케가 `인용목록` 으로 돌려주는 걸 `_실_판정` 이 실제로 읽는지는
    #    이 블록이 못 잡는다(2026-09-03 사고, docs/0-3_초록이_가린다.md ⓐ). 그래서 오케를
    #    스키마 그대로 갈아끼우고 `_실_판정` 을 목을 통하지 않고 직접 부른다
    #    (`tests/test_계약_키집합.py` 오케물리기 와 같은 기법 — pytest 가 안 돌아도
    #    `--selftest` 혼자서 같은 축을 잡게 한다).
    print("\n[실경로 판정 — 오케 키 이름을 실제로 읽는가 (ⓐ)]")
    import sys as _sys
    import types as _types
    import llm_schema

    def _가짜_판정(질문, **kw):
        인용 = llm_schema.인용(s번호="S14", doc_id="D1", 조번호="제39조",
                              조제목="기계장치", 원문="…", extraction="text")
        전제 = llm_schema.전제(사실="협약상 참여인력이다", 근거조항="S14",
                              매핑=["F4.역할"], 미충족시="불가")
        # 🔴 값은 전부 「비지 않은」 센티널이다 — 실경로가 키 이름을 잘못 읽으면
        #    폴백 기본값([]·None)이 나오므로 **빈 값 = 키 이름을 못 읽었다** 가 된다
        #    (tests/test_계약_키집합.py 와 같은 원칙. 여기서 `참조사슬=[]` 를 주면
        #    실경로가 못 읽어도 폴백([])과 구분이 안 돼 이 검사가 무력해진다).
        r = llm_schema.최종응답(판정="조건부", 요약="자가검사용",
                              해야할일=[{"항목": "사전승인 신청", "설명": "…"}],
                              인용목록=[인용], 전제목록=[전제], 신뢰등급="B",
                              버전스탬프="자가검사",
                              참조사슬=[{"표기": "지침 제39조", "관계": "준용"}])
        return r.to_dict()

    _가짜_orch = _types.ModuleType("orchestrate")
    _가짜_orch.판정 = _가짜_판정
    _원래_orch, _원래_게이트 = _sys.modules.get("orchestrate"), _워치독.게이트
    _sys.modules["orchestrate"] = _가짜_orch
    _워치독.게이트 = lambda: None
    try:
        실경로out = _실_판정(판정요청(
            정규화={"_원문": "디자이너가 쓸 맥북 프로 250만원", "비목후보": []},
            사업명="예비창업패키지"))
    finally:
        _워치독.게이트 = _원래_게이트
        if _원래_orch is not None:
            _sys.modules["orchestrate"] = _원래_orch
        else:
            _sys.modules.pop("orchestrate", None)

    계약키 = set(_목_판정["가능"])
    빈칸 = [k for k in 계약키 if not 실경로out.get(k)]
    확인("실경로(_실_판정)가 오케 키 이름을 전부 읽는다 — 목이 아니다",
         not 빈칸, f"빈 키: {빈칸}" if 빈칸 else "")
    확인("실경로 인용도 {조번호,조제목,원문,doc_id}",
         all(set(x) >= {"조번호", "조제목", "원문", "doc_id"}
             for x in 실경로out.get("인용", [])))

    print("\n[현물 제거 — f1 은 2칸]")
    pr = c.get("/api/profile").json()
    확인("f1 에 현물 칸이 없다",
         set(pr["f1"]) == {"정부지원_현금", "자기부담_현금", "협약시작일", "협약종료일"},
         str(sorted(pr["f1"])))
    좋음 = c.put("/api/profile", json={
        "f1": {"정부지원_현금": 40_000_000, "자기부담_현금": 10_000_000},
        "f3": [{"비목": "기계장치", "재원": "정부지원", "금액": 2_500_000}],
        "f4": [{"역할": "개발자", "타사업참여율": 30}]})
    확인("PUT /api/profile 200", 좋음.status_code == 200)
    확인("f3 에 형태(현금/현물) 칸이 없다",
         "형태" not in 좋음.json()["받은값"]["f3"][0])
    확인("f4 에 이름 칸이 없다", "이름" not in 좋음.json()["받은값"]["f4"][0])
    나쁨 = c.put("/api/profile", json={"f3": [{"비목": "기계장치", "재원": "정부지원",
                                              "형태": "현물", "금액": 1}]})
    확인("f3.형태 를 보내도 무시된다 (스키마에 없다)",
         나쁨.status_code == 200 and "형태" not in 나쁨.json()["받은값"]["f3"][0])

    print("\n[입력 검증]")
    확인("비목 enum 밖은 422",
         c.post("/api/judge", json={"정규화": {}, "확정비목": "회식비"}).status_code == 422)
    확인("빈 질문은 422",
         c.post("/api/normalize", json={"질문": ""}).status_code == 422)

    print("\n[H6 비용 방어]")
    확인("관리 엔드포인트는 토큰 없이 403", c.get("/admin/cost").status_code == 403)
    # HTTP 헤더는 ASCII 만 실린다 — 관리 토큰에 한글을 쓰면 클라이언트가 못 보낸다
    os.environ["SUDDOE_ADMIN_TOKEN"] = "selftest-token"
    st = c.get("/admin/cost", headers={"X-Admin-Token": "selftest-token"})
    확인("토큰이 맞으면 200", st.status_code == 200)

    # 같은 질문을 두 번 — 심사위원 여럿이 같은 예시 칩을 누르는 그 경우다
    본 = {"질문": "같은 질문 캐시 확인", "사업명": "예비창업패키지"}
    전 = 가드.적중
    _, e1 = sse("/api/normalize", 본)
    _, e2 = sse("/api/normalize", 본)
    확인("같은 질문은 캐시로 답한다", 가드.적중 == 전 + 1 and e2["완료"][0]["캐시"] is True)
    확인("캐시 응답이 첫 응답과 같다", e1["결과"][0] == e2["결과"][0])
    확인("캐시 적중은 일일 호출을 되돌린다",
         가드.상태()["캐시"]["적중"] > 0, str(가드.상태()["캐시"]))
    확인("/admin/gate 가 f_axis 슬롯표를 준다",
         "슬롯" in c.get("/admin/gate", headers={"X-Admin-Token": "selftest-token"}).json())

    q = c.get("/admin/queue", headers={"X-Admin-Token": "selftest-token"}).json()
    확인("/admin/queue 가 재검수 큐를 준다",
         "요약" in q and "항목" in q and "상태별" in q, f"대기 {q.get('전체')}건")
    확인("/admin/queue 는 토큰 없이 403", c.get("/admin/queue").status_code == 403)
    q2 = c.get("/admin/queue?종류=A4개정&limit=5",
               headers={"X-Admin-Token": "selftest-token"}).json()
    확인("/admin/queue 필터가 먹는다",
         all(x["종류"] == "A4개정" for x in q2["항목"]) and len(q2["항목"]) <= 5)

    한도전 = 가드.IP시간당
    가드.IP시간당 = 1
    가드._ip.clear()
    c.post("/api/normalize", json={"질문": "첫 번째"})
    막힘 = c.post("/api/normalize", json={"질문": "두 번째"})
    확인("IP 시간당 한도를 넘으면 429", 막힘.status_code == 429, str(막힘.json().get("오류"))[:40])
    가드.IP시간당 = 한도전
    가드._ip.clear()

    캡전 = 가드.일일캡
    가드.일일캡 = 0
    확인("일일 캡을 넘으면 429",
         c.post("/api/normalize", json={"질문": "캡 초과"}).status_code == 429)
    가드.일일캡 = 캡전

    가드.개방시작 = "2099-01-01T00:00:00+00:00"
    확인("심사 기간 밖이면 429",
         c.post("/api/normalize", json={"질문": "창 닫힘"}).status_code == 429)
    가드.개방시작 = ""

    print(f"\n{'✅ 전부 통과' if not 실패 else f'🔴 실패 {실패}건'}")
    return 1 if 실패 else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    import uvicorn
    uvicorn.run(app, host=os.environ.get("SUDDOE_HOST", "127.0.0.1"),
                port=_int환경("SUDDOE_PORT", 8080))
