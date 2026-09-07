# -*- coding: utf-8 -*-
"""테스트 공용 — 매 테스트 직전에 서버 모듈의 `MOCK` 을 덮어 목/실 모드를 격리한다.

`MOCK` 은 import 시점에 값으로 복사되므로 환경변수만 바꿔서는 안 바뀐다.
파일이 원하는 모드는 모듈 최상단 `실DB = True`(명시) 또는 본문의
`os.environ["SUDDOE_MOCK"] = "0"`(관례) 로 읽는다. 둘 다 없으면 목이다.
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
    "server.persist",
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
    if 선언 is not None:                                       # 명시가 이긴다
        return bool(선언)
    파일 = getattr(모듈, "__file__", None)                     # 관례
    if not 파일:
        return False
    try:
        본문 = Path(파일).read_text(encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        return False
    return any(t in 본문 for t in _실DB표식)


@pytest.fixture(autouse=True)
def _목모드(request):
    """테스트 하나마다 모드를 다시 세운다."""
    _모드_설정(목=not _실DB_원하나(request.module))
    yield
