# -*- coding: utf-8 -*-
"""S4 자가검토 — 정지 조건을 «발동시켜» 본다. 실제 팟은 켜지 않는다 (가짜팟 주입)."""
import io
import json
import logging
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
REPO = r"C:\Users\dogun\Downloads\Desktop\Desktop\Desktop\Desktop\김건도\3-1 여름방학\금융 AI공모전"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
logging.basicConfig(level=logging.WARNING, format="   [log:%(levelname)s] %(message)s")

import importlib
import server.gpu_watchdog as gw


class 가짜팟(gw.팟제어):
    """🔴 실제 RunPod API 를 안 친다. 호출 기록만 남긴다."""
    가능 = True

    def __init__(self, 상태=gw.가동, 시작성공=True, 정지성공=True, 조회예외=False):
        self._상태, self.시작성공, self.정지성공, self.조회예외 = 상태, 시작성공, 정지성공, 조회예외
        self.기록 = []

    def 상태(self):
        self.기록.append("상태")
        return gw.알수없음 if self.조회예외 else self._상태

    def 시작(self):
        self.기록.append("시작")
        if self.시작성공:
            self._상태 = gw.가동
        return self.시작성공

    def 정지(self):
        self.기록.append("정지")
        if self.정지성공:
            self._상태 = gw.중지
        return self.정지성공


class 가짜시계:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def 앞당기기(self, 초):
        self.t += 초


def 만들기(제어, **환경):
    for k in ("SUDDOE_GPU_IDLE_MIN", "SUDDOE_GPU_WARN_MIN", "SUDDOE_GPU_CHECK_SEC",
              "SUDDOE_GPU_START_SEC", "SUDDOE_GPU_POLL_SEC", "RUNPOD_API_KEY", "RUNPOD_POD_ID"):
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in 환경.items()})
    시계 = 가짜시계()
    w = gw.GPU워치독(제어, 시계=시계, 잠들기=lambda s: 시계.앞당기기(s),
                    준비확인=lambda: 제어._상태 == gw.가동 if 제어 else True)
    return w, 시계


def 줄(제목):
    print("\n" + "=" * 72 + f"\n{제목}\n" + "=" * 72)


# ══════════════════════════════════════════════════════════════════
줄("① 유휴 종료 — 시간을 앞당겨 실제로 stop 을 트리거한다")
팟 = 가짜팟(gw.가동)
w, 시계 = 만들기(팟, SUDDOE_GPU_IDLE_MIN=30)
print("  임계초 =", w.유휴임계초, "· 활성 =", w.활성, "· 제어가능 =", w.제어.가능)
print("  t+0     검사 →", w.한번_검사(), "| 현황", json.dumps(w.현황(), ensure_ascii=False))
시계.앞당기기(29 * 60)
print("  t+29분  검사 →", w.한번_검사(), "| 현황", json.dumps(w.현황(), ensure_ascii=False))
시계.앞당기기(60 * 1 + 1)
r = w.한번_검사()
print("  t+30분  검사 →", r, "| 팟기록", 팟.기록, "| 팟상태", 팟._상태)
print("  현황:", json.dumps(w.현황(), ensure_ascii=False))
assert r == "정지함" and "정지" in 팟.기록 and 팟._상태 == gw.중지, "정지가 발동 안 함"
print("  ✅ 정지 발동 확인 (한 번 더 검사하면 이미중지 →", w.한번_검사(), ")")
assert "정지" not in 팟.기록[팟.기록.index("정지") + 1:], "중복 정지"

# 유휴 도중 keepalive 로 리셋되는지
팟2 = 가짜팟(gw.가동)
w2, 시계2 = 만들기(팟2, SUDDOE_GPU_IDLE_MIN=30)
시계2.앞당기기(29 * 60)
w2.keepalive()
시계2.앞당기기(29 * 60)
print("  keepalive 후 t+29분 → ", w2.한번_검사(), "(리셋됐으면 대기여야 한다)")
assert w2.한번_검사().startswith("대기"), "keepalive 가 타이머를 안 리셋했다"

# ══════════════════════════════════════════════════════════════════
줄("② 프론트 계약 — 종료 5분 전 모달 조건")
팟3 = 가짜팟(gw.가동)
w3, 시계3 = 만들기(팟3, SUDDOE_GPU_IDLE_MIN=30, SUDDOE_GPU_WARN_MIN=5)
for 분 in (0, 24, 25, 26, 30):
    시계3.t = 1000.0 + 분 * 60
    h = w3.현황()
    모달 = h["종료예정초"] is not None and h["종료예정초"] <= h["경고초"]
    print(f"  유휴 {분:>2}분 → {json.dumps(h, ensure_ascii=False)}  모달={모달}")

# ══════════════════════════════════════════════════════════════════
줄("③ 재기동 — 「꺼짐 → 판정 요청 → 기동 → 판정 완료」 전 구간")


def 판정_스트림(w, 실판정):
    """main.py `judge()` 의 gen() 을 패치안 그대로 흉내낸다. SSE 문자열을 그대로 뱉는다."""
    def _sse(이름, 값):
        return f"event: {이름}\ndata: {json.dumps(값, ensure_ascii=False)}\n\n"
    out_lines = []
    for 단계, 설명 in (("검색", "관련 조항을 찾는 중"), ("룰조회", "비목별 한도·증빙을 확인하는 중"),
                      ("조립", "판정을 작성하는 중")):
        out_lines.append(_sse("진행", {"단계": 단계, "설명": 설명}))
    for 진 in w.기동_진행():                      # ← 패치 지점
        out_lines.append(_sse("진행", 진))
    try:
        out = 실판정()
    except Exception as e:
        print(f"     (except 경로 탐: {type(e).__name__}: {e})")
        out = {"판정": "판단불가", "요약": "일시적인 오류로 판정하지 못했습니다. 주관기관 문의가 필요합니다."}
    for 이름, 값 in (("판정", {k: out.get(k) for k in ("판정", "요약")}), ("해야할일", []),
                    ("인용", []), ("전제", []), ("참조사슬", []), ("결과", out),
                    ("저장", {"저장": False}), ("완료", {"캐시": False})):
        out_lines.append(_sse(이름, 값))
    return out_lines


팟4 = 가짜팟(gw.중지)
w4, 시계4 = 만들기(팟4, SUDDOE_GPU_IDLE_MIN=30, SUDDOE_GPU_START_SEC=300, SUDDOE_GPU_POLL_SEC=5)
w4._팟상태 = gw.중지


def 실판정_흉내():
    w4.게이트()                                   # ← `_실_판정` 첫 줄 패치 지점
    return {"판정": "가능", "요약": "구매해도 됩니다."}


줄기 = 판정_스트림(w4, 실판정_흉내)
print("".join(줄기))
이름들 = [l.split("\n")[0][len("event: "):] for l in 줄기]
print("  이벤트 이름 순서:", 이름들)
print("  팟 기록:", 팟4.기록, "| 최종 팟상태:", 팟4._상태, "| 워치독 인식:", w4._팟상태)
계약 = {"진행", "판정", "해야할일", "인용", "전제", "참조사슬", "결과", "저장", "완료", "문의초안"}
새이름 = set(이름들) - 계약
print("  🔴 계약 밖 이벤트 이름:", 새이름 or "없음")
assert not 새이름, f"새 SSE 이벤트 이름이 생겼다: {새이름}"
assert json.loads(줄기[-3].split("data: ")[1])["판정"] == "가능", "기동 후 판정이 안 됨"
print("  ✅ 꺼짐 → 기동 → 판정 완료 전 구간 통과, 새 이벤트 이름 0개")

줄("③-b 기동 «실패» → 판단불가로 닫힌다")
팟5 = 가짜팟(gw.중지, 시작성공=False)
w5, 시계5 = 만들기(팟5, SUDDOE_GPU_IDLE_MIN=30)
w5._팟상태 = gw.중지


def 실판정5():
    w5.게이트()
    return {"판정": "가능", "요약": "여기 오면 안 된다"}


줄기5 = 판정_스트림(w5, 실판정5)
이름5 = [l.split("\n")[0][len("event: "):] for l in 줄기5]
결과5 = json.loads(줄기5[-3].split("data: ")[1])
print("  이벤트:", 이름5)
print("  결과:", json.dumps(결과5, ensure_ascii=False))
assert 결과5["판정"] == "판단불가", "기동 실패가 판단불가로 안 닫혔다"
assert set(이름5) <= 계약, "새 이벤트 이름"
print("  ✅ 기동 실패 → 판단불가 · 이벤트열 그대로")

줄("③-c 이미 가동 중이면 기동 이벤트를 «하나도» 안 낸다 (평상시 이벤트열 불변)")
팟6 = 가짜팟(gw.가동)
w6, _ = 만들기(팟6, SUDDOE_GPU_IDLE_MIN=30)
추가 = list(w6.기동_진행())
print("  기동_진행 산출:", 추가)
assert 추가 == [], "가동 중인데 기동 이벤트가 났다"

# ══════════════════════════════════════════════════════════════════
줄("④ RUNPOD_API_KEY 없음 — 절대 안 끈다")
importlib.reload(gw)
os.environ.pop("RUNPOD_API_KEY", None)
os.environ.pop("RUNPOD_POD_ID", None)
os.environ["SUDDOE_GPU_IDLE_MIN"] = "30"
시계7 = 가짜시계()
w7 = gw.GPU워치독(시계=시계7, 잠들기=lambda s: None, 준비확인=lambda: True)
print("  제어 =", type(w7.제어).__name__, "· 가능 =", w7.제어.가능, "·", w7.제어.사유)
시계7.앞당기기(10 * 3600)
r7 = w7.한번_검사()
print("  유휴 10시간 후 검사 →", r7)
print("  현황:", json.dumps(w7.현황(), ensure_ascii=False))
assert r7 == "제어불가", "키가 없는데 뭔가를 했다"
print("  기동_진행 산출:", list(w7.기동_진행()), "(깨울 것도 없다)")
w7._팟상태 = gw.중지
w7.게이트()   # 제어 불가면 게이트도 안 막는다
print("  ✅ 키 없음: 정지 0회 · 게이트 통과 · 종료예정초 null")

줄("④-b API «실패» — 정지 실패는 종료로 이어지지 않는다")
팟8 = 가짜팟(gw.가동, 정지성공=False)
w8, 시계8 = 만들기(팟8, SUDDOE_GPU_IDLE_MIN=30)
시계8.앞당기기(31 * 60)
r8 = w8.한번_검사()
print("  →", r8, "| 팟상태(실물):", 팟8._상태, "| 워치독 인식:", w8._팟상태)
assert r8 == "정지실패 — 끄지 않음" and 팟8._상태 == gw.가동
w8.게이트()   # 실패 후에도 판정은 막히지 않는다
print("  ✅ 정지 실패해도 팟은 그대로 · 게이트 통과")

줄("④-c 상태 조회 실패 — stop 을 쏘지 않는다")
팟9 = 가짜팟(gw.가동, 조회예외=True)
w9, 시계9 = 만들기(팟9, SUDDOE_GPU_IDLE_MIN=30)
시계9.앞당기기(31 * 60)
r9 = w9.한번_검사()
print("  →", r9, "| 팟기록:", 팟9.기록)
assert "정지" not in 팟9.기록, "상태를 모르는데 stop 을 쐈다"
print("  ✅ 상태불명 → 정지 시도 0회")

# ══════════════════════════════════════════════════════════════════
줄("⑤ SUDDOE_GPU_IDLE_MIN=0 — 워치독 전체 비활성 (심사 당일)")
팟10 = 가짜팟(gw.가동)
w10, 시계10 = 만들기(팟10, SUDDOE_GPU_IDLE_MIN=0, RUNPOD_API_KEY="x", RUNPOD_POD_ID="y")
print("  활성 =", w10.활성)
시계10.앞당기기(24 * 3600)
r10 = w10.한번_검사()
print("  유휴 24시간 후 검사 →", r10, "| 팟기록:", 팟10.기록)
print("  현황:", json.dumps(w10.현황(), ensure_ascii=False))
w10.시작_루프()
print("  시작_루프 후 스레드:", w10._스레드)
assert r10 == "비활성" and 팟10.기록 == [] and w10._스레드 is None
assert w10.현황()["종료예정초"] is None
print("  ✅ 비활성: 정지 0회 · 스레드 0개 · 종료예정초 null · 상태 API 는 살아 있다")

# ══════════════════════════════════════════════════════════════════
줄("⑥ 라우터 배선 — /api/gpu/status · /api/gpu/keepalive")
importlib.reload(gw)
print("  경로:", [(r.path, sorted(r.methods)) for r in gw.router.routes])
print("  status 응답:", json.dumps(gw.gpu_status(), ensure_ascii=False))
print("  keepalive 응답:", json.dumps(gw.gpu_keepalive(), ensure_ascii=False))

print("\n" + "=" * 72 + "\n전 시나리오 통과\n" + "=" * 72)

# ══════════════════════════════════════════════════════════════════
줄("⑦ 「유휴정지 → 재기동」 한 인스턴스에서 연속으로 — 상태 캐시가 재기동을 막지 않는가")
팟11 = 가짜팟(gw.가동)
w11, 시계11 = 만들기(팟11, SUDDOE_GPU_IDLE_MIN=30)
list(w11.기동_진행()); print("  판정1 (가동) → 팟기록", 팟11.기록)
list(w11.기동_진행()); print("  판정2 (연속)  → 팟기록", 팟11.기록, "← API 재조회 없어야 한다")
assert 팟11.기록 == ["상태"], f"판정마다 API 를 쳤다: {팟11.기록}"
시계11.앞당기기(31 * 60)
print("  t+31분 검사 →", w11.한번_검사(), "| 팟기록", 팟11.기록)
assert 팟11._상태 == gw.중지
진행 = list(w11.기동_진행())
print("  정지 후 판정3 → 진행", [p["설명"] for p in 진행], "| 팟기록", 팟11.기록)
assert "시작" in 팟11.기록 and 팟11._상태 == gw.가동, "정지 후 재기동이 안 됐다"
w11.게이트(); print("  ✅ 정지 → 재기동 → 게이트 통과 (같은 인스턴스에서 연속)")

줄("⑧ 유휴 임계 «직후» 판정이 오면 — 캐시를 안 믿고 실물을 다시 본다")
팟12 = 가짜팟(gw.가동)
w12, 시계12 = 만들기(팟12, SUDDOE_GPU_IDLE_MIN=30)
list(w12.기동_진행())              # _팟상태 = 가동 으로 캐시
시계12.앞당기기(31 * 60)           # 검사 루프가 아직 안 돌았는데 판정이 먼저 왔다
팟12._상태 = gw.중지               # 밖에서(또는 직전 주기에) 이미 멈춰 있었다
진행12 = list(w12.기동_진행())
print("  진행:", [p["설명"] for p in 진행12], "| 팟기록", 팟12.기록)
assert "시작" in 팟12.기록, "임계 초과인데 캐시를 믿고 지나갔다"
print("  ✅ 유휴가 임계를 넘긴 뒤엔 캐시를 안 믿는다")
print("\n" + "=" * 72 + "\n추가 시나리오 통과\n" + "=" * 72)
