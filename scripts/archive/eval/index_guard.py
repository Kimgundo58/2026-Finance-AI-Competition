# -*- coding: utf-8 -*-
"""판정 검색 대상에 넣기 통과 조건 — "실수로 안 넣기"가 아니라 "넣을 수 없게" 만든다.

CLAUDE.md 는 예전부터 *"인덱서는 경로 블랙리스트를 코드로 강제한다"* 고 선언했으나
2026-08-28 확인 결과 **실제 코드에는 없었다.** 여기가 그 구현이다.

거부 대상
  archive/          구 74파일 데이터셋 · 폐기 문서. 이력 추적용
  _골든셋/           창진원 공식 답변. 넣으면 정답 유출
  _테스트_L3/        L3 업로드 파이프라인 테스트 입력
  _범위밖_보류/      위임 추적에 딸려온 무관 규범 161건
  layer == "L4"     타 대학·타 기관 규정 24건. 남의 학교 규정이 사용자 판정에
                    인용되는 순간 그 자체가 오답이다

L4 는 `documents` 에는 남긴다 — L3 파이프라인 테스트(파싱 통과 조건 통과율·참조 해소율·
멀티테넌시 누수)에 입력으로 쓴다. 다만 index_target 은 무조건 False 다.
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
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
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
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

# 🔴 2026-08-31 L3 추가 (E 세션 BLOCKED · A 처리).
#    `CLAUDE.md` 의 "L3 는 다른 테이블이라 누수가 구조적으로 불가능하다" 는 L3 가
#    `corpus.chunks` 에 **절대 안 들어갈 때만** 참이다. 그전까지는 어떤 인제스천 경로가
#    layer='L3' 로 태깅해 `stage0_ingest` 를 태우면 `index_target=True` 로 통과했다.
#    현재 chunks 는 L1 20,030 · L2 495 로 깨끗해서 **지표에 안 나타나는 무음 구멍**이었다.
BLOCKED_LAYERS: frozenset[str] = frozenset({"L3", "L4", "L5"})

# 같은 거부라도 이유가 다르다. L4·L5 는 "남의 규정이라 테스트 전용" 이고,
# L3 는 "우리 기관 규정이지만 **가는 테이블이 다르다**" 이다. 한 문장으로 뭉치면
# 나중에 이 메시지를 본 사람이 L3 를 통과시키려 든다.
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
    """넣으면 안 되는 것이면 예외. 조용히 지나가지 않는다."""
    why = reject_reason(path, layer)
    if why is not None:
        raise IndexGuardError(f"판정 인덱스 투입 거부 — {why}: {path}")
