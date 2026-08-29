# -*- coding: utf-8 -*-
"""PreToolUse(Bash) — 한글 출력 깨짐 차단.

Bash 툴로 python 계열을 돌릴 때 PYTHONIOENCODING=utf-8 이 없으면 한글 경로·출력이
콘솔에서 깨진다. CLAUDE.md 에 적어두는 것으로는 매번 빠졌으므로 훅에서 막는다.
(구 CLAUDE.md L202-203)
"""
import json
import re
import sys

# 명령 세그먼트(&&, ||, ;, | 로 구분) 맨 앞의 python/py/pytest 만 잡는다.
# `grep python foo.md` 같은 건 걸리지 않게 한다.
PY_CALL = re.compile(
    r"(?:^|&&|\|\||;|\|)\s*(?:[A-Za-z]:)?[^\s|;&]*\b(?:python3?|py|pytest)\b"
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # 훅이 세션을 막아서는 안 된다
    cmd = (data.get("tool_input") or {}).get("command", "") or ""

    if PY_CALL.search(cmd) and "PYTHONIOENCODING" not in cmd:
        sys.stderr.write(
            "[hook] 한글 경로·출력이 깨진다.\n"
            "명령 앞에 `export PYTHONIOENCODING=utf-8 && ` 를 붙여 다시 실행할 것.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
