# -*- coding: utf-8 -*-
"""🔴 125초 «천장» 을 실제로 넘겨서 재는 시험. J1 의 A/B ①②가 못 잰 자리를 메운다.

왜 다시 짜나 — J1 의 ①은 `max_tokens=2000` 이라 **한 요청이 36초에 끝났다.**
36초짜리로는 125초 천장에 닿을 수가 없다. 「524 가 안 났다」는 「천장이 없다」가 아니라
**「천장까지 안 갔다」** 다. 잴 수 없는 것을 값이 0 으로 읽으면 안 된다(CLAUDE.md).

설계 — 생성이 «확실히» 125초를 넘게 만든다.
  실측 단일 생성 속도 = 2000토큰 / 36.18초 ≈ 55.3 tok/s   (A100 · 유휴 · AWQ)
  ⇒ 125초를 넘기려면 7,000토큰 이상. 여유를 둬 **12,000토큰**을 요구한다 (≈217초 예상)
  ⇒ 모델이 일찍 멈추면 안 되므로 「끝없이 이어 쓰라」는 프롬프트를 준다.
     🔴 guided_json 은 «걸지 않는다» — 스키마가 짧은 JSON 을 강제해 조기 종료시킨다

세 가지를 «같은 조건» 으로 잰다 (순서도 고정 — 뒤로 갈수록 캐시가 더워진다):
  ⓐ stream=false  → 524 가 나는가. **나야 천장 가설이 산다**
  ⓑ stream=true   → 끝까지 오는가. **와야 처방이 선다**
  ⓒ stream=false 를 한 번 더 → ⓐ 가 우연이 아닌지 (524 는 재현돼야 사실이다)
"""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from adapter import vllm_url  # noqa: E402

URL = vllm_url().rstrip("/") + "/v1/chat/completions"
목표토큰 = 12000
프롬프트 = ("창업지원금 사업비 집행 실무에 대해 아주 길고 자세한 안내문을 쓰라. "
        "비목마다 정의·한도·증빙·자주 나는 실수·예시를 각각 여러 문단으로 풀어 쓰고, "
        "재료비·외주용역비·기계장치·인건비·여비·교육훈련비·광고선전비·지급수수료·"
        "특허권 등 무형자산 취득비·일반수용비를 순서대로 모두 다뤄라. "
        "요약하지 말고 최대한 길게, 끝까지 이어 쓰라.")


def 한번(스트리밍: bool) -> dict:
    본문 = {"model": "Qwen/Qwen3-32B-AWQ",
          "messages": [{"role": "user", "content": 프롬프트}],
          "temperature": 0.0, "max_tokens": 목표토큰,
          # 🔴 `ignore_eos` 가 «없으면» 이 시험이 성립하지 않는다. 1차 시도에서 모델이
          #    5,773토큰에서 스스로 멈춰(finish_reason='stop') 100.9초에 끝났다 —
          #    125초 천장에 «닿지도 못했다». 프롬프트로 길이를 유도하는 것은
          #    모델의 선택에 달려 있어 시험 조건을 우리가 못 정한다.
          #    `ignore_eos=True` 는 EOS 를 무시하고 max_tokens 를 «반드시» 채우게 한다
          #    (vLLM 확장). 이 시험의 목적은 문장 품질이 아니라 «벽시계 시간» 이다.
          "ignore_eos": True}
    if 스트리밍:
        본문["stream"] = True
        본문["stream_options"] = {"include_usage": True}
    req = urllib.request.Request(
        URL, data=json.dumps(본문, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "suddoe-judge/1.0"})
    t = time.time()
    ttfb = None
    조각 = 0
    본문길이 = 0
    토큰 = None
    종료 = None
    try:
        # 🔴 소켓 타임아웃을 400초로 넉넉히 준다 — **우리가 먼저 끊으면 천장을 못 본다.**
        with urllib.request.urlopen(req, timeout=400) as r:
            if 스트리밍:
                for raw in r:
                    줄 = raw.decode("utf-8", "replace").strip()
                    if not 줄.startswith("data:"):
                        continue
                    몸통 = 줄[5:].strip()
                    if 몸통 == "[DONE]":
                        break
                    try:
                        d = json.loads(몸통)
                    except json.JSONDecodeError:
                        continue
                    if ttfb is None:
                        ttfb = round(time.time() - t, 2)
                    if d.get("usage"):
                        토큰 = d["usage"].get("completion_tokens")
                    for ch in d.get("choices") or []:
                        델타 = ch.get("delta") or {}
                        조각 += 1
                        본문길이 += len(델타.get("content") or "") + len(델타.get("reasoning_content") or "")
                        if ch.get("finish_reason"):
                            종료 = ch["finish_reason"]
            else:
                d = json.loads(r.read().decode())
                ttfb = round(time.time() - t, 2)
                m = d["choices"][0]["message"]
                본문길이 = len(m.get("content") or "") + len(m.get("reasoning_content") or "")
                토큰 = d.get("usage", {}).get("completion_tokens")
                종료 = d["choices"][0].get("finish_reason")
        return {"stream": 스트리밍, "결과": "성공", "지연초": round(time.time() - t, 2),
                "ttfb초": ttfb, "조각수": 조각, "본문자수": 본문길이,
                "completion_tokens": 토큰, "finish_reason": 종료}
    except urllib.error.HTTPError as e:
        몸 = e.read()[:80]
        return {"stream": 스트리밍, "결과": f"HTTP {e.code}", "지연초": round(time.time() - t, 2),
                "본문": repr(몸)}
    except Exception as e:                                   # noqa: BLE001
        return {"stream": 스트리밍, "결과": f"{type(e).__name__}: {str(e)[:120]}",
                "지연초": round(time.time() - t, 2)}


def main() -> int:
    print(f"URL {URL}\n목표 생성 {목표토큰}토큰 (실측 55.3 tok/s ⇒ 약 {목표토큰/55.3:.0f}초 예상)\n")
    결과 = []
    for 이름, 스 in (("ⓐ stream=false", False), ("ⓑ stream=true", True), ("ⓒ stream=false 재현", False)):
        print(f"── {이름} ──")
        r = 한번(스)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        결과.append((이름, r))
        print()
    a, b, c = 결과[0][1], 결과[1][1], 결과[2][1]

    def _524(r):
        return str(r.get("결과", "")).startswith("HTTP 524")

    print("판정")
    긴가 = max(a.get("지연초", 0), c.get("지연초", 0))
    if 긴가 < 125 and not (_524(a) or _524(c)):
        print(f"  🔴 비스트리밍이 {긴가}초에 «끝났다» — 125초에 닿지도 않았다.")
        print("     천장을 다시 못 쟀다. 토큰을 더 올려서 다시 재야 한다 — 「524 안 남」으로 읽지 마라")
        return 2
    if _524(a) and _524(c):
        print(f"  ⓐⓒ 524 재현 — 천장이 오늘도 살아 있다 (각 {a['지연초']}초 · {c['지연초']}초)")
        if b.get("결과") == "성공":
            print(f"  ⓑ 스트리밍 {b['지연초']}초 완주 (조각 {b['조각수']} · {b['completion_tokens']}토큰)")
            print("  ⇒ 스트리밍이 천장을 없앤다. SUDDOE_LLM_STREAM=1 근거가 선다")
            return 0
        print(f"  ⓑ 스트리밍도 실패({b.get('결과')}) — 처방이 안 듣는다. 동시성 하향으로 간다")
        return 1
    print(f"  ⓐ {a.get('결과')} {a.get('지연초')}초 · ⓒ {c.get('결과')} {c.get('지연초')}초 — "
          "재현이 갈렸다. 한 번 더 재기 전엔 아무 결론도 내지 마라")
    return 3


if __name__ == "__main__":
    sys.exit(main())
