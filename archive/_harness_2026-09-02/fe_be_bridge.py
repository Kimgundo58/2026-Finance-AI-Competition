# -*- coding: utf-8 -*-
"""프론트↔백엔드 간극 세션 전용 하네스 (session-pinned).

이 훅은 `.claude/_fe_be_session.json` 에 박힌 session_id 와 일치하는 세션에서만
동작한다. 같은 저장소에서 도는 다른 세션(전처리·Agent 파이프라인 등)에서는
stdin 을 읽자마자 0 으로 빠진다 — 남의 세션을 건드리지 않는다.

거는 규칙 셋:

  ① 범위     — Agent 흐름(LLM 호출·검색·룰·인덱싱·전처리) 은 이 세션의 일이 아니다.
               `scripts/` · `db/init/01_schema.sql` · `db/init/04_agent.sql` ·
               `2026_Finance_DATA_FOR_RAG/` 쓰기를 막는다. Bash 경유 쓰기도 막는다
               (redirect · sed -i · tee · mv/cp/rm · python open(...,'w')).
  ② 판정     — 프론트와 백엔드가 부딪힐 때 어느 쪽으로 맞추는지를 매 턴 주입한다.
               아키텍처가 뒤집히면 백엔드 기준, 구현하면 그만이면 프론트 기준.
  ③ 기록     — 이 세션이 서버·프론트용 스키마를 건드렸는데 `실제 구현.md` 가
               그대로면 Stop 에서 한 번 되돌린다 (세션당 1회, 루프 방지).

해제는 `.claude/settings.local.json` 에서 이 훅 항목을 지우거나 핀 파일을 지우면 된다.
"""
import json
import os
import re
import sys
import time

try:  # Windows 콘솔에서 한글 stderr 가 깨지지 않게
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PIN = os.path.join(ROOT, ".claude", "_fe_be_session.json")

# ── ① 범위 밖 — Agent 흐름 / 타 세션 점유 ────────────────────────────
범위밖 = (
    "scripts/",                        # 전처리·검색·룰·LLM 슬롯 전부
    "db/init/01_schema.sql",           # 코퍼스 스키마 (ai-25 세션이 잡고 있다)
    "db/init/04_agent.sql",            # Agent 층 스키마
    "2026_Finance_DATA_FOR_RAG/",      # 코퍼스·stage 산출물
    "법령 PDF/",
)

# ── 경고만 (막지는 않는다) ────────────────────────────────────────────
주의 = {
    "db/init/": "스키마 모양은 **백엔드 기준**이다. 프론트 편의로 컬럼을 늘리기 전에 "
                "기존 테이블로 뽑을 수 있는지 먼저 확인할 것.",
    "서비스 아키텍쳐.md": "정본 스냅샷이다. 프론트 요구로 구조를 뒤집는 서술을 넣지 말 것.",
    "RAG.md": "축 정본 문서다. 프론트 편의로 검색 규약을 바꾸지 말 것.",
    "LLM.md": "축 정본 문서다. 호출 수·스키마 규약은 백엔드 기준.",
    "Agent.md": "Agent 흐름은 이 세션 범위 밖이다. 읽기만 할 것.",
    "rule_base.md": "축 정본 문서다.",
}

지침 = """[fe-be 하네스] 이 세션의 범위와 판정 규칙

■ 하는 일 — 프론트와 백엔드 사이의 간극을 백엔드에서 메운다.
  정본: `프론트_데이터요구서_0901.md` · `프론트 연동 사양.md` · `백엔드_프론트비교_0830.md`
        · `프로토타입_해부_구현명세.md`. 작업면은 `server/` · `db/init/02_frontend.sql`
        · `db/init/03_input_fields.sql` · `tests/`.

■ 안 하는 일 — Agent 흐름(LLM 호출·검색·룰 조회·인덱싱·전처리)은 범위 밖이다.
  `scripts/` 와 코퍼스는 훅이 쓰기를 막는다. 필요하면 사용자에게 올린다.

■ 충돌 판정 (재논의 금지)
  · 아키텍처가 뒤집히는 것  → **백엔드 기준.** 확정 원칙(판정 1건=LLM 2회, 인덱스 경계,
    L1/L2/L3 레이어·우선순위, 판단불가 기본값, 물리 분리), 데이터 정합성, 스키마 무결성.
    프론트가 원해도 없는 사실을 만들지 않는다.
  · 구현하면 그만인 것    → **프론트 기준.** 필드명·라벨·상태 어휘·정렬·페이지네이션·
    응답 형태·집계값·CSV 열 구성·엔드포인트 모양. 프론트가 부르기 편한 쪽으로 맞춘다.
  · 애매하면 먼저 «프론트 요구를 그대로 만족시키는 백엔드 구현»을 찾는다. 정말 원칙을
    건드릴 때만 백엔드로 뒤집고, 뒤집었다는 사실을 사용자에게 한 줄로 보고한다.
"""


def 핀_읽기():
    try:
        with open(PIN, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def 핀_쓰기(d) -> None:
    try:
        with open(PIN, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def 범위밖_찾기(경로: str):
    norm = (경로 or "").replace("\\", "/")
    # 절대경로면 저장소 기준 상대경로로 자른다
    r = ROOT.replace("\\", "/")
    if norm.startswith(r):
        norm = norm[len(r):].lstrip("/")
    for seg in 범위밖:
        if norm.startswith(seg) or f"/{seg}" in norm:
            return seg
    return None


# ── Bash 경유 쓰기 탐지 ──────────────────────────────────────────────
_토큰 = re.compile(r"""[^\s'"|;&()]+|'[^']*'|"[^"]*\"""")


def bash_쓰기대상(cmd: str):
    """명령에서 «쓰기 대상» 으로 보이는 토큰만 골라 돌려준다."""
    대상 = []
    toks = [t.strip("'\"") for t in _토큰.findall(cmd)]

    # redirect: > FILE, >> FILE
    for m in re.finditer(r">>?\s*([^\s'\";|&)]+)", cmd):
        대상.append(m.group(1))

    낮 = cmd.lower()
    if re.search(r"\bsed\b[^|;]*\s-i", 낮) or "--in-place" in 낮:
        대상 += toks
    if re.search(r"\btee\b", 낮):
        i = next((k for k, t in enumerate(toks) if t == "tee"), None)
        if i is not None:
            대상 += toks[i + 1:]
    if re.search(r"\b(mv|cp|rsync|patch|truncate|rm|del)\b", 낮):
        대상 += toks
    # 파이썬 인라인 쓰기
    if re.search(r"""open\([^)]*['"][wa]""", cmd) or "write_text" in cmd:
        대상 += toks
    # git 이 파일을 되돌리는 것도 쓰기다
    if re.search(r"\bgit\s+(checkout|restore|apply|clean|reset)\b", 낮):
        대상 += toks
    return 대상


# ── 레인 소유권 (first-touch) ────────────────────────────────────────
# 조율 세션이 레인을 정의하고, 각 레인은 «자기 파일을 처음 쓰는 순간» 그 세션에 묶인다.
# 등록 명령이 없다 — 프롬프트에 한 줄도 안 넣어도 되고, 잊어버릴 수도 없다.
# 🔴 레인을 안 잡은 세션(Agent 파이프라인 등)은 이 훅이 건드리지 않는다.

# 소유권을 따지는 구역. 밖(스크래치패드·임시파일)은 통과시킨다.
관리구역 = ("server/", "tests/", "db/")


def 상대(경로: str) -> str:
    n = (경로 or "").replace("\\", "/")
    r = ROOT.replace("\\", "/")
    return n[len(r):].lstrip("/") if n.startswith(r) else n


def 레인_찾기(핀, 경로: str):
    """이 경로를 소유한 레인 이름. 없으면 None."""
    rel = 상대(경로)
    for 이름, v in (핀.get("lanes") or {}).items():
        for o in v.get("owns", []):
            if rel == o or rel.startswith(o.rstrip("/") + "/"):
                return 이름
    return None


def 관리대상(경로: str) -> bool:
    rel = 상대(경로)
    return rel.startswith(관리구역) or (rel.endswith(".md") and "/" not in rel)


def 거부(사유: str) -> int:
    sys.stderr.write(사유)
    return 2


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    핀 = 핀_읽기()
    if not 핀:
        return 0
    sid = data.get("session_id")
    조율 = sid == 핀.get("session_id")
    내레인 = next((k for k, v in (핀.get("lanes") or {}).items()
                 if v.get("session_id") == sid), None)

    이벤트 = data.get("hook_event_name") or ""
    도구 = data.get("tool_name") or ""
    입력 = data.get("tool_input") or {}

    # ── ② 판정 규칙 주입 ────────────────────────────────────────────
    # 🔴 2026-09-01 수정 (ai-25 제보): 여기에 세션 게이팅이 없어서 핀 파일만 있으면
    #    저장소의 **모든 세션**(코퍼스·Agent 파이프라인 포함)에 매 턴 지침이 뿌려졌다.
    #    PreToolUse 는 `내레인 is None → return 0` 으로 제대로 걸렀는데 이쪽만 샜다.
    #    조율 · 레인 소유 세션 · 배정됐지만 아직 first-touch 전인 세션에만 준다.
    if 이벤트 == "UserPromptSubmit":
        배정됨 = sid in (핀.get("배정") or {}).values()
        if not (조율 or 내레인 or 배정됨):
            return 0
        print(지침)
        return 0

    # ── ① 범위 강제 ────────────────────────────────────────────────
    if 이벤트 == "PreToolUse":
        후보 = []
        if 도구 in ("Write", "Edit", "NotebookEdit"):
            후보 = [입력.get("file_path") or 입력.get("notebook_path") or ""]
        elif 도구 == "Bash":
            후보 = bash_쓰기대상(입력.get("command") or "")

        # ── first-touch 레인 등록 ───────────────────────────────────
        # 미등록 세션이 레인 파일을 «처음 쓰는 순간» 그 레인에 묶인다. 등록 명령이 없다.
        if not 조율 and 내레인 is None:
            for p in 후보:
                L = 레인_찾기(핀, p)
                if not L:
                    continue
                주인 = (핀["lanes"][L] or {}).get("session_id")
                if 주인 is None:
                    핀["lanes"][L]["session_id"] = sid
                    핀_쓰기(핀)
                    내레인 = L
                    break
                if 주인 != sid:
                    return 거부(
                        f"[fe-be 하네스] `{상대(p)}` 는 레인 {L} 소유다 — 다른 세션이 쓰고 있다.\n"
                        "덮어쓰면 그쪽 작업이 사라진다. 네 레인 파일 안에서 해결하거나,\n"
                        "계약을 바꿔야 하면 조율 세션에 보고하고 멈춘다.\n"
                    )
            if 내레인 is None:
                return 0     # 이 저장소의 다른 세션(Agent 파이프라인 등) — 건드리지 않는다

        # ── 레인 밖 쓰기 차단 ───────────────────────────────────────
        if 내레인:
            for p in 후보:
                if not 관리대상(p):
                    continue
                if 레인_찾기(핀, p) != 내레인:
                    owns = ", ".join((핀["lanes"][내레인] or {}).get("owns", []))
                    return 거부(
                        f"[fe-be 하네스] 레인 {내레인} 은 `{상대(p)}` 를 쓰지 않는다 — 거부.\n"
                        f"  네 소유: {owns}\n"
                        "계약 파일(`server/models.py`·`_common.py`·`mock_data.py`)은 조율 세션이\n"
                        "동결했다. 필드가 모자라면 고치지 말고 보고할 것 — 세 레인이 각자\n"
                        "필드를 늘리면 프론트가 받는 응답이 세 갈래로 갈린다.\n"
                    )

        for p in 후보:
            seg = 범위밖_찾기(p)
            if seg:
                return 거부(
                    f"[fe-be 하네스] `{seg}` 는 이 세션의 범위 밖이다 — 쓰기 거부.\n"
                    f"  대상: {p}\n"
                    "이 세션은 프론트↔백엔드 간극만 메운다. Agent 흐름(LLM·검색·룰·인덱싱·\n"
                    "전처리)과 코퍼스는 다른 세션이 잡고 있어 덮어쓰면 충돌한다.\n"
                    "→ `server/` · `db/init/02_frontend.sql` · `db/init/03_input_fields.sql`\n"
                    "  · `tests/` 에서 해결할 방법을 먼저 찾는다.\n"
                    "→ 정말 저 파일이어야 하면 무엇을 왜 바꿔야 하는지 사용자에게 올리고 멈춘다.\n"
                )

        경로 = (입력.get("file_path") or "").replace("\\", "/")
        for 키, 말 in 주의.items():
            if 키 in 경로:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": f"[fe-be 하네스] {키} — {말}",
                    }
                }, ensure_ascii=False))
                break

        # 이 세션이 실제로 만진 것을 기록해 둔다 (Stop 에서 쓴다)
        if 도구 in ("Write", "Edit") and 경로:
            rel = 상대(경로)
            if rel.startswith(("server/", "db/init/", "tests/")):
                핀.setdefault("touched", [])
                if rel not in 핀["touched"]:
                    핀["touched"].append(rel)
                    핀_쓰기(핀)
        return 0

    # ── ③ 기록 되돌림 (세션당 1회) ──────────────────────────────────
    if 이벤트 == "Stop":
        if not 핀.get("touched") or 핀.get("nagged"):
            return 0
        기록 = os.path.join(ROOT, "실제 구현.md")
        try:
            신선 = os.path.getmtime(기록) >= 핀.get("started_at", 0)
        except Exception:
            신선 = True
        if 신선:
            return 0
        핀["nagged"] = True
        핀_쓰기(핀)
        목록 = " · ".join(핀["touched"][:6])
        return 거부(
            "[fe-be 하네스] 이번 세션에서 산출물을 만들었는데 `실제 구현.md` 가 그대로다.\n"
            f"  만진 것: {목록}\n"
            "`arrangement_implementation` 스킬로 «왜 그렇게 했는지»와 «나중에 고칠 때\n"
            "알아야 할 것»을 남기고 끝낸다. 특히 프론트/백엔드 중 어느 쪽으로 맞췄고\n"
            "그 이유가 «아키텍처» 인지 «구현하면 그만» 인지 한 줄로 적을 것.\n"
            "(이 알림은 세션당 한 번만 뜬다)\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
