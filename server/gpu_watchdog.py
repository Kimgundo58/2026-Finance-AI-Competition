# -*- coding: utf-8 -*-
"""GPU 유휴 워치독 — 안 쓰는 동안 RunPod 팟을 멈춘다.

GPU 를 쓰는 지점은 `/api/normalize` 의 질문 경로와 `/api/judge` 의 실판정 둘뿐이다.
「유휴」 = 그 두 호출을 안 한 시간.

    ① 서버 워치독      `한번_검사()` 를 주기적으로 — 유휴 초과면 pod stop
    ② 프론트용 계약    GET /api/gpu/status · POST /api/gpu/keepalive
    ③ 자동 재기동      `기동_진행()` — 꺼진 상태로 요청이 오면 pod start 후 폴링

정지는 fail-open 이다 — 키가 없거나 API 가 실패하면 끄지 않고 로그만 남긴다.
판정 게이트는 상태를 모를 때 통과시킨다.

환경변수
    SUDDOE_GPU_IDLE_MIN       유휴 임계 분. 기본 30. 0 이면 워치독 전체 비활성
    SUDDOE_GPU_WARN_MIN       프론트 모달 예고 분. 기본 5
    SUDDOE_GPU_CHECK_SEC      검사 주기. 기본 60
    SUDDOE_GPU_START_SEC      기동 후 준비 대기 상한. 기본 300
    SUDDOE_GPU_POLL_SEC       기동 중 폴링 주기. 기본 5
    SUDDOE_GPU_KEEPALIVE_MAX_MIN  keepalive 로 미룰 수 있는 최대 분. 기본 60.
                              마지막 실제 GPU 호출 기준 — 그 뒤로는 keepalive 를 쳐도 정지한다
    SUDDOE_GPU_WAKE_DAILY_CAP 하루 실제 기동(정지→시작) 상한. 기본 3.
                              넘으면 시작을 시도하지 않고 중지로 남긴다
    RUNPOD_API_KEY            없으면 제어 불가 — 절대 끄지 않는다
    RUNPOD_POD_ID             대상 팟. 없으면 제어 불가
    RUNPOD_REST               기본 https://rest.runpod.io/v1
    VLLM_URL                  준비 확인 대상
    SUDDOE_MOCK               1(기본)이면 워치독 전체 비활성
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Iterator

from fastapi import APIRouter

_log = logging.getLogger("suddoe.gpu")

# 상태 어휘는 프론트 계약이다. 넷째는 밖으로 안 나간다 — `현황()` 이 「가동」으로 접는다.
가동, 중지, 기동중, 알수없음 = "가동", "중지", "기동중", "알수없음"


class GPU기동실패(RuntimeError):
    """팟을 못 깨웠다. 판정 경로가 이걸 판단불가로 닫는다."""


def _int환경(키: str, 기본: int) -> int:
    try:
        return int(os.environ.get(키, 기본))
    except ValueError:
        return 기본


def _float환경(키: str, 기본: float) -> float:
    try:
        return float(os.environ.get(키, 기본))
    except ValueError:
        return 기본


def _목모드() -> bool:
    """목 서버는 GPU 를 안 부르므로 영원히 유휴다 — 워치독을 통째로 끈다.

    식은 `_common.MOCK` 과 같다. 이 모듈이 `_common` 없이 단독으로 돌아야 해서 import 하지 않는다.
    """
    return os.environ.get("SUDDOE_MOCK", "1") == "1"


# ════════════════════════════════════════════════════════════════════
# 팟 제어 — 주입 가능한 자리. 테스트는 실제 팟을 켜지 않는다
# ════════════════════════════════════════════════════════════════════

class 팟제어:
    """기본 구현은 아무것도 못 한다. `가능=False` 면 워치독은 정지도 기동도 시도하지 않는다."""

    가능 = False
    사유 = "RUNPOD_API_KEY 또는 RUNPOD_POD_ID 가 없다"

    def 상태(self) -> str:
        return 알수없음

    def 시작(self) -> bool:
        return False

    def 정지(self) -> bool:
        return False

    def 잔액(self) -> float | None:
        """항상 None — RunPod REST v1 에 잔액 조회 엔드포인트가 없다."""
        return None


class RunPod팟(팟제어):
    """RunPod REST 로 pod start/stop. `desiredStatus` RUNNING=가동, EXITED/STOPPED=중지."""

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
            # 조회 실패를 중지로 읽으면 게이트가 판정을 막는다 — 알수없음으로 둔다
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
            # 실패는 안 끈 것이다. 재시도는 다음 주기가 한다
            _log.error("pod stop 실패 — 팟은 그대로 돈다: %s %s", type(e).__name__, e)
            return False


def _팟id() -> str:
    """`ops.gpu_pod.pod_id` 우선, `RUNPOD_POD_ID` env 폴백. 팟이 바뀌어도 재배포가 필요 없다."""
    try:
        from _lib import db                                       # noqa: PLC0415
        with db.connect() as conn:
            r = conn.execute("SELECT pod_id FROM ops.gpu_pod WHERE id='default'").fetchone()
        if r and r[0]:
            return str(r[0])
    except Exception:                                             # noqa: BLE001
        pass                # 스키마 미적용·DB 없음 — env 로 간다
    return os.environ.get("RUNPOD_POD_ID", "")


def 기본제어() -> 팟제어:
    키, 팟 = os.environ.get("RUNPOD_API_KEY", ""), _팟id()
    if not 키 or not 팟:
        _log.info("GPU 워치독: 제어 불가 (키·팟id 없음) — 상태만 보고하고 끄지 않는다")
        return 팟제어()
    return RunPod팟(키, 팟)


def _vllm_준비() -> bool:
    """vLLM `/health` 응답 여부. User-Agent 를 꼭 실어야 한다 — 없으면 RunPod 프록시가 403 으로 끊는다."""
    # 주소는 `adapter.vllm_url()` 로 얻는다 — 판정 경로와 같은 주소를 봐야 한다.
    try:
        from adapter import vllm_url                              # noqa: PLC0415
        base = vllm_url()
    except Exception:                                             # noqa: BLE001
        base = os.environ.get("VLLM_URL", "http://localhost:8000")
    url = base.rstrip("/") + "/health"
    req = urllib.request.Request(url, headers={"User-Agent": "suddoe-gpu/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:                                        # noqa: BLE001
        _log.warning("vLLM 헬스체크 실패 — %s: %s (url=%s)", type(e).__name__, e, url)
        return False


# ════════════════════════════════════════════════════════════════════
# 워치독
# ════════════════════════════════════════════════════════════════════

class GPU워치독:
    """마지막 GPU 호출 시각을 기준으로 정지·기동을 판단한다.

    `시계`·`잠들기`·`제어`·`준비확인` 이 전부 주입 가능하다 — 테스트가 시간을 앞당길 수 있다.
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
        self.일일깨우기캡 = _int환경("SUDDOE_GPU_WAKE_DAILY_CAP", 3)
        self._락 = threading.RLock()
        self._마지막호출 = self.시계()      # GPU 를 실제로 쓴 시각 (권위)
        self._마지막생존 = self.시계()      # keepalive — 「사람이 화면 앞에 있다」일 뿐
        self._팟상태 = 알수없음          # 부팅 직후엔 모른다. 모르면 막지 않는다
        self._깨움날짜 = self._오늘()
        self._오늘_깨움 = 0               # 실제 stop→start 왕복 횟수. 가동 중 재사용은 안 센다
        self._스레드: threading.Thread | None = None
        self._멈춤 = threading.Event()
        self._vllm_최근확인: bool | None = None   # None = 아직 한 번도 안 봄
        self._vllm_확인시각 = 0.0

    # ── 활성 여부 ───────────────────────────────────────────────────
    @property
    def 활성(self) -> bool:
        """비활성이면 정지도 기동도 안 한다. 상태 API 는 그대로 산다. 목 모드·IDLE_MIN=0 이 비활성."""
        return (not self.목모드) and self.유휴임계초 > 0

    # ── ① 마지막 호출 시각 ─────────────────────────────────────────
    def 호출기록(self) -> None:
        """GPU 를 실제로 쓰는 자리에서만 부른다. 이것만이 연장 상한을 되감는다."""
        with self._락:
            self._마지막호출 = self._마지막생존 = self.시계()

    def 생존신호(self) -> None:
        """keepalive 전용. 마지막 실제 GPU 호출로부터 `연장상한초` 까지만 유휴를 미룬다.

        인증 없이 열린 자리라 GPU 호출과 같은 무게로 두면 탭 하나로 팟이 영원히 산다.
        """
        with self._락:
            self._마지막생존 = self.시계()

    # ── 하루 깨우기 캡 ──────────────────────────────────────────────
    # 날짜 경계는 `비용가드._오늘()`(server/main.py)과 같은 UTC 자정이다.
    @staticmethod
    def _오늘() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _날짜_롤오버(self) -> None:
        오늘 = self._오늘()
        if self._깨움날짜 != 오늘:
            self._깨움날짜, self._오늘_깨움 = 오늘, 0

    def 깨우기_허용(self) -> bool:
        """실제 stop→start 왕복을 하나 더 해도 되는가. 가동 중 재사용·폴링은 여기 안 걸린다."""
        with self._락:
            self._날짜_롤오버()
            return self._오늘_깨움 < self.일일깨우기캡

    def _깨움_기록(self) -> None:
        with self._락:
            self._날짜_롤오버()
            self._오늘_깨움 += 1

    @property
    def 유휴초(self) -> float:
        """마지막 유효 활동(실제 GPU 호출, 또는 연장상한 안의 keepalive)으로부터의 초."""
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

    # ── vLLM 헬스 — «팟은 가동인데 모델은 죽어있다» 를 감추지 않는다 ─────────
    # `제어.가능`·`활성` 과 무관하게 동작한다.
    def _vllm_상태(self) -> bool | None:
        """캐시된 vLLM `/health`. `검사주기초` 만큼 캐시한다. 목모드에선 안 친다."""
        if self.목모드:
            return None
        with self._락:
            지금 = self.시계()
            신선 = (self._vllm_최근확인 is not None
                   and (지금 - self._vllm_확인시각) < self.검사주기초)
            if 신선:
                return self._vllm_최근확인
        결과 = self.준비확인()              # 락 밖 — HTTP 지연이 다른 요청을 안 막는다
        with self._락:
            self._vllm_최근확인, self._vllm_확인시각 = 결과, self.시계()
        return 결과

    # ── ② 프론트 계약 ──────────────────────────────────────────────
    def 현황(self) -> dict:
        상태 = self._팟상태
        # 프론트 어휘는 가동|중지|기동중 셋뿐이다. 「알수없음」은 가동으로 접는다.
        # `_vllm_상태()` 는 HTTP 를 칠 수 있으므로 락 밖에서 먼저 계산한다.
        vllm_응답 = self._vllm_상태()
        with self._락:
            self._날짜_롤오버()
            return {
                "상태": 상태 if 상태 in (가동, 중지, 기동중) else 가동,
                "유휴초": int(self.유휴초),
                "종료예정초": (None if self._종료예정초() is None
                            else int(self._종료예정초())),
                # 부가 필드. 프론트가 임계값을 하드코딩하지 않게 같이 준다
                "경고초": self.예고초,
                "유휴임계초": self.유휴임계초 if self.활성 else 0,
                "제어가능": self.제어.가능,
                "오늘_깨움": self._오늘_깨움,
                "일일깨우기캡": self.일일깨우기캡,
                # 진단용: null=미확인, true/false=최근 vLLM /health 결과
                "vLLM_응답": vllm_응답,
                # false 면 「상태」 필드는 조회 실패를 가동으로 접은 값이다
                "팟_조회됨": self._팟상태 != 알수없음,
            }

    def keepalive(self) -> dict:
        self.생존신호()
        return self.현황()

    # ── ① 주기 검사 ────────────────────────────────────────────────
    def 한번_검사(self) -> str:
        """한 번 돌고 무슨 일이 있었는지를 문자열로 돌려준다 (테스트 관측용)."""
        if not self.활성:
            return "비활성"
        if not self.제어.가능:
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
        """기동 시 한 번 부른다. 비활성이면 스레드를 아예 안 만든다."""
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
                    # 루프가 죽으면 워치독이 통째로 사라진다. 무슨 일이 있어도 돈다
                    _log.exception("워치독 검사 실패 — 다음 주기에 다시 본다")

        self._스레드 = threading.Thread(target=루프, name="gpu-watchdog", daemon=True)
        self._스레드.start()
        _log.info("GPU 워치독 시작: 유휴 %d분 초과 시 정지, %d초 주기",
                  self.유휴임계초 // 60, self.검사주기초)

    def 정지_루프(self) -> None:
        self._멈춤.set()

    # ── ③ 자동 재기동 ──────────────────────────────────────────────
    def 기동_진행(self) -> Iterator[dict]:
        """꺼져 있으면 깨우면서 진행 상황을 흘린다. 예외를 던지지 않는다.

        yield 되는 것은 `{"단계","설명"}` dict 뿐이다 — main.py 가 기존 `진행` 이벤트로 감싼다.
        이미 가동 중이면 아무것도 yield 하지 않는다.
        """
        if self.목모드 or not self.제어.가능:
            self.호출기록()
            return          # 목이거나(깨울 이유 없다) 우리가 끈 적이 없다(깨울 것도 없다)
        # 팟을 멈추는 주체는 이 워치독뿐이다. 가동 확인 후 유휴가 임계를 안 넘겼으면
        # 정지가 발동했을 리 없다 — 판정마다 RunPod API 를 치지 않는다.
        건너뜀 = self._팟상태 == 가동 and (not self.활성 or self.유휴초 < self.유휴임계초)
        self.호출기록()
        if 건너뜀:
            return
        상태 = self.제어.상태()
        if 상태 in (가동, 알수없음):
            self._팟상태 = 상태
            return
        if 상태 == 중지 and not self.깨우기_허용():
            # 한도 초과 — 시도조차 안 한다. 팟상태는 중지로 남아 게이트가 판단불가로 닫는다
            self._팟상태 = 중지
            _log.error("오늘 GPU 기동 한도(%d회) 초과 — 시작을 시도하지 않는다", self.일일깨우기캡)
            yield {"단계": "기동",
                   "설명": f"오늘 GPU 기동 한도({self.일일깨우기캡}회)를 다 썼습니다. 내일 다시 시도해 주세요"}
            return
        self._팟상태 = 기동중
        yield {"단계": "기동", "설명": "AI 서버를 깨우는 중입니다"}
        if 상태 == 중지:
            self._깨움_기록()          # 시도 자체를 센다 — start() 가 실패해도 RunPod 호출은 났다
            if not self.제어.시작():
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
        """판정 경로 첫 줄에서 부른다. 확실히 중지·기동중일 때만 막는다 — 모르면 통과."""
        self.호출기록()
        if self.목모드 or not self.제어.가능:
            return
        if self._팟상태 in (중지, 기동중):
            raise GPU기동실패(f"AI 서버가 준비되지 않았습니다 (상태={self._팟상태})")


워치독 = GPU워치독()


# ════════════════════════════════════════════════════════════════════
# ② 라우터 — 프론트 계약
# ════════════════════════════════════════════════════════════════════

router = APIRouter(prefix="/api/gpu", tags=["gpu"])


@router.get("/status")
def gpu_status() -> dict:
    """`{"상태":"가동|중지|기동중","유휴초":n,"종료예정초":n|null, ...}` — 캐시된 값. RunPod API 를 안 친다."""
    return 워치독.현황()


@router.post("/keepalive")
def gpu_keepalive() -> dict:
    """유휴 타이머를 리셋한다. 모달의 「더 쓸게요」 가 여기를 친다."""
    return 워치독.keepalive()


# ── /wake — 판정 요청과 분리된 사전 기동 ──────────────────────────────
# 콜드부팅이 Cloud Run 요청 타임아웃보다 길어, 판정 SSE 안에서 기동하면 요청이 끊긴다.
# 화면이 먼저 `/wake` 를 치고 `/status` 를 폴링해 준비된 뒤 질문을 보낸다.
_기동_락 = threading.Lock()


def _백그라운드_기동() -> None:
    if not _기동_락.acquire(blocking=False):
        return                              # 이미 누가 기동 중이다 — 새로 안 띄운다
    try:
        for _ in 워치독.기동_진행():
            pass                            # 진행 상태는 self._팟상태 에 이미 반영된다
    except Exception:                       # noqa: BLE001
        _log.exception("백그라운드 기동 실패 — /status 가 다음 진실이다")
    finally:
        _기동_락.release()


@router.post("/wake")
def gpu_wake() -> dict:
    """즉시 반환한다. 기동은 백그라운드 스레드가 하고, 진행은 `GET /status` 폴링으로 본다.

    `호출기록()` 을 안 찍는다 — 유휴 타이머는 실제 판정(`게이트()`)만 되돌린다.
    """
    if not _기동_락.locked():
        threading.Thread(target=_백그라운드_기동, daemon=True, name="gpu-wake").start()
    return 워치독.현황()


@router.post("/reap")
def gpu_reap() -> dict:
    """`한번_검사()` 1회 실행. 외부 스케줄러가 치는 자리로 둔 것이며 아직 미배선이다."""
    결과 = 워치독.한번_검사()
    return {"결과": 결과, **워치독.현황()}
