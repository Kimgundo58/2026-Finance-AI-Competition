# -*- coding: utf-8 -*-
"""PreToolUse(Write|Edit|NotebookEdit) — 읽기 전용 구역 쓰기 차단.

`archive/` 는 이력 추적용이고, `_골든셋/` 은 정답지, `_테스트_L3/` 는 L3 파이프라인
테스트 입력, `_범위밖_보류/` 는 위임 추적에 딸려온 무관 규범이다.
넷 다 "고칠 일이 없는데 실수로 고쳐지면 조용히 오염되는" 구역이라 훅에서 막는다.
(구 CLAUDE.md L109-110·L124)

인덱스 투입 차단은 별개다 — scripts/archive/eval/index_guard.py 가 담당한다.
"""
import json
import sys

BLOCKED = (
    "archive/",
    "_골든셋/",
    "_테스트_L3/",
    "_범위밖_보류/",
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    path = (data.get("tool_input") or {}).get("file_path", "") or ""
    norm = path.replace("\\", "/")

    for seg in BLOCKED:
        if seg in norm:
            sys.stderr.write(
                f"[hook] `{seg}` 는 읽기 전용 구역이다. 쓰기 거부.\n"
                "이력 추적·정답지·테스트 입력이라 수정하면 안 된다.\n"
                "정말 필요하면 사용자에게 먼저 확인할 것.\n"
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
