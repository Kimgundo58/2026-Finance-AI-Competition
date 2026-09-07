# -*- coding: utf-8 -*-
"""판정 검색 인덱스 투입 가드 — 경로 블랙리스트와 레이어(L3·L4·L5)를 코드로 거부한다.

훅과 테스트가 import 한다. 거부 경로: archive/ · _골든셋/ · _테스트_L3/ · _범위밖_보류/
"""
from __future__ import annotations

# `scripts/_lib` 이 보일 때까지 위로 올라가 scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# archive 하위 폴더끼리 import 하므로 scripts/archive/ 의 모든 하위 폴더도 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)



class IndexGuardError(RuntimeError):
    """판정 인덱스에 넣으면 안 되는 것을 넣으려 했다."""


BLOCKED_PATHS: tuple[str, ...] = (
    "archive/",
    "_골든셋/",
    "_테스트_L3/",
    "_범위밖_보류/",
)

# L3 는 tenant.l3_articles 로 가고, L4·L5 는 타 기관 규정이라 테스트 전용이다.
BLOCKED_LAYERS: frozenset[str] = frozenset({"L3", "L4", "L5"})

# L3 는 거부 이유가 달라 문구를 따로 둔다.
_거부문구 = {"L3": "레이어 `L3` 는 tenant.l3_articles 로 간다 — 검색 인덱스 대상이 아니다"}


def reject_reason(path: str, layer: str | None = None) -> str | None:
    """거부 사유를 돌려준다. 넣어도 되면 None."""
    norm = str(path).replace("\\", "/")
    for seg in BLOCKED_PATHS:
        if seg in norm:
            return f"경로 블랙리스트 `{seg}`"
    if layer and layer.upper() in BLOCKED_LAYERS:
        return _거부문구.get(layer.upper(),
                            f"레이어 `{layer}` 는 판정 인덱스 대상이 아니다 (테스트 전용)")
    return None


def is_indexable(path: str, layer: str | None = None) -> bool:
    return reject_reason(path, layer) is None


def assert_indexable(path: str, layer: str | None = None) -> None:
    """넣으면 안 되는 것이면 IndexGuardError."""
    why = reject_reason(path, layer)
    if why is not None:
        raise IndexGuardError(f"판정 인덱스 투입 거부 — {why}: {path}")
