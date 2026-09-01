# -*- coding: utf-8 -*-
"""저장소 루트 해석 — scripts/ 전역에 흩어져 있던 sys.path 조작을 여기로 걷는다.

호출하는 쪽(scripts/*.py, scripts/_work/*.py)은 `_lib` 자체를 import 하기 위한
최소 부트스트랩 한 줄만 남긴다 — scripts/ 가 sys.path 에 있어야 `from _lib import
paths` 가 되기 때문에 이 한 줄은 여기로 옮길 수 없다(닭-달걀). 그 이후 나머지
(repo root 계산·중복 삽입 가드·추가 경로)는 전부 `ensure_on_path()` 가 맡는다.

    import sys, os
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _here if os.path.basename(_here) == "scripts"
                         else os.path.dirname(_here))
    from _lib import paths
    paths.ensure_on_path()

깊이는 scripts/ 와 scripts/_work/ 두 단뿐이라 이 한 줄로 전부 커버된다(2026-09-02
실측 — 그보다 깊은 호출자 없음).
"""
from __future__ import annotations

import sys
from pathlib import Path

# 이 파일(scripts/_lib/paths.py) 자기 위치 기준으로 고정한다 — 호출자 파일의
# 깊이(scripts/ 인지 scripts/_work/ 인지)와 무관하게 같은 repo root 가 나온다.
ROOT = Path(__file__).resolve().parent.parent.parent


def ensure_on_path() -> Path:
    """repo root 와 scripts/ 를 sys.path 맨 앞에 넣는다(이미 있으면 넣지 않는다).

    반환값은 repo root — `from scripts.xxx import yyy` 형태가 필요한 호출자를
    위한 것이다(현재 20개 전환 대상 중에는 없다 — stage0_extract.py 등 repo root
    자체를 path 에 넣는 3개는 성격이 달라 2차로 미뤘다. `docs/기록/_레인_W3.md` 참조).
    """
    for p in (str(ROOT / "scripts"), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return ROOT
