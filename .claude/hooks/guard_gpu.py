# -*- coding: utf-8 -*-
"""GPU 팟 과사용 차단 — PreToolUse(Bash) + Stop 두 시점.

## 왜 훅인가
`runpod_session` 스킬과 프롬프트에 "끝나면 닫아라" 를 적어도 컨텍스트가 길어지면 빠진다.
그리고 🔴 **RunPod 에는 서버측 자동종료가 없다** (2026-08-31 실측 — `--terminate-after` 는
존재하지 않는 플래그다). 세션이 잊으면 크레딧이 계속 나간다.

## 두 가지를 막는다

1. **PreToolUse(Bash) — 중복 개설.**
   대장(`.claude/_runpod_open.json`)에 이미 팟이 있는데 또 `open` 하려 하면 거부한다.
   8세션 병렬에서 각자 팟을 열면 잔액이 한 시간에 증발한다 (2026-08-31 8세션 구성).

2. **Stop — 열어둔 채 턴 종료.**
   대장이 비어 있지 않으면 종료를 막고 경과시간·누적비용을 들이민다.
   무한 루프를 막으려고 `SUPPRESS_MIN` 분 동안은 다시 막지 않는다 —
   실제 작업 중이면 그 사이에 계속 진행할 수 있고, 잊었다면 주기적으로 다시 걸린다.

## 못 막는 것 (정직하게)
세션이나 PC 가 죽으면 이 훅도 같이 죽는다. 마지막 방어선은 **잔액**과 **사람**이다.
`GPU Guideline.md` §0-b · `.claude/skills/runpod_session/SKILL.md` 절대규칙 1.
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .claude/
대장 = os.path.join(ROOT, "_runpod_open.json")
낙인 = os.path.join(ROOT, "_gpu_nag.json")          # 런타임 상태. 커밋 대상 아님

SUPPRESS_MIN = 15
OPEN_CALL = re.compile(r"runpod_pod\.py\s+open\b")


def 열린팟() -> list:
    try:
        with open(대장, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except Exception:
        return []          # 대장이 없거나 깨졌으면 막지 않는다 — 훅이 세션을 잡아먹으면 안 된다


def 요약(pods: list) -> str:
    줄 = []
    for p in pods:
        pid = p.get("id", "?")
        rate = float(p.get("rate") or 0)
        t0 = p.get("opened_at")
        경과 = ""
        try:
            h = (time.time() - float(t0)) / 3600.0
            경과 = f" · {h:.2f}h 경과 · 누적 약 ${h * rate:.2f}"
        except Exception:
            pass
        줄.append(f"  - {pid} · {p.get('gpu', '?')} · ${rate:.2f}/h{경과}")
    return "\n".join(줄)


def pre_tool(data: dict) -> int:
    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    if not OPEN_CALL.search(cmd):
        return 0
    pods = 열린팟()
    if not pods:
        return 0
    sys.stderr.write(
        "[hook] 🔴 이미 열린 GPU 팟이 대장에 있다. 두 번째 팟을 열지 않는다.\n"
        + 요약(pods) + "\n"
        "기존 팟을 쓰거나 먼저 닫아라:\n"
        "  export PYTHONIOENCODING=utf-8 && python scripts/runpod_pod.py ls\n"
        "  export PYTHONIOENCODING=utf-8 && python scripts/runpod_pod.py close --id <id>\n"
        "(8세션 병렬 중이면 GPU 는 A 세션만 연다 — `0831_최종구현.md` §5)\n"
    )
    return 2


def stop(data: dict) -> int:
    if data.get("stop_hook_active"):
        return 0                       # 이미 이 훅 때문에 이어가는 중 — 다시 막지 않는다
    pods = 열린팟()
    if not pods:
        return 0
    now = time.time()
    try:
        with open(낙인, encoding="utf-8") as f:
            직전 = float(json.load(f).get("last", 0))
    except Exception:
        직전 = 0
    if now - 직전 < SUPPRESS_MIN * 60:
        return 0                       # 최근에 알렸다 — 작업 중일 수 있으니 통과시킨다
    try:
        with open(낙인, "w", encoding="utf-8") as f:
            json.dump({"last": now}, f)
    except Exception:
        pass
    sys.stderr.write(
        "[hook] 🔴 GPU 팟이 아직 켜져 있다. 켜져 있는 동안 크레딧이 나간다.\n"
        + 요약(pods) + "\n"
        "지금 실행 중인 작업이 없으면 **묻지 말고 즉시 닫는다** (오너 상시 지시):\n"
        "  export PYTHONIOENCODING=utf-8 && python scripts/runpod_pod.py close --id <id>\n"
        "작업이 진행 중이면 무엇이 도는 중인지 한 줄로 밝히고 계속한다.\n"
        f"(이 경고는 {SUPPRESS_MIN}분간 다시 뜨지 않는다. 서버측 자동종료는 존재하지 않는다)\n"
    )
    return 2


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    ev = data.get("hook_event_name") or ""
    if ev == "PreToolUse":
        return pre_tool(data)
    if ev == "Stop":
        return stop(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
