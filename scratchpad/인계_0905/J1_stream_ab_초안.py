# -*- coding: utf-8 -*-
"""524 처방(스트리밍) A/B + 정규화 단일시도 우측꼬리 관측 — 팟이 열린 뒤 5~10분 안에
돌리는 확인용 스크립트 (초안).

🔴 워크트리는 이미 파여 있다(`금융 AI공모전-J1`, 브랜치 `work/판정-J1-0905`) —
   `J1_최종.patch` 로 `scripts/adapter.py`·`scripts/normalize_run.py`(스트리밍 포함)가
   이미 그 트리에 적용돼 있다. 이 스크립트는 **그 트리의 코드를 부른다**
   (아래 `_J1_트리` 참고) — 본 트리(`금융 AI공모전/`)의 `scripts/` 는 되돌려져 있어
   여기서 부르면 스트리밍이 없는 옛 코드를 테스트하게 된다. 지금은 돌릴 GPU 가 없다
   (팟은 ai-04 가 연다).

## ①②  스트리밍 A/B
ai-04 실측(`실측_프록시_125초천장.md`)의 처방 후보 ⑶ 을 "믿고 켜지" 않기 위한 검증.
판정용이 아니라 A/B 전용 합성 프롬프트로 돈다 (정답셋을 태우지 않는다).

    ① stream=false 로 같은 프롬프트 → HTTP 524 가 재현되는가   (천장이 실재하는지)
    ② stream=true  로 같은 프롬프트 → 끝까지 응답이 오는가     (처방이 듣는지)

**둘 다 성립해야** `SUDDOE_LLM_STREAM=1` 을 기본으로 켠다. ①이 재현 안 되면(예:
그날 프록시 상태·GPU 부하가 달라 524 자체가 안 뜨면) 천장 가설부터 다시 봐야 하므로
②만 보고 "스트리밍이 통과했다"고 결론 내리지 않는다.

## ③  정규화 단일시도 우측꼬리 — ai-04 가 못 잰 자리 (2026-09-05 지적)
run 194 의 정규화 실패 74건은 전부 60초 기본 타임아웃에 «잘려» 있다 — 몇 초짜리였는지
모른다(우측 검열). 타임아웃을 상향하는 처방이 "거의 다 산다"(대부분 60~90초권)인지
"524 로 바뀔 뿐"(상당수가 125초 위)인지가 갈리므로, 여기서 골든셋 문항 20개를
`llm_호출` **직접**(재시도=0·타임아웃=300, 검열 없이·비스트리밍) 돌려 실제 분포를
관측한다. 이게 「추정」이 아니라 「관측」으로 답을 가르는 유일한 방법이다.

## ④  동시 6 + 스트리밍 — ai-04 설계 질문(2026-09-05)에 대한 실측
「스트리밍이 524 를 없애면 동시 6→4 하향의 근거(=타임아웃)도 같이 없어지는가」를
가르는 자리. 인계문서의 «동시 6→4» 권고는 «천장(125초) 위로 밀려서 타임아웃난다»는
전제였는데, 스트리밍이 천장을 없애면 190초짜리는 «느린 것» 이지 «실패» 가 아니게
된다 — 단, 실제로 몇 초까지 늘어나는지는 안 쟀다. 같은 장문 프롬프트를 스트리밍
켜고 동시 6개 던져 **각 요청이 실제로 몇 초 걸리는지** 재서 최댓값을 본다. 이 값이
ai-04 의 설계 갈림길(A: 동시6 유지 / B: 동시4 로 하향)을 정한다 — 상세는
`scratchpad/보고_J1_0905.md` §② 후속 논의 참고.

실행 (팟이 이미 열려 있고 `ops.gpu_pod.vllm_url` 이 채워진 상태에서, **본 트리에서**):
    PYTHONIOENCODING=utf-8 python "scratchpad/인계_0905/J1_stream_ab_초안.py"

비용: 스트리밍 A/B 2회 + 정규화 꼬리 관측 20회 + 동시성 스트레스 6회 = 요청 28회.
GPU 시간 15~20분 내외, 크레딧 영향 무시 가능 — 그래도 매 요청 전에 출력한다.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 🔴 J1 워크트리의 scripts/ 를 명시적으로 앞에 꽂는다 — 본 트리의 scripts/normalize_run.py
#    는 되돌려져 있어 스트리밍 코드가 없다(§ 위 경고). 상대경로("scripts")를 그냥 넣으면
#    이 스크립트를 어느 cwd 에서 돌리든 본 트리를 먼저 찾아 조용히 옛 코드를 테스트한다.
_J1_트리 = ("C:/Users/dogun/Downloads/Desktop/Desktop/Desktop/Desktop/"
           "김건도/3-1 여름방학/금융 AI공모전-J1/scripts")
sys.path.insert(0, _J1_트리)
from adapter import vllm_url  # noqa: E402  # ops.gpu_pod 우선, env 폴백은 adapter 가 가진 그대로

# 🔴 125초를 확실히 넘기는 게 목적이다. run 194 실측(completion_tokens p90 976 · max
#    1,457)의 꼬리보다 확실히 위로 잡는다 — 애매하게 짧으면 ①이 524 없이 성공해
#    "천장이 없다"는 잘못된 결론을 낼 수 있다. 2,000 토큰이면 p50 생성속도(약
#    10 tok/s, `docs/9-3_실측에서_나온_것.md`) 기준 약 200초 — 여유 있게 천장을 넘는다.
_프롬프트 = (
    "다음 숫자를 1부터 200까지 하나씩 한국어로 풀어 쓰고, 각 숫자마다 그 숫자가 "
    "3의 배수인지 5의 배수인지 아닌지를 한 문장으로 설명하라. 절대 요약하지 말고 "
    "200개 전부 빠짐없이 순서대로 출력하라."
)
_최대토큰 = 2000
_타임아웃 = 300  # 클라이언트 총 마감 — adapter.py·normalize_run.py 새 기본값과 맞춘다


def _호출(스트리밍: bool) -> dict:
    본문 = {
        "model": "Qwen/Qwen3-32B-AWQ",
        "messages": [{"role": "user", "content": _프롬프트}],
        "temperature": 0.0,
        "max_tokens": _최대토큰,
    }
    if 스트리밍:
        본문["stream"] = True
        본문["stream_options"] = {"include_usage": True}
    data = json.dumps(본문, ensure_ascii=False).encode()
    url = f"{vllm_url()}/v1/chat/completions"
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "suddoe-streamab/1.0"})
    t0 = time.time()
    결과 = {"stream": 스트리밍, "url": url}
    try:
        with urllib.request.urlopen(req, timeout=_타임아웃) as r:
            if not 스트리밍:
                d = json.loads(r.read().decode())
                결과["ok"] = True
                결과["finish_reason"] = d["choices"][0].get("finish_reason")
                결과["completion_tokens"] = d.get("usage", {}).get("completion_tokens")
            else:
                조각수 = 0
                첫바이트_t = None
                종료이유 = None
                usage = {}
                for raw in r:
                    if 첫바이트_t is None:
                        첫바이트_t = time.time()
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
                    if d.get("usage"):
                        usage = d["usage"]
                    for ch in d.get("choices") or []:
                        if (ch.get("delta") or {}).get("content"):
                            조각수 += 1
                        if ch.get("finish_reason"):
                            종료이유 = ch["finish_reason"]
                결과["ok"] = True
                결과["ttfb초"] = round((첫바이트_t or time.time()) - t0, 2)
                결과["조각수"] = 조각수
                결과["finish_reason"] = 종료이유
                결과["completion_tokens"] = usage.get("completion_tokens")
    except urllib.error.HTTPError as e:
        결과["ok"] = False
        결과["오류"] = f"HTTP {e.code}"
    except Exception as e:                                       # noqa: BLE001
        결과["ok"] = False
        결과["오류"] = f"{type(e).__name__}: {str(e)[:200]}"
    결과["지연초"] = round(time.time() - t0, 2)
    return 결과


def _정규화_꼬리_관측(n: int = 20) -> list[dict]:
    """골든셋 문항 `n`개를 정규화 슬롯으로, 재시도 없이·타임아웃 300초로 직접 부른다.

    🔴 `normalize_run.정규화()`(부르는 쪽)가 아니라 `llm_호출()`(부름받는 쪽)을 바로
    쓴다 — `정규화()` 의 60초 기본값(J1 트리에서는 이미 240으로 고쳤다, § 보고 ①)과
    무관하게 재게 하기 위해서다. `스트리밍` 인자를 안 넘기므로 J1 트리의
    `_스트리밍기본`(기본 꺼짐, 이번에 고침)을 그대로 따른다 — 즉 **비스트리밍**으로
    잰다. 여기서 잰 값이 «단일 시도 생성시간» 그 자체이고, 300초 안에서 HTTP 524 가
    나온다면 그게 바로 "125초 위 꼬리가 실재한다"는 관측이다.
    """
    from _lib import db                        # noqa: E402
    from normalize_run import (                # noqa: E402
        _지시_조립, 호출자리1_스키마, llm_호출)

    with db.connect(connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT gold_id, 질문 FROM eval.golden_set ORDER BY random() LIMIT %s", (n,)
        ).fetchall()

    스키마 = 호출자리1_스키마()
    비목목록 = ", ".join(스키마["properties"]["비목후보"]["items"]["properties"]["비목"]["enum"])
    결과: list[dict] = []
    for gid, q in rows:
        프롬프트 = _지시_조립("N0").format(비목=비목목록, 질문=q)
        t0 = time.time()
        항목 = {"gold_id": gid}
        try:
            _, 메타 = llm_호출(프롬프트, 스키마, 최대토큰=2000, 타임아웃=300, 재시도=0)
            항목["ok"] = True
            항목["지연초"] = round(time.time() - t0, 2)
            항목["종료이유"] = 메타.get("종료이유")
        except Exception as e:                                    # noqa: BLE001
            항목["ok"] = False
            항목["지연초"] = round(time.time() - t0, 2)
            항목["오류"] = f"{type(e).__name__}: {str(e)[:150]}"
        print(f"  gold_id={gid:<5} {'성공' if 항목['ok'] else '실패'}  "
              f"{항목['지연초']}초" + (f"  {항목.get('오류','')}" if not 항목["ok"] else ""))
        결과.append(항목)
    return 결과


def _동시성_스트레스(n: int = 6) -> list[dict]:
    """스트리밍 켜고 같은 장문 프롬프트를 동시 `n`개 던져 **각 요청의 실제 소요시간**을 잰다.

    ai-04 의 설계 갈림길(A: 동시6 유지 / B: 동시4 하향)을 가르는 값이다 — 스트리밍이
    524 자체는 없애도, 동시 요청이 서로 decode 처리량(메모리 대역폭)을 나눠 쓰면
    «시간이 걸릴 뿐» 인 응답이 얼마나 늘어나는지는 별개 질문이다. 여기서 잰 최댓값이
    운영상 받아들일 만한 지연인지는 오너가 정한다 — 이 함수는 값만 낸다.
    """
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(_호출, True) for _ in range(n)]
        결과 = [f.result() for f in as_completed(futs)]
    for i, r in enumerate(결과, 1):
        print(f"  #{i} {'성공' if r['ok'] else '실패'}  {r['지연초']}초"
              + (f"  {r.get('오류','')}" if not r["ok"] else ""))
    return 결과


def main() -> int:
    print(f"vLLM 주소: {vllm_url()}")
    print("\n① stream=false — 524 가 재현되는가")
    a = _호출(스트리밍=False)
    print(json.dumps(a, ensure_ascii=False, indent=2))

    print("\n② stream=true — 끝까지 오는가")
    b = _호출(스트리밍=True)
    print(json.dumps(b, ensure_ascii=False, indent=2))

    print("\n③ 정규화 단일시도 우측꼬리 — 골든셋 20문항, 재시도 없이 300초 한도")
    c = _정규화_꼬리_관측(20)
    지연들 = [x["지연초"] for x in c]
    실패125위 = [x for x in c if not x["ok"]]
    성공125위 = [x for x in c if x["ok"] and x["지연초"] > 125]
    print(f"  최대 {max(지연들)}초 · 125초 초과 성공 {len(성공125위)}건 · 실패(타임아웃 등) {len(실패125위)}건")

    천장_재현 = (not a["ok"]) and a.get("오류") == "HTTP 524"
    처방_통과 = b["ok"] and b.get("finish_reason") in ("stop", "length")

    d: list[dict] = []
    if 처방_통과:
        print("\n④ 동시 6 + 스트리밍 — 각 요청이 실제로 몇 초까지 늘어나는가")
        d = _동시성_스트레스(6)
    else:
        print("\n④ 건너뜀 — ②(스트리밍 단일 성공)가 안 됐다. 동시성부터 볼 필요가 없다")

    print("\n판정")
    print(f"  ① 천장 재현       : {'예' if 천장_재현 else '아니오'}"
          f"{'' if 천장_재현 else ' — 524 가 안 났다. 프롬프트가 짧았거나 그날 부하가 달랐을 수 있다. 천장 가설부터 다시 볼 것'}")
    print(f"  ② 스트리밍 성공   : {'예' if 처방_통과 else '아니오'}")
    if 성공125위 or 실패125위:
        print("  ③ 정규화도 125초 위 꼬리가 있다 — 타임아웃 상향만으로는 «524 로 바뀔 뿐»,"
              " 정규화에도 스트리밍(또는 동시성 처방)이 필요하다")
    else:
        print(f"  ③ 표본 {len(c)}건 전부 125초 아래 — 타임아웃 상향만으로 대부분 산다는"
              " 가설과 일치 (표본이 작다는 점은 유지)")
    if d:
        d최대 = max(x["지연초"] for x in d)
        d실패 = [x for x in d if not x["ok"]]
        print(f"  ④ 동시6·스트리밍 최장 {d최대}초 · 실패 {len(d실패)}/{len(d)}건"
              f"{' — 예상(190초 근처)보다 훨씬 크면 동시4 하향(B) 쪽, 비슷하거나 작으면 동시6 유지(A) 쪽' if not d실패 else ' — 스트리밍으로도 실패가 남는다, 동시6 유지는 위험'}")

    if 천장_재현 and 처방_통과:
        print("  ⇒ ①② 둘 다 성립 — SUDDOE_LLM_STREAM=1 기본 전환 근거 있음")
        print("     동시 6 vs 4 는 ④ 의 값으로 오너·ai-04 가 정한다 (이 스크립트는 값만 낸다)")
        return 0
    print("  ⇒ ①② 조건 미충족 — 스트리밍을 기본으로 켜지 마라. 원인부터 다시 본다")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
