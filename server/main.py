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
from server._common import (DSN, MOCK, _sse, _sse응답, _질의,      # noqa: E402
                            비목_ENUM, 판정_ENUM, 창업활동비_사업 as _창업활동비_사업)
from server.models import (F1, F3항, F4항, F5, 정규화요청,          # noqa: E402
                           판정요청, 프로필)
from server import routes_l3, routes_plans, routes_tasks           # noqa: E402


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
}

# 🔴 4-way 를 전부 그려봐야 한다. 판단불가는 에러 화면이 아니라 정상 경로다 (§3).
#    프론트는 ?목=조건부|불가|판단불가 로 갈아끼워 네 화면을 다 만든다.
_목_판정: dict[str, dict] = {
    "가능": {
        "판정": "가능",
        "요약": "1인 1대 한도 내에서 구매 가능합니다.",
        "해야할일": [
            {"항목": "비교견적 2곳 이상 확보", "설명": "50만원 이상 구매는 비교견적을 남깁니다."},
            {"항목": "사업비 카드로 결제", "설명": "현금·개인카드 결제는 인정되지 않습니다."},
            {"항목": "자산 등록", "설명": "취득일로부터 30일 이내에 자산으로 등록합니다."},
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
            {"항목": "사전승인 신청", "설명": "100만원 이상 기계장치는 주관기관 사전승인이 필요합니다."},
            {"항목": "비교견적 2곳 이상 확보", "설명": "50만원 이상 구매는 비교견적을 남깁니다."},
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
            {"항목": "법인·사업자 명의로 재구매", "설명": "구매 명의가 사업자와 일치해야 합니다."},
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get(
        "SUDDOE_CORS", "http://localhost:3000,http://localhost:5173").split(",") if o],
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)

# ── 라우터 (2026-09-01 분해) ────────────────────────────────────────────
# 계통이 갈린다. 지출계획=routes_plans · 할일=routes_tasks · L3=routes_l3 + 아래 normalize.
#    남의 라우터 파일을 고치지 말 것 — 훅이 막고, 막히기 전에 이미 충돌한다.
app.include_router(routes_plans.router)
app.include_router(routes_tasks.router)
app.include_router(routes_l3.router)


def _관리자(token: str | None) -> None:
    """🔴 fail closed. 토큰이 설정 안 돼 있으면 관리 엔드포인트는 열리지 않는다."""
    기대 = os.environ.get("SUDDOE_ADMIN_TOKEN", "")
    if not 기대 or token != 기대:
        raise HTTPException(403, "관리자 토큰이 필요합니다 (SUDDOE_ADMIN_TOKEN)")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "모드": "mock" if MOCK else "live",
            "판정_enum": list(판정_ENUM), "시각": datetime.now(timezone.utc).isoformat()}


@app.get("/api/vocab")
def vocab(사업명: str | None = None) -> dict:
    """비목 enum 10종. 🔴 창업활동비는 예비창업패키지에만 있다 (§9)."""
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

    # F5 는 판정 후 폐기다. 캐시 열쇠에는 해시로만 들어간다
    # 🔴 폼 경로에서는 질문이 None 이다. 폼 값도 열쇠에 넣는다
    k = 비용가드.열쇠("normalize", (body.질문 or "").strip(),
                     body.품목, body.금액, body.용도, body.사업명,
                     body.f5.친족거래, body.f5.전직임직원업체)
    캐시 = 가드.꺼내기(k)

    def gen():
        yield _sse("진행", {"단계": "정규화", "설명": "질문에서 사실을 뽑는 중"})
        if 캐시 is not None:
            yield _sse("결과", 캐시)
            yield _sse("완료", {"캐시": True})
            return
        try:
            out = dict(_목_정규화) if MOCK else _실_정규화(body)
        except Exception as e:                                # noqa: BLE001
            # 🔴 모든 실패의 기본값은 판단불가다. 500 을 던지지 않는다
            yield _sse("오류", {"메시지": "정규화에 실패했습니다", "종류": type(e).__name__})
            yield _sse("완료", {"실패": True})
            return
        # 목/실 공통 — 질문원문(합성 문장)을 싣는다. 저장·검색·표시 전용이다
        out.setdefault("질문원문", _합성_질문(body))
        가드.넣기(k, out)
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
    if body.확정비목 and body.확정비목 not in 비목_ENUM:
        raise HTTPException(422, f"비목은 enum 10종 뿐입니다: {비목_ENUM}")

    k = 비용가드.열쇠("judge", body.org_id, body.사업명, body.확정비목,
                     json.dumps(body.정규화, ensure_ascii=False, sort_keys=True),
                     body.f5.친족거래, body.f5.전직임직원업체, 목)
    캐시 = 가드.꺼내기(k)

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
            가드.넣기(k, out)
        else:
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
            가드.넣기(k, out)
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
        from retrieve import 질문벡터  # type: ignore
        질문벡터("워밍업")
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

def _실_정규화(body: 정규화요청) -> dict:
    if body.질문 and body.질문.strip():
        from normalize_run import 정규화
        out, _메타 = 정규화(body.질문, 비목목록=비목_ENUM)
    else:
        # 🔴 폼 경로 — 품목·금액·용도가 이미 구조화돼 왔다. 다시 LLM 으로 뽑을 게 없다
        #    (합성 문장을 되짚어 넣는 건 정보를 잃는다). 비목후보는 화면 9 에서
        #    사용자가 직접 확정하므로 여기서 추측하지 않는다.
        용도 = body.용도 or ""
        if body.추가설명:
            용도 = f"{용도} ({body.추가설명})".strip()
        out = {"품목": body.품목, "금액": body.금액, "금액_추정여부": False,
               "용도": 용도, "비목후보": []}
    out.setdefault("결제수단", None)
    out.setdefault("구매명의", None)
    out.setdefault("신청일", None)
    out.setdefault("비교견적", None)
    # 🔴 자연어 경로(`normalize_run.정규화`)는 지급수수료류에서 하위항목을 뽑아 실을 수
    #    있다 — 폼 경로는 못 뽑으니 없애지 말고 None 으로라도 키를 살린다. 경로마다
    #    키가 있다 없다 하면 프론트가 `'하위항목' in 결과` 로 분기할 때 갈린다
    #    (§8 「실서버로 갈아끼워도 프론트 코드는 한 줄도 안 바뀐다」, 2026-09-01 ai-14).
    out.setdefault("하위항목", None)
    return out


def _실_판정(body: 판정요청) -> dict:
    import orchestrate
    질문 = body.정규화.get("_원문") or body.정규화.get("질문") or ""
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
    r = orchestrate.판정(질문, 사업명=body.사업명, org_id=body.org_id, plan_id=body.plan_id)
    # 오케 반환값 → API 계약 (`프론트 연동 사양.md` §8). 이름이 다른 것만 옮긴다
    return {
        "판정": r.get("판정", "판단불가"),
        "요약": r.get("요약", ""),
        "해야할일": r.get("해야할일", []),
        "인용": r.get("인용", []),
        "전제": r.get("전제") or r.get("전제목록") or [],
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
    확인("확률·점수 필드가 없다 (§3 ❌)",
         not any(k in j for k in ("확률", "점수", "confidence", "score")))

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
