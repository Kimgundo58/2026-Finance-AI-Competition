# -*- coding: utf-8 -*-
"""W-GPU 레인 — 실판정 1건 e2e 캡처.

`/api/normalize` → `/api/judge` 를 실 모드로 태우고 SSE 를 통째로 받는다.
확인 대상은 넷이다:
    이벤트열 · 판정값 enum · **LLM 호출 실측 횟수** · 인용이 S번호 추출인가

호출 횟수는 닻 셋으로 따로 센다 — 서로 안 보고 세야 교차검증이 된다:
    ① 프록시(scratchpad/vllm_probe.py) — HTTP 왕복
    ② vLLM /metrics `vllm:request_success_total` — 서버가 스스로 센 것
    ③ (참고) SSE `진행` 이벤트의 단계 이름

사용:
    python scratchpad/wgpu_e2e.py --경로 자연어
    python scratchpad/wgpu_e2e.py --경로 폼
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

API = os.environ.get("SUDDOE_API", "http://127.0.0.1:8080")
PROBE = os.environ.get("PROBE_URL", "http://127.0.0.1:8011")
VLLM = os.environ.get("VLLM_TARGET", "")
ORG = os.environ.get("TEST_ORG", "1d6be2e1-7296-5492-a24b-c0838b431a7f")
사업 = os.environ.get("TEST_PROGRAM", "초기창업패키지")
OUT = Path(__file__).resolve().parent


def _가져오기(url: str, 본문=None, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(본문, ensure_ascii=False).encode() if 본문 is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "suddoe-judge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def sse(경로: str, 본문: dict, 이름: str) -> list[tuple[str, object]]:
    """SSE 를 순서 그대로 받는다. 같은 이벤트가 여러 번 오면 여러 번 담는다."""
    url = f"{API}{경로}"
    req = urllib.request.Request(url, data=json.dumps(본문, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream",
                                          "User-Agent": "suddoe-judge/1.0"})
    이벤트: list[tuple[str, object]] = []
    원문: list[str] = []
    t0 = time.time()
    현재 = None
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            줄 = raw.decode("utf-8", "replace").rstrip("\n")
            원문.append(줄)
            if 줄.startswith("event: "):
                현재 = 줄[7:].strip()
            elif 줄.startswith("data: "):
                d = 줄[6:]
                try:
                    d = json.loads(d)
                except Exception:
                    pass
                이벤트.append((현재 or "?", d))
                print(f"    · {현재}  (+{time.time()-t0:.1f}s)", flush=True)
            elif 줄.startswith(":"):
                이벤트.append(("«주석»", 줄))
    (OUT / f"_sse_{이름}.txt").write_text("\n".join(원문), encoding="utf-8")
    print(f"  [{이름}] {len(이벤트)}개 · {time.time()-t0:.1f}초 "
          f"· 원문 scratchpad/_sse_{이름}.txt", flush=True)
    return 이벤트


def 프록시수() -> dict:
    try:
        return json.loads(_가져오기(f"{PROBE}/__probe/stats", timeout=10))
    except Exception as e:
        return {"오류": f"{type(e).__name__}"}


def 프록시리셋() -> None:
    try:
        _가져오기(f"{PROBE}/__probe/reset", timeout=10)
    except Exception:
        pass


def 메트릭수() -> dict:
    """vLLM 이 스스로 센 성공 요청 수 — 프록시와 독립된 닻."""
    if not VLLM:
        return {}
    try:
        req = urllib.request.Request(f"{VLLM}/metrics",
                                     headers={"User-Agent": "suddoe-judge/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            본문 = r.read().decode()
    except Exception as e:
        return {"오류": f"{type(e).__name__}"}
    합 = 0.0
    for 줄 in 본문.splitlines():
        if 줄.startswith("vllm:request_success_total"):
            try:
                합 += float(줄.rsplit(" ", 1)[1])
            except Exception:
                pass
    return {"성공요청_누적": 합}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--경로", choices=["자연어", "폼"], default="자연어")
    # 🔴 프론트는 `/api/normalize` 가 준 dict 를 «그대로» 판정에 넘긴다. 거기엔
    #    `_원문` 도 `질문` 도 없다 (정규화가 싣는 키 이름은 `질문원문` 이다).
    #    그래서 기본값을 «원문 안 넣음» 으로 둔다 — 내가 넣으면 `_실_판정` 의
    #    문장 되짚기(main.py:940)가 가려져 실제 흐름을 못 잰다.
    ap.add_argument("--원문주입", action="store_true",
                    help="`_원문` 을 손으로 넣어 되짚기를 우회한다 (대조용)")
    a = ap.parse_args()
    이름꼬리 = a.경로 + ("_원문주입" if a.원문주입 else "")

    질문 = "디자이너가 쓸 맥북 프로 250만원 구매하려고 하는데 사업비로 써도 되나요?"
    if a.경로 == "자연어":
        정규화본문 = {"질문": 질문, "사업명": 사업}
    else:
        정규화본문 = {"품목": "맥북 프로", "금액": 2_500_000,
                  "용도": "디자이너 작업용", "사업명": 사업}

    보고: dict = {"경로": 이름꼬리, "원문주입": a.원문주입, "요청": 정규화본문, "org_id": ORG, "사업명": 사업}

    print(f"\n══ {a.경로} 경로 ══", flush=True)
    print(f"  health: {_가져오기(f'{API}/api/health', timeout=30)[:200]}", flush=True)

    프록시리셋()
    m0 = 메트릭수()
    보고["metrics_시작"] = m0

    # ── ① 정규화 ────────────────────────────────────────────────────
    print("  [1/2] POST /api/normalize", flush=True)
    ev1 = sse(f"/api/normalize?org_id={ORG}", 정규화본문, f"normalize_{이름꼬리}")
    p1 = 프록시수()
    m1 = 메트릭수()
    보고["normalize_이벤트열"] = [n for n, _ in ev1]
    보고["normalize_프록시수"] = p1
    보고["normalize_metrics"] = m1

    결과1 = next((d for n, d in reversed(ev1) if n == "결과"), None)
    보고["정규화결과"] = 결과1
    if not isinstance(결과1, dict):
        print("  🔴 정규화 결과 없음 — 여기서 멈춘다", flush=True)
        (OUT / f"_보고_{이름꼬리}.json").write_text(
            json.dumps(보고, ensure_ascii=False, indent=1), encoding="utf-8")
        return 1

    # ── ② 판정 ──────────────────────────────────────────────────────
    후보 = [c.get("비목") for c in (결과1.get("비목후보") or []) if isinstance(c, dict)]
    확정 = 후보[0] if 후보 else None
    print(f"  [2/2] POST /api/judge  (확정비목={확정})", flush=True)
    넘길정규화 = {**결과1, "_원문": 질문} if a.원문주입 else dict(결과1)
    보고["판정에_넘긴_정규화"] = 넘길정규화
    판정본문 = {"정규화": 넘길정규화, "확정비목": 확정,
             "사업명": 사업, "org_id": ORG}
    ev2 = sse(f"/api/judge?org_id={ORG}", 판정본문, f"judge_{이름꼬리}")
    p2 = 프록시수()
    m2 = 메트릭수()
    보고["judge_이벤트열"] = [n for n, _ in ev2]
    보고["judge_프록시수"] = p2
    보고["judge_metrics"] = m2

    결과2 = next((d for n, d in reversed(ev2) if n == "결과"), None)
    보고["판정결과"] = 결과2

    # ── 대조 ────────────────────────────────────────────────────────
    계약 = ["진행", "진행", "진행", "판정", "해야할일", "인용", "전제",
           "참조사슬", "결과", "저장", "완료"]
    실제 = [n for n, _ in ev2 if n != "«주석»"]
    보고["judge_계약대조"] = {"계약": 계약, "실제": 실제, "일치": 계약 == 실제}

    판정값 = (결과2 or {}).get("판정")
    보고["판정값"] = 판정값
    보고["판정값_enum_안"] = 판정값 in ("가능", "조건부", "불가", "판단불가", None)

    인용 = (결과2 or {}).get("인용") or []
    보고["인용수"] = len(인용)
    보고["인용_샘플"] = 인용[:2]

    # 인용 원문이 DB 원문과 «글자 그대로» 같은가 (LLM 이 지어냈으면 다르다)
    보고["인용_S번호흔적"] = [
        {"조번호": c.get("조번호"), "원문길이": len(c.get("원문") or ""),
         "S번호형태": bool(re.fullmatch(r"S\d+", str(c.get("조번호") or "")))}
        for c in 인용[:5] if isinstance(c, dict)]

    (OUT / f"_보고_{이름꼬리}.json").write_text(
        json.dumps(보고, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n── 요약 ──", flush=True)
    print(f"  normalize 이벤트열 : {보고['normalize_이벤트열']}", flush=True)
    print(f"  judge 이벤트열     : {실제}", flush=True)
    print(f"  계약 일치          : {보고['judge_계약대조']['일치']}", flush=True)
    print(f"  판정값             : {판정값}  (enum 안: {보고['판정값_enum_안']})", flush=True)
    print(f"  인용 {len(인용)}건", flush=True)
    print(f"  프록시 chat 호출   : normalize {p1.get('chat')} → judge {p2.get('chat')}",
          flush=True)
    print(f"  vLLM metrics       : {m0.get('성공요청_누적')} → "
          f"{m1.get('성공요청_누적')} → {m2.get('성공요청_누적')}", flush=True)
    print(f"  보고 scratchpad/_보고_{이름꼬리}.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
