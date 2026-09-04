# -*- coding: utf-8 -*-
"""GPU 워치독 — 정지·기동·하루 깨우기 캡이 «실제로 발동하는가» (Q2, 2026-09-04)

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_gpu_watchdog.py -q

🔴 이 파일이 막는 것은 «타이머가 돈다» 를 «비용이 준다» 의 증거로 쓰는 것이다
   (`docs/8_운영/8-3_GPU.md` §6 비용가드). `GPU워치독` 은 `시계`·`잠들기`·`제어`·
   `준비확인` 을 전부 주입받게 설계돼 있다 — 그 설계를 실제로 써서 정지 조건을
   **발동시켜** 본다. 실팟은 안 켠다(`팟제어` 페이크로 대체).

🔴 2026-09-04 실측(코드 읽기, 실행 아님) — «넷 다 이미 있다» 는 틀렸다:
     동시 팟 1개    → 코드 가드 없음. RUNPOD_POD_ID 가 한 개 값이라는 구조로만 성립
     하루 깨우기 3회 → 이 커밋 전까지 코드 어디에도 없었다. 이 파일에서 새로 테스트
     수명 1시간     → `scripts/runpod_pod.py`(수동 팟 오픈용)에만 있다. 이 워치독과 다른 코드경로
     잔액 경보      → RunPod REST v1 에 잔액 엔드포인트가 없다(openapi.json 확인).
                     서버 프로세스에서는 구현 불가 — `gpu_watchdog.팟제어.잔액()` 참고
   → 이 파일이 실제로 태우는 것은 **유휴 정지**와 **하루 깨우기 캡** 둘뿐이다.
     동시팟1·수명1시간·잔액경보는 이 파일로 "발동시켜 볼" 코드 자체가 없다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import gpu_watchdog as gw              # noqa: E402


class 가짜팟(gw.팟제어):
    """실물 대신 — 호출을 그대로 기록한다. 상태 전이는 테스트가 손으로 민다."""

    가능 = True

    def __init__(self, 초기상태: str = gw.가동):
        self._상태 = 초기상태
        self.정지호출 = 0
        self.시작호출 = 0
        self.상태조회횟수 = 0
        self.시작실패시킬까 = False

    def 상태(self) -> str:
        self.상태조회횟수 += 1
        return self._상태

    def 정지(self) -> bool:
        self.정지호출 += 1
        self._상태 = gw.중지
        return True

    def 시작(self) -> bool:
        self.시작호출 += 1
        if self.시작실패시킬까:
            return False
        self._상태 = gw.기동중          # 기동_진행 의 폴링 루프가 그 뒤 준비확인() 을 본다
        return True


class 가짜시계:
    """`time.monotonic` 대체. `흐르다()` 로 손으로 민다 — 실제로 안 잔다."""

    def __init__(self, 시작: float = 0.0):
        self.now = 시작

    def __call__(self) -> float:
        return self.now

    def 흐르다(self, 초: float) -> None:
        self.now += 초


def _새워치독(**환경) -> tuple[gw.GPU워치독, 가짜팟, 가짜시계]:
    """환경변수를 monkeypatch 하지 않고 생성자 인자로 흉내낸다 — `_int환경` 이
    os.environ 을 읽으므로, 여기서는 기본값을 직접 속성으로 덮어써 격리한다."""
    시계 = 가짜시계()
    팟 = 가짜팟()
    w = gw.GPU워치독(제어=팟, 시계=시계, 잠들기=lambda s: None, 준비확인=lambda: True)
    w.유휴임계초 = 환경.get("유휴임계초", 60)
    w.연장상한초 = 환경.get("연장상한초", 3600)
    w.일일깨우기캡 = 환경.get("일일깨우기캡", 3)
    w.기동상한초 = 환경.get("기동상한초", 300)
    return w, 팟, 시계


def 확인(설명: str, 조건: bool) -> None:
    if not 조건:
        raise AssertionError(f"실패: {설명}")
    print(f"[통과] {설명}")


# ════════════════════════════════════════════════════════════════════
# ① 유휴 정지 — «타이머가 돈다» 가 아니라 «stop() 이 실제로 불린다»
# ════════════════════════════════════════════════════════════════════

def test_유휴_초과하면_정지가_실제로_불린다():
    w, 팟, 시계 = _새워치독(유휴임계초=60)
    w.목모드 = False                      # SUDDOE_MOCK=1 기본이라 명시로 끈다
    w._팟상태 = gw.가동
    w.호출기록()

    확인("59초 유휴 — 아직 안 끈다", w.한번_검사().startswith("대기"))
    확인("정지() 아직 0회", 팟.정지호출 == 0)

    시계.흐르다(61)
    결과 = w.한번_검사()
    확인("61초 유휴 — «정지함» 을 반환", 결과 == "정지함")
    확인("팟제어.정지() 가 실제로 1회 불렸다", 팟.정지호출 == 1)
    확인("워치독 내부 상태도 중지로 갱신됐다", w._팟상태 == gw.중지)


def test_비활성이면_유휴가_쌓여도_정지_안_한다():
    """SUDDOE_GPU_IDLE_MIN=0 재현 — 심사 당일 이 값을 건다는 그 자리."""
    w, 팟, 시계 = _새워치독(유휴임계초=0)   # 0분 → 활성 False
    w.목모드 = False
    확인("유휴임계초=0 이면 비활성", w.활성 is False)
    w._팟상태 = gw.가동
    w.호출기록()
    시계.흐르다(999999)
    확인("비활성이면 검사 자체가 «비활성» 을 반환", w.한번_검사() == "비활성")
    확인("정지() 는 한 번도 안 불렸다", 팟.정지호출 == 0)


def test_keepalive는_상한을_넘으면_무력화된다():
    w, 팟, 시계 = _새워치독(유휴임계초=60, 연장상한초=120)
    w.목모드 = False
    w._팟상태 = gw.가동
    w.호출기록()                          # 마지막 «실제 호출» t=0

    시계.흐르다(100)
    w.생존신호()                          # keepalive — 상한(120) 안이라 유효
    확인("연장상한 안의 keepalive 는 유휴를 되돌린다", w.유휴초 < 60)

    시계.흐르다(130)                      # t=230 — 마지막 실제호출(t=0)로부터 상한(120) 훌쩍 밖
    w.생존신호()                          # 계속 쳐도 min(마지막생존,상한) 이 120 을 못 넘는다
    확인("상한 밖에선 유휴초가 계속 큰다(120 고정이 아니라)", w.유휴초 >= 100)
    결과 = w.한번_검사()
    확인("상한 밖 keepalive 는 무시되고 정지가 발동한다", 결과 == "정지함")


# ════════════════════════════════════════════════════════════════════
# ② 하루 깨우기 캡 — 2026-09-04 신설. 코드에 없던 가드
# ════════════════════════════════════════════════════════════════════

def test_하루_깨우기_캡을_넘으면_start를_아예_안_부른다():
    """🔴 «기동중이면 캐시로 재확인을 건너뛴다» (`기동_진행` 의 «건너뜀» 최적화) 를
    피하려고, 매 왕복 사이에 시계를 유휴임계초 이상 흘리고 `한번_검사()` 로 실제로
    정지시킨다 — 그래야 다음 `기동_진행()` 이 «새 깨우기» 로 잡힌다. 이게 실사용과
    같은 모양이다: 팟이 유휴로 꺼진 뒤에야 다음 요청이 다시 깨운다."""
    w, 팟, 시계 = _새워치독(일일깨우기캡=3, 유휴임계초=60)
    w.목모드 = False
    팟._상태 = gw.중지

    for i in range(3):
        list(w.기동_진행())               # 제너레이터 — 소비해야 부작용이 난다
        시계.흐르다(1000)                 # 유휴임계초를 훌쩍 넘긴다
        결과 = w.한번_검사()
        확인(f"{i+1}번째 기동 뒤 유휴로 실제 정지됨({결과})", 결과 == "정지함")

    확인("3회까지는 시작()이 3번 불렸다", 팟.시작호출 == 3)

    진행4 = list(w.기동_진행())
    확인("4번째는 시작()을 안 부른다 — 캡이 막는다", 팟.시작호출 == 3)
    확인("한도 초과 메시지를 낸다", any("한도" in d.get("설명", "") for d in 진행4))
    확인("팟상태는 중지로 남는다(가동으로 거짓 보고 안 함)", w._팟상태 == gw.중지)

    # 게이트() 는 «준비 안 됨» 을 판단불가로 닫아야 한다
    with pytest.raises(gw.GPU기동실패):
        w.게이트()


def test_이미_가동중이면_깨우기_캡을_안_먹는다():
    """가동 중인 팟을 계속 쓰는 것 — 「깨우기」 가 아니다. 캡을 소모하면 안 된다."""
    w, 팟, 시계 = _새워치독(일일깨우기캡=1, 유휴임계초=60)
    w.목모드 = False
    팟._상태 = gw.가동
    w._팟상태 = gw.가동
    w.호출기록()

    for _ in range(5):
        list(w.기동_진행())

    확인("가동 중 재사용은 시작() 을 한 번도 안 부른다", 팟.시작호출 == 0)
    확인("깨우기 카운터도 안 늘었다", w._오늘_깨움 == 0)


def test_기동중이면_시작을_또_안_부르고_폴링만_한다():
    """상태가 이미 «기동중»(다른 요청이 먼저 start 를 쐈다) 이면 start() 를 또 안 부른다."""
    w, 팟, 시계 = _새워치독(일일깨우기캡=3)
    w.목모드 = False
    팟._상태 = gw.기동중
    list(w.기동_진행())
    확인("기동중이었으면 시작()을 새로 안 부른다", 팟.시작호출 == 0)
    확인("깨우기 카운터도 안 늘었다(시도 자체가 없었으므로)", w._오늘_깨움 == 0)


def test_날짜가_바뀌면_캡이_리셋된다():
    w, 팟, 시계 = _새워치독(일일깨우기캡=1)
    확인("초기 허용", w.깨우기_허용() is True)
    w._깨움_기록()
    확인("1회 쓰면 캡(1) 소진", w.깨우기_허용() is False)

    # 날짜 롤오버를 흉내낸다 — _오늘() 을 다음날로 바꿔치기
    w._깨움날짜 = "2000-01-01"           # 오늘일 리 없는 값으로 강제 롤오버 유도
    확인("날짜가 바뀌면 다시 허용된다", w.깨우기_허용() is True)
    확인("카운터도 0으로 리셋됐다", w._오늘_깨움 == 0)


def test_현황에_깨우기_카운터가_실린다():
    w, 팟, 시계 = _새워치독(일일깨우기캡=3)
    w._깨움_기록()
    현황 = w.현황()
    확인("오늘_깨움 필드가 있다", 현황["오늘_깨움"] == 1)
    확인("일일깨우기캡 필드가 있다", 현황["일일깨우기캡"] == 3)


# ════════════════════════════════════════════════════════════════════
# ③ `_상태해석` — 실물 없이, RunPod 공식 OpenAPI 스키마 기준으로 «분기가 다 서는가»
#    🔴 이것만으론 «스키마가 맞다» 를 증명 못 한다 — 「실물이 오면 즉시 갈린다」
#    까지가 이 테스트의 몫이다(중앙 지시, 2026-09-04). 실물 확인은 항목1(왕복 창)
#    이 연 뒤 `scripts/runpod_pod.py roundtrip` 으로 한다.
#
#    2026-09-04 `https://rest.runpod.io/v1/openapi.json` 대조 결과(WebFetch):
#      · GET /pods/{id} 의 상태 필드는 **`desiredStatus` 하나뿐**이다.
#        enum 은 정확히 {RUNNING, EXITED, TERMINATED} 셋 — "STARTING"·"PENDING"
#        류의 네 번째 값이 **스키마에 없다.** `_상태해석` 의 `기동중 if s else 알수없음`
#        분기는 실물 desiredStatus 값으로는 절대 안 걸린다 — 이 워치독 안에서
#        `기동중` 은 100% **클라이언트가 스스로 매기는** 상태고(`기동_진행()` 이
#        `시작()` 부르기 직전에 손으로 찍는다), API 응답에서 읽어오는 값이 아니다.
#      · `status`(코드가 fallback 으로 읽는 필드) 는 스키마에 **없다** — 죽은 분기지만
#        해롭진 않다(그냥 항상 desiredStatus 로 떨어진다).
#      · `STOPPED` 매핑도 실물 enum 엔 없다(EXITED·TERMINATED 만 실재) — 방어적으로
#        남겨도 되지만 실물 대조 결과 「그 값은 절대 안 온다」.
#      · `desiredStatus` 설명이 "the current **expected** status" 다 — 「지금 진짜
#        상태」가 아니라 「목표 상태」일 수 있다는 뜻이다. `POST /start` 직후 바로
#        `상태()` 를 읽으면 실제 부팅 전인데도 RUNNING 이 올 가능성 — **이게 항목1
#        실물 왕복에서 제일 먼저 볼 것**이다(타이밍 갭 유무). `기동_진행()` 은 이미
#        `준비확인()`(vLLM /health)을 별도 게이트로 쓰므로 그 갭이 있어도 안전하지만,
#        `한번_검사()`(유휴 정지 판단)는 `상태()` 만 본다 — 갭이 크면 영향권이다.
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("응답,기대", [
    ({"desiredStatus": "RUNNING"}, gw.가동),
    ({"desiredStatus": "EXITED"}, gw.중지),
    ({"desiredStatus": "TERMINATED"}, gw.중지),
    ({"desiredStatus": "running"}, gw.가동),        # 대소문자 방어
    ({}, gw.알수없음),                              # 필드 자체가 없는 응답(장애·오탈자)
    ({"desiredStatus": ""}, gw.알수없음),
    ({"desiredStatus": None}, gw.알수없음),
    ({"status": "RUNNING"}, gw.가동),               # 스키마엔 없는 필드지만 fallback 은 살아있다
])
def test_상태해석_실물_스키마_커버리지(응답, 기대):
    확인(f"{응답} → {기대}", gw.RunPod팟._상태해석(응답) == 기대)


def test_상태해석_기동중은_API값으로는_절대_안_나온다():
    """실물 enum 3종(RUNNING/EXITED/TERMINATED) 전부를 넣어도 기동중이 안 나온다 —
    기동중은 API 응답이 아니라 이 워치독이 스스로 매기는 상태라는 걸 실물 스키마로 확인."""
    for s in ("RUNNING", "EXITED", "TERMINATED"):
        결과 = gw.RunPod팟._상태해석({"desiredStatus": s})
        확인(f"desiredStatus={s} 는 기동중이 아니다", 결과 != gw.기동중)


# ════════════════════════════════════════════════════════════════════
# ④ vLLM 헬스 — «팟은 가동인데 모델은 죽어있다» 사각지대 (ai-d7 실측 계기, 2026-09-04)
# ════════════════════════════════════════════════════════════════════

def test_vllm상태는_캐시되고_검사주기마다_새로_친다():
    호출횟수 = {"n": 0}

    def 가짜준비확인():
        호출횟수["n"] += 1
        return 호출횟수["n"] == 1        # 첫 호출만 True, 이후는 False

    시계 = 가짜시계()
    w = gw.GPU워치독(제어=가짜팟(), 시계=시계, 잠들기=lambda s: None,
                    준비확인=가짜준비확인)
    w.목모드 = False
    w.검사주기초 = 60

    확인("첫 호출 — True, HTTP 1회", w._vllm_상태() is True)
    확인("호출횟수 1", 호출횟수["n"] == 1)

    시계.흐르다(10)
    확인("캐시 안에선 재호출 안 함(여전히 True)", w._vllm_상태() is True)
    확인("호출횟수 여전히 1", 호출횟수["n"] == 1)

    시계.흐르다(60)
    확인("캐시(60초) 밖 — 다시 쳐서 False", w._vllm_상태() is False)
    확인("호출횟수 2", 호출횟수["n"] == 2)


def test_목모드에서는_vllm_상태를_아예_안_친다():
    호출됨 = {"v": False}

    def 절대안불려야함():
        호출됨["v"] = True
        return True

    w, 팟, 시계 = _새워치독()
    w.목모드 = True                      # 기본값이기도 하다 — 명시로 재확인
    w.준비확인 = 절대안불려야함
    확인("목모드는 None", w._vllm_상태() is None)
    확인("HTTP 를 안 쳤다", 호출됨["v"] is False)


def test_현황에_vllm_응답_필드가_있다_제어불가_상황에서도():
    """🔴 2026-09-04 운영 재현 — RUNPOD_API_KEY/POD_ID 가 Cloud Run env 에 없어
    `제어.가능=False` 인데 `/api/normalize`·`/api/judge` 는 LLM실패였다(ai-d7 관측).
    이 필드가 «제어불가와 무관하게» 도는지 확인한다."""
    제어불가팟 = gw.팟제어()             # 가능=False — 지금 운영과 같은 모양
    확인("전제: 이 제어는 가능=False", 제어불가팟.가능 is False)
    w = gw.GPU워치독(제어=제어불가팟, 시계=가짜시계(), 잠들기=lambda s: None,
                    준비확인=lambda: False)   # vLLM 은 죽어있다고 가정
    w.목모드 = False
    현황 = w.현황()
    확인("제어불가라도 vLLM_응답 필드가 채워진다", 현황["vLLM_응답"] is False)
    확인("«상태» 는 여전히 가동으로 접힌다(계약 안 건드림) — 그래서 vLLM_응답이 필요하다",
        현황["상태"] == gw.가동)


def test_실제_vllm_준비는_UserAgent를_싣는다():
    """🔴 2026-09-04 GPU 창 실측 회귀 — RunPod 앞단 Cloudflare 가 기본 urllib UA
    (`Python-urllib/3.x`)를 봇으로 읽어 403(1010)으로 끊는다(`normalize_run.py:90`·
    `adapter.py:207` 과 같은 자리, 여기가 세 번째였다). UA 없이 다시 퇴행하면 이 테스트가
    잡는다 — 실제 네트워크는 안 쓴다, urlopen 호출만 가로챈다."""
    잡힌_요청 = {}

    class 가짜응답:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def 가짜_urlopen(req, timeout=None):
        잡힌_요청["req"] = req
        잡힌_요청["timeout"] = timeout
        return 가짜응답()

    with patch.object(gw.urllib.request, "urlopen", side_effect=가짜_urlopen):
        with patch.dict("os.environ", {"VLLM_URL": "https://example-proxy.runpod.net"}):
            결과 = gw._vllm_준비()

    확인("헬스체크는 True 를 돌려준다(가짜 200)", 결과 is True)
    요청 = 잡힌_요청["req"]
    확인("Request 객체를 쓴다(맨 url 문자열이 아니라 — 헤더를 실으려면 필수)",
        isinstance(요청, gw.urllib.request.Request))
    확인("User-Agent 헤더가 실린다", 요청.get_header("User-agent") not in (None, ""))
    확인("URL 이 /health 로 끝난다", 요청.full_url.endswith("/health"))


def test_vllm_준비_실패는_이제_로그로_남는다(caplog):
    """전엔 `except Exception: return False` 로 사유가 통째로 삼켜졌다 — 그래서
    운영에서 «왜» false 인지 아무도 몰랐다. 이제 경고 로그에 사유가 남는지 확인한다."""
    def 터지는_urlopen(req, timeout=None):
        raise TimeoutError("simulated timeout")

    with patch.object(gw.urllib.request, "urlopen", side_effect=터지는_urlopen):
        with patch.dict("os.environ", {"VLLM_URL": "https://example-proxy.runpod.net"}):
            import logging
            with caplog.at_level(logging.WARNING, logger="suddoe.gpu"):
                결과 = gw._vllm_준비()

    확인("실패하면 False", 결과 is False)
    확인("경고 로그에 예외 종류가 찍힌다", any("TimeoutError" in r.message for r in caplog.records))


# ════════════════════════════════════════════════════════════════════
# ⑤ 잔액 — 서버 프로세스에서 구현 불가라는 걸 «None 반환» 으로 명시했는지
# ════════════════════════════════════════════════════════════════════

def test_잔액은_구현되지_않고_None을_돌려준다():
    """🔴 이건 «성공 테스트» 가 아니라 «거짓 성공을 안 만든다» 는 테스트다.
    RunPod REST v1 에 잔액 엔드포인트가 없다(2026-09-04 openapi.json 확인) —
    그래서 미구현 상태를 코드가 스스로 정직하게 말하는지만 본다."""
    기본 = gw.팟제어()
    확인("기본 팟제어.잔액() 은 None", 기본.잔액() is None)
    실제클래스 = gw.RunPod팟(키="dummy", 팟id="dummy")
    확인("RunPod팟.잔액() 도 오버라이드 없이 None 상속", 실제클래스.잔액() is None)


if __name__ == "__main__":
    import inspect
    실패 = 0
    for 이름, 함수 in sorted(globals().items()):
        if 이름.startswith("test_") and callable(함수):
            try:
                함수()
            except AssertionError as e:
                실패 += 1
                print(f"[실패] {이름}: {e}")
            except Exception as e:                             # noqa: BLE001
                실패 += 1
                print(f"[예외] {이름}: {type(e).__name__}: {e}")
    print(f"\n{'전부 통과' if 실패 == 0 else f'{실패}건 실패'}")
    sys.exit(1 if 실패 else 0)
