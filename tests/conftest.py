# -*- coding: utf-8 -*-
"""테스트 공용 — 목/실 모드가 «먼저 import 된 파일» 에 끌려가는 것을 끊는다.

🔴 **왜 필요한가 (2026-09-01)**

`server/_common.py` 의 `MOCK` 은 **모듈 최초 import 시점에 한 번** 확정되는 상수다.
각 라우터가 `from ._common import MOCK` 로 «값» 을 복사해 가므로, 나중에 환경변수를
바꿔도 이미 import 된 모듈은 안 바뀐다.

그래서 파일별로 단독 실행하면 4개 파일 20건이 전부 통과하는데,

    pytest tests/test_plans.py tests/test_tasks.py ... -q     ← 합쳐서 돌리면 5건 실패

`test_plans.py` 가 먼저 import 되며 `MOCK=1`(기본값) 로 굳고, 뒤이어
`test_tasks.py` 가 `os.environ["SUDDOE_MOCK"]="0"` 을 걸어도 소용이 없어
실 DB 를 검증해야 할 할일 테스트가 목 경로로 흘렀다. 실패는 그 구현의
결함이 아니라 **테스트 자동 점검의 결합**이다 (단독 6/6 통과가 그 증거).

여기서 매 테스트 직전에 각 서버 모듈의 `MOCK` 속성을 직접 덮어 격리한다.

■ 파일이 어느 모드를 원하는지 어떻게 아는가 — 두 가지, 위가 이긴다
  ① 모듈 최상단에 `실DB = True`  (명시)
  ② 파일 본문에 `os.environ["SUDDOE_MOCK"] = "0"` 이 있으면 실 DB 로 본다 (현행 관례)
  둘 다 없으면 목이다. 각 테스트 파일만 고치면 되고 이 파일은 안 건드린다.

■ 모드와 무관한 테스트도 있다 — `test_plans.py` 는 HTTP 를 안 거치고
  `routes_plans._실_*` 를 직접 부른다. 그쪽은 이 스위치의 영향을 받지 않는다.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# `MOCK` 을 복사해 간 모듈 전부. 새 라우터가 생기면 여기 한 줄 추가한다.
_모듈 = (
    "server._common",
    "server.routes_plans",
    "server.routes_tasks",
    "server.routes_l3",
    "server.persist",          # 2026-09-01 추가 — 판정_저장() 을 목/실 전환으로 태울 때 필요
    "server.main",
)

_실DB표식 = ('SUDDOE_MOCK"] = "0"', "SUDDOE_MOCK'] = '0'", 'SUDDOE_MOCK"]="0"')


def _모드_설정(목: bool) -> None:
    for 이름 in _모듈:
        모듈 = sys.modules.get(이름)
        if 모듈 is None:
            try:
                모듈 = importlib.import_module(이름)
            except Exception:                                  # noqa: BLE001
                continue
        if hasattr(모듈, "MOCK"):
            모듈.MOCK = 목


def _실DB_원하나(모듈) -> bool:
    선언 = getattr(모듈, "실DB", None)
    if 선언 is not None:                                       # ① 명시가 이긴다
        return bool(선언)
    파일 = getattr(모듈, "__file__", None)                     # ② 관례
    if not 파일:
        return False
    try:
        본문 = Path(파일).read_text(encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        return False
    return any(t in 본문 for t in _실DB표식)


@pytest.fixture(autouse=True)
def _목모드(request):
    """테스트 하나마다 모드를 다시 세운다 — 파일 순서에 결과가 안 달리게."""
    _모드_설정(목=not _실DB_원하나(request.module))
    yield
