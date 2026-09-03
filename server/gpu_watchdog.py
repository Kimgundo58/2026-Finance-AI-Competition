# -*- coding: utf-8 -*-
"""GPU 유휴 워치독 — 안 쓰는 동안 RunPod 팟을 멈춘다.

**GPU 를 쓰는 지점은 둘뿐이다** (2026-09-03 실측):
`POST /api/normalize` 의 질문 경로(①정규화 LLM) · `POST /api/judge` 의 실판정(④조립 LLM).
목록·상세·할일·vocab·프로필은 DB 만 치고, 임베딩 KURE-v1 은 CPU 다 (`retrieve.py:145`).
→ **「유휴」 = 위 두 호출을 안 한 시간.** 다른 화면은 아무리 눌러도 유휴다.

3층이다. ③ 이 없으면 30분 뒤 서비스가 그냥 죽는다.

    ① 서버 워치독(권위)  `한번_검사()` 를 60초마다 — 유휴 초과면 pod stop
       🔴 브라우저는 신뢰하지 않는다. 탭을 그냥 닫으면 모달이 뜰 기회가 없다
    ② 프론트용 계약      GET /api/gpu/status · POST /api/gpu/keepalive
    ③ 자동 재기동        `기동_진행()` — 꺼진 상태로 요청이 오면 pod start 후 폴링
       🔴 안내는 **기존** SSE `진행` 이벤트의 단계·설명으로만 낸다. 이 모듈은
          이벤트 이름을 모른다 — `{"단계","설명"}` dict 만 yield 하고 감싸는 건 main.py 다

🔴 **잘못 끄는 게 돈보다 비싸다.** 정지는 fail-closed 가 아니라 fail-**open** 이다 —
   키가 없거나 API 가 실패하면 **끄지 않고 로그만 남긴다.** 반대로 판정 게이트는
   상태를 모를 때 통과시킨다 (`알수없음` 은 막지 않는다).

환경변수
    SUDDOE_GPU_IDLE_MIN       유휴 임계 분. 기본 30. **0 이면 워치독 전체 비활성**
                              (심사 당일 이걸 건다 — 상태 API 는 살아 있고 정지만 안 한다)
    SUDDOE_GPU_WARN_MIN       프론트 모달 예고 분. 기본 5
    SUDDOE_GPU_CHECK_SEC      검사 주기. 기본 60
    SUDDOE_GPU_START_SEC      기동 후 준비 대기 상한. 기본 300
    SUDDOE_GPU_POLL_SEC       기동 중 폴링 주기. 기본 5
    SUDDOE_GPU_KEEPALIVE_MAX_MIN  🔴 keepalive 로 미룰 수 있는 최대 분. 기본 60.
                              마지막 «실제 GPU 호출» 기준이다 — 그 뒤로는 keepalive 를
                              아무리 쳐도 정지한다 (`생존신호()` 가 이유)
    RUNPOD_API_KEY            없으면 «제어 불가» — 절대 끄지 않는다
    RUNPOD_POD_ID             대상 팟. 없으면 «제어 불가»
    RUNPOD_REST               기본 https://rest.runpod.io/v1
    VLLM_URL                  준비 확인 대상 (`scripts/adapter.py` 와 같은 변수)
    SUDDOE_MOCK               🔴 1(기본)이면 워치독 전체 비활성 — 목 서버가 실 팟을
                              끄거나 켜는 사고를 막는다. `_목모드()` 가 기준
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator

from fastapi import APIRouter

_log = logging.getLogger("suddoe.gpu")

# 상태 어휘는 프론트 계약이다. 넷째는 밖으로 안 나간다 — `현황()` 이 「가동」으로 접는다.
가동, 중지, 기동중, 알수없음 = "가동", "중지", "기동중", "알수없음"


class GPU기동실패(RuntimeError):
    """팟을 못 깨웠다. `_실_판정` 의 기존 except 경로가 이걸 판단불가로 닫는다."""


def _int환경(키: str, 기본: int) -> int:
    try:
        return int(os.environ.get(키, 기본))
    except ValueError:
        return 기본


def _목모드() -> bool:
    """🔴 **목 서버가 실 GPU 팟을 끄는 사고를 여기서 막는다.**

    목 서버는 GPU 를 «절대» 안 부른다 (`judge` 는 `elif MOCK` 분기에서 닫히고
    `_실_판정` 까지 안 간다). 그래서 목 서버의 유휴 시각은 **영원히 안 갱신된다** —
    워치독을 그대로 두면 30분 뒤 목 서버가 실 팟에 stop 을 쏜다. 켜는 쪽도 같다:
    목 서버가 팟을 깨우면 아무도 안 쓰는 GPU 가 돈다.

    `SUDDOE_MOCK` 기본값이 **1** 이라(`server/_common.py:23`) 더 위험하다 —
    환경변수를 안 준 배포는 전부 목이다. 식을 `_common.MOCK` 과 같게 유지한다
    (import 하지 않는 이유는 이 모듈이 `_common` 없이도 단독으로 돌아야 하기 때문).
    """
    return os.environ.get("SUDDOE_MOCK", "1") == "1"


# ════════════════════════════════════════════════════════════════════
# 팟 제어 — 주입 가능한 자리. 테스트는 실제 팟을 켜지 않는다
# ════════════════════════════════════════════════════════════════════

class 팟제어:
    """기본 구현은 «아무것도 못 한다» 다. 키가 없을 때 이게 쓰인다.

    🔴 `가능=False` 면 워치독은 정지도 기동도 시도하지 않는다. 로그만 남긴다.
    """

    가능 = False
    사유 = "RUNPOD_API_KEY 또는 RUNPOD_POD_ID 가 없다"

    def 상태(self) -> str:
        return 알수없음

    def 시작(self) -> bool:
        return False

    def 정지(self) -> bool:
        return False


class RunPod팟(팟제어):
    """RunPod REST 로 pod start/stop.

    ⚠️ 응답 스키마는 팟을 실제로 켜 본 적이 없어 **미검증**이다 (S4 는 기동 금지).
       `desiredStatus` 가 RUNNING 이면 가동, EXITED/STOPPED 면 중지로 읽는다.
       실물로 한 번 확인할 것 — 그때까지 `_상태해석` 만 고치면 된다.
    """

    가능 = True

    def __init__(self, 키: str, 팟id: str, base: str | None = None, 타임아웃: int = 15):
        self.키, self.팟id = 키, 팟id
        self.base = (base or os.environ.get("RUNPOD_REST", "https://rest.runpod.io/v1")).rstrip("/")
        self.타임아웃 = 타임아웃
        self.사유 = ""

    def _호출(self, 경로: str, 메서드: str = "GET") -> dict:
        req = urllib.request.Request(
            f"{self.base}{경로}", method=메서드,
            headers={"Authorization": f"Bearer {self.키}", "Content-Type": "application/json"},
            data=b"" if 메서드 == "POST" else None)
        with urllib.request.urlopen(req, timeout=self.타임아웃) as r:
            본문 = r.read().decode("utf-8", "replace")
        return json.loads(본문) if 본문.strip() else {}

    @staticmethod
    def _상태해석(d: dict) -> str:
        s = str(d.get("desiredStatus") or d.get("status") or "").upper()
        if s == "RUNNING":
            return 가동
        if s in ("EXITED", "STOPPED", "TERMINATED"):
            return 중지
        return 기동중 if s else 알수없음

    def 상태(self) -> str:
        try:
            return self._상태해석(self._호출(f"/pods/{self.팟id}"))
        except Exception as e:                                    # noqa: BLE001
            # 🔴 조회 실패를 «중지» 로 읽으면 안 된다 — 그러면 게이트가 판정을 막는다
            _log.warning("팟 상태 조회 실패 — 알수없음으로 둔다: %s", type(e).__name__)
            return 알수없음

    def 시작(self) -> bool:
        try:
            self._호출(f"/pods/{self.팟id}/start", "POST")
            return True
        except Exception as e:                                    # noqa: BLE001
            _log.error("pod start 실패: %s %s", type(e).__name__, e)
            return False

    def 정지(self) -> bool:
        try:
            self._호출(f"/pods/{self.팟id}/stop", "POST")
            _log.warning("GPU 팟 정지 요청 보냄: %s", self.팟id)
            return True
        except Exception as e:                                    # noqa: BLE001
            # 🔴 실패는 «안 끈 것» 이다. 재시도는 다음 주기가 알아서 한다
            _log.error("pod stop 실패 — 팟은 그대로 돈다: %s %s", type(e).__name__, e)
            return False


def 기본제어() -> 팟제어:
    키, 팟 = os.environ.get("RUNPOD_API_KEY", ""), os.environ.get("RUNPOD_POD_ID", "")
    if not 키 or not 팟:
        _log.info("GPU 워치독: 제어 불가 (키·팟id 없음) — 상태만 보고하고 끄지 않는다")
        return 팟제어()
    return RunPod팟(키, 팟)


def _vllm_준비() -> bool:
    url = os.environ.get("VLLM_URL", "http://localhost:8000").rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception:                                             # noqa: BLE001
        return False


# ════════════════════════════════════════════════════════════════════
# 워치독
# ════════════════════════════════════════════════════════════════════

class GPU워치독:
    """마지막 GPU 호출 시각 하나가 전부다. 나머지는 그 위의 산수다.

    `시계`·`잠들기`·`제어`·`준비확인` 이 전부 주입 가능하다 —
    시간을 앞당겨 정지 조건을 **실제로 발동시켜** 볼 수 있어야 한다.
    """

    def __init__(self, 제어: 팟제어 | None = None, *,
                 시계: Callable[[], float] = time.monotonic,
                 잠들기: Callable[[float], None] = time.sleep,
                 준비확인: Callable[[], bool] = _vllm_준비):
        self.목모드 = _목모드()
        self.제어 = 제어 if 제어 is not None else 기본제어()
        self.시계, self.잠들기, self.준비확인 = 시계, 잠들기, 준비확인
        self.유휴임계초 = _int환경("SUDDOE_GPU_IDLE_MIN", 30) * 60
        self.예고초 = _int환경("SUDDOE_GPU_WARN_MIN", 5) * 60
        self.검사주기초 = _int환경("SUDDOE_GPU_CHECK_SEC", 60)
        self.기동상한초 = _int환경("SUDDOE_GPU_START_SEC", 300)
        self.폴링주기초 = _int환경("SUDDOE_GPU_POLL_SEC", 5)
        self.연장상한초 = _int환경("SUDDOE_GPU_KEEPALIVE_MAX_MIN", 60) * 60
        self._락 = threading.RLock()
        self._마지막호출 = self.시계()      # GPU 를 «실제로» 쓴 시각 (권위)
        self._마지막생존 = self.시계()      # keepalive — 「사람이 화면 앞에 있다」일 뿐
        self._팟상태 = 알수없음          # 🔴 부팅 직후엔 모른다. 모르면 «막지 않는다»
        self._스레드: threading.Thread | None = None
        self._멈춤 = threading.Event()

    # ── 활성 여부 ───────────────────────────────────────────────────
    @property
    def 활성(self) -> bool:
        """비활성이면 정지도 기동도 안 한다. 상태 API 는 그대로 산다.

        두 가지가 비활성으로 접힌다:
          · `SUDDOE_GPU_IDLE_MIN=0` — 심사 당일 이걸 건다
          · 🔴 **목 모드** — 목 서버는 GPU 를 안 부르니 영원히 유휴다 (`_목모드` 참조)
        """
        return (not self.목모드) and self.유휴임계초 > 0

    # ── ① 마지막 호출 시각 ─────────────────────────────────────────
    def 호출기록(self) -> None:
        """GPU 를 **실제로 쓰는** 자리에서만 부른다 (`_실_정규화` LLM 분기 · `_실_판정`).

        🔴 `keepalive()` 와 «다른 무게» 다. 이것만이 연장 상한을 되감는다.
        """
        with self._락:
            self._마지막호출 = self._마지막생존 = self.시계()

    def 생존신호(self) -> None:
        """keepalive 전용. 🔴 GPU 호출과 **같은 무게로 취급하지 않는다.**

        `/api/gpu/keepalive` 는 인증 없이 열린 자리다(게스트도 쳐야 하니까). 이걸
        `호출기록()` 과 동일하게 두면 **워치독이 무력화된다** — 공격이 아니라 정상
        사용에서 먼저 난다: 프론트가 모달 전에 keepalive 를 자동 전송하도록 짜면
        탭 하나 열어둔 것만으로 팟이 영원히 산다. 그게 이 워치독을 만든 이유다.

        그래서 keepalive 는 마지막 **실제 GPU 호출** 로부터
        `SUDDOE_GPU_KEEPALIVE_MAX_MIN`(기본 60분)까지만 유휴를 미룬다.
        그 뒤로는 아무리 쳐도 정지가 발동한다 — 판정을 한 건이라도 더 해야 풀린다.
        """
        with self._락:
            self._마지막생존 = self.시계()

    @property
    def 유휴초(self) -> float:
        """마지막 «유효 활동» 으로부터의 초.

        유효 활동 = 실제 GPU 호출, 또는 그로부터 `연장상한초` 안에 들어온 keepalive.
        상한 밖의 keepalive 는 **없는 것으로 친다** (`생존신호` 참조).
        """
        with self._락:
            상한 = self._마지막호출 + self.연장상한초
            유효 = max(self._마지막호출, min(self._마지막생존, 상한))
            return max(0.0, self.시계() - 유효)

    def _종료예정초(self) -> float | None:
        """None = 「끌 계획이 없다」. 프론트는 None 이면 모달을 띄우면 안 된다."""
        if not self.활성 or not self.제어.가능:
            return None
        if self._팟상태 == 중지:
            return None
        return max(0.0, self.유휴임계초 - self.유휴초)

    # ── ② 프론트 계약 ──────────────────────────────────────────────
    def 현황(self) -> dict:
        상태 = self._팟상태
        # 🔴 프론트 어휘는 가동|중지|기동중 셋뿐이다. 「알수없음」은 가동으로 접는다 —
        #    종료예정초 가 null 인 것이 「모른다」의 신호다
        with self._락:
            return {
                "상태": 상태 if 상태 in (가동, 중지, 기동중) else 가동,
                "유휴초": int(self.유휴초),
                "종료예정초": (None if self._종료예정초() is None
                            else int(self._종료예정초())),
                # ↓ 부가 필드. 프론트가 임계값을 하드코딩하지 않게 같이 준다
                "경고초": self.예고초,
                "유휴임계초": self.유휴임계초 if self.활성 else 0,
                "제어가능": self.제어.가능,
            }

    def keepalive(self) -> dict:
        self.생존신호()
        return self.현황()

    # ── ① 60초 주기 검사 ───────────────────────────────────────────
    def 한번_검사(self) -> str:
        """한 번 돌고 «무슨 일이 있었는지» 를 문자열로 돌려준다.

        반환값이 있는 이유는 테스트가 정지 조건을 발동시켰는지 **관측**해야 하기 때문이다.
        """
        if not self.활성:
            return "비활성"
        if not self.제어.가능:
            # 🔴 여기서 끄면 안 된다. 끌 수단이 없다는 뜻이고, 있어도 끄면 안 된다
            return "제어불가"
        유휴 = self.유휴초
        if 유휴 < self.유휴임계초:
            return f"대기 (유휴 {int(유휴)}초 < {self.유휴임계초}초)"
        상태 = self.제어.상태()
        if 상태 == 중지:
            self._팟상태 = 중지
            return "이미중지"
        if 상태 == 알수없음:
            # 조회가 안 되는 상태에서 stop 을 쏘면 무엇에 쏘는지 모른다. 다음 주기에 다시 본다
            return "상태불명 — 끄지 않음"
        self._팟상태 = 상태          # 조회가 성공했다 — 상태 API 가 실물을 비추게 한다
        if not self.제어.정지():
            return "정지실패 — 끄지 않음"
        self._팟상태 = 중지
        _log.warning("GPU 유휴 %d분 초과 — 팟을 멈췄다", int(유휴 // 60))
        return "정지함"

    def 시작_루프(self) -> None:
        """main.py 가 기동 시 한 번 부른다. 비활성이면 스레드를 아예 안 만든다."""
        if not self.활성:
            _log.info("GPU 워치독 비활성 (SUDDOE_GPU_IDLE_MIN=0) — 정지 루프 없음")
            return
        if self._스레드 and self._스레드.is_alive():
            return

        def 루프():
            while not self._멈춤.wait(self.검사주기초):
                try:
                    self.한번_검사()
                except Exception:                                 # noqa: BLE001
                    # 🔴 루프가 죽으면 워치독이 통째로 사라진다. 무슨 일이 있어도 돈다
                    _log.exception("워치독 검사 실패 — 다음 주기에 다시 본다")

        self._스레드 = threading.Thread(target=루프, name="gpu-watchdog", daemon=True)
        self._스레드.start()
        _log.info("GPU 워치독 시작: 유휴 %d분 초과 시 정지, %d초 주기",
                  self.유휴임계초 // 60, self.검사주기초)

    def 정지_루프(self) -> None:
        self._멈춤.set()

    # ── ③ 자동 재기동 ──────────────────────────────────────────────
    def 기동_진행(self) -> Iterator[dict]:
        """꺼져 있으면 깨우면서 진행 상황을 흘린다. **예외를 던지지 않는다.**

        yield 되는 것은 `{"단계","설명"}` dict 뿐이다 — main.py 가 **기존** `진행`
        이벤트로 감싼다. 🔴 새 이벤트 이름을 만들면 프론트 파서가 깨진다.
        이미 가동 중이면 **아무것도 yield 하지 않는다** (평상시 이벤트열이 안 바뀐다).
        """
        if self.목모드 or not self.제어.가능:
            self.호출기록()
            return          # 목이거나(깨울 이유 없다) 우리가 끈 적이 없다(깨울 것도 없다)
        # 🔴 팟을 멈추는 주체는 이 워치독뿐이다. 한 번 「가동」으로 확인해 뒀고 그 뒤로
        #    유휴가 임계를 안 넘겼으면 정지가 발동했을 리 없다 —
        #    판정 한 건마다 RunPod API 를 치지 않는다. (밖에서 콘솔로 끈 경우는 못 잡는다.
        #    그때는 유휴가 쌓여 다음 판정의 조회에서 걸린다)
        건너뜀 = self._팟상태 == 가동 and (not self.활성 or self.유휴초 < self.유휴임계초)
        self.호출기록()
        if 건너뜀:
            return
        상태 = self.제어.상태()
        if 상태 in (가동, 알수없음):
            self._팟상태 = 상태
            return
        self._팟상태 = 기동중
        yield {"단계": "기동", "설명": "AI 서버를 깨우는 중입니다"}
        if 상태 == 중지 and not self.제어.시작():
            self._팟상태 = 중지
            _log.error("pod start 실패 — 판정은 판단불가로 닫힌다")
            return
        마감 = self.시계() + self.기동상한초
        while self.시계() < 마감:
            if self.준비확인():
                self._팟상태 = 가동
                self.호출기록()          # 기동에 쓴 시간은 유휴가 아니다
                yield {"단계": "기동", "설명": "AI 서버가 준비됐습니다"}
                return
            self.잠들기(self.폴링주기초)
            남 = int(마감 - self.시계())
            yield {"단계": "기동",
                   "설명": f"AI 서버를 깨우는 중입니다 (최대 {max(0, 남)}초)"}
        self._팟상태 = 기동중
        _log.error("기동 대기 %d초 초과 — 판단불가로 닫는다", self.기동상한초)

    def 게이트(self) -> None:
        """`_실_정규화`·`_실_판정` 첫 줄에서 부른다.

        🔴 «모르면 통과» 다. 막는 건 확실히 중지·기동중일 때뿐 —
        상태 조회가 흔들린다고 판정이 막히면 안 된다.
        """
        self.호출기록()
        if self.목모드 or not self.제어.가능:
            return
        if self._팟상태 in (중지, 기동중):
            raise GPU기동실패(f"AI 서버가 준비되지 않았습니다 (상태={self._팟상태})")


워치독 = GPU워치독()


# ════════════════════════════════════════════════════════════════════
# ② 라우터 — 프론트 계약. main.py 는 include_router 한 줄만 더한다
# ════════════════════════════════════════════════════════════════════

router = APIRouter(prefix="/api/gpu", tags=["gpu"])


@router.get("/status")
def gpu_status() -> dict:
    """`{"상태":"가동|중지|기동중","유휴초":n,"종료예정초":n|null, ...}`

    🔴 캐시된 값을 돌려준다 — 프론트가 몇 초마다 폴링해도 RunPod API 를 안 친다.
       실제 조회는 60초 주기 루프와 기동 경로가 한다.
    """
    return 워치독.현황()


@router.post("/keepalive")
def gpu_keepalive() -> dict:
    """유휴 타이머를 리셋한다. 모달의 「더 쓸게요」 가 여기를 친다.

    🔴 이건 **보조**다. 브라우저가 이걸 못 쳐도(탭을 그냥 닫아도) ① 이 끈다.
    """
    return 워치독.keepalive()
