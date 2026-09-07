# -*- coding: utf-8 -*-
"""저장소 루트 해석과 sys.path 등록.

호출자는 `_lib` 를 import 할 수 있게 scripts/ 를 sys.path 에 넣는 부트스트랩 한 줄만 두고,
나머지는 `ensure_on_path()` 에 맡긴다.

    import sys, os
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _here if os.path.basename(_here) == "scripts"
                         else os.path.dirname(_here))
    from _lib import paths
    paths.ensure_on_path()
"""
from __future__ import annotations

import sys
from pathlib import Path

# 이 파일 위치 기준이라 호출자 깊이와 무관하게 같은 repo root 가 나온다.
ROOT = Path(__file__).resolve().parent.parent.parent


def ensure_on_path() -> Path:
    """repo root 와 scripts/ 를 sys.path 맨 앞에 넣는다(이미 있으면 넣지 않는다). repo root 를 돌려준다."""
    for p in (str(ROOT / "scripts"), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return ROOT
