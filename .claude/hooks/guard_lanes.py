# -*- coding: utf-8 -*-
"""정리 작업 레인 강제 (2026-09-02 밤 무인 진행용).

여러 세션이 같은 저장소를 동시에 고칠 때 나는 사고는 둘이다.
  ① 두 세션이 같은 파일을 쓴다 → 나중 쪽이 앞선 쪽을 말없이 지운다
  ② 워커가 «겸사겸사» 남의 파일까지 고친다 → 아침에 원인 추적이 불가능해진다

`.claude/_lanes.json` 에 세션별 소유 경로를 박고, 등록된 세션이 소유 밖을 쓰면 막는다.

설계 원칙 — **등록 안 된 세션은 통과시킨다.**
사람이 자는 동안 도는 훅이라, 모르는 세션을 막아 전부 세우는 것보다
아는 세션만 정확히 가두는 쪽이 안전하다. 등록은 중앙 세션(ai-ae)이
워커의 계획을 승인할 때 session_id 를 받아 넣는다.

  중앙   → 어디든 쓴다 (병합·검수가 일이다)
  워커   → 소유 경로만. 그 밖은 «누가 소유인지» 를 알려주고 거부
  미등록 → 통과 (이 저장소의 다른 작업을 건드리지 않는다)

읽기는 막지 않는다. 남의 레인을 읽고 맞추는 건 오히려 권장이다.

해제는 `.claude/_lanes.json` 을 지우면 된다 — 훅이 즉시 무력화된다.
"""
import json
import os
import re
import sys

try:
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LANES = os.path.join(ROOT, ".claude", "_lanes.json")

# 문서 줄수 상한 — 이번 정리의 목표치. 기록 문서는 통독하지 않으므로 제외한다.
줄수상한 = 100
상한제외 = ("docs/기록/", "docs/_")


def 설정_읽기():
    try:
        with open(LANES, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def 상대(p):
    p = (p or "").replace("\\", "/")
    r = ROOT.replace("\\", "/")
    return p[len(r):].lstrip("/") if p.startswith(r) else p


def 레인_찾기(설정, sid):
    """이 세션이 속한 레인. 한 레인에 세션이 여럿일 수 있다 (백엔드 = BE + B1).

    레인 안에서 누가 어느 파일을 잡는지는 그 레인의 중앙(BE)이 관리한다 —
    훅은 레인 «사이» 만 가른다.
    """
    if not sid:
        return None
    for 이름, v in (설정.get("레인") or {}).items():
        멤버 = v.get("sessions") or {}
        if sid in (멤버.values() if isinstance(멤버, dict) else 멤버):
            return 이름
    return None


def 소유레인(설정, path):
    """이 경로를 어느 레인이 소유하는가. 아무도 소유 안 하면 None."""
    p = 상대(path)
    if not p:
        return None
    best, best_len = None, -1
    for 이름, v in (설정.get("레인") or {}).items():
        for own in v.get("owns") or []:
            own = own.replace("\\", "/")
            hit = p.startswith(own) if own.endswith("/") else (p == own or p.startswith(own))
            if hit and len(own) > best_len:
                best, best_len = 이름, len(own)
    return best


# Bash 경유 쓰기도 잡는다 — redirect · sed -i · tee · mv/cp/rm · git mv
BASH_쓰기 = [
    re.compile(r">>?\s*([^\s|&;<>]+)"),
    re.compile(r"\bsed\s+(?:-[a-zA-Z]*\s+)*-i[^\s]*\s+(?:'[^']*'|\"[^\"]*\"|[^\s]+)\s+([^\s|&;]+)"),
    re.compile(r"\btee\s+(?:-a\s+)?([^\s|&;]+)"),
    re.compile(r"\b(?:mv|cp)\s+(?:-[a-zA-Z]+\s+)*[^\s]+\s+([^\s|&;]+)"),
    re.compile(r"\brm\s+(?:-[a-zA-Z]+\s+)*([^\s|&;]+)"),
    re.compile(r"\bgit\s+mv\s+(?:-[a-zA-Z]+\s+)*[^\s]+\s+([^\s|&;]+)"),
]


def bash_쓰기대상(cmd):
    out = []
    for pat in BASH_쓰기:
        for m in pat.finditer(cmd or ""):
            t = m.group(1).strip("'\"")
            if t and not t.startswith("/dev/") and t != "$null":
                out.append(t)
    return out


def 거부(msg):
    sys.stderr.write(msg)
    return 2


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    설정 = 설정_읽기()
    if not 설정:
        return 0                      # 핀 없음 → 훅 무력

    sid = data.get("session_id")
    if sid and sid == 설정.get("중앙"):
        return 0                      # 중앙은 어디든 쓴다

    이벤트 = data.get("hook_event_name") or ""
    도구 = data.get("tool_name") or ""
    입력 = data.get("tool_input") or {}

    내레인 = 레인_찾기(설정, sid)

    # ── 줄수 상한 (PostToolUse · 경고) ──────────────────────────────
    if 이벤트 == "PostToolUse":
        p = 상대(입력.get("file_path") or "")
        if p.startswith("docs/") and p.endswith(".md") and not p.startswith(상한제외):
            full = os.path.join(ROOT, p)
            try:
                n = sum(1 for _ in open(full, encoding="utf-8", errors="ignore"))
            except Exception:
                return 0
            if n > 줄수상한:
                return 거부(
                    f"[레인] `{p}` 가 {n}줄이다 — 상한 {줄수상한}줄.\n"
                    "이번 정리의 목적이 «읽는 문서를 100줄 이하로» 다.\n"
                    "더 쪼개고, 쪼갠 구조를 ai-ae 에 보고할 것.\n"
                )
        return 0

    if 이벤트 != "PreToolUse":
        return 0
    if 내레인 is None:
        return 0                      # 미등록 세션 — 건드리지 않는다

    # ── 소유 밖 쓰기 차단 ──────────────────────────────────────────
    if 도구 in ("Write", "Edit", "NotebookEdit"):
        후보 = [입력.get("file_path") or 입력.get("notebook_path") or ""]
    elif 도구 == "Bash":
        후보 = bash_쓰기대상(입력.get("command") or "")
    else:
        return 0

    내소유 = ", ".join((설정["레인"][내레인] or {}).get("owns") or [])
    for p in 후보:
        주인 = 소유레인(설정, p)
        if 주인 is None or 주인 == 내레인:
            continue
        return 거부(
            f"[레인] `{상대(p)}` 는 레인 {주인} 소유다 — 레인 {내레인} 은 쓸 수 없다.\n"
            f"  네 소유: {내소유}\n"
            "덮어쓰면 그쪽 작업이 사라진다. 필요하면 ai-ae 에 SendMessage 로 요청할 것.\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
